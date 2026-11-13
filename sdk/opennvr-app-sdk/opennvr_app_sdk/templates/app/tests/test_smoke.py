# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Smoke tests for __APP_NAME__ — the parity bar for a generated app.

These drive the app through the SDK's decode → rule → dispatch path
without a NATS broker, using ``opennvr_app_sdk.testing``: a recorder
channel captures whatever the rule fires, and the event builders
produce contract-shaped inference events. ``app.build(config,
dispatcher)`` gives you the compiled detector directly. Keep this green
as you replace the starter rule with your own.
"""
from __future__ import annotations

from opennvr_app_sdk import Alert
from opennvr_app_sdk.testing import RecorderChannel, detection, feed, inference_event

from __APP_MODULE__ import app


def _build(**overrides):
    """Construct the app with an in-memory dispatcher."""
    config = app.config_class()(
        nats_url="nats://test:4222",
        subject_pattern="opennvr.inference.>",
        **overrides,
    )
    recorder = RecorderChannel()
    return app.build(config, recorder.dispatcher()), recorder


# ── The parity bar ─────────────────────────────────────────────────


def test_matching_detection_fires_one_alert():
    """A watched detection fires exactly one alert, carrying the camera
    + correlation id through to the §11.5 envelope."""
    detector, recorder = _build()
    fired = feed(detector, inference_event(detection("person"), camera_id="cam-1",
                                           correlation_id="corr-1"))

    assert len(fired) == 1
    alert = fired[0]
    assert isinstance(alert, Alert)
    assert alert.camera_id == "cam-1"
    assert alert.correlation_id == "corr-1"
    # The dispatcher actually delivered it to the channel.
    assert recorder.alerts == fired


def test_unwatched_label_is_quiet():
    """A detection the rule doesn't watch fires nothing."""
    detector, recorder = _build()
    assert feed(detector, inference_event(detection("bicycle"))) == []
    assert recorder.alerts == []


def test_low_confidence_is_quiet():
    """A detection below ``min_confidence`` fires nothing — the filter
    is the rule's ``min_confidence="$min_confidence"``, so the operator
    controls it from config.yml with no code change."""
    detector, _ = _build(min_confidence=0.95)
    assert feed(detector, inference_event(detection("person", confidence=0.6))) == []


def test_no_detections_is_quiet():
    """An event with an empty detections list fires nothing."""
    detector, _ = _build()
    assert feed(detector, inference_event()) == []


def test_config_loader_roundtrips(tmp_path):
    """The YAML loader parses a minimal config and applies defaults."""
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("min_confidence: 0.7\n")
    cfg = app.load_config(str(cfg_file))
    assert cfg.min_confidence == 0.7
    # Defaults the SDK supplies: the deployment's NATS endpoint (from
    # the environment the installer exports) and the detection stream.
    assert cfg.nats_url
    assert cfg.subject_pattern == "opennvr.inference.>"
    assert cfg.consume_tier0 is True


def test_manifest_identity_matches_module():
    """The manifest is the app's declarative identity; the index entry
    mirrors it (docs/CONTRIBUTING_APPS.md)."""
    manifest = app.manifest()
    assert manifest.id == "__APP_ID__"
    assert manifest.name == "__APP_NAME__"
    assert "__TASK__" in manifest.requires_tasks
    assert manifest.subscribes == "opennvr.inference.>"
    assert [a.name for a in manifest.emits] == ["__APP_ID__"]
