# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Function-based health: healthy means DOING THE JOB, not process-up."""
from detect_pipeline.health import HealthState, evaluate

T0 = 1_000_000.0
AFTER_GRACE = T0 + 120.0  # past the 90s startup grace


def _state(**over):
    st = HealthState(
        enabled=True,
        detector_requested="onnx",
        detector_actual="OnnxDetector",
        nats_configured=True,
        nats_connected=lambda: True,
        workers_running=lambda: 2,
        newest_frame_age_s=lambda: 1.0,
        started_at=T0,
    )
    for k, v in over.items():
        setattr(st, k, v)
    return st


def test_healthy_when_everything_works():
    ok, detail = evaluate(_state(), now=AFTER_GRACE)
    assert ok and detail["problems"] == []


def test_disabled_service_is_healthy_idle():
    ok, detail = evaluate(_state(enabled=False, workers_running=lambda: 0),
                          now=AFTER_GRACE)
    assert ok and "disabled" in detail["note"]


def test_detector_degraded_to_stub_is_unhealthy():
    # The QA zombie: model missing, container "healthy", detecting nothing.
    ok, detail = evaluate(_state(detector_actual="StubDetector"), now=AFTER_GRACE)
    assert not ok
    assert any("degraded to stub" in p for p in detail["problems"])


def test_stub_by_explicit_config_is_fine():
    ok, _ = evaluate(_state(detector_requested="stub",
                            detector_actual="StubDetector"), now=AFTER_GRACE)
    assert ok


def test_bus_disconnected_after_grace_is_unhealthy():
    ok, detail = evaluate(_state(nats_connected=lambda: False), now=AFTER_GRACE)
    assert not ok
    assert any("event bus" in p for p in detail["problems"])


def test_bus_disconnected_within_grace_is_tolerated():
    ok, _ = evaluate(_state(nats_connected=lambda: False), now=T0 + 10)
    assert ok


def test_stale_frames_with_running_workers_is_unhealthy():
    ok, detail = evaluate(_state(newest_frame_age_s=lambda: 300.0), now=AFTER_GRACE)
    assert not ok
    assert any("stale" in p for p in detail["problems"])


def test_no_frames_ever_with_running_workers_is_unhealthy():
    ok, _ = evaluate(_state(newest_frame_age_s=lambda: None), now=AFTER_GRACE)
    assert not ok


def test_no_workers_no_frame_requirement():
    # No cameras discovered yet: not a zombie, just idle.
    ok, _ = evaluate(_state(workers_running=lambda: 0,
                            newest_frame_age_s=lambda: None), now=AFTER_GRACE)
    assert ok
