# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Integration tests — full IntrusionDetector loop with stubbed KAI-C.

Wires up: a tmp config + a tmp JPEG frame source + a fake KAI-C HTTP
endpoint that returns canned detection responses. Exercises every
branch of ``IntrusionDetector.step``:

* Detection in zone + restricted hours → alert fires
* Detection in zone + safe hours → no alert
* Detection outside zone → no alert
* Empty detection list → no alert
* KAI-C unreachable → no alert (but no crash)
* KAI-C returns FailureEnvelope → no alert (logged + skip)
* Non-watched label inside zone → no alert
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from textwrap import dedent

import httpx
import pytest

from alerts import AlertDispatcher
from intrusion_detection import (
    AppConfig,
    CameraWatch,
    IntrusionDetector,
    KaicClient,
    RestrictedHours,
    load_config,
)
from zone import Zone


# ── Fake KAI-C ─────────────────────────────────────────────────────


class _FakeKaicTransport:
    """Backs httpx.MockTransport with operator-controlled responses."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.next_response: tuple[int, dict] = (200, {
            "status": "ok",
            "model_name": "yolov8n",
            "model_version": "v1",
            "inference_ms": 12,
            "result": {"detections": [], "frame_dimensions": {"w": 1920, "h": 1080}},
        })

    def respond(self, request: httpx.Request) -> httpx.Response:
        body = bytes(request.read())
        self.requests.append({
            "url": str(request.url),
            "method": request.method,
            "headers": dict(request.headers),
            "body": json.loads(body) if body else None,
        })
        status, payload = self.next_response
        return httpx.Response(status, json=payload)

    def set_detections(self, detections: list[dict]) -> None:
        self.next_response = (200, {
            "status": "ok",
            "model_name": "yolov8n",
            "model_version": "v1",
            "inference_ms": 12,
            "result": {
                "detections": detections,
                "frame_dimensions": {"w": 1920, "h": 1080},
            },
        })

    def set_error_envelope(self) -> None:
        self.next_response = (200, {
            "status": "ok",
            "model_name": "yolov8n",
            "model_version": "v1",
            "inference_ms": 0,
            "result": {
                "status": "error",
                "error": {
                    "category": "model_error",
                    "code": "out_of_memory",
                    "message": "GPU OOM",
                    "transient": False,
                    "details": {},
                },
            },
        })

    def set_unreachable(self) -> None:
        self.next_response = (502, {"detail": "unreachable"})


# ── Helpers ────────────────────────────────────────────────────────


def _detection(label: str, *, x: float, y: float, w: float = 0.1, h: float = 0.1,
               confidence: float = 0.92) -> dict:
    """§5.1 DetectionItem shape — normalized bbox in [0, 1]."""
    return {
        "label": label,
        "confidence": confidence,
        "bbox": {"x": x, "y": y, "w": w, "h": h},
        "track_id": None,
        "attributes": {},
    }


def _build_detector(
    tmp_path: Path,
    *,
    transport: _FakeKaicTransport,
    restricted_hours: RestrictedHours,
    now: _dt.datetime,
    watch_labels: list[str] | None = None,
) -> tuple[IntrusionDetector, CameraWatch]:
    """Build a fully-wired detector with one camera in tmp_path."""
    # Camera frame
    frame_path = tmp_path / "frame.jpg"
    frame_path.write_bytes(b"FAKE_JPEG")
    # Zone covers the center half of a 1920x1080 frame
    zone = Zone.from_config("center", [[480, 270], [1440, 270], [1440, 810], [480, 810]])
    camera = CameraWatch(
        camera_id="cam-test",
        frame_url=f"file://{frame_path}",
        zone=zone,
        frame_width=1920,
        frame_height=1080,
    )
    config = AppConfig(
        kaic_url="http://kaic.test",
        kaic_adapter_name="yolov8",
        kaic_api_key="test-key",
        poll_interval_seconds=1.0,
        watch_labels=watch_labels or ["person", "car"],
        restricted_hours=restricted_hours,
        cameras=[camera],
        webhook_url=None,
    )
    client = httpx.Client(transport=httpx.MockTransport(transport.respond))
    kaic = KaicClient(
        config.kaic_url, config.kaic_adapter_name,
        api_key=config.kaic_api_key,
        timeout_seconds=30.0,
        http_client=client,
    )

    # Peer-review PR-12/PR-13: use the normal AlertDispatcher
    # constructor with a recorder channel; let tests inspect
    # ``recorder.alerts`` directly rather than monkey-patching
    # ``detector._test_fired``.
    class _RecorderChannel:
        name = "recorder"

        def __init__(self) -> None:
            self.alerts: list[Alert] = []

        def send(self, alert):
            self.alerts.append(alert)
            return True

    recorder = _RecorderChannel()
    dispatcher = AlertDispatcher([recorder])
    detector = IntrusionDetector(config, kaic, dispatcher, now=lambda: now)
    # Expose the recorder so individual tests can assert on dispatch
    # without poking at private state.
    detector.recorder = recorder  # type: ignore[attr-defined]
    return detector, camera


# ── Tests ──────────────────────────────────────────────────────────


def test_person_in_zone_during_restricted_hours_fires_alert(tmp_path):
    transport = _FakeKaicTransport()
    # Person at center of frame (0.5, 0.5) → bbox center at (960, 540)
    # which is inside the center zone (480-1440 x 270-810).
    transport.set_detections([_detection("person", x=0.45, y=0.45)])
    detector, _ = _build_detector(
        tmp_path,
        transport=transport,
        restricted_hours=RestrictedHours(start=_dt.time(0, 0), end=_dt.time(23, 59)),
        now=_dt.datetime(2026, 5, 19, 10, 0),  # in window
    )
    fired = detector.step(detector._config.cameras[0])
    assert len(fired) == 1
    assert fired[0].camera_id == "cam-test"
    assert "person" in fired[0].title.lower()
    # Dispatcher received the same alerts step() returned.
    assert detector.recorder.alerts == fired


def test_person_in_zone_outside_restricted_hours_no_alert(tmp_path):
    transport = _FakeKaicTransport()
    transport.set_detections([_detection("person", x=0.45, y=0.45)])
    detector, _ = _build_detector(
        tmp_path,
        transport=transport,
        # Restricted = night only
        restricted_hours=RestrictedHours(start=_dt.time(22, 0), end=_dt.time(6, 0)),
        now=_dt.datetime(2026, 5, 19, 14, 0),  # afternoon — safe
    )
    fired = detector.step(detector._config.cameras[0])
    assert fired == []
    # KAI-C should NOT have been called — safe hours skip the network entirely
    assert len(transport.requests) == 0


def test_person_outside_zone_no_alert(tmp_path):
    transport = _FakeKaicTransport()
    # Detection in the top-left corner — bbox center at (96, 54) — outside center zone
    transport.set_detections([_detection("person", x=0.0, y=0.0)])
    detector, _ = _build_detector(
        tmp_path,
        transport=transport,
        restricted_hours=RestrictedHours(start=_dt.time(0, 0), end=_dt.time(23, 59)),
        now=_dt.datetime(2026, 5, 19, 10, 0),
    )
    fired = detector.step(detector._config.cameras[0])
    assert fired == []


def test_empty_detections_no_alert(tmp_path):
    transport = _FakeKaicTransport()  # default response = []
    detector, _ = _build_detector(
        tmp_path,
        transport=transport,
        restricted_hours=RestrictedHours(start=_dt.time(0, 0), end=_dt.time(23, 59)),
        now=_dt.datetime(2026, 5, 19, 10, 0),
    )
    fired = detector.step(detector._config.cameras[0])
    assert fired == []


def test_non_watched_label_in_zone_no_alert(tmp_path):
    transport = _FakeKaicTransport()
    transport.set_detections([_detection("dog", x=0.45, y=0.45)])
    detector, _ = _build_detector(
        tmp_path,
        transport=transport,
        restricted_hours=RestrictedHours(start=_dt.time(0, 0), end=_dt.time(23, 59)),
        now=_dt.datetime(2026, 5, 19, 10, 0),
        watch_labels=["person", "car"],  # "dog" not watched
    )
    fired = detector.step(detector._config.cameras[0])
    assert fired == []


def test_kaic_unreachable_no_alert_no_crash(tmp_path):
    transport = _FakeKaicTransport()
    transport.set_unreachable()
    detector, _ = _build_detector(
        tmp_path,
        transport=transport,
        restricted_hours=RestrictedHours(start=_dt.time(0, 0), end=_dt.time(23, 59)),
        now=_dt.datetime(2026, 5, 19, 10, 0),
    )
    fired = detector.step(detector._config.cameras[0])  # must not raise
    assert fired == []


def test_kaic_returns_non_dict_body_no_alert_no_crash(tmp_path):
    """Regression for self-review SR-12: a pathological non-dict body
    (which shouldn't happen but defensive) returns no alerts instead
    of crashing the loop."""
    transport = _FakeKaicTransport()
    transport.next_response = (200, ["not", "a", "dict"])  # type: ignore[assignment]
    detector, _ = _build_detector(
        tmp_path,
        transport=transport,
        restricted_hours=RestrictedHours(start=_dt.time(0, 0), end=_dt.time(23, 59)),
        now=_dt.datetime(2026, 5, 19, 10, 0),
    )
    fired = detector.step(detector._config.cameras[0])
    assert fired == []


def test_kaic_error_envelope_no_alert(tmp_path):
    transport = _FakeKaicTransport()
    transport.set_error_envelope()
    detector, _ = _build_detector(
        tmp_path,
        transport=transport,
        restricted_hours=RestrictedHours(start=_dt.time(0, 0), end=_dt.time(23, 59)),
        now=_dt.datetime(2026, 5, 19, 10, 0),
    )
    fired = detector.step(detector._config.cameras[0])
    assert fired == []


def test_kaic_request_threads_correlation_id_and_api_key(tmp_path):
    transport = _FakeKaicTransport()
    transport.set_detections([_detection("person", x=0.45, y=0.45)])
    detector, _ = _build_detector(
        tmp_path,
        transport=transport,
        restricted_hours=RestrictedHours(start=_dt.time(0, 0), end=_dt.time(23, 59)),
        now=_dt.datetime(2026, 5, 19, 10, 0),
    )
    detector.step(detector._config.cameras[0])
    assert len(transport.requests) == 1
    headers = transport.requests[0]["headers"]
    assert headers.get("x-correlation-id")
    assert len(headers["x-correlation-id"]) >= 16
    assert headers.get("x-internal-api-key") == "test-key"
    # Body carries camera_id and base64 frame
    body = transport.requests[0]["body"]
    assert body["camera_id"] == "cam-test"
    assert body["frame_b64"]


# ── Restricted-hours edge cases ────────────────────────────────────


def test_restricted_hours_cross_midnight_late_night():
    """22:00 - 06:00 window. 23:30 IS in window."""
    rh = RestrictedHours(start=_dt.time(22, 0), end=_dt.time(6, 0))
    assert rh.contains(_dt.datetime(2026, 5, 19, 23, 30))


def test_restricted_hours_cross_midnight_early_morning():
    """22:00 - 06:00 window. 03:00 IS in window."""
    rh = RestrictedHours(start=_dt.time(22, 0), end=_dt.time(6, 0))
    assert rh.contains(_dt.datetime(2026, 5, 19, 3, 0))


def test_restricted_hours_cross_midnight_safe_afternoon():
    """22:00 - 06:00 window. 14:00 is NOT in window."""
    rh = RestrictedHours(start=_dt.time(22, 0), end=_dt.time(6, 0))
    assert not rh.contains(_dt.datetime(2026, 5, 19, 14, 0))


def test_restricted_hours_normal_window_inclusive_start():
    rh = RestrictedHours(start=_dt.time(9, 0), end=_dt.time(17, 0))
    assert rh.contains(_dt.datetime(2026, 5, 19, 9, 0))
    assert rh.contains(_dt.datetime(2026, 5, 19, 16, 59))
    assert not rh.contains(_dt.datetime(2026, 5, 19, 17, 0))  # exclusive end


# ── Config loader ──────────────────────────────────────────────────


def test_load_config_minimal(tmp_path: Path):
    cfg = tmp_path / "config.yml"
    cfg.write_text(dedent("""
        kaic_url: "http://kaic:8100"
        kaic_adapter_name: "yolov8"
        restricted_hours:
            start: "00:00"
            end: "23:59"
        cameras:
          - camera_id: "cam-1"
            frame_url: "file:///tmp/x.jpg"
            zone:
              - [0, 0]
              - [100, 0]
              - [100, 100]
              - [0, 100]
    """))
    config = load_config(str(cfg))
    assert config.kaic_url == "http://kaic:8100"
    assert config.kaic_adapter_name == "yolov8"
    assert config.watch_labels == ["person"]
    assert len(config.cameras) == 1


def test_load_config_rejects_no_cameras(tmp_path: Path):
    cfg = tmp_path / "config.yml"
    cfg.write_text("kaic_url: x\nrestricted_hours:\n    start: '00:00'\n    end: '01:00'\n")
    with pytest.raises(ValueError, match="at least one camera"):
        load_config(str(cfg))


def test_load_config_rejects_bad_hours(tmp_path: Path):
    cfg = tmp_path / "config.yml"
    cfg.write_text(dedent("""
        kaic_url: "x"
        restricted_hours:
            start: "not a time"
            end: "01:00"
        cameras:
          - camera_id: "c"
            frame_url: "file:///x"
            zone: [[0,0],[1,0],[1,1]]
    """))
    with pytest.raises(ValueError, match="restricted_hours"):
        load_config(str(cfg))


def test_load_config_rejects_zero_poll_interval(tmp_path: Path):
    cfg = tmp_path / "config.yml"
    cfg.write_text(dedent("""
        kaic_url: "x"
        poll_interval_seconds: 0
        restricted_hours:
            start: "00:00"
            end: "01:00"
        cameras:
          - camera_id: "c"
            frame_url: "file:///x"
            zone: [[0,0],[1,0],[1,1]]
    """))
    with pytest.raises(ValueError, match="poll_interval_seconds"):
        load_config(str(cfg))
