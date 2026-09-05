# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Smoke tests for __APP_NAME__ — the parity bar for a generated app.

These drive the detector THROUGH ``handle_event`` (the SDK base's
decode → on_detections → dispatch path) without spinning up a NATS
broker: an in-memory recorder channel captures whatever the rule fires.
Keep this green as you replace the starter rule with your own.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from opennvr_app_sdk import Alert, AlertDispatcher

from __APP_MODULE__ import __APP_CLASS__, AppConfig, load_config


class _RecorderChannel:
    """Captures dispatched alerts in memory so a test can assert on them
    without a webhook or NATS broker."""

    name = "recorder"

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def send(self, alert: Alert) -> bool:
        self.alerts.append(alert)
        return True


def _build(
    *, watch_labels: list[str] | None = None,
) -> tuple[__APP_CLASS__, _RecorderChannel]:
    """Construct the detector with an in-memory dispatcher."""
    config = AppConfig(
        nats_url="nats://test:4222",
        subject_pattern="opennvr.inference.>",
        watch_labels=watch_labels or ["person"],
    )
    recorder = _RecorderChannel()
    dispatcher = AlertDispatcher([recorder])
    detector = __APP_CLASS__(config, dispatcher)
    return detector, recorder


def _event(*, label: str = "person", camera_id: str = "cam-1") -> dict[str, Any]:
    """A minimal §12 InferenceCompletedEvent body with one detection."""
    return {
        "correlation_id": "corr-1",
        "adapter": "yolov8",
        "adapter_version": "1.0.0",
        "camera_id": camera_id,
        "model_fingerprint": "sha256:test",
        "completed_at": "2026-01-02T03:04:05Z",
        "result": {
            "detections": [
                {
                    "label": label,
                    "confidence": 0.9,
                    "bbox": {"x": 0.4, "y": 0.4, "w": 0.1, "h": 0.1},
                    "track_id": None,
                    "attributes": {},
                },
            ],
        },
    }


# ── The parity bar ─────────────────────────────────────────────────


def test_matching_detection_fires_one_alert():
    """A watched-label detection fires exactly one alert, carrying the
    camera + correlation id through to the §11.5 envelope."""
    detector, recorder = _build(watch_labels=["person"])
    fired = detector.handle_event(_event(label="person", camera_id="cam-1"))

    assert len(fired) == 1
    alert = fired[0]
    assert isinstance(alert, Alert)
    assert alert.camera_id == "cam-1"
    assert alert.correlation_id == "corr-1"
    # The dispatcher actually delivered it to the channel.
    assert recorder.alerts == fired


def test_non_watched_label_is_quiet():
    """A detection whose label isn't watched fires nothing."""
    detector, recorder = _build(watch_labels=["person"])
    fired = detector.handle_event(_event(label="bicycle"))

    assert fired == []
    assert recorder.alerts == []


def test_no_detections_is_quiet():
    """An event with an empty detections list fires nothing."""
    detector, recorder = _build()
    event = _event()
    event["result"]["detections"] = []
    assert detector.handle_event(event) == []


def test_config_loader_roundtrips(tmp_path):
    """The YAML loader parses a minimal config and applies defaults."""
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "nats_url: nats://localhost:4222\n"
        "watch_labels:\n"
        "  - person\n"
    )
    cfg = load_config(str(cfg_file))
    assert cfg.nats_url == "nats://localhost:4222"
    assert cfg.watch_labels == ["person"]
    assert cfg.subject_pattern == "opennvr.inference.>"  # default applied


def test_manifest_identity_matches_module():
    """The manifest is the app's declarative identity; the index entry
    mirrors it (docs/CONTRIBUTING_APPS.md)."""
    detector, _ = _build()
    assert detector.manifest.id == "__APP_ID__"
    assert detector.manifest.name == "__APP_NAME__"
    assert "__TASK__" in detector.manifest.requires_tasks
