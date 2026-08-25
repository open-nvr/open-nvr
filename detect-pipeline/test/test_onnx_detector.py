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
