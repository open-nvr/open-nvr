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
    assert cfg.onnx_backend == "auto"             # per-family resolution (onnx→cvdnn)
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


def test_gate_defaults_shadow():
    from detect_pipeline.run import _gate_factory
    cfg = config_from_env({})
    # shadow by default: measure-only (audits escalate/suppress, runs no
    # expensive model) so deployments accumulate would-save data from day
    # one. Behavior-changing enforcement remains opt-in.
    assert cfg.gate_mode == "shadow"
    assert cfg.metrics_port == 9109
    assert _gate_factory(cfg) is not None   # shadow builds a (measure-only) gate
    cfg_off = config_from_env({"DETECT_GATE_MODE": "off"})
    assert _gate_factory(cfg_off) is None   # off still means no gate at all


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

def test_nats_options_include_token_and_never_give_up_reconnecting():
    """Reconnects must be UNBOUNDED. They used to stop after 10 attempts and
    nothing rebuilt the client, so a broker restart that outlasted ten tries
    left every camera's live events dead until the container was restarted.
    The retry WAIT is what keeps a misconfigured broker from looping hot."""
    from detect_pipeline.run import _nats_connect_options

    opts = _nats_connect_options("nats://nats:4222", "sekret")
    assert opts["servers"] == ["nats://nats:4222"]
    assert opts["token"] == "sekret"
    assert opts["max_reconnect_attempts"] == -1
    assert opts["reconnect_time_wait"] > 0


def test_nats_options_omit_token_when_absent():
    from detect_pipeline.run import _nats_connect_options

    opts = _nats_connect_options("nats://nats:4222", None)
    assert "token" not in opts


# ── guided promotion: managed gate-mode override ────────────────────

def test_fetch_detect_config_parses_and_authenticates():
    import io
    import json as _json
    from detect_pipeline.providers import fetch_detect_config

    seen = {}

    class _Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def opener(req, timeout=None):
        seen["url"] = req.full_url
        seen["key"] = req.get_header("X-internal-api-key")
        return _Resp(_json.dumps({"gate_mode": "enforce"}).encode())

    conf = fetch_detect_config("http://core:8000", "sekret", opener=opener)
    assert conf == {"gate_mode": "enforce"}
    assert seen["url"].endswith("/api/v1/internal/camera-agent/detect-config")
    assert seen["key"] == "sekret"


def test_fetch_detect_config_failure_returns_none():
    from detect_pipeline.providers import fetch_detect_config

    def opener(req, timeout=None):
        raise OSError("core down")

    assert fetch_detect_config("http://core:8000", "k", opener=opener) is None


def test_apply_gate_change_stops_workers_and_swaps_factory():
    from detect_pipeline.service import WorkerManager

    class _FakeWorker:
        def __init__(self): self.stopped = False
        def start(self): pass
        def stop(self): self.stopped = True
        def is_alive(self): return not self.stopped

    class _Provider:
        def list_cameras(self): return []

    made = []
    mgr = WorkerManager(_Provider(), sink=None,
                        worker_factory=lambda spec, sink: _FakeWorker())
    w = _FakeWorker()
    mgr._workers["cam1"] = w

    new_factory = lambda: "new-gate"  # noqa: E731
    mgr.apply_gate_change(new_factory, dispatcher="d", router="r")
    assert w.stopped                       # all workers recycled
    assert mgr.running_ids() == set()      # next reconcile rebuilds them
    assert mgr._gate_factory is new_factory
    assert mgr._dispatcher == "d" and mgr._router == "r"


def test_decode_skip_from_env_and_invalid_falls_back():
    """DETECT_DECODE_SKIP flows into the config; a typo degrades to full
    decode instead of killing every worker at ffmpeg-spawn time."""
    from detect_pipeline.run import config_from_env

    assert config_from_env({}).decode_skip == "nonref"   # safe-by-default saving
    assert config_from_env({"DETECT_DECODE_SKIP": "NoKey "}).decode_skip == "nokey"
    assert config_from_env({"DETECT_DECODE_SKIP": "keyframes"}).decode_skip == "none"


def test_decode_threads_and_fast_from_env():
    from detect_pipeline.run import config_from_env

    cfg = config_from_env({})
    assert cfg.decode_threads == 2 and cfg.fast_decode is False
    cfg = config_from_env({"DETECT_DECODE_THREADS": "0", "DETECT_DECODE_FAST": "true"})
    assert cfg.decode_threads == 0 and cfg.fast_decode is True
    assert config_from_env({"DETECT_DECODE_THREADS": "lots"}).decode_threads == 2


def test_decode_idle_from_env():
    from detect_pipeline.run import config_from_env

    assert config_from_env({}).decode_idle == "nokey"                  # adaptive ON by default
    cfg = config_from_env({"DETECT_DECODE_IDLE": "nokey",
                           "DETECT_DECODE_IDLE_AFTER": "30"})
    assert cfg.decode_idle == "nokey" and cfg.decode_idle_after == 30.0
    assert config_from_env({"DETECT_DECODE_IDLE": "off"}).decode_idle == ""
    assert config_from_env({"DETECT_DECODE_IDLE": "keyframes"}).decode_idle == ""


def test_detector_factory_rfdetr(tmp_path, monkeypatch):
    """DETECT_DETECTOR=rfdetr builds the DETR detector; backend 'auto'
    resolves to ort for this family; a missing model degrades to the stub."""
    from detect_pipeline.run import _detector_factory, config_from_env

    cfg = config_from_env({"DETECT_DETECTOR": "rfdetr",
                           "DETECT_ONNX_MODEL": str(tmp_path / "nope.onnx")})
    assert type(_detector_factory(cfg)()).__name__ == "StubDetector"

    model = tmp_path / "rfdetr-nano.onnx"
    model.write_bytes(b"stub")
    import detect_pipeline.detr_detector as dd

    built = {}
    monkeypatch.setattr(dd, "OnnxDetrDetector",
                        lambda **kw: built.update(kw) or object())
    cfg = config_from_env({
        "DETECT_DETECTOR": "rfdetr",
        "DETECT_ONNX_MODEL": str(model),
        "DETECT_ONNX_INPUT": "384",
    })
    _detector_factory(cfg)()
    assert built["model_path"] == str(model)
    assert built["input_size"] == 384
    assert built["backend"] == "ort"          # family default via 'auto'


def test_resolve_onnx_backend_auto_and_explicit():
    from detect_pipeline.run import _resolve_onnx_backend

    assert _resolve_onnx_backend("auto", "cvdnn") == "cvdnn"
    assert _resolve_onnx_backend("", "ort") == "ort"
    assert _resolve_onnx_backend("cvdnn", "ort") == "cvdnn"   # explicit wins
    assert _resolve_onnx_backend("tensorflow", "cvdnn") == "cvdnn"
