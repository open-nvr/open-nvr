# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for best-frame (thumbnail) selection."""
from __future__ import annotations

from detect_pipeline.thumbnail import (
    Attribute,
    ThumbCandidate,
    is_better_thumbnail,
    on_edge,
)

FRAME = (720, 1280)  # (h, w)


def _cand(label="person", box=(100, 100, 200, 300), score=0.7, attrs=()):
    return ThumbCandidate(label=label, box=box, score=score, attributes=tuple(attrs))


def test_on_edge():
    assert on_edge((0, 10, 100, 100), FRAME) is True
    assert on_edge((10, 10, 1279, 100), FRAME) is True     # right edge (w-1)
    assert on_edge((10, 10, 100, 719), FRAME) is True      # bottom edge (h-1)
    assert on_edge((10, 10, 100, 100), FRAME) is False


def test_higher_score_wins_by_5pct():
    cur = _cand(score=0.70)
    assert is_better_thumbnail(cur, _cand(score=0.76), FRAME) is True
    assert is_better_thumbnail(cur, _cand(score=0.74), FRAME) is False   # only +0.04


def test_larger_area_wins_by_10pct():
    cur = _cand(box=(0 + 10, 10, 110, 210), score=0.7)     # 100×200
    bigger = _cand(box=(10, 10, 130, 250), score=0.7)      # 120×240 = +44%
    assert is_better_thumbnail(cur, bigger, FRAME) is True
    barely = _cand(box=(10, 10, 114, 214), score=0.7)      # ~+4% area
    assert is_better_thumbnail(cur, barely, FRAME) is False


def test_new_edge_clipped_loses_even_with_slightly_higher_score():
    cur = _cand(box=(100, 100, 200, 300), score=0.70)
    new = _cand(box=(0, 100, 200, 300), score=0.73)        # touches left edge, +0.03
    assert is_better_thumbnail(cur, new, FRAME) is False


def test_person_with_better_face_wins_regardless():
    cur = _cand(label="person", score=0.9)
    new = _cand(
        label="person", score=0.5,
        attrs=[Attribute("face", (120, 120, 160, 170))],
    )
    assert is_better_thumbnail(cur, new, FRAME) is True


def test_person_with_existing_face_is_sticky():
    cur = _cand(
        label="person", score=0.5,
        attrs=[Attribute("face", (120, 120, 160, 170))],
    )
    # new is bigger + higher score but has no (better) face → must NOT replace
    new = _cand(label="person", box=(50, 50, 400, 600), score=0.95)
    assert is_better_thumbnail(cur, new, FRAME) is False


def test_car_license_plate_priority():
    cur = _cand(label="car", score=0.9)
    new = _cand(
        label="car", score=0.4,
        attrs=[Attribute("license_plate", (120, 260, 180, 290))],
    )
    assert is_better_thumbnail(cur, new, FRAME) is True


def test_no_improvement_keeps_current():
    cur = _cand(score=0.7)
    assert is_better_thumbnail(cur, _cand(score=0.7), FRAME) is False


# ─── blur veto ──────────────────────────────────────────────────────────

def test_a_much_blurrier_frame_does_not_win_on_score_or_area():
    from detect_pipeline.thumbnail import blur_vetoed
    cur = ThumbCandidate("person", (100, 100, 200, 300), 0.70, sharpness=200.0)
    soft = ThumbCandidate("person", (100, 100, 230, 360), 0.80, sharpness=60.0)   # +0.1 score, +69 % area
    assert blur_vetoed(cur, soft) is True
    assert is_better_thumbnail(cur, soft, FRAME) is False
    assert is_better_thumbnail(cur, soft, FRAME, blur_veto=0.0) is True, "the veto off: wins on score"
    ok = ThumbCandidate("person", (100, 100, 230, 360), 0.80, sharpness=120.0)   # 0.6 of the current
    assert is_better_thumbnail(cur, ok, FRAME) is True
    # unmeasured sharpness (0) on either side never vetoes
    assert is_better_thumbnail(ThumbCandidate("person", (100, 100, 200, 300), 0.7),
                               soft, FRAME) is True


def test_sharpness_is_measured_at_one_scale_so_a_closer_subject_is_not_softer():
    """The same subject twice as large in the frame (upscaled pixels) must
    still measure above the veto fraction of the far one, or walking closer
    would be refused; a truly blurred crop measures far lower."""
    import cv2
    import numpy as np
    from detect_pipeline.thumbnail import BLUR_VETO, crop_sharpness
    small = np.full((80, 40, 3), 60, np.uint8)
    small[10:70, 8:32] = 230; small[36:44, 8:32] = 20; small[10:70, 18:22] = 20   # bright body, dark cross
    frame_small = np.zeros((200, 200, 3), np.uint8); frame_small[10:90, 10:50] = small
    big = cv2.resize(small, (80, 160), interpolation=cv2.INTER_LINEAR)
    frame_big = np.zeros((400, 400, 3), np.uint8); frame_big[10:170, 10:90] = big
    s_small = crop_sharpness(frame_small, (10, 10, 50, 90))
    s_big = crop_sharpness(frame_big, (10, 10, 90, 170))
    assert s_small > 0 and s_big > BLUR_VETO * s_small, (s_small, s_big)
    blurred = cv2.GaussianBlur(big, (0, 0), 4)
    frame_blur = np.zeros((400, 400, 3), np.uint8); frame_blur[10:170, 10:90] = blurred
    assert crop_sharpness(frame_blur, (10, 10, 90, 170)) < 0.1 * s_big
    assert crop_sharpness(frame_big, (10, 10, 12, 12)) == 0.0, "degenerate box"
