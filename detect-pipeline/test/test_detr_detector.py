# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RF-DETR decode head: pure-tensor tests — no model file, no onnxruntime."""
from __future__ import annotations

import numpy as np

from detect_pipeline.detr_detector import (
    COCO91_LABELS,
    OnnxDetrDetector,
    labels_for_c,
    postprocess_detr,
    preprocess_detr,
)


def _logits_for(prob: float) -> float:
    """Inverse sigmoid, so tests can speak in probabilities."""
    return float(np.log(prob / (1.0 - prob)))


def test_coco91_table_maps_person_and_gaps():
    assert COCO91_LABELS[1] == "person"        # DETR convention: id 1 = person
    assert COCO91_LABELS[0] is None            # background
    assert COCO91_LABELS[12] is None           # a COCO id gap
    assert COCO91_LABELS[3] == "car"
    assert labels_for_c(80)[0] == "person"     # contiguous table
    assert labels_for_c(90)[0] == "person"     # 91 minus background


def test_postprocess_decodes_cxcywh_and_sigmoid_topk():
    q, c = 5, 91
    dets = np.zeros((1, q, 4), dtype=np.float32)
    logits = np.full((1, q, c), _logits_for(0.01), dtype=np.float32)
    # query 2: a person centered at (0.5, 0.5), 0.2×0.4, prob 0.9
    dets[0, 2] = [0.5, 0.5, 0.2, 0.4]
    logits[0, 2, 1] = _logits_for(0.9)
    out = postprocess_detr(dets, logits, conf_threshold=0.4)
    assert len(out) == 1
    d = out[0]
    assert d.label == "person" and abs(d.score - 0.9) < 1e-3
    x1, y1, x2, y2 = d.box
    assert abs(x1 - 0.4) < 1e-5 and abs(y1 - 0.3) < 1e-5
    assert abs(x2 - 0.6) < 1e-5 and abs(y2 - 0.7) < 1e-5


def test_postprocess_drops_background_and_respects_threshold():
    dets = np.zeros((1, 3, 4), dtype=np.float32)
    dets[0, :] = [0.5, 0.5, 0.5, 0.5]
    logits = np.full((1, 3, 91), _logits_for(0.01), dtype=np.float32)
    logits[0, 0, 0] = _logits_for(0.99)   # background: must never surface
    logits[0, 1, 3] = _logits_for(0.35)   # car below the 0.4 floor
    out = postprocess_detr(dets, logits, conf_threshold=0.4)
    assert out == []


def test_postprocess_clamps_boxes_to_unit_square():
    dets = np.array([[[0.05, 0.5, 0.3, 0.4]]], dtype=np.float32)  # spills left
    logits = np.full((1, 1, 91), _logits_for(0.01), dtype=np.float32)
    logits[0, 0, 1] = _logits_for(0.8)
    (d,) = postprocess_detr(dets, logits, conf_threshold=0.4)
    assert d.box[0] == 0.0 and 0.0 <= min(d.box) and max(d.box) <= 1.0


def test_preprocess_shape_and_imagenet_normalization():
    crop = np.full((100, 120, 3), 255, dtype=np.uint8)   # pure white BGR
    blob = preprocess_detr(crop, 32)
    assert blob.shape == (1, 3, 32, 32)
    # white = 1.0 → (1 - mean)/std per channel (RGB order)
    assert abs(blob[0, 0, 0, 0] - (1 - 0.485) / 0.229) < 1e-4
    assert abs(blob[0, 2, 0, 0] - (1 - 0.406) / 0.225) < 1e-4


class _FakeBackend:
    name = "fake"

    def __init__(self, outputs):
        self._outputs = outputs
        self.blobs = []

    def infer_all(self, blob):
        self.blobs.append(blob)
        return self._outputs


def test_detector_identifies_outputs_by_shape_either_order():
    dets = np.zeros((1, 2, 4), dtype=np.float32)
    dets[0, 0] = [0.5, 0.5, 0.2, 0.2]
    logits = np.full((1, 2, 91), _logits_for(0.01), dtype=np.float32)
    logits[0, 0, 1] = _logits_for(0.9)
    for outputs in ([dets, logits], [logits, dets]):   # order must not matter
        det = OnnxDetrDetector(backend_impl=_FakeBackend(outputs), input_size=32)
        found = det.detect(np.zeros((64, 64, 3), dtype=np.uint8))
        assert [d.label for d in found] == ["person"]


def test_detector_empty_on_missing_outputs():
    det = OnnxDetrDetector(backend_impl=_FakeBackend([]), input_size=32)
    assert det.detect(np.zeros((64, 64, 3), dtype=np.uint8)) == []
