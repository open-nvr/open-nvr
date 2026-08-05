# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for region geometry (ported from Frigate util/image.py)."""
from __future__ import annotations

from detect_pipeline.regions import (
    area,
    calculate_region,
    clipped,
    get_min_region_size,
    intersection,
    intersection_over_union,
)

FRAME = (720, 1280)  # (height, width)


def test_min_region_size_floor_320_for_large_models():
    assert get_min_region_size(640, 640) == 320
    assert get_min_region_size(320, 320) == 320


def test_min_region_size_small_models_aligned_to_4():
    assert get_min_region_size(300, 300) == 300      # already /4
    assert get_min_region_size(150, 150) == 152      # 150 -> next /4
    assert get_min_region_size(96, 96) == 96


def test_calculate_region_is_square_aligned_and_min_model_size():
    r = calculate_region(FRAME, 100, 100, 140, 160, model_size=320)
    x1, y1, x2, y2 = r
    assert (x2 - x1) == (y2 - y1)          # square
    assert (x2 - x1) == 320                # box is small -> clamps to model size
    assert (x2 - x1) % 4 == 0


def test_calculate_region_scales_by_multiplier_for_large_box():
    # 400px-tall box × 1.35 = 540 -> //4*4 = 540
    r = calculate_region(FRAME, 100, 100, 300, 500, model_size=320, multiplier=1.35)
    assert (r[2] - r[0]) == 540
    assert (r[2] - r[0]) % 4 == 0


def test_calculate_region_stays_inside_frame():
    # box near the corner must not produce a region past the frame edge
    r = calculate_region(FRAME, 1250, 700, 1279, 719, model_size=320)
    assert r[0] >= 0 and r[1] >= 0
    assert r[2] <= FRAME[1] and r[3] <= FRAME[0]


def test_intersection_and_iou():
    a = (0, 0, 10, 10)
    b = (5, 5, 15, 15)
    assert intersection(a, b) == (5, 5, 10, 10)
    assert intersection((0, 0, 4, 4), (10, 10, 20, 20)) is None
    assert intersection_over_union(a, a) == 1.0
    assert intersection_over_union((0, 0, 4, 4), (100, 100, 104, 104)) == 0.0
    partial = intersection_over_union(a, b)
    assert 0.0 < partial < 1.0


def test_area_is_inclusive():
    assert area((0, 0, 9, 9)) == 100        # 10×10 inclusive


def test_clipped_true_near_region_border_not_frame_edge():
    region = (100, 100, 420, 420)           # not touching any frame edge
    box_at_border = (103, 200, 300, 300)    # left side within 5px of region left
    assert clipped(box_at_border, region, FRAME) is True
    box_centered = (200, 200, 300, 300)
    assert clipped(box_centered, region, FRAME) is False


def test_clipped_false_when_region_on_frame_edge():
    region = (0, 0, 320, 320)               # region hugs the top-left frame edge
    box = (1, 1, 100, 100)                  # near the region edge, but that's the frame edge
    assert clipped(box, region, FRAME) is False
