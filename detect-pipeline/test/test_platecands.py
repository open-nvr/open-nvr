# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Multi-frame OCR, Tier-0 half: candidate retention + early attempts.

The failure this round exists for: one OCR lottery ticket per car (the
vehicle-best frame, which is usually the plate-WORST frame) triggered
only when the track died — which on a busy road can take minutes. These
tests pin the two halves: several diverse candidates ride each vehicle
track, and the first attempt fires the moment the track confirms.
"""
from __future__ import annotations

import numpy as np

from detect_pipeline.platecands import (
    CandidateRing,
    EarlyAttemptPolicy,
    candidate_score,
    sharpness,
)


# ── scoring ────────────────────────────────────────────────────────


def test_sharpness_prefers_crisp_over_blurred():
    import cv2

    rng = np.random.default_rng(7)
    crisp = rng.integers(0, 255, (60, 80, 3), dtype=np.uint8)
    blurred = cv2.GaussianBlur(crisp, (9, 9), 5)
    assert sharpness(crisp) > sharpness(blurred) * 2


def test_sharpness_never_raises_on_junk():
    assert sharpness(None) == 0.0
    assert sharpness(np.zeros((0, 0, 3), np.uint8)) == 0.0


def test_score_scales_with_area_and_sharpness():
    crisp = np.random.default_rng(1).integers(0, 255, (40, 40), dtype=np.uint8)
    assert candidate_score(2000, crisp) > candidate_score(1000, crisp)


# ── the candidate ring ─────────────────────────────────────────────


def _crop():
    return np.zeros((8, 8, 3), np.uint8)


def test_ring_keeps_top_k_and_replaces_the_worst():
    ring = CandidateRing(max_candidates=3, min_gap_s=0.0)
    for i, score in enumerate([10, 30, 20]):
        assert ring.offer(float(i), score, _crop())
    assert not ring.offer(3.0, 5, _crop())        # worse than the worst
    assert ring.offer(4.0, 40, _crop())           # replaces the 10
    assert sorted(c.score for c in ring.ranked()) == [20, 30, 40]
    assert ring.best().score == 40
    # ranked() is most-promising first — the sweep order at ingest.
    assert [c.score for c in ring.ranked()] == [40, 30, 20]


def test_ring_diversity_gap_spreads_candidates_across_the_pass():
    """Four crops from the same half-second are one lottery ticket
    photocopied. Rapid-fire offers must not fill the ring."""
    # The gap yields to an outright better frame (a much better look
    # is never refused just for arriving quickly):
    ring2 = CandidateRing(max_candidates=4, min_gap_s=1.0)
    ring2.offer(0.0, 10, _crop())
    assert ring2.offer(0.1, 11, _crop()) is True
    # ...but an equal-or-worse frame inside the gap is refused:
    ring3 = CandidateRing(max_candidates=4, min_gap_s=1.0)
    ring3.offer(0.0, 10, _crop())
    assert ring3.offer(0.1, 10, _crop()) is False
    assert ring3.offer(1.5, 10, _crop()) is True  # gap elapsed


def test_ring_rejects_empty_or_zero_score():
    ring = CandidateRing()
    assert not ring.offer(0.0, 0.0, _crop())
    assert not ring.offer(0.0, 5.0, None)
    assert ring.best() is None


# ── the early-attempt policy ───────────────────────────────────────


def test_first_attempt_fires_at_confirmation_only():
    p = EarlyAttemptPolicy(max_attempts=2)
    assert not p.should_attempt(confirmed=False, now=0.0, best_score=100)
    assert p.should_attempt(confirmed=True, now=0.0, best_score=100)


def test_attempt_budget_is_a_hard_cap():
    p = EarlyAttemptPolicy(max_attempts=1)
    assert p.should_attempt(confirmed=True, now=0.0, best_score=100)
    p.note_attempt(now=0.0, score=100)
    # A vastly better frame later cannot exceed the budget.
    assert not p.should_attempt(confirmed=True, now=60.0, best_score=1e9)


def test_retry_requires_much_better_frame_and_a_time_gap():
    p = EarlyAttemptPolicy(max_attempts=2, improve_factor=1.5,
                           min_retry_gap_s=2.0)
    p.note_attempt(now=0.0, score=100)
    assert not p.should_attempt(confirmed=True, now=1.0, best_score=1000)  # too soon
    assert not p.should_attempt(confirmed=True, now=3.0, best_score=120)   # not better enough
    assert p.should_attempt(confirmed=True, now=3.0, best_score=150)       # 1.5x + gap


# ── orchestration over live tracks ─────────────────────────────────


class _FakePoster:
    def __init__(self, accept=True):
        self.accept = accept
        self.submitted = []

    def submit(self, attempt):
        self.submitted.append(attempt)
        return self.accept


class _FakeTrack:
    def __init__(self, id, label="car", confirmed=True, ring=None):
        self.id = id
        self.label = label
        self.confirmed = confirmed
        self.plate_ring = ring


def _ring_with(score=100.0):
    ring = CandidateRing(min_gap_s=0.0)
    ring.offer(0.0, score, np.zeros((8, 8, 3), np.uint8))
    return ring


def test_early_attempts_fire_once_per_confirmed_vehicle():
    from detect_pipeline.plate_attempts import EarlyPlateAttempts

    poster = _FakePoster()
    ea = EarlyPlateAttempts(poster, "cam1", nvr_camera_id=1,
                            max_attempts=2, clock=lambda: 5.0)
    tracks = [_FakeTrack(1, ring=_ring_with())]
    assert ea.observe(tracks) == 1
    assert ea.observe(tracks) == 0        # same frame quality → no retry
    a = poster.submitted[0]
    assert a.nvr_camera_id == 1 and a.track_id == "1" and a.jpeg


def test_early_attempts_skip_people_unconfirmed_and_ringless():
    from detect_pipeline.plate_attempts import EarlyPlateAttempts

    poster = _FakePoster()
    ea = EarlyPlateAttempts(poster, "cam1", nvr_camera_id=1)
    assert ea.observe([
        _FakeTrack(1, label="person", ring=_ring_with()),
        _FakeTrack(2, confirmed=False, ring=_ring_with()),
        _FakeTrack(3, ring=None),
    ]) == 0
    assert poster.submitted == []


def test_queue_full_still_consumes_the_budget():
    """An overloaded box must not amplify its own load by retrying the
    same attempt every frame."""
    from detect_pipeline.plate_attempts import EarlyPlateAttempts

    poster = _FakePoster(accept=False)
    ea = EarlyPlateAttempts(poster, "cam1", nvr_camera_id=1, max_attempts=1)
    tracks = [_FakeTrack(1, ring=_ring_with())]
    assert ea.observe(tracks) == 0        # submitted but not queued
    assert len(poster.submitted) == 1
    assert ea.observe(tracks) == 0        # budget spent — no resubmit
    assert len(poster.submitted) == 1


def test_policies_are_dropped_with_their_tracks():
    from detect_pipeline.plate_attempts import EarlyPlateAttempts

    poster = _FakePoster()
    ea = EarlyPlateAttempts(poster, "cam1", nvr_camera_id=1)
    ea.observe([_FakeTrack(1, ring=_ring_with())])
    assert len(ea._policies) == 1
    ea.observe([])                        # track gone
    assert len(ea._policies) == 0


# ── the poster wire format ─────────────────────────────────────────


def test_attempt_poster_body_and_path():
    import json as _json

    from detect_pipeline.plate_attempts import ATTEMPT_PATH, Attempt, AttemptPoster

    captured = {}

    class _Resp:
        status = 202
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def opener(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = _json.loads(req.data.decode())
        captured["key"] = req.get_header("X-internal-api-key")
        return _Resp()

    poster = AttemptPoster("http://core:8000", "sekrit", opener=opener)
    poster._post(Attempt(camera_id="cam1", nvr_camera_id=7, track_id="42",
                         ts=123.5, jpeg=b"\xff\xd8jpeg"))
    assert captured["url"].endswith(ATTEMPT_PATH)
    assert captured["body"]["camera_id"] == 7
    assert captured["body"]["track_id"] == "42"
    assert captured["body"]["ts"] == 123.5
    assert captured["key"] == "sekrit"
    # camera-agent prefix — the endpoint lives on core's internal router.
    assert "/api/v1/internal/camera-agent/plates/attempt" == ATTEMPT_PATH


# ── tracker + lifecycle integration ────────────────────────────────


def _det(label="car", box=(10, 10, 90, 60), score=0.9):
    from detect_pipeline.tracking import Detection
    return Detection(label=label, box=box, score=score)


def _tracker(retain=True):
    from detect_pipeline.tracking import TrackConfig, Tracker
    t = Tracker((120, 160), TrackConfig(fps=2))
    t.retain_plate_candidates = retain
    return t


def test_tracker_retains_candidates_only_when_opted_in():
    bgr = np.random.default_rng(3).integers(0, 255, (120, 160, 3), dtype=np.uint8)
    on = _tracker(retain=True)
    on.update([_det()], bgr)
    assert on.tracks and on.tracks[0].plate_ring is not None
    assert len(on.tracks[0].plate_ring) >= 1

    off = _tracker(retain=False)
    off.update([_det()], bgr)
    assert off.tracks[0].plate_ring is None


def test_tracker_ignores_non_vehicles_and_pixelless_frames():
    bgr = np.zeros((120, 160, 3), np.uint8)
    t = _tracker(retain=True)
    t.update([_det(label="person")], bgr)
    assert t.tracks[0].plate_ring is None
    t2 = _tracker(retain=True)
    t2.update([_det()], None)             # no pixels → nothing to retain
    assert t2.tracks[0].plate_ring is None


def test_visit_carries_ranked_candidate_jpegs():
    from detect_pipeline.events_poster import VisitLifecycle

    bgr = np.random.default_rng(5).integers(0, 255, (120, 160, 3), dtype=np.uint8)
    t = _tracker(retain=True)
    lc = VisitLifecycle("cam1", nvr_camera_id=1, min_duration_s=0.0)
    for _ in range(3):
        t.update([_det()], bgr)
        lc.observe(t.tracks, 100.0)
    lc.observe(t.tracks, 103.0)
    visits = lc.observe([], 104.0)        # track gone → visit finishes
    assert len(visits) == 1
    v = visits[0]
    assert v.candidate_jpegs, "vehicle visit lost its plate candidates"
    assert all(j[:2] == b"\xff\xd8" for j in v.candidate_jpegs)  # real JPEGs


def test_visit_poster_ships_candidates_in_body():
    import json as _json

    from datetime import datetime, timezone

    from detect_pipeline.events_poster import Visit, VisitPoster

    captured = {}

    class _Resp:
        status = 201
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def opener(req, timeout=None):
        captured["body"] = _json.loads(req.data.decode())
        return _Resp()

    poster = VisitPoster("http://core:8000", None, opener=opener)
    now = datetime.now(timezone.utc)
    poster._post(Visit(
        camera_id="cam1", label="car", score=0.9, track_id="7",
        started_at=now, ended_at=now, stationary=False, jpeg=b"\xff\xd8x",
        nvr_camera_id=1, candidate_jpegs=(b"\xff\xd8a", b"\xff\xd8b"),
    ))
    assert len(captured["body"]["candidate_jpegs_b64"]) == 2
