# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for service config parsing + manager wiring (no NATS/core)."""
from __future__ import annotations

import numpy as np

from detect_pipeline.bus import EventSink
from detect_pipeline.run import _detector_factory, build_manager, config_from_env


def test_defaults_are_enabled_with_onnx():
    cfg = config_from_env({})
    assert cfg.enabled is True                    # on by default
    assert cfg.detector == "onnx"                 # ONNX is the default detector
    assert cfg.onnx_model.endswith("yolov8n.onnx")
    assert cfg.onnx_input == 640
    assert cfg.onnx_backend == "cvdnn"            # zero-dep default backend
    assert cfg.onnx_providers == ""


def test_onnx_backend_and_providers_override():
    cfg = config_from_env({
        "DETECT_ONNX_BACKEND": "ort",
        "DETECT_ONNX_PROVIDERS": "OpenVINOExecutionProvider,CPUExecutionProvider",
    })
    assert cfg.onnx_backend == "ort"
    assert cfg.onnx_providers == "OpenVINOExecutionProvider,CPUExecutionProvider"
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


def test_model_id_derivation_for_benchmarking():
    # onnx default -> model file basename; explicit override wins; non-onnx -> type
    assert config_from_env({}).model_id == "yolov8n"
    assert config_from_env({"DETECT_ONNX_MODEL": "/w/yolo11s.onnx"}).model_id == "yolo11s"
    assert config_from_env({"DETECT_MODEL_ID": "rfdetr-n-int8"}).model_id == "rfdetr-n-int8"
    assert config_from_env({"DETECT_DETECTOR": "blob"}).model_id == "blob"


def test_onnx_detector_falls_back_to_stub_when_model_missing():
    cfg = config_from_env({"DETECT_ONNX_MODEL": "/does/not/exist.onnx"})
    det = _detector_factory(cfg)()                            # must not raise
    assert det.detect(np.zeros((8, 8, 3), np.uint8)) == []    # degraded to stub


def test_onnx_factory_degrades_to_stub_when_backend_construction_raises(monkeypatch, tmp_path):
    model = tmp_path / "m.onnx"
    model.write_bytes(b"not a real onnx model")          # exists, so we reach construction
    import detect_pipeline.onnx_detector as od

    def boom(*a, **k):
        raise RuntimeError("bad model / missing onnxruntime")

    monkeypatch.setattr(od, "OnnxYoloDetector", boom)     # _detector_factory imports it at call time
    cfg = config_from_env({"DETECT_ONNX_MODEL": str(model), "DETECT_ONNX_BACKEND": "ort"})
    det = _detector_factory(cfg)()                        # must not raise
    assert det.detect(np.zeros((8, 8, 3), np.uint8)) == []  # degraded to stub


def test_unknown_backend_falls_back_to_cvdnn(monkeypatch, tmp_path, caplog):
    model = tmp_path / "m.onnx"
    model.write_bytes(b"x")
    import detect_pipeline.onnx_detector as od
    seen = {}

    def capture(*a, **k):
        seen["backend"] = k.get("backend")
        from detect_pipeline.detector import StubDetector
        return StubDetector()

    monkeypatch.setattr(od, "OnnxYoloDetector", capture)
    cfg = config_from_env({"DETECT_ONNX_MODEL": str(model), "DETECT_ONNX_BACKEND": "garbage"})
    _detector_factory(cfg)()
    assert seen["backend"] == "cvdnn"                     # invalid -> safe default, not stub-by-error


def test_hog_detector_falls_back_to_stub_when_unavailable(monkeypatch):
    import detect_pipeline.detectors_local as dl

    monkeypatch.setattr(dl, "hog_available", lambda: False)   # simulate OpenCV 5
    cfg = config_from_env({"DETECT_DETECTOR": "hog"})
    det = _detector_factory(cfg)()                            # must not raise
    assert det.detect(np.zeros((8, 8, 3), np.uint8)) == []    # degraded to stub


def test_gate_defaults_off():
    from detect_pipeline.run import _gate_factory
    cfg = config_from_env({})
    assert cfg.gate_mode == "off"          # PR A behavior unchanged by default
    assert cfg.metrics_port == 9109
    assert _gate_factory(cfg) is None       # no gate when off


def test_gate_factory_shadow_and_enforce():
    from detect_pipeline.gate import Gate
    from detect_pipeline.run import _gate_factory

    gf = _gate_factory(config_from_env({
        "DETECT_GATE_MODE": "shadow",
        "DETECT_GATE_CRITICAL_CLASSES": "person, weapon",
        "DETECT_GATE_HEARTBEAT_S": "5",
    }))
    g = gf()                                # fresh, stateful gate per camera
    assert isinstance(g, Gate) and g.cfg.shadow is True
    assert g.cfg.critical_classes == frozenset({"person", "weapon"})
    assert g.cfg.heartbeat_s == 5.0
    assert _gate_factory(config_from_env({"DETECT_GATE_MODE": "enforce"}))().cfg.shadow is False


def test_gate_unknown_mode_disables():
    from detect_pipeline.run import _gate_factory
    assert _gate_factory(config_from_env({"DETECT_GATE_MODE": "garbage"})) is None


def test_build_manager_honours_disabled():
    cfg = config_from_env({"DETECT_PIPELINE_ENABLED": "false"})
    mgr = build_manager(cfg, EventSink(lambda s, d: None))
    mgr.reconcile()                                # disabled -> no discovery, no workers
    assert mgr.running_ids() == set()


# ── NATS connect options (token auth against the compose broker) ────

def test_nats_options_include_token_and_bounded_reconnects():
    from detect_pipeline.run import _nats_connect_options

    opts = _nats_connect_options("nats://nats:4222", "sekret")
    assert opts["servers"] == ["nats://nats:4222"]
    assert opts["token"] == "sekret"
    assert opts["max_reconnect_attempts"] == 10


def test_nats_options_omit_token_when_absent():
    from detect_pipeline.run import _nats_connect_options

    opts = _nats_connect_options("nats://nats:4222", None)
    assert "token" not in opts
