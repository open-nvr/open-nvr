# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Turn-taking sized to the hardware: Smart Turn v3's thread count follows
the cores this process may use, a single-core box gets a silence timer
instead of the model, and numpy's BLAS/OpenMP pools are capped so the
end-of-turn log-mel doesn't fan out across the machine."""
from __future__ import annotations

import os

import pytest

import camera_agent
from camera_agent import (
    AppConfig,
    available_cores,
    describe_turn_profile,
    turn_hardware_profile,
)
from context import CameraSpec


def _cfg(**over) -> AppConfig:
    return AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t",
        cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="front")],
        **over,
    )


# ── the auto profile ───────────────────────────────────────────────────


@pytest.mark.parametrize("cores,detector,threads", [
    (1, "timer", 1),
    (2, "smart", 1),
    (4, "smart", 1),
    (7, "smart", 1),
    (8, "smart", 2),
    (32, "smart", 2),
])
def test_auto_profile_follows_core_count(cores, detector, threads):
    p = turn_hardware_profile(_cfg(), cores=cores)
    assert (p["detector"], p["cpu_threads"], p["cores"]) == (detector, threads, cores)
    assert p["reason"]


def test_explicit_config_wins_over_auto():
    p = turn_hardware_profile(_cfg(turn_detector="timer", turn_cpu_threads=3), cores=16)
    assert p["detector"] == "timer" and p["cpu_threads"] == 3
    # the model can't be asked for more threads than there are cores
    p = turn_hardware_profile(_cfg(turn_cpu_threads=8), cores=2)
    assert p["cpu_threads"] == 2
    # smart on a single core is honoured when the operator insists
    p = turn_hardware_profile(_cfg(turn_detector="smart"), cores=1)
    assert p["detector"] == "smart" and p["cpu_threads"] == 1


def test_bad_detector_value_falls_back_to_auto():
    p = turn_hardware_profile(_cfg(turn_detector="magic"), cores=4)
    assert p["detector"] == "smart"


def test_describe_mentions_the_choice():
    assert "Smart Turn v3" in describe_turn_profile(_cfg(turn_detector="smart"))
    assert "silence timer" in describe_turn_profile(_cfg(turn_detector="timer"))


# ── the hardware probe ─────────────────────────────────────────────────


def test_available_cores_is_at_least_one_and_bounded_by_the_machine():
    n = available_cores()
    assert 1 <= n <= (os.cpu_count() or n)


def test_available_cores_honours_a_cgroup_quota(monkeypatch, tmp_path):
    (tmp_path / "cpu.max").write_text("150000 100000\n")   # 1.5 CPUs
    real_read = camera_agent.Path.read_text

    def fake_read(self, *a, **k):
        if str(self) == "/sys/fs/cgroup/cpu.max":
            return (tmp_path / "cpu.max").read_text()
        return real_read(self, *a, **k)

    monkeypatch.setattr(camera_agent.Path, "read_text", fake_read)
    monkeypatch.setattr(camera_agent.os, "sched_getaffinity", lambda _pid: set(range(16)), raising=False)
    assert available_cores() == 2   # ceil(1.5), never the 16 the mask allows


# ── config plumbing ────────────────────────────────────────────────────


def test_config_loads_turn_hardware_knobs(tmp_path):
    (tmp_path / "c.yml").write_text(
        "kaic_url: http://k\nkaic_api_key: x\nsystem_prompt: t\n"
        "turn_detector: timer\nturn_cpu_threads: 2\nturn_timer_secs: 1.1\n"
    )
    cfg = camera_agent.load_config(str(tmp_path / "c.yml"))
    assert cfg.turn_detector == "timer"
    assert cfg.turn_cpu_threads == 2
    assert cfg.turn_timer_secs == pytest.approx(1.1)
    assert cfg.turn_max_secs == pytest.approx(8.0)   # the model's window


def test_math_thread_caps_are_defaulted_at_import():
    # camera_agent sets these before numpy can be imported; an explicit
    # operator value in the environment would have been left alone.
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        assert os.environ.get(var)


def test_hardware_endpoint_reports_the_turn_profile():
    from fastapi.testclient import TestClient

    from camera_agent import CameraAgentRuntime, build_app

    hw = TestClient(build_app(CameraAgentRuntime(_cfg()))).get("/hardware").json()
    assert hw["turn"]["detector"] in ("smart", "timer")
    assert hw["turn"]["cpu_threads"] >= 1


# ── the real Pipecat objects (skipped where Pipecat isn't installed) ───


pipecat = pytest.importorskip("pipecat")


def test_smart_profile_builds_smart_turn_with_the_thread_count():
    from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy

    params = camera_agent.build_user_turn_params(_cfg(turn_detector="smart", turn_cpu_threads=1))
    stop = params.user_turn_strategies.stop[0]
    assert isinstance(stop, TurnAnalyzerUserTurnStopStrategy)
    analyzer = stop._turn_analyzer
    so = analyzer._session.get_session_options()
    assert so.intra_op_num_threads == 1 and so.inter_op_num_threads == 1
    assert analyzer.params.max_duration_secs == pytest.approx(8.0)


def test_timer_profile_builds_no_model():
    from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy

    params = camera_agent.build_user_turn_params(_cfg(turn_detector="timer", turn_timer_secs=0.7))
    stop = params.user_turn_strategies.stop[0]
    assert isinstance(stop, SpeechTimeoutUserTurnStopStrategy)
    assert stop._user_speech_timeout == pytest.approx(0.7)
