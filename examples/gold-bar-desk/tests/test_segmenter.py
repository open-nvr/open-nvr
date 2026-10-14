# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The FastSAM output decoder, on hand-built tensors (no model needed)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bar_desk.segmenter import decode, letterbox  # noqa: E402


def outputs(dets, n=50):
    """output0 [1, 37, n] and output1 [1, 32, 160, 160] holding ``dets``:
    (cx, cy, w, h, score), each with a mask that is solid inside its box."""
    out0 = np.zeros((1, 37, n), np.float32)
    protos = np.zeros((1, 32, 160, 160), np.float32)
    protos[0, 0] = 1.0                       # one prototype that is 1 everywhere
    for i, (cx, cy, w, h, score) in enumerate(dets):
        out0[0, :5, i] = (cx, cy, w, h, score)
        out0[0, 5, i] = 8.0                  # coef on that prototype -> sigmoid ~1
    return out0, protos


def test_boxes_come_back_in_corner_form_above_the_threshold():
    out0, out1 = outputs([(100, 200, 40, 80, 0.9), (300, 300, 40, 80, 0.1)])
    boxes, scores, masks = decode(out0, out1, conf=0.25)
    assert len(boxes) == 1
    assert np.allclose(boxes[0], [80, 160, 120, 240])
    assert masks.shape == (1, 160, 160) and masks.min() > 0.99


def test_overlapping_duplicates_are_suppressed():
    out0, out1 = outputs([(100, 200, 40, 80, 0.9), (102, 201, 40, 80, 0.8),
                          (200, 200, 40, 80, 0.7)])
    boxes, scores, _ = decode(out0, out1, conf=0.25, iou=0.7)
    assert len(boxes) == 2
    assert list(np.round(scores, 1)) == [0.9, 0.7]


def test_nothing_above_threshold_is_empty_not_an_error():
    out0, out1 = outputs([(100, 200, 40, 80, 0.1)])
    boxes, scores, masks = decode(out0, out1)
    assert len(boxes) == 0 and masks.shape == (0, 160, 160)


def test_letterbox_keeps_aspect_and_centres():
    crop = np.zeros((600, 300, 3), np.uint8)
    img, r, left, top = letterbox(crop)
    assert img.shape == (640, 640, 3)
    assert r == 640 / 600
    assert top == 0 and left == (640 - 320) // 2
