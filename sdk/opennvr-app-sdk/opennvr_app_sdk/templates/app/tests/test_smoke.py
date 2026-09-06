# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Smoke tests for __APP_NAME__ — the parity bar for a generated app.

These drive the detector through the SDK's decode → on_detections →
dispatch path without a NATS broker, using ``opennvr_app_sdk.testing``:
a recorder channel captures whatever the rule fires, and the event
builders produce contract-shaped inference events. Keep this green as
you replace the starter rule with your own.
"""
from __future__ import annotations

from opennvr_app_sdk import Alert
from opennvr_app_sdk.testing import RecorderChannel, detection, feed, inference_event

from __APP_MODULE__ import __APP_CLASS__, AppConfig, load_config


def _build(*, watch_labels: list[str] | None = None) -> tuple[__APP_CLASS__, RecorderChannel]:
    """Construct the detector with an in-memory dispatcher."""
    config = AppConfig(
        nats_url="nats://test:4222",
        subject_pattern="opennvr.inference.>",
        watch_labels=watch_labels or ["person"],
    )
    recorder = RecorderChannel()
    return __APP_CLASS__(config, recorder.dispatcher()), recorder


# ── The parity bar ─────────────────────────────────────────────────


def test_matching_detection_fires_one_alert():
    """A watched-label detection fires exactly one alert, carrying the
    camera + correlation id through to the §11.5 envelope."""
    detector, recorder = _build(watch_labels=["person"])
    fired = feed(detector, inference_event(detection("person"), camera_id="cam-1",
                                           correlation_id="corr-1"))

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
    assert feed(detector, inference_event(detection("bicycle"))) == []
    assert recorder.alerts == []


def test_no_detections_is_quiet():
    """An event with an empty detections list fires nothing."""
    detector, _ = _build()
    assert feed(detector, inference_event()) == []


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
