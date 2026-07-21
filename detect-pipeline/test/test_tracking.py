# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the lean size-aware tracker."""
from __future__ import annotations

from detect_pipeline.tracking import (
    Detection,
    Track,
    TrackConfig,
    Tracker,
    bottom_center_distance,
)

FRAME = (720, 1280)


def _cfg(**kw):
    base = dict(fps=5, min_initialized=1, max_disappeared=3, stationary_threshold=3)
    base.update(kw)
    return TrackConfig(**base)


def test_distance_zero_for_identical_and_grows_with_separation():
    b = (100, 100, 160, 300)
    assert bottom_center_distance(b, b) == 0.0
    near = (110, 100, 170, 300)
    far = (600, 100, 660, 300)
    assert bottom_center_distance(b, near) < bottom_center_distance(b, far)


def test_new_detection_creates_confirmed_track_when_init_is_one():
    tk = Tracker(FRAME, _cfg(min_initialized=1))
    tracks = tk.update([Detection("person", (100, 100, 160, 300), 0.8)])
    assert len(tracks) == 1
    assert tracks[0].label == "person" and tracks[0].confirmed


def test_initialization_delay_hides_track_until_enough_hits():
    tk = Tracker(FRAME, _cfg(min_initialized=3))
    d = Detection("person", (100, 100, 160, 300), 0.8)
    assert tk.update([d]) == []            # hit 1 -> tentative
    assert tk.update([d]) == []            # hit 2 -> tentative
    tracks = tk.update([d])               # hit 3 -> confirmed
    assert len(tracks) == 1 and tracks[0].hits == 3


def test_same_object_keeps_one_id_across_movement():
    tk = Tracker(FRAME, _cfg(min_initialized=1))
    ids = set()
    box = [100, 100, 160, 300]
    for _ in range(5):
        box[0] += 15; box[2] += 15         # drift right
        tr = tk.update([Detection("person", tuple(box), 0.8)])
        ids.add(tr[0].id)
    assert len(ids) == 1                    # no ID churn


def test_two_nearby_same_class_objects_stay_separate():
    tk = Tracker(FRAME, _cfg(min_initialized=1))
    a = Detection("car", (100, 200, 200, 400), 0.9)   # parked
    b = Detection("car", (260, 200, 360, 400), 0.9)   # passing
    tracks = tk.update([a, b])
    assert len({t.id for t in tracks}) == 2
    # move b closer to a's lane but distinct; ids must persist
    b2 = Detection("car", (230, 200, 330, 400), 0.9)
    tracks = tk.update([a, b2])
    assert len({t.id for t in tracks}) == 2


def test_confirmed_track_survives_gaps_then_ages_out():
    tk = Tracker(FRAME, _cfg(min_initialized=1, max_disappeared=2))
    d = Detection("person", (100, 100, 160, 300), 0.8)
    tk.update([d])
    assert len(tk.update([])) == 1          # miss 1 (survives)
    assert len(tk.update([])) == 1          # miss 2 (survives)
    assert len(tk.update([])) == 0          # miss 3 > max_disappeared -> dropped


def test_motionless_count_and_stationary_flag():
    tk = Tracker(FRAME, _cfg(min_initialized=1, stationary_threshold=3))
    box = (100, 100, 160, 300)
    for _ in range(4):
        tracks = tk.update([Detection("person", box, 0.8)])
    t = tracks[0]
    assert t.motionless_count >= 3
    assert t.stationary is True


def test_motion_resets_motionless_count():
    tk = Tracker(FRAME, _cfg(min_initialized=1, stationary_threshold=3))
    box = [100, 100, 160, 300]
    for _ in range(4):
        tk.update([Detection("person", tuple(box), 0.8)])
    # moderate move: still the same object (matches), but IoU drops below the
    # motionless threshold, so motionless_count resets. (A teleport would
    # correctly be treated as a *different* object by the size-aware metric.)
    moved = (140, 100, 200, 300)          # shifted 40px; IoU ~0.2, distance ~0.67
    tracks = tk.update([Detection("person", moved, 0.8)])
    assert len(tracks) == 1               # same track, not a new one
    assert tracks[0].motionless_count == 0
    assert tracks[0].stationary is False


def test_best_frame_tracked_per_object():
    tk = Tracker(FRAME, _cfg(min_initialized=1))
    tk.update([Detection("person", (100, 100, 160, 300), 0.60)])
    tracks = tk.update([Detection("person", (100, 100, 160, 300), 0.80)])  # +0.20 score
    assert tracks[0].best is not None
    assert tracks[0].best.score == 0.80     # best updated to the higher-scoring frame
