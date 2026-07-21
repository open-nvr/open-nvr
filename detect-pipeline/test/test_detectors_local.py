# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the local reference detectors."""
from __future__ import annotations

import numpy as np

from detect_pipeline.detectors_local import BrightBlobDetector, HogPersonDetector


def test_hog_returns_nothing_on_blank():
    blank = np.full((320, 320, 3), 128, np.uint8)
    assert HogPersonDetector().detect(blank) == []


def test_blob_detects_bright_square_normalized():
    crop = np.zeros((100, 100, 3), np.uint8)
    crop[20:60, 30:70] = 255                    # bright 40×40 square
    dets = BrightBlobDetector(threshold=200, min_area_frac=0.01).detect(crop)
    assert len(dets) == 1
    d = dets[0]
    assert d.label == "object"
    x1, y1, x2, y2 = d.box                       # normalized 0..1, over the square
    assert 0.2 < x1 < 0.35 and 0.6 < x2 < 0.75
    assert 0.15 < y1 < 0.25 and 0.55 < y2 < 0.65


def test_blob_ignores_tiny_specks():
    crop = np.zeros((100, 100, 3), np.uint8)
    crop[10:12, 10:12] = 255                     # 2×2 speck, below min area
    assert BrightBlobDetector(min_area_frac=0.01).detect(crop) == []
