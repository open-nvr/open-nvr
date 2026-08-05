# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the baseline-vs-gated benchmark harness (synthetic tracks)."""
from __future__ import annotations

from detect_pipeline.bench import BenchResult, run_benchmark
from detect_pipeline.gate import GateConfig
from detect_pipeline.tracking import Track


def _moving():
    return Track(id=1, label="person", box=(10, 10, 30, 60), score=0.9,
                 hits=5, confirmed=True, motionless_count=0, stationary_threshold=50)


def _stationary(tid=2):
    return Track(id=tid, label="car", box=(50, 50, 90, 90), score=0.8,
                 hits=5, confirmed=True, motionless_count=100, stationary_threshold=50)


def test_moving_object_escalates_once_then_cooldown():
    # 30 frames @ 5fps = 6s, under the 30s cooldown -> one escalate (new_track)
    stream = [([_moving()], i * 0.2) for i in range(30)]
    r = run_benchmark(stream)
    assert r.baseline_calls == 30          # naive: one call per track per frame
    assert r.gated_calls == 1              # gated: once, on the new track
    assert r.escalations_by_reason == {"new_track": 1}
    assert r.events == 1 and r.missed_events == 0
    assert r.reduction_factor == 30


def test_stationary_object_suppressed_counts_as_missed():
    stream = [([_stationary()], i * 0.2) for i in range(10)]
    r = run_benchmark(stream)
    assert r.gated_calls == 0
    assert r.events == 1 and r.missed_events == 1
    assert r.miss_rate == 1.0
    assert r.baseline_calls == 10


def test_heartbeat_rescues_a_stationary_object():
    # A settled object is suppressed (see test above -> missed) UNLESS a heartbeat
    # forces a periodic escalate. With heartbeat on, the same object is not missed.
    stream = [([_stationary()], i * 0.2) for i in range(15)]   # 0..2.8s
    r = run_benchmark(stream, GateConfig(shadow=False, heartbeat_s=1.0))
    assert r.gated_calls >= 1
    assert "heartbeat" in r.escalations_by_reason
    assert r.missed_events == 0             # heartbeat means it wasn't missed


def test_result_dict_and_summary():
    r = BenchResult(frames=100, baseline_calls=300, gated_calls=3, events=3,
                    missed_events=0, escalations_by_reason={"new_track": 3})
    d = r.as_dict()
    assert d["reduction_factor"] == 100.0 and d["miss_rate"] == 0.0
    assert "100x fewer" in r.summary() or "100.0x fewer" in r.summary()


def test_fps_property_from_wall_time():
    r = BenchResult(frames=100, wall_seconds=2.0)
    assert r.fps == 50.0
    assert r.as_dict()["fps"] == 50.0
    assert "fps=50.0" in r.summary()
    # untimed run: fps is None, omitted from the summary line
    assert BenchResult(frames=100).fps is None
    assert "fps=" not in BenchResult(frames=100).summary()
