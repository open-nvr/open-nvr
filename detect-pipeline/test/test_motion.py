# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the ported motion detector."""
from __future__ import annotations

import numpy as np

from detect_pipeline.motion import MotionConfig, MotionDetector

H, W = 200, 200


def _flat(value: int) -> np.ndarray:
    return np.full((H, W), value, np.uint8)


def _warm_up(md: MotionDetector, value: int = 120, frames: int = 60) -> None:
    """Feed a static scene until the detector calibrates."""
    for _ in range(frames):
        md.detect(_flat(value))


def test_disabled_returns_empty():
    md = MotionDetector((H, W), MotionConfig(enabled=False))
    assert md.detect(_flat(255)) == []


def test_static_scene_calibrates_and_goes_quiet():
    md = MotionDetector((H, W))
    _warm_up(md)
    assert md.is_calibrating() is False
    assert md.detect(_flat(120)) == []      # no motion on a settled static scene


def test_moving_object_produces_a_box_in_region():
    md = MotionDetector((H, W))
    _warm_up(md)
    frame = _flat(120)
    frame[40:80, 140:180] = 255             # bright patch, bottom-right-ish
    boxes = md.detect(frame)
    assert boxes, "a bright patch on a calibrated scene should produce motion"
    x1, y1, x2, y2 = boxes[0]
    # coords are in full-frame space and within bounds
    assert 0 <= x1 < x2 <= W and 0 <= y1 < y2 <= H
    # roughly localized to the patch region (right/lower half), not the whole frame
    assert x2 > W // 2 and y2 > H // 4
    assert (x2 - x1) < W and (y2 - y1) < H


def test_whole_frame_flash_sets_calibrating():
    md = MotionDetector((H, W))
    _warm_up(md)
    assert md.is_calibrating() is False
    md.detect(_flat(255))                   # dawn/IR-cut style full-frame change
    assert md.is_calibrating() is True      # pipeline will stop sending regions


def test_skip_motion_threshold_drops_frame():
    md = MotionDetector((H, W), MotionConfig(skip_motion_threshold=0.5))
    _warm_up(md)
    boxes = md.detect(_flat(255))           # ~100% change > 0.5
    assert boxes == []
    assert md.is_calibrating() is True


def test_boxes_scale_to_full_frame_when_downscaled():
    # frame_height 100 on a 200-tall input → resize_factor 2 → boxes scaled up.
    md = MotionDetector((H, W), MotionConfig(frame_height=100))
    assert md.resize_factor == 2.0
    _warm_up(md)
    frame = _flat(120)
    frame[100:160, 100:160] = 255
    boxes = md.detect(frame)
    assert boxes
    x1, y1, x2, y2 = boxes[0]
    assert x2 <= W and y2 <= H              # scaled coords stay within the full frame
