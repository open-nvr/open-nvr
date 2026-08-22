# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Eval harness: spec parsing, IoU matching, and aggregation — pure pieces."""
from __future__ import annotations

import pytest

from detect_pipeline.detector import RawDetection
from detect_pipeline.evalcmp import (
    ModelStats,
    iou,
    match_frame,
    parse_model_spec,
)


def _d(label, box, score=0.9):
    return RawDetection(label, score, box)


def test_parse_model_spec_full_and_default_input():
    s = parse_model_spec("rfdetr=weights/rfdetr.onnx:detr:ort:384")
    assert (s.name, s.path, s.family, s.backend, s.input_size) == (
        "rfdetr", "weights/rfdetr.onnx", "detr", "ort", 384)
    s = parse_model_spec("y8=weights/yolov8n.onnx:yolo:cvdnn")
    assert s.input_size == 640

    for bad in ("noeq", "n=path:yolo", "n=p:family?:ort", "n=p:yolo:tf"):
        with pytest.raises(ValueError):
            parse_model_spec(bad)


def test_iou_basic():
    assert iou((0, 0, 1, 1), (0, 0, 1, 1)) == pytest.approx(1.0)
    assert iou((0, 0, 0.5, 0.5), (0.5, 0.5, 1, 1)) == 0.0
    assert iou((0, 0, 1, 1), (0.5, 0, 1.5, 1)) == pytest.approx(1 / 3, abs=1e-6)


def test_match_frame_same_label_iou_and_counts():
    ref = [_d("person", (0.1, 0.1, 0.3, 0.5)), _d("car", (0.6, 0.6, 0.9, 0.9))]
    cand = [
        _d("person", (0.11, 0.1, 0.31, 0.5)),   # matches person
        _d("car", (0.0, 0.0, 0.1, 0.1)),        # wrong place — no match
        _d("dog", (0.6, 0.6, 0.9, 0.9)),        # right place, wrong label
    ]
    matched, missed, extra = match_frame(ref, cand)
    assert (matched, missed, extra) == (1, 1, 2)


def test_match_frame_greedy_takes_best_pair_once():
    ref = [_d("person", (0.0, 0.0, 0.4, 0.4))]
    cand = [
        _d("person", (0.0, 0.0, 0.4, 0.4)),     # perfect
        _d("person", (0.05, 0.0, 0.45, 0.4)),   # good, but the ref is taken
    ]
    assert match_frame(ref, cand) == (1, 0, 1)


def test_stats_summary_latency_and_agreement():
    st = ModelStats()
    st.note([_d("person", (0, 0, 1, 1))], 10.0)
    st.note([], 30.0)
    st.matched, st.missed, st.extra = 8, 2, 1
    s = st.summary()
    assert s["frames"] == 2 and s["detections"] == 1
    assert s["ms_mean"] == 20.0
    assert s["agreement"] == pytest.approx(0.8)
    assert s["labels"] == {"person": 1}


def test_run_eval_end_to_end_with_stub_detectors(tmp_path):
    """Full harness pass over a tiny synthetic clip with injected detectors:
    sampling, timing, aggregation, and reference matching all wired."""
    import cv2
    import numpy as np
    from detect_pipeline.evalcmp import ModelSpec, run_eval

    clip = str(tmp_path / "clip.avi")
    w = cv2.VideoWriter(clip, cv2.VideoWriter_fourcc(*"MJPG"), 5, (64, 48))
    assert w.isOpened()
    for i in range(10):
        frame = np.full((48, 64, 3), 30 * (i % 3), dtype=np.uint8)
        w.write(frame)
    w.release()

    class _Always:
        def __init__(self, dets):
            self._dets = dets
        def detect(self, bgr):
            return list(self._dets)

    ref_spec = ModelSpec("ref", "-", "yolo", "cvdnn", 640)
    cand_spec = ModelSpec("cand", "-", "detr", "ort", 384)
    person = _d("person", (0.1, 0.1, 0.4, 0.6))
    result = run_eval(
        clip, [cand_spec], reference=ref_spec, every=2, max_frames=100,
        detectors={"ref": _Always([person]), "cand": _Always([person])},
    )
    ref, cand = result["models"]["ref"], result["models"]["cand"]
    assert ref["frames"] == cand["frames"] > 0
    assert cand["agreement"] == 1.0 and cand["missed"] == 0
    assert cand["labels"] == {"person": cand["detections"]}
