# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Motion masks blind the motion gate; object masks drop detections that
stand inside them; both arrive per camera from core and a redrawn mask
restarts the worker."""
from __future__ import annotations

import numpy as np

from detect_pipeline.detector import RawDetection
from detect_pipeline.ffmpeg_presets import frame_size_bytes
from detect_pipeline.frame_source import Frame
from detect_pipeline.masks import (
    ObjectMask,
    ObjectMaskFilter,
    parse_motion_masks,
    parse_object_masks,
    parse_polygon,
    rasterize,
)
from detect_pipeline.motion import MotionDetector
from detect_pipeline.pipeline import DetectPipeline
from detect_pipeline.providers import _to_spec
from detect_pipeline.service import CameraSpec, WorkerManager
from detect_pipeline.tracking import Detection, TrackConfig, Tracker

FULL = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
# The right half of the frame.
RIGHT = ((0.5, 0.0), (1.0, 0.0), (1.0, 1.0), (0.5, 1.0))
# A small square in the top-left corner.
CORNER = ((0.0, 0.0), (0.1, 0.0), (0.1, 0.1), (0.0, 0.1))


# ── parsing ──────────────────────────────────────────────────────────

def test_polygons_are_parsed_strictly():
    assert parse_polygon([[0, 0], [1, 0], [0.5, 1]]) == ((0.0, 0.0), (1.0, 0.0), (0.5, 1.0))
    for bad in ([[0, 0], [1, 1]], [[0, 0], [1, 0], [2, 1]], [[0, 0], [1, 0], [True, 1]],
                [[0, 0], [1, 0], [float("nan"), 1]], "nope", None, [[0, 0], [1, 0], [1]]):
        assert parse_polygon(bad) is None, bad


def test_mask_lists_drop_the_malformed_and_keep_the_rest():
    assert parse_motion_masks([list(map(list, RIGHT)), "junk", [[0, 0]]]) == (RIGHT,)
    assert parse_motion_masks(None) == () and parse_motion_masks({"a": 1}) == ()
    masks = parse_object_masks([
        {"polygon": list(map(list, CORNER)), "labels": ["Car", " person ", ""]},
        list(map(list, RIGHT)),                     # a bare polygon = every label
        {"polygon": [[0, 0]]},                      # malformed → dropped
        {"polygon": list(map(list, FULL)), "labels": []},   # [] = every label
    ])
    assert masks == (
        ObjectMask(CORNER, frozenset({"car", "person"})),
        ObjectMask(RIGHT, None),
        ObjectMask(FULL, None),
    )


def test_rasterize_fills_the_polygon_to_the_frame_edge():
    img = rasterize([RIGHT], (10, 20))
    assert img.shape == (10, 20)
    assert img[:, 10:].all() and not img[:, :9].any()
    assert img[9, 19] == 255, "a mask drawn to the edge reaches the last pixel"
    assert not rasterize([], (10, 20)).any()


# ── object masks ─────────────────────────────────────────────────────

def test_object_mask_drops_what_stands_inside_it():
    f = ObjectMaskFilter([ObjectMask(RIGHT, None)], (100, 200))
    inside = Detection("person", (120, 10, 160, 60), 0.9)     # bottom-centre x=140
    outside = Detection("person", (10, 10, 60, 60), 0.9)
    # A box whose BOTTOM is outside but which reaches into the mask: kept.
    leaning = Detection("car", (80, 10, 130, 60), 0.9)          # bottom-centre x=105 → inside
    straddling = Detection("car", (40, 10, 110, 60), 0.9)       # bottom-centre x=75 → outside
    assert f([inside, outside, leaning, straddling]) == [outside, straddling]
    assert f.dropped == 2
    # Clamped: a box hanging off the bottom edge is tested at the last row.
    assert f.covers("dog", (150, 90, 170, 400))


def test_object_mask_label_filter_applies_only_to_those_labels():
    f = ObjectMaskFilter([ObjectMask(RIGHT, frozenset({"car"}))], (100, 200))
    car = Detection("car", (120, 10, 160, 60), 0.9)
    person = Detection("person", (120, 10, 160, 60), 0.9)
    assert f([car, person]) == [person]
    assert f.covers("CAR", (120, 10, 160, 60)), "labels compare case-insensitively"


def test_pipeline_applies_the_object_mask_before_tracking():
    W, H = 320, 240

    class _Motion:
        def detect(self, luma):
            return [(100, 80, 160, 200)]

        def is_calibrating(self):
            return False

    class _OneBox:
        def detect(self, crop):
            return [RawDetection("person", 0.9, (0.25, 0.25, 0.75, 0.75))]

    frame = Frame(bytes(frame_size_bytes(W, H)), W, H, 0, 0.0)
    for mask, expect_dets, expect_masked in (
        (ObjectMask(FULL, None), 0, 1),       # everything stands inside
        (ObjectMask(CORNER, None), 1, 0),     # nothing stands in the corner
        (ObjectMask(FULL, frozenset({"car"})), 1, 0),   # cars only: the person passes
    ):
        tracker = Tracker((H, W), TrackConfig(fps=5, min_initialized=1))
        pipe = DetectPipeline(None, _Motion(), _OneBox(), tracker,
                              object_mask=ObjectMaskFilter([mask], (H, W)))
        result = pipe.process_frame(frame)
        assert len(result.detections) == expect_dets, mask
        assert result.masked_detections == expect_masked, mask
    # No mask configured: nothing counted.
    tracker = Tracker((H, W), TrackConfig(fps=5, min_initialized=1))
    result = DetectPipeline(None, _Motion(), _OneBox(), tracker).process_frame(frame)
    assert len(result.detections) == 1 and result.masked_detections == 0


# ── motion masks ─────────────────────────────────────────────────────

H, W = 200, 200


def _flat(value: int) -> np.ndarray:
    return np.full((H, W), value, np.uint8)


def _warm(md: MotionDetector, frames: int = 60) -> None:
    for _ in range(frames):
        md.detect(_flat(120))


def test_motion_under_the_mask_is_not_motion():
    plain = MotionDetector((H, W))
    masked = MotionDetector((H, W), mask=[RIGHT])
    _warm(plain)
    _warm(masked)
    assert 0.45 < masked.masked_fraction < 0.55
    frame = _flat(120)
    frame[40:80, 140:180] = 255             # a bright patch on the RIGHT: under the mask
    assert plain.detect(frame), "the unmasked detector sees the patch"
    assert masked.detect(frame) == [], "the masked one does not"
    # ...while the same patch on the LEFT is still motion for both.
    frame = _flat(120)
    frame[40:80, 20:60] = 255
    assert masked.detect(frame) and plain.detect(frame)


def test_a_masked_clock_never_blocks_calibration_or_motion_elsewhere():
    """The case the mask exists for: an OSD clock ticking every frame.
    Without the mask the gate sees perpetual motion; with it the scene is
    quiet and a real object elsewhere is still found."""
    clock = ((0.7, 0.0), (1.0, 0.0), (1.0, 0.15), (0.7, 0.15))
    md = MotionDetector((H, W), mask=[clock])
    for i in range(80):
        frame = _flat(120)
        frame[5:25, 150:190] = 255 if i % 2 else 60      # the clock flickers
        boxes = md.detect(frame)
    assert md.is_calibrating() is False
    assert boxes == [], "the flickering clock alone is no motion"
    frame = _flat(120)
    frame[5:25, 150:190] = 255
    frame[100:150, 40:90] = 255                           # and a visitor
    boxes = md.detect(frame)
    assert len(boxes) == 1 and boxes[0][2] <= 120, boxes  # only the visitor, on the left


def test_no_mask_leaves_the_detector_exactly_as_before():
    md = MotionDetector((H, W), mask=())
    assert md.mask_keep is None and md.masked_fraction == 0.0


# ── the spec ─────────────────────────────────────────────────────────

def _cam(**extra):
    return {"camera_id": "cam9", "open_nvr_camera_id": "9", "name": "n",
            "frame_url": "rtsp://m/cam-9?jwt=a", **extra}


def test_core_masks_land_on_the_spec_and_absent_means_none():
    spec = _to_spec(_cam(
        motion_masks=[list(map(list, RIGHT))],
        object_masks=[{"polygon": list(map(list, CORNER)), "labels": ["car"]}],
    ))
    assert spec.motion_masks == (RIGHT,)
    assert spec.object_masks == (ObjectMask(CORNER, frozenset({"car"})),)
    bare = _to_spec(_cam())
    assert bare.motion_masks == () and bare.object_masks == ()


def test_a_redrawn_mask_is_a_restart_but_a_new_token_is_not():
    a = CameraSpec("cam9", "n", "rtsp://m/cam-9?jwt=a", motion_masks=(RIGHT,))
    same_mask_new_jwt = CameraSpec("cam9", "n", "rtsp://m/cam-9?jwt=b", motion_masks=(RIGHT,))
    redrawn = CameraSpec("cam9", "n", "rtsp://m/cam-9?jwt=b", motion_masks=(CORNER,))
    cleared = CameraSpec("cam9", "n", "rtsp://m/cam-9?jwt=b")
    assert WorkerManager._baked(a) == WorkerManager._baked(same_mask_new_jwt)
    assert WorkerManager._baked(a) != WorkerManager._baked(redrawn)
    assert WorkerManager._baked(a) != WorkerManager._baked(cleared)
    hash(a)   # stays hashable: the manager keys on specs
