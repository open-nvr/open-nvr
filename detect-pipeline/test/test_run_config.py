# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for service config parsing + manager wiring (no NATS/core)."""
from __future__ import annotations

from detect_pipeline.bus import EventSink
from detect_pipeline.run import build_manager, config_from_env


def test_defaults_are_enabled_with_hog():
    cfg = config_from_env({})
    assert cfg.enabled is True                    # on by default
    assert cfg.detector == "hog"
    assert cfg.core_url.startswith("http://opennvr-core")
    assert cfg.refresh_seconds == 30.0


def test_disable_flag_parsing():
    for val in ("false", "0", "no", "off", "FALSE"):
        assert config_from_env({"DETECT_PIPELINE_ENABLED": val}).enabled is False
    for val in ("true", "1", "yes", "on"):
        assert config_from_env({"DETECT_PIPELINE_ENABLED": val}).enabled is True


def test_env_overrides():
    cfg = config_from_env({
        "OPENNVR_INTERNAL_URL": "http://core:9000",
        "INTERNAL_API_KEY": "tok",
        "NATS_URL": "nats://nats:4222",
        "DETECT_HWACCEL": "vaapi",
        "DETECT_MODEL_SIZE": "640",
    })
    assert cfg.core_url == "http://core:9000" and cfg.api_key == "tok"
    assert cfg.nats_url == "nats://nats:4222" and cfg.hwaccel == "vaapi"
    assert cfg.model_size == 640


def test_detector_factory_falls_back_to_stub_when_hog_unavailable(monkeypatch):
    import numpy as np

    import detect_pipeline.detectors_local as dl
    from detect_pipeline.run import _detector_factory

    monkeypatch.setattr(dl, "hog_available", lambda: False)   # simulate OpenCV 5
    det = _detector_factory("hog")()                          # must not raise
    assert det.detect(np.zeros((8, 8, 3), np.uint8)) == []    # degraded to stub


def test_build_manager_honours_disabled():
    cfg = config_from_env({"DETECT_PIPELINE_ENABLED": "false"})
    mgr = build_manager(cfg, EventSink(lambda s, d: None))
    mgr.reconcile()                                # disabled -> no discovery, no workers
    assert mgr.running_ids() == set()
