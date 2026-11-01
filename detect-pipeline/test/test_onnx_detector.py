# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the ONNX YOLO detector — decode + NMS, no model file needed."""
from __future__ import annotations

import numpy as np
import pytest

from detect_pipeline.onnx_detector import (
    COCO_LABELS,
    CvDnnBackend,
    OnnxYoloDetector,
    OrtBackend,
    build_backend,
    postprocess_yolo,
    resolve_providers,
)

INPUT = 640
NC = 80


def _output(dets, n=8400):
    """Build a YOLOv8-shaped (1, 4+NC, n) tensor. dets: (cx,cy,w,h,class,score)."""
    arr = np.zeros((1, 4 + NC, n), np.float32)
    for i, (cx, cy, w, h, cls, score) in enumerate(dets):
        arr[0, 0, i] = cx
        arr[0, 1, i] = cy
        arr[0, 2, i] = w
        arr[0, 3, i] = h
        arr[0, 4 + cls, i] = score
    return arr


def test_decodes_single_person_box_normalized():
    out = _output([(160, 160, 40, 80, 0, 0.9)])   # class 0 = person
    dets = postprocess_yolo(out, input_size=INPUT)
    assert len(dets) == 1
    d = dets[0]
    assert d.label == "person"
    assert abs(d.score - 0.9) < 1e-5
    # cx160,cy160,w40,h80 -> x1=140,y1=120,x2=180,y2=200 ; normalized by 640
    assert d.box == pytest.approx((140 / 640, 120 / 640, 180 / 640, 200 / 640), abs=1e-4)


def test_confidence_threshold_filters():
    out = _output([(160, 160, 40, 80, 0, 0.10)])   # below default 0.25
    assert postprocess_yolo(out, input_size=INPUT) == []


def test_nms_suppresses_overlapping_same_class():
    out = _output([
        (160, 160, 40, 80, 2, 0.90),               # car, high
        (162, 161, 41, 79, 2, 0.80),               # near-duplicate car, lower
    ])
    dets = postprocess_yolo(out, input_size=INPUT, iou_threshold=0.45)
    assert len(dets) == 1
    assert dets[0].label == "car" and abs(dets[0].score - 0.90) < 1e-5


def test_handles_transposed_and_squeezed_shapes():
    out = _output([(100, 100, 20, 20, 0, 0.8)])
    squeezed = out[0]                               # (84, 8400)
    transposed = squeezed.T                         # (8400, 84)
    assert len(postprocess_yolo(squeezed, input_size=INPUT)) == 1
    assert len(postprocess_yolo(transposed, input_size=INPUT)) == 1


def test_empty_output_is_safe():
    assert postprocess_yolo(np.zeros((1, 4 + NC, 8400), np.float32), input_size=INPUT) == []


class _FakeNet:
    def __init__(self, output):
        self._output = output
        self.blob = None

    def setInput(self, blob):
        self.blob = blob

    def forward(self):
        return self._output


def test_detector_with_injected_net_runs_full_path():
    net = _FakeNet(_output([(320, 320, 60, 120, 0, 0.85)]))
    det = OnnxYoloDetector(input_size=INPUT, net=net)
    crop = np.zeros((INPUT, INPUT, 3), np.uint8)
    dets = det.detect(crop)
    assert net.blob is not None and net.blob.shape == (1, 3, INPUT, INPUT)  # NCHW blob
    assert len(dets) == 1 and dets[0].label == "person"


def test_detector_requires_model_or_net():
    with pytest.raises(ValueError):
        OnnxYoloDetector()                          # no model_path, no net


def test_coco_labels_length():
    assert len(COCO_LABELS) == 80


# ─────────────────────── pluggable backend: ORT ───────────────────────

class _FakeSession:
    """Stand-in for an onnxruntime InferenceSession (no onnxruntime needed)."""

    def __init__(self, output):
        self._output = output
        self.fed = None

    def get_inputs(self):
        class _I:
            name = "images"
        return [_I()]

    def run(self, output_names, feed):
        self.fed = feed
        return [self._output]


def test_ort_backend_with_injected_session():
    sess = _FakeSession(_output([(320, 320, 60, 120, 0, 0.85)]))
    det = OnnxYoloDetector(input_size=INPUT, session=sess)
    assert det.backend_name == "ort"
    dets = det.detect(np.zeros((INPUT, INPUT, 3), np.uint8))
    # the same NCHW blob preprocessing feeds either backend
    assert sess.fed["images"].shape == (1, 3, INPUT, INPUT)
    assert len(dets) == 1 and dets[0].label == "person"


def test_cvdnn_and_ort_agree_on_same_output():
    out = _output([(200, 200, 40, 40, 2, 0.7)])
    cv_det = OnnxYoloDetector(input_size=INPUT, net=_FakeNet(out))
    ort_det = OnnxYoloDetector(input_size=INPUT, session=_FakeSession(out))
    assert cv_det.backend_name == "cvdnn" and ort_det.backend_name == "ort"
    assert cv_det.detect(np.zeros((INPUT, INPUT, 3), np.uint8)) == \
        ort_det.detect(np.zeros((INPUT, INPUT, 3), np.uint8))


def test_resolve_providers_filters_unavailable_and_keeps_cpu():
    provs = resolve_providers(
        ["OpenVINOExecutionProvider", "TensorrtExecutionProvider"],
        available=["OpenVINOExecutionProvider", "CPUExecutionProvider"],
    )
    assert provs == ["OpenVINOExecutionProvider", "CPUExecutionProvider"]  # TRT dropped, CPU kept


def test_resolve_providers_none_uses_available():
    assert resolve_providers(None, ["CUDAExecutionProvider", "CPUExecutionProvider"]) == \
        ["CUDAExecutionProvider", "CPUExecutionProvider"]


def test_resolve_providers_accepts_comma_string_and_appends_cpu():
    assert resolve_providers("OpenVINOExecutionProvider", available=[]) == \
        ["OpenVINOExecutionProvider", "CPUExecutionProvider"]


def test_build_backend_selects_type():
    assert isinstance(build_backend("cvdnn", net=_FakeNet(_output([]))), CvDnnBackend)
    assert isinstance(build_backend("ort", session=_FakeSession(_output([]))), OrtBackend)
    with pytest.raises(ValueError):
        build_backend("tensorrt")   # not a backend name (it's an ORT provider)


def test_build_backend_aliases():
    assert build_backend("opencv", net=_FakeNet(_output([]))).name == "cvdnn"
    assert build_backend("onnxruntime", session=_FakeSession(_output([]))).name == "ort"


def test_resolve_providers_all_filtered_falls_back_to_cpu():
    # every requested EP unavailable -> only the CPU fallback remains
    assert resolve_providers(["HailoExecutionProvider"], available=["CPUExecutionProvider"]) == \
        ["CPUExecutionProvider"]


# ── ORT thread capping (DETECT_CV_THREADS governs cv2 only) ─────────

class _FakeSessionOptions:
    def __init__(self):
        self.intra_op_num_threads = 0
        self.inter_op_num_threads = 0


class _FakeOrt:
    SessionOptions = _FakeSessionOptions


def test_ort_session_options_honour_detect_cv_threads(monkeypatch):
    """cv2.setNumThreads caps OpenCV ONLY. An ORT session built with no
    options defaults intra_op to the core count and allocates its own pool,
    so the ort/rfdetr paths escaped the service's only CPU governor — and
    several sessions each believed they owned every core."""
    from detect_pipeline.onnx_detector import _ort_session_options

    monkeypatch.setenv("DETECT_CV_THREADS", "3")
    opts = _ort_session_options(_FakeOrt)
    assert opts.intra_op_num_threads == 3
    assert opts.inter_op_num_threads == 1


def test_ort_session_options_let_ort_decide_when_uncapped(monkeypatch):
    """0 means "uncapped" for OpenCV; it must mean the same for ORT rather
    than pinning it to a single thread."""
    from detect_pipeline.onnx_detector import _ort_session_options

    monkeypatch.setenv("DETECT_CV_THREADS", "0")
    opts = _ort_session_options(_FakeOrt)
    assert opts.intra_op_num_threads == 0


def test_ort_session_options_survive_a_garbage_value(monkeypatch):
    from detect_pipeline.onnx_detector import _ort_session_options

    monkeypatch.setenv("DETECT_CV_THREADS", "banana")
    assert _ort_session_options(_FakeOrt).intra_op_num_threads == 2   # the default


# ── fixed-shape exports must not take the camera down ───────────────

class _FixedShapeDetector:
    """Stands in for ANY family whose ONNX export is fixed-shape.

    Deliberately not a cv2 net: the probe must work for RF-DETR (384/432/560)
    and whatever comes next, not just YOLO — so it goes through the
    detector's own detect().
    """

    def __init__(self, accepts: int, requested: int):
        self.accepts = accepts
        self.input_size = requested
        self.calls: list[int] = []

    def detect(self, crop):
        self.calls.append(self.input_size)
        if self.input_size != self.accepts:
            raise RuntimeError("outTotal == inpTotal in function 'getOutShape'")
        return []


def test_probe_adopts_the_size_the_model_actually_accepts():
    """The shipped yolov8n.onnx is exported at a FIXED 640. Feeding 320 raised
    deep in the graph — once per region, per frame. Nothing wrapped it, so it
    reached the worker loop and the camera went dark in a restart loop: a
    documented env knob could take detection down entirely."""
    from detect_pipeline.detector import resolve_input_size

    d = _FixedShapeDetector(accepts=640, requested=320)
    assert resolve_input_size(d, 320) == 640
    assert d.input_size == 320, "probing must not leave the size mutated"


def test_probe_covers_the_detr_family_too():
    """RF-DETR ships at 384/432/560 and fails identically — the probe is
    family-agnostic on purpose."""
    from detect_pipeline.detector import resolve_input_size

    assert resolve_input_size(_FixedShapeDetector(432, 640), 640) == 432
    assert resolve_input_size(_FixedShapeDetector(560, 384), 384) == 560


def test_probe_keeps_a_size_that_already_works():
    from detect_pipeline.detector import resolve_input_size

    d = _FixedShapeDetector(accepts=640, requested=640)
    assert resolve_input_size(d, 640) == 640
    assert d.calls == [640], "a working size must not trigger extra probing"


def test_probe_returns_the_request_when_nothing_fits():
    """Never loop forever or invent a size: report and let it surface."""
    from detect_pipeline.detector import resolve_input_size

    assert resolve_input_size(_FixedShapeDetector(99, 640), 640) == 640


# ── provider device shorthand (OpenNVR Accelerate) ─────────────────────────

def test_parse_provider_specs_device_shorthand_and_multi_device_continuation():
    from detect_pipeline.onnx_detector import parse_provider_specs
    assert parse_provider_specs("OpenVINOExecutionProvider:NPU,CPUExecutionProvider") == [
        ("OpenVINOExecutionProvider", "NPU"), ("CPUExecutionProvider", None)]
    # AUTO:NPU,GPU survives the comma split: GPU is not a provider name
    assert parse_provider_specs("OpenVINOExecutionProvider:AUTO:NPU,GPU,CPUExecutionProvider") == [
        ("OpenVINOExecutionProvider", "AUTO:NPU,GPU"), ("CPUExecutionProvider", None)]
    assert parse_provider_specs(["CUDAExecutionProvider:1"]) == [("CUDAExecutionProvider", "1")]
    assert parse_provider_specs(None) == [] and parse_provider_specs("") == []


def test_resolve_providers_carries_device_as_provider_options():
    provs = resolve_providers(
        "OpenVINOExecutionProvider:AUTO:NPU,GPU",
        available=["OpenVINOExecutionProvider", "CPUExecutionProvider"],
    )
    assert provs == [("OpenVINOExecutionProvider", {"device_type": "AUTO:NPU,GPU"}),
                     "CPUExecutionProvider"]
    provs = resolve_providers("CUDAExecutionProvider:1", available=["CUDAExecutionProvider"])
    assert provs == [("CUDAExecutionProvider", {"device_id": "1"}), "CPUExecutionProvider"]


def test_resolve_providers_device_on_provider_without_option_is_dropped():
    provs = resolve_providers("CPUExecutionProvider:0", available=["CPUExecutionProvider"])
    assert provs == ["CPUExecutionProvider"]   # no device option for CPU: bare name, no duplicate


def test_describe_providers_is_human_readable():
    from detect_pipeline.onnx_detector import describe_providers
    assert describe_providers([("OpenVINOExecutionProvider", {"device_type": "NPU"}),
                               "CPUExecutionProvider"]) == \
        "OpenVINOExecutionProvider(NPU), CPUExecutionProvider"


def test_ort_backend_exposes_effective_providers_and_warns_on_fallback(caplog):
    import logging

    class _Sess(_FakeSession):
        def get_providers(self):
            return ["CPUExecutionProvider"]

    b = OrtBackend(session=_Sess(_output([])))
    assert b.effective == ["CPUExecutionProvider"]      # injected session: no request, no warning
    b.requested = [("OpenVINOExecutionProvider", {"device_type": "NPU"}), "CPUExecutionProvider"]
    with caplog.at_level(logging.WARNING):
        eff = b._effective_providers()
    assert eff == ["CPUExecutionProvider"]
    assert "could not use OpenVINOExecutionProvider(NPU)" in caplog.text


def test_ort_backend_keeps_the_request_when_the_wheel_lacks_the_provider(monkeypatch, caplog, tmp_path):
    """A plain-onnxruntime wheel asked for OpenVINO: resolve_providers drops
    it before the session. ``requested`` must still say OpenVINO, so the
    tier0_detector_backend metric shows requested != effective rather than a
    CPU run that went as planned."""
    import logging
    import sys
    import types

    class _Sess(_FakeSession):
        def __init__(self, path, sess_options=None, providers=None):
            super().__init__(_output([]))

        def get_providers(self):
            return ["CPUExecutionProvider"]

    fake = types.ModuleType("onnxruntime")
    fake.InferenceSession = _Sess
    fake.SessionOptions = _FakeSessionOptions
    fake.get_available_providers = lambda: ["CPUExecutionProvider"]
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    model = tmp_path / "m.onnx"
    model.write_bytes(b"x")

    with caplog.at_level(logging.WARNING):
        b = OrtBackend(str(model), providers="OpenVINOExecutionProvider:NPU")
    assert b.requested == [("OpenVINOExecutionProvider", {"device_type": "NPU"})]
    assert b.effective == ["CPUExecutionProvider"]
    assert b.fallback_error is None
    assert "could not use OpenVINOExecutionProvider(NPU)" in caplog.text


def test_ort_backend_falls_back_to_cpu_when_the_accelerator_cannot_start(monkeypatch, caplog, tmp_path):
    """OpenVINO raising at session creation (device not passed through,
    driver missing) must leave Tier-0 running on CPU with the reason kept,
    not dead in a restart loop."""
    import logging
    import sys
    import types

    seen = []

    class _Sess(_FakeSession):
        def __init__(self, path, sess_options=None, providers=None):
            seen.append(list(providers))
            if providers and providers[0] != "CPUExecutionProvider":
                raise RuntimeError("clGetPlatformIDs failed")
            super().__init__(_output([]))

        def get_providers(self):
            return ["CPUExecutionProvider"]

    fake = types.ModuleType("onnxruntime")
    fake.InferenceSession = _Sess
    fake.SessionOptions = _FakeSessionOptions
    fake.get_available_providers = lambda: ["OpenVINOExecutionProvider", "CPUExecutionProvider"]
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    model = tmp_path / "m.onnx"
    model.write_bytes(b"x")

    with caplog.at_level(logging.ERROR):
        b = OrtBackend(str(model), providers="OpenVINOExecutionProvider:GPU")
    # ``requested`` keeps what was asked for: the metric shows the fallback as
    # requested != effective, not as a CPU run that went as planned.
    assert b.requested == [("OpenVINOExecutionProvider", {"device_type": "GPU"})]
    assert b.effective == ["CPUExecutionProvider"]
    assert b.fallback_error.startswith("RuntimeError: clGetPlatformIDs")
    assert "could not start on OpenVINOExecutionProvider(GPU)" in caplog.text
    assert seen[0][0][0] == "OpenVINOExecutionProvider" and seen[1] == ["CPUExecutionProvider"]

    # no accelerator requested: a failure is a real error
    class _Broken(_FakeSession):
        def __init__(self, *a, **k):
            raise ValueError("bad model")

    fake.InferenceSession = _Broken
    with pytest.raises(ValueError):
        OrtBackend(str(model), providers="CPUExecutionProvider")


# ── plugin execution providers (QNN, the -qualcomm flavour) ──────────────

class _PluginOrt:
    """Enough of the onnxruntime module surface for the plugin registry."""

    def __init__(self, available=("CPUExecutionProvider",)):
        self._available = list(available)
        self.registered = []

    def get_available_providers(self):
        return list(self._available)

    def register_execution_provider_library(self, name, path):
        self.registered.append((name, path))


def _reset_plugin_registry(monkeypatch):
    import detect_pipeline.onnx_detector as od
    monkeypatch.setattr(od, "_registered_plugin_eps", set())
    return od


def test_qnn_device_shorthand_is_the_backend_type():
    provs = resolve_providers("QNNExecutionProvider:htp",
                              available=["QNNExecutionProvider", "CPUExecutionProvider"])
    assert provs == [("QNNExecutionProvider", {"backend_type": "htp"}), "CPUExecutionProvider"]


def test_register_plugin_providers_registers_once_and_reports_missing_package(monkeypatch, caplog):
    import logging, sys, types
    od = _reset_plugin_registry(monkeypatch)
    fake_pkg = types.SimpleNamespace(get_library_path=lambda: "/lib/libonnxruntime_providers_qnn.so")
    monkeypatch.setitem(sys.modules, "onnxruntime_qnn", fake_pkg)
    ort = _PluginOrt()
    assert od.register_plugin_providers(ort, "QNNExecutionProvider:htp,CPUExecutionProvider") == {"QNNExecutionProvider"}
    assert od.register_plugin_providers(ort, ["QNNExecutionProvider"]) == {"QNNExecutionProvider"}
    assert ort.registered == [("QNNExecutionProvider", "/lib/libonnxruntime_providers_qnn.so")]  # once
    # available_providers() folds the registered plugin into the filter list
    assert od.available_providers(ort, "QNNExecutionProvider") == ["CPUExecutionProvider", "QNNExecutionProvider"]
    assert resolve_providers("QNNExecutionProvider:htp", od.available_providers(ort, "QNNExecutionProvider:htp"))[0] == \
        ("QNNExecutionProvider", {"backend_type": "htp"})
    # the cpu flavour has no onnxruntime_qnn: the request degrades, said out loud
    od2 = _reset_plugin_registry(monkeypatch)
    monkeypatch.delitem(sys.modules, "onnxruntime_qnn")
    monkeypatch.setattr("importlib.import_module", lambda name: (_ for _ in ()).throw(ImportError(name)))
    with caplog.at_level(logging.WARNING):
        assert od2.register_plugin_providers(_PluginOrt(), "QNNExecutionProvider") == set()
    assert "needs the onnxruntime_qnn package" in caplog.text
    assert resolve_providers("QNNExecutionProvider", ["CPUExecutionProvider"]) == ["CPUExecutionProvider"]


def test_provider_options_layers_defaults_shorthand_and_env():
    from detect_pipeline.onnx_detector import provider_options
    import sys, types
    monkeypatch_pkg = types.SimpleNamespace(get_qnn_htp_path=lambda: "/p/libQnnHtp.so", get_qnn_cpu_path=lambda: "/p/libQnnCpu.so")
    sys.modules["onnxruntime_qnn"] = monkeypatch_pkg
    try:
        base = provider_options(("QNNExecutionProvider", {"backend_type": "htp"}), env={})
        assert base["backend_path"] == "/p/libQnnHtp.so" and "backend_type" not in base
        assert provider_options(("QNNExecutionProvider", {"backend_type": "cpu"}), env={})["backend_path"] == "/p/libQnnCpu.so"
        # an explicit backend_path is left alone
        assert provider_options(("QNNExecutionProvider", {"backend_type": "htp"}),
                                env={"DETECT_ONNX_PROVIDER_OPTIONS": "backend_path=/mine.so"})["backend_path"] == "/mine.so"
    finally:
        del sys.modules["onnxruntime_qnn"]
    # without the package the shorthand stays as the documented option name
    base = provider_options(("QNNExecutionProvider", {"backend_type": "htp"}), env={})
    assert base["backend_type"] == "htp"
    assert base["enable_htp_fp16_precision"] == "1"
    assert base["htp_performance_mode"] == "sustained_high_performance"
    # the shorthand wins over the default, the env wins over both
    assert provider_options(("QNNExecutionProvider", {"backend_type": "cpu"}), env={})["backend_type"] == "cpu"
    over = provider_options(("QNNExecutionProvider", {"backend_type": "htp"}),
                            env={"DETECT_ONNX_PROVIDER_OPTIONS": "htp_performance_mode=burst, vtcm_mb=8"})
    assert over["htp_performance_mode"] == "burst" and over["vtcm_mb"] == "8"
    # providers without defaults carry only what they were given
    assert provider_options(("CUDAExecutionProvider", {"device_id": "1"}), env={}) == {"device_id": "1"}
    assert provider_options("CPUExecutionProvider", env={}) == {}


@pytest.fixture(autouse=True)
def _htp_device_present(monkeypatch, tmp_path):
    """The HTP backend now needs the DSP node the host lends the container;
    the plugin tests run as if it were lent (a plain file stands in for
    /dev/fastrpc-cdsp). The one test about its absence points it away."""
    dev = tmp_path / "fastrpc-cdsp"
    dev.write_bytes(b"")
    monkeypatch.setattr("detect_pipeline.onnx_detector.QNN_HTP_DEVICE", str(dev))


def test_ort_backend_runs_on_cpu_and_says_so_without_the_dsp_node(monkeypatch, tmp_path, caplog):
    """The field failure (Ventuno Q, 2026-11-01): a detect-pipeline recreated
    by a plain `docker compose up` had no /dev/fastrpc-cdsp, yet the QNN
    plugin still listed an HTP device, so the session was built, named QNN
    and ran every node on CPU — 18 cameras over capacity. Without the node
    the plugin is not attached: ORT gets the classic CPU list, the backend
    reports CPU against the QNN request, and the warning names the missing
    file and the overlay that lends it."""
    import sys, types
    od = _reset_plugin_registry(monkeypatch)
    calls = {}

    class _Opts:
        def add_provider_for_devices(self, devices, opts):
            raise AssertionError("no DSP node, nothing to attach")

    class _Session(_FakeSession):
        def __init__(self, model_path, sess_options=None, providers="UNSET"):
            super().__init__(_output([]))
            calls["providers"] = providers
        def get_providers(self):
            return ["CPUExecutionProvider"]

    fake_ort = types.SimpleNamespace(
        get_available_providers=lambda: ["CPUExecutionProvider"],
        register_execution_provider_library=lambda name, path: None,
        get_ep_devices=lambda: [types.SimpleNamespace(ep_name="QNNExecutionProvider")],   # the plugin lies
        SessionOptions=_Opts,
        InferenceSession=_Session,
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    monkeypatch.setitem(sys.modules, "onnxruntime_qnn", types.SimpleNamespace(get_library_path=lambda: "/x.so"))
    monkeypatch.setattr("detect_pipeline.onnx_detector.QNN_HTP_DEVICE", str(tmp_path / "absent"))
    monkeypatch.setenv("DETECT_CV_THREADS", "0")
    model = tmp_path / "m.onnx"; model.write_bytes(b"")
    import logging
    with caplog.at_level(logging.WARNING):
        b = OrtBackend(str(model), providers="QNNExecutionProvider:htp")
    assert calls["providers"] == ["CPUExecutionProvider"]
    assert b.requested[0] == ("QNNExecutionProvider", {"backend_type": "htp"})
    assert b.effective == ["CPUExecutionProvider"]
    assert "not in this container" in caplog.text and "accel-qualcomm" in caplog.text
    # QNN's own CPU backend needs no DSP
    assert od.plugin_device_missing("QNNExecutionProvider", ("QNNExecutionProvider", {"backend_type": "cpu"})) is None


def test_ort_backend_attaches_plugin_provider_through_session_options(monkeypatch, tmp_path):
    """A registered plugin EP goes in via SessionOptions.add_provider and the
    session is built WITHOUT a providers list (CPU stays the implicit fallback)."""
    import sys, types
    od = _reset_plugin_registry(monkeypatch)
    calls = {}

    class _Opts:
        def __init__(self):
            self.added = []; self.config = {}
        def add_provider(self, name, opts):          # by-name: refused for plugins
            raise AssertionError("plugin providers must be attached by device")
        def add_provider_for_devices(self, devices, opts):
            self.added.append((devices[0].ep_name, dict(opts)))
        def add_session_config_entry(self, k, v):
            self.config[k] = v

    class _Dev:
        def __init__(self, ep_name):
            self.ep_name = ep_name

    class _Session(_FakeSession):
        def __init__(self, model_path, sess_options=None, providers="UNSET"):
            super().__init__(_output([]))
            calls.setdefault("models", []).append(model_path)
            calls["providers"] = providers
            calls["opts"] = sess_options
            # the runtime dumps the compiled graph where it was told to
            dump = (sess_options.config if sess_options else {}).get("ep.context_file_path")
            if dump and calls.get("compiles", True):
                open(dump, "wb").write(b"ctx")
        def get_providers(self):
            return ["QNNExecutionProvider", "CPUExecutionProvider"]

    fake_ort = types.SimpleNamespace(
        get_available_providers=lambda: ["CPUExecutionProvider"],
        register_execution_provider_library=lambda name, path: calls.setdefault("registered", []).append(name),
        get_ep_devices=lambda: [_Dev("CPUExecutionProvider"), _Dev("QNNExecutionProvider")],
        SessionOptions=_Opts,
        InferenceSession=_Session,
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    monkeypatch.setitem(sys.modules, "onnxruntime_qnn", types.SimpleNamespace(
        get_library_path=lambda: "/x.so", get_qnn_htp_path=lambda: "/x/libQnnHtp.so"))
    monkeypatch.setenv("DETECT_CV_THREADS", "0")
    model = tmp_path / "m.onnx"; model.write_bytes(b"")
    b = OrtBackend(str(model), providers="QNNExecutionProvider:htp")
    assert calls["registered"] == ["QNNExecutionProvider"]
    assert calls["providers"] == "UNSET"                      # not passed alongside the plugin
    assert calls["opts"].added[0][0] == "QNNExecutionProvider"
    # the shorthand became the library path the package knows (backend_type failed on the plugin)
    assert calls["opts"].added[0][1]["backend_path"] == "/x/libQnnHtp.so"
    assert "backend_type" not in calls["opts"].added[0][1]
    assert b.effective[0] == "QNNExecutionProvider"
    # first build compiled from the model and cached the graph next to it
    ctx = tmp_path / ".opennvr-accel" / "m.onnx.QNNExecutionProvider.ctx.onnx"
    assert ctx.exists() and calls["models"] == [str(model)]
    assert calls["opts"].config["ep.context_enable"] == "1"
    # second build loads the cache instead of the model
    OrtBackend(str(model), providers="QNNExecutionProvider:htp")
    assert calls["models"][-1] == str(ctx)


def test_ort_backend_reports_cpu_when_the_plugin_compiled_nothing(monkeypatch, tmp_path, caplog):
    """get_providers() keeps listing a plugin whose device failed; the missing
    compiled-graph dump is what tells the truth, and the metric/log follow it."""
    import logging, sys, types
    od = _reset_plugin_registry(monkeypatch)

    class _Opts:
        def __init__(self):
            self.config = {}
        def add_provider_for_devices(self, devices, opts):
            pass
        def add_session_config_entry(self, k, v):
            self.config[k] = v

    class _Session(_FakeSession):
        def __init__(self, model_path, sess_options=None, providers="UNSET"):
            super().__init__(_output([]))       # dumps nothing: the device never came up
        def get_providers(self):
            return ["QNNExecutionProvider", "CPUExecutionProvider"]

    fake_ort = types.SimpleNamespace(
        get_available_providers=lambda: ["CPUExecutionProvider"],
        register_execution_provider_library=lambda name, path: None,
        get_ep_devices=lambda: [types.SimpleNamespace(ep_name="QNNExecutionProvider")],
        SessionOptions=_Opts, InferenceSession=_Session,
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    monkeypatch.setitem(sys.modules, "onnxruntime_qnn", types.SimpleNamespace(
        get_library_path=lambda: "/x.so", get_qnn_htp_path=lambda: "/x/libQnnHtp.so"))
    monkeypatch.setenv("DETECT_CV_THREADS", "0")
    model = tmp_path / "m.onnx"; model.write_bytes(b"")
    with caplog.at_level(logging.WARNING):
        b = OrtBackend(str(model), providers="QNNExecutionProvider:htp")
    assert b.effective == ["CPUExecutionProvider"]
    assert "could not use QNNExecutionProvider(htp)" in caplog.text
    assert not (tmp_path / ".opennvr-accel" / "m.onnx.QNNExecutionProvider.ctx.onnx").exists()


def test_ort_backend_falls_back_to_the_providers_list_when_the_plugin_has_no_device(monkeypatch, tmp_path, caplog):
    """Registered, but the library found no device (wrong board, no /dev node):
    the session is built the classic way and CPU carries it, said out loud."""
    import sys, types
    od = _reset_plugin_registry(monkeypatch)
    calls = {}

    class _Opts:
        def add_provider_for_devices(self, devices, opts):
            raise AssertionError("no device, nothing to attach")

    class _Session(_FakeSession):
        def __init__(self, model_path, sess_options=None, providers="UNSET"):
            super().__init__(_output([]))
            calls["providers"] = providers
        def get_providers(self):
            return ["CPUExecutionProvider"]

    fake_ort = types.SimpleNamespace(
        get_available_providers=lambda: ["CPUExecutionProvider"],
        register_execution_provider_library=lambda name, path: None,
        get_ep_devices=lambda: [],
        SessionOptions=_Opts,
        InferenceSession=_Session,
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    monkeypatch.setitem(sys.modules, "onnxruntime_qnn", types.SimpleNamespace(get_library_path=lambda: "/x.so"))
    monkeypatch.setenv("DETECT_CV_THREADS", "0")
    model = tmp_path / "m.onnx"; model.write_bytes(b"")
    import logging
    with caplog.at_level(logging.WARNING):
        b = OrtBackend(str(model), providers="QNNExecutionProvider:htp")
    assert calls["providers"] == ["CPUExecutionProvider"]       # the plugin name never reaches ORT's list
    assert b.requested[0] == ("QNNExecutionProvider", {"backend_type": "htp"})
    assert b.effective == ["CPUExecutionProvider"]
    assert "reports no device" in caplog.text and "could not use QNNExecutionProvider(htp)" in caplog.text