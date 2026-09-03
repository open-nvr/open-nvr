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


# ── evidence crops carry context ───────────────────────────────────────


def test_best_crop_includes_context_around_the_box():
    """The crop becomes the visit's stored evidence — the photo the operator
    sees for "who came today?". The bare detection box is the least legible
    framing of it: field case, a person leaned over a desk camera and the
    evidence was a 163x187 crop of their knuckles with zero surroundings."""
    import numpy as np
    from detect_pipeline.tracking import _crop_bgr

    frame = np.zeros((1080, 1920, 3), np.uint8)
    crop = _crop_bgr(frame, (800, 400, 1000, 700))       # 200x300 box mid-frame
    h, w = crop.shape[:2]
    assert w > 200 and h > 300, f"no context added: got {w}x{h}"
    assert w == 200 + 2 * 50 and h == 300 + 2 * 75, (
        f"expected a quarter-box margin per side, got {w}x{h}")


def test_best_crop_margin_clamps_at_the_frame_edge():
    """A box in a corner cannot borrow context that does not exist — the crop
    clamps to the frame instead of failing or wrapping."""
    import numpy as np
    from detect_pipeline.tracking import _crop_bgr

    frame = np.zeros((480, 640, 3), np.uint8)
    crop = _crop_bgr(frame, (0, 0, 100, 100))
    h, w = crop.shape[:2]
    assert (w, h) == (125, 125), f"corner crop should clamp to 125x125, got {w}x{h}"

    crop = _crop_bgr(frame, (540, 380, 640, 480))        # bottom-right corner
    h, w = crop.shape[:2]
    assert (w, h) == (125, 125), f"corner crop should clamp, got {w}x{h}"


def test_best_crop_of_a_full_frame_box_is_the_frame():
    """margin cannot push past the frame however large the box."""
    import numpy as np
    from detect_pipeline.tracking import _crop_bgr

    frame = np.zeros((480, 640, 3), np.uint8)
    crop = _crop_bgr(frame, (0, 0, 640, 480))
    assert crop.shape[:2] == (480, 640)


# ── scene evidence ──────────────────────────────────────────────────
#
# The scene is the WHOLE frame behind best_crop, encoded eagerly at
# best-frame update time. "Eagerly" is the entire design: retaining the
# BGR pixels instead would be 6.2 MB x DETECT_MAX_TRACKS per camera, so
# these tests pin the memory and CPU invariants, not just the happy path.


def _frame(h=480, w=640):
    import numpy as np
    return np.zeros((h, w, 3), np.uint8)


def _scene_tracker(monkeypatch, encode=None, **kw):
    """Tracker with scene retention on and a counting fake encoder."""
    import detect_pipeline.bestframe as bf
    calls = {"n": 0}

    def enc(bgr, *, max_px=1280, quality=78):
        calls["n"] += 1
        if encode is not None:
            return encode(bgr)
        return b"SCENEJPEG"

    monkeypatch.setattr(bf, "encode_scene_jpeg", enc)
    tk = Tracker(FRAME, _cfg(**kw))
    tk.retain_scene = True
    return tk, calls


def test_scene_is_encoded_once_per_frame_however_many_tracks_peak(monkeypatch):
    """Fifty tracks peaking on one frame must cost ONE imencode, not fifty —
    and must SHARE the bytes, not hold fifty copies of the same image."""
    tk, calls = _scene_tracker(monkeypatch)
    dets = [
        Detection("person", (x, 100, x + 60, 300), 0.8)
        for x in (100, 220, 340, 460, 580)
    ]
    tracks = tk.update(dets, _frame())
    assert len(tracks) == 5
    assert calls["n"] == 1, f"encoded {calls['n']} times for one frame"
    first = tracks[0].best_scene_jpeg
    assert first is not None
    assert all(t.best_scene_jpeg is first for t in tracks), "bytes not shared"


def test_scene_frame_is_not_retained_between_updates(monkeypatch):
    """The memo holds a reference to a 6 MB array; if update() leaked it, a
    24/7 camera would keep one frame alive forever for nothing."""
    tk, _calls = _scene_tracker(monkeypatch)
    tk.update([Detection("person", (100, 100, 160, 300), 0.8)], _frame())
    assert tk._scene_bgr is None
    assert tk._scene_jpeg is None and tk._scene_done is False


def test_scene_only_moves_when_the_best_crop_does(monkeypatch):
    """The two images must describe the SAME frame. A later frame that loses
    is_better_thumbnail updates neither — and costs no encode."""
    tk, calls = _scene_tracker(monkeypatch)
    big = Detection("person", (100, 100, 300, 600), 0.9)
    tk.update([big], _frame())
    assert calls["n"] == 1
    kept = tk.tracks[0].best_scene_jpeg
    # Same track, smaller + lower score: not a better thumbnail.
    tk.update([Detection("person", (105, 105, 200, 400), 0.6)], _frame())
    assert tk.tracks[0].best_scene_jpeg is kept
    assert calls["n"] == 1, "encoded for a frame that was not the best"


def test_scene_retention_is_off_by_default():
    """Every existing caller and test pays one boolean, never an encode."""
    tk = Tracker(FRAME, _cfg())
    tk.update([Detection("person", (100, 100, 160, 300), 0.8)], _frame())
    tr = tk.tracks[0]
    assert tr.best_crop is not None                 # crop still retained
    assert tr.best_scene_jpeg is None


def test_a_failed_scene_encode_costs_one_attempt_and_never_the_crop(monkeypatch):
    """A bad frame must not cost the visit its evidence photo, and must not
    be retried once per track — the failure itself is memoised."""
    def boom(_bgr):
        raise RuntimeError("cv2 said no")

    tk, calls = _scene_tracker(monkeypatch, encode=boom)
    dets = [
        Detection("person", (x, 100, x + 60, 300), 0.8)
        for x in (100, 220, 340)
    ]
    tracks = tk.update(dets, _frame())
    assert all(t.best_crop is not None for t in tracks)
    assert all(t.best_scene_jpeg is None for t in tracks)
    assert calls["n"] == 1, "a failed encode was retried per track"


def test_plate_candidates_stay_box_sized_with_scene_retention_on(monkeypatch):
    """The scene must never reach the OCR ring: a whole-frame candidate is a
    plate a few pixels tall, which would quietly wreck LPR recall."""
    tk, _calls = _scene_tracker(monkeypatch)
    tk.retain_plate_candidates = True
    frame = _frame(1080, 1920)
    tk.update([Detection("car", (800, 400, 1000, 700), 0.9)], frame)
    ring = tk.tracks[0].plate_ring
    assert ring is not None
    for cand in ring.ranked():
        assert cand.crop.shape[:2] != frame.shape[:2], "candidate is the whole frame"
