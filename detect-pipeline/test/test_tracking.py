# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the lean size-aware tracker."""
from __future__ import annotations

from pathlib import Path

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


def test_crop_margin_is_a_tracker_setting_applied_to_both_crops():
    """DETECT_CROP_MARGIN: a wider margin keeps the whole car in the read
    frame when the detector's box trails a moving one (the LPR dialog
    showed an Audi with its left side cut off). One setting, both crops
    — the visit's evidence and the plate candidates come from the same
    helper, so they cannot drift apart."""
    import numpy as np
    from detect_pipeline.tracking import _crop_bgr

    frame = np.zeros((1080, 1920, 3), np.uint8)
    crop = _crop_bgr(frame, (800, 400, 1000, 700), margin=0.5)
    h, w = crop.shape[:2]
    assert (w, h) == (200 + 2 * 100, 300 + 2 * 150), f"got {w}x{h}"
    src = (Path(__file__).resolve().parents[1] / "detect_pipeline"
           / "tracking.py").read_text()
    assert src.count("margin=self.crop_margin") == 2, (
        "both _crop_bgr call sites must use the tracker's crop_margin")


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
    tk.update([Detection("person", (105, 105, 200, 450), 0.6)], _frame())
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


# ─── matched_now: detected this frame vs coasting ───────────────────────
#
# Reported against the live overlay: phantom boxes piling up on a moving
# scene while the real vehicle went unboxed. The tracker keeps an
# unmatched track alive at its last box (coasting) — correct for visit
# continuity, wrong to DRAW. These pin the per-update flag the bus ships
# so a renderer can tell the two apart.


def test_spawn_and_match_set_matched_now():
    tk = Tracker(FRAME, _cfg(min_initialized=1))
    (tr,) = tk.update([Detection("car", (100, 100, 200, 200), 0.9)])
    assert tr.matched_now is True                      # spawned from a detection
    (tr,) = tk.update([Detection("car", (102, 101, 202, 201), 0.9)])
    assert tr.matched_now is True                      # matched again


def test_a_coasting_track_is_not_matched_now():
    tk = Tracker(FRAME, _cfg(min_initialized=1))
    tk.update([Detection("car", (100, 100, 200, 200), 0.9)])
    (tr,) = tk.update([])                              # confirmed track survives a miss
    assert tr.matched_now is False
    assert tr.misses == 1


def test_unscanned_coasting_is_not_matched_now_even_with_zero_misses():
    """THE case that rules out `misses == 0` as the signal: a track whose
    region was skipped this frame coasts WITHOUT counting a miss, so on
    misses alone it looks identical to one detected this frame."""
    tk = Tracker(FRAME, _cfg(min_initialized=1))
    tk.update([Detection("car", (100, 100, 200, 200), 0.9)])
    # Scanned a region nowhere near the track → the tracker coasts it.
    (tr,) = tk.update([], scanned_regions=[(1000, 1000, 1100, 1100)])
    assert tr.misses == 0
    assert tr.matched_now is False


def test_matched_now_resets_every_update():
    tk = Tracker(FRAME, _cfg(min_initialized=1))
    tk.update([Detection("car", (100, 100, 200, 200), 0.9)])
    tk.update([])
    (tr,) = tk.update([Detection("car", (101, 101, 201, 201), 0.9)])
    assert tr.matched_now is True                      # back once re-detected


def test_since_match_s_is_zero_on_match_and_grows_while_coasting():
    t = [100.0]
    tk = Tracker(FRAME, _cfg(min_initialized=1), clock=lambda: t[0])
    (tr,) = tk.update([Detection("car", (100, 100, 200, 200), 0.9)])
    assert tr.since_match_s == 0.0
    t[0] = 101.5
    (tr,) = tk.update([], scanned_regions=[(1000, 1000, 1100, 1100)])   # coast, unscanned
    assert tr.since_match_s == 1.5 and tr.misses == 0
    t[0] = 102.0
    (tr,) = tk.update([Detection("car", (101, 101, 201, 201), 0.9)])
    assert tr.since_match_s == 0.0


# ── confusable labels (car/truck/bus) share one track ───────────────

_FH, _FW = 720, 1280


def test_confusable_labels_feed_one_track_and_vote_the_label():
    """car 0.72 / truck 0.70 one frame, truck 0.73 / car 0.71 the next: with
    cross-label NMS only one survives per frame, and it must keep matching
    the SAME track rather than starving two."""
    tk = Tracker((_FH, _FW), TrackConfig(fps=5, min_initialized=1))
    box = (100, 100, 400, 300)
    (tr,) = tk.update([Detection("car", box, 0.72)])
    assert tr.label == "car"
    (tr2,) = tk.update([Detection("truck", box, 0.73)])
    assert tr2.id == tr.id and tr2.label == "car"      # one vote each: keeps its own
    (tr3,) = tk.update([Detection("truck", box, 0.73)])
    assert tr3.id == tr.id and tr3.label == "truck"    # majority now says truck
    assert tk.population == 1


def test_unrelated_labels_still_never_match():
    tk = Tracker((_FH, _FW), TrackConfig(fps=5, min_initialized=1))
    box = (100, 100, 400, 300)
    tk.update([Detection("car", box, 0.9)])
    tk.update([Detection("person", box, 0.9)])
    assert tk.population == 2


def test_a_part_box_inside_a_tracked_person_spawns_no_second_track():
    """Camera 23, 2026-11-05: on frames where only the upper body was
    detected, that box sat far from the full-body track's bottom-centre,
    spawned a track of its own and posted a second visit per pass."""
    tk = Tracker((576, 704), TrackConfig(fps=5, min_initialized=2))
    body = (300, 100, 420, 420)
    tk.update([Detection("person", body, 0.8)], frame_ts=1.0)
    tk.update([Detection("person", body, 0.8)], frame_ts=1.2)
    assert len(tk._tracks) == 1 and tk._tracks[0].confirmed
    # the whole is missed, the upper body is boxed: absorbed, not spawned
    tk.update([Detection("person", (308, 102, 380, 170), 0.9)], frame_ts=1.4)   # head and shoulders
    assert len(tk._tracks) == 1 and tk.spawns_absorbed == 1
    # a torso box 75 % inside the tracked person (arms out, wider than the
    # body) beside the whole: one track, whichever way it got there
    tk.update([Detection("person", body, 0.8), Detection("person", (304, 100, 422, 300), 0.9)], frame_ts=1.5)
    assert len(tk._tracks) == 1
    # a person beside the first, half outside the box, is a new track
    tk.update([Detection("person", body, 0.8), Detection("person", (390, 110, 520, 430), 0.8)], frame_ts=1.6)
    assert len(tk._tracks) == 2
    # the guard can be switched off
    tk.spawn_containment = 0.0
    tk.update([Detection("person", body, 0.8), Detection("person", (390, 110, 520, 430), 0.8),
               Detection("person", (308, 102, 380, 170), 0.9)], frame_ts=1.8)
    assert len(tk._tracks) == 3


def test_a_coasting_track_is_matched_with_a_wider_gate():
    """Behind a colleague for three frames, then a stride away: the same
    track, not a second visit."""
    tk = Tracker((576, 704), TrackConfig(fps=5, min_initialized=2))
    box = (300, 100, 380, 400)
    for ts in (1.0, 1.2):
        tk.update([Detection("person", box, 0.8)], frame_ts=ts)
    tid = tk._tracks[0].id
    for ts in (1.4, 1.6, 1.8):
        tk.update([], frame_ts=ts)                       # occluded: three misses
    far = (560, 100, 640, 400)                           # 3.25 widths away: beyond the fixed gate
    assert bottom_center_distance(box, far) > TrackConfig(fps=5, min_initialized=2).threshold_for("person")
    tk.update([Detection("person", far, 0.8)], frame_ts=2.0)
    assert len(tk._tracks) == 1 and tk._tracks[0].id == tid and tk._tracks[0].box == far
    # with growth off the same detection starts a new track
    tk2 = Tracker((576, 704), TrackConfig(fps=5, min_initialized=2, coast_gate_growth=0.0))
    for ts in (1.0, 1.2):
        tk2.update([Detection("person", box, 0.8)], frame_ts=ts)
    for ts in (1.4, 1.6, 1.8):
        tk2.update([], frame_ts=ts)
    tk2.update([Detection("person", far, 0.8)], frame_ts=2.0)
    assert len(tk2._tracks) == 2


def test_a_box_around_two_tracked_people_is_a_group_box():
    tk = Tracker((576, 704), TrackConfig(fps=5, min_initialized=2))
    a, b = (100, 100, 200, 400), (260, 100, 360, 400)
    for ts in (1.0, 1.2):
        tk.update([Detection("person", a, 0.8), Detection("person", b, 0.8)], frame_ts=ts)
    assert len(tk._tracks) == 2
    # the detector adds one box around both: absorbed, not a third track
    tk.update([Detection("person", a, 0.8), Detection("person", b, 0.8), Detection("person", (90, 90, 370, 410), 0.7)], frame_ts=1.4)
    assert len(tk._tracks) == 2 and tk.spawns_absorbed == 1
    # a group box that did get a track (its members were missed when it
    # spawned) is marked on every frame the members are inside it
    tk3 = Tracker((576, 704), TrackConfig(fps=5, min_initialized=2))
    g = (90, 90, 370, 410)
    tk3.update([Detection("person", g, 0.7)], frame_ts=1.0)
    tk3.update([Detection("person", g, 0.7)], frame_ts=1.2)
    # (overlap suppression drops the group box on frames where its members
    # are boxed, so the tracker sees the members alone; the group track
    # coasts, and its box holds both)
    tk3.update([Detection("person", a, 0.8), Detection("person", b, 0.8)], frame_ts=1.4)
    tk3.update([Detection("person", a, 0.8), Detection("person", b, 0.8)], frame_ts=1.6)
    grp = next(t for t in tk3._tracks if t.box == g)
    assert grp.group_now and grp.group_frames >= 1 and len(tk3._tracks) == 3
    assert grp.box == g, "the group track did not steal a member's box (size gate)"


def test_a_box_of_a_very_different_size_never_matches():
    """A person's track must not take the box the detector drew around a
    group (three times as wide), nor a group track a member's box."""
    tk = Tracker((576, 704), TrackConfig(fps=5, min_initialized=2))
    person = (300, 100, 380, 400)
    for ts in (1.0, 1.2):
        tk.update([Detection("person", person, 0.8)], frame_ts=ts)
    group = (200, 90, 560, 410)
    tk.update([Detection("person", group, 0.7)], frame_ts=1.4)
    assert len(tk._tracks) == 2 and tk._tracks[0].box == person, "coasting, not switched to the group box"
    tk2 = Tracker((576, 704), TrackConfig(fps=5, min_initialized=2, width_grow_max=0.0, width_shrink_max=0.0))
    for ts in (1.0, 1.2):
        tk2.update([Detection("person", person, 0.8)], frame_ts=ts)
    tk2.update([Detection("person", group, 0.7)], frame_ts=1.4)
    assert len(tk2._tracks) == 1 and tk2._tracks[0].box == group, "the old behaviour with the gate off"
    # half hidden behind a chair: a box half as wide is still this person
    tk3 = Tracker((576, 704), TrackConfig(fps=5, min_initialized=2))
    for ts in (1.0, 1.2):
        tk3.update([Detection("person", person, 0.8)], frame_ts=ts)
    tk3.update([Detection("person", (340, 100, 380, 400), 0.7)], frame_ts=1.4)
    assert len(tk3._tracks) == 1 and tk3._tracks[0].box == (340, 100, 380, 400)


# ── Motion-predicted, two-stage assignment (walk 13, 2026-11-05) ─────

def test_the_motion_filter_predicts_along_the_velocity_and_keeps_the_size():
    from detect_pipeline.tracking import BoxMotion
    kf = BoxMotion((100, 100, 200, 400))
    for x in (120, 140, 160, 180):                      # 20 px a frame to the right
        kf.predict()
        kf.update((x, 100, x + 100, 400))
    p = kf.predict()
    assert 190 < p[0] < 210 and 290 < p[2] < 310, p    # ahead of the last box
    assert abs((p[2] - p[0]) - 100) < 5 and abs((p[3] - p[1]) - 300) < 5
    # the size carries no velocity: two narrow frames do not predict a narrower one
    kf.update((p[0], 100, p[0] + 60, 400))
    kf.predict()
    kf.update((p[0] + 20, 100, p[0] + 60, 400))
    q = kf.predict()
    assert q[2] - q[0] > 45, "width predicted from measurements, never extrapolated"


def test_assignment_is_global_not_greedy():
    import numpy as np
    from detect_pipeline.tracking import assign
    # greedy takes (0,0) at 0.1 and leaves row 1 with 0.9; optimal is (0,1)+(1,0) = 0.35
    cost = np.array([[0.1, 0.2], [0.15, 0.9]])
    assert sorted(assign(cost, 1.0)) == [(0, 1), (1, 0)]
    # rectangular, and pairs at or above max_cost are not returned
    cost = np.array([[0.1, 1.0, 1.0]])
    assert assign(cost, 1.0) == [(0, 0)]
    assert assign(np.ones((2, 3)), 1.0) == []
    assert assign(np.zeros((0, 3)), 1.0) == []


def test_crossing_people_keep_their_ids_by_motion():
    """Two people walking towards each other and crossing: with a motion
    prediction each keeps the box on their own side of the cross."""
    tk = Tracker((576, 704), TrackConfig(fps=5, min_initialized=2))
    a = lambda i: (100 + 40 * i, 100, 180 + 40 * i, 400)      # left to right
    b = lambda i: (500 - 40 * i, 100, 580 - 40 * i, 400)      # right to left
    for i in range(10):
        tracks = tk.update([Detection("person", a(i), 0.9), Detection("person", b(i), 0.9)], frame_ts=i / 5)
    assert len(tracks) == 2
    by_id = {t.id: t.box for t in tracks}
    assert by_id[1] == a(9) and by_id[2] == b(9)


def test_a_low_score_box_keeps_a_track_alive_but_never_spawns_one():
    tk = Tracker((576, 704), TrackConfig(fps=5, min_initialized=2, high_score=0.5))
    box = (300, 100, 380, 400)
    tk.update([Detection("person", box, 0.8)], frame_ts=1.0)
    tk.update([Detection("person", box, 0.8)], frame_ts=1.2)
    tid = tk._tracks[0].id
    tk.update([Detection("person", (305, 100, 385, 400), 0.3)], frame_ts=1.4)   # unsure box, same place
    assert len(tk._tracks) == 1 and tk._tracks[0].id == tid and tk._tracks[0].matched_now
    assert tk.low_matches == 1
    tk.update([Detection("person", (600, 100, 680, 400), 0.3)], frame_ts=1.6)   # unsure box elsewhere: no track
    assert len(tk._tracks) == 1 and not tk._tracks[0].matched_now
    assert tk.spawns_dropped == 1
    # a coasting track still takes an unsure box where it last was (the
    # distance fallback): a far, small person is unsure most frames
    tk.update([], frame_ts=1.8)
    tk.update([Detection("person", box, 0.3)], frame_ts=2.0)
    assert tk._tracks[0].matched_now and len(tk._tracks) == 1


def test_a_person_who_stops_short_keeps_the_track():
    """Walking fast to a wall and stopping: the prediction runs past
    them, the last box does not."""
    tk = Tracker((576, 704), TrackConfig(fps=5, min_initialized=2))
    for i in range(6):
        tk.update([Detection("person", (100 + 60 * i, 100, 180 + 60 * i, 400), 0.9)], frame_ts=i / 5)
    tid = tk._tracks[0].id
    last = (400, 100, 480, 400)
    tk.update([Detection("person", last, 0.9)], frame_ts=1.2)              # stopped
    tk.update([Detection("person", last, 0.9)], frame_ts=1.4)
    assert len(tk._tracks) == 1 and tk._tracks[0].id == tid


def test_a_narrow_moment_does_not_block_the_full_box_after_it():
    """Half behind a colleague for a few frames (a narrow box), then the
    whole person again: the width gate measures against the recent
    widest box, so the full box is still this track."""
    tk = Tracker((576, 704), TrackConfig(fps=5, min_initialized=2))
    full = (300, 100, 400, 400)
    for ts in (1.0, 1.2, 1.4):
        tk.update([Detection("person", full, 0.9)], frame_ts=ts)
    tid = tk._tracks[0].id
    for ts in (1.6, 1.8):
        tk.update([Detection("person", (300, 100, 350, 400), 0.9)], frame_ts=ts)   # half as wide
    assert len(tk._tracks) == 1 and tk._tracks[0].box[2] == 350
    tk.update([Detection("person", full, 0.9)], frame_ts=2.0)
    assert len(tk._tracks) == 1 and tk._tracks[0].id == tid and tk._tracks[0].box == full


def test_a_torso_box_never_matches_the_person_it_belongs_to():
    tk = Tracker((576, 704), TrackConfig(fps=5, min_initialized=2))
    body = (300, 100, 400, 400)
    for ts in (1.0, 1.2):
        tk.update([Detection("person", body, 0.9)], frame_ts=ts)
    tk.update([Detection("person", (300, 100, 400, 220), 0.9)], frame_ts=1.4)   # 120 of 300 high
    tr = tk._tracks[0]
    assert tr.box == body and not tr.matched_now and len(tk._tracks) == 1, "a part: absorbed, not matched"


def test_motion_off_restores_the_distance_matcher():
    tk = Tracker((576, 704), TrackConfig(fps=5, min_initialized=1, motion=False))
    tk.update([Detection("person", (100, 100, 180, 400), 0.9)], frame_ts=1.0)
    tk.update([Detection("person", (120, 100, 200, 400), 0.9)], frame_ts=1.2)
    assert len(tk._tracks) == 1 and tk._tracks[0].kf is None and tk._tracks[0].matched_now
