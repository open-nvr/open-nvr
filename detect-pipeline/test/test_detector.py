# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the detector-adapter interface + tensor shaping."""
from __future__ import annotations

import numpy as np
import pytest

from detect_pipeline.detector import (
    RawDetection,
    StubDetector,
    crop_and_resize,
    detections_to_frame,
    to_bgr,
)
from detect_pipeline.ffmpeg_presets import frame_size_bytes

W, H = 64, 48


def test_to_bgr_shape():
    data = bytes(frame_size_bytes(W, H))          # a valid-sized I420 buffer
    bgr = to_bgr(data, W, H)
    assert bgr.shape == (H, W, 3)


def test_crop_and_resize_to_model_input():
    bgr = np.zeros((H, W, 3), np.uint8)
    out = crop_and_resize(bgr, (10, 5, 40, 35), out_w=320, out_h=320)
    assert out.shape == (320, 320, 3)


def test_crop_rejects_empty_region():
    bgr = np.zeros((H, W, 3), np.uint8)
    with pytest.raises(ValueError):
        crop_and_resize(bgr, (10, 10, 10, 10), 320, 320)


def test_detections_map_from_crop_to_full_frame():
    region = (100, 200, 200, 300)                 # 100×100 region at (100,200)
    raws = [RawDetection("person", 0.9, (0.5, 0.5, 1.0, 1.0))]
    dets = detections_to_frame(raws, region)
    assert len(dets) == 1
    d = dets[0]
    assert d.label == "person" and d.score == 0.9
    # (0.5,0.5)-(1.0,1.0) of a 100px region at offset (100,200)
    assert d.box == (150, 250, 200, 300)


def test_stub_detector_returns_nothing():
    assert StubDetector().detect(np.zeros((320, 320, 3), np.uint8)) == []
