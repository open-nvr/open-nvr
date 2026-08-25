# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded-load guards (#track-explosion).

Field failure these lock in: a camera on a cluttered desk (wires, boards)
fed yolov8n-at-0.25 phantoms ("kite" x992, "banana" x168...) that confirmed
into 181 standing tracks; per-frame re-verification of that population put
the detector at ~30 s/frame and pinned two cores indefinitely. The pipeline
must stay bounded no matter what it looks at:

* track cap        — the track list never grows past ``max_tracks``
* spawn-score floor— weak evidence never creates a track
* coast TTL        — an unverified track always drains, in WALL time
* region budget    — the detector never runs more than ``max_regions`` crops
* label allowlist  — phantom classes are dropped before tracking
"""
from __future__ import annotations

from detect_pipeline.detector import RawDetection
from detect_pipeline.ffmpeg_presets import frame_size_bytes
from detect_pipeline.frame_source import Frame
from detect_pipeline.pipeline import DetectPipeline
from detect_pipeline.tracking import Detection, TrackConfig, Tracker

W, H = 1280, 720


# ── tracker bounds ──────────────────────────────────────────────────

def _dets(n: int, score: float = 0.9) -> list[Detection]:
    """n same-label detections far enough apart to never match each other."""
    out = []
    for i in range(n):
        x = (i % 16) * 80
        y = (i // 16) * 90
        out.append(Detection("person", (x, y, x + 40, y + 60), score))
    return out


def test_track_cap_bounds_population():
    tr = Tracker((H, W), TrackConfig(fps=5, min_initialized=1, max_tracks=10))
    tr.update(_dets(100))
    assert len(tr.tracks) <= 10
    assert tr.spawns_dropped >= 90
    # and it stays bounded on repeat floods
    tr.update(_dets(100))
    assert len(tr.tracks) <= 10


def test_min_spawn_score_refuses_weak_evidence():
    tr = Tracker((H, W), TrackConfig(fps=5, min_initialized=1, min_spawn_score=0.5))
    tr.update([Detection("person", (0, 0, 40, 60), 0.4)])
    assert tr.tracks == [] and tr.spawns_dropped == 1
    tr.update([Detection("person", (200, 0, 240, 60), 0.6)])
    assert len(tr.tracks) == 1


def test_weak_score_still_matches_existing_track():
    """The floor gates SPAWNING only — hysteresis, not a match filter."""
    tr = Tracker((H, W), TrackConfig(fps=5, min_initialized=1, min_spawn_score=0.5))
    tr.update([Detection("person", (0, 0, 40, 60), 0.9)])
    (t,) = tr.tracks
    tr.update([Detection("person", (2, 0, 42, 60), 0.35)])   # weak, but same object
    assert tr.tracks[0].id == t.id and tr.tracks[0].misses == 0


def test_coast_ttl_expires_unverified_track_in_wall_time():
    clk = {"t": 0.0}
    tr = Tracker(
        (H, W),
        TrackConfig(fps=5, min_initialized=1, coast_ttl_seconds=100.0),
        clock=lambda: clk["t"],
    )
    tr.update([Detection("person", (0, 0, 40, 60), 0.9)])
    # Coasting (nothing scanned its region) keeps it alive within the TTL...
    clk["t"] = 50.0
    assert len(tr.update([], scanned_regions=[])) == 1
    # ...but wall time, not frame count, is the ceiling: past the TTL it dies
    # even though it never accumulated a single miss.
    clk["t"] = 150.0
    assert tr.update([], scanned_regions=[]) == []


def test_coast_ttl_spares_recently_matched_tracks():
    clk = {"t": 0.0}
    tr = Tracker(
        (H, W),
        TrackConfig(fps=5, min_initialized=1, coast_ttl_seconds=100.0),
        clock=lambda: clk["t"],
    )
    tr.update([Detection("person", (0, 0, 40, 60), 0.9)])
    for t in (60.0, 120.0, 180.0):                 # re-matched before each deadline
        clk["t"] = t
        assert len(tr.update([Detection("person", (0, 0, 40, 60), 0.9)])) == 1


def test_coast_ttl_zero_disables_expiry():
    clk = {"t": 0.0}
    tr = Tracker(
        (H, W),
        TrackConfig(fps=5, min_initialized=1, coast_ttl_seconds=0.0),
        clock=lambda: clk["t"],
    )
    tr.update([Detection("person", (0, 0, 40, 60), 0.9)])
    clk["t"] = 1e9
    assert len(tr.update([], scanned_regions=[])) == 1


# ── pipeline bounds ─────────────────────────────────────────────────

def _frame(seq: int) -> Frame:
    return Frame(bytes(frame_size_bytes(W, H)), W, H, seq, float(seq))


class _ManyBoxMotion:
    """Five well-separated motion boxes — five distinct region candidates."""

    def detect(self, luma):
        return [
            (0, 0, 50, 50), (450, 0, 500, 50), (900, 0, 950, 50),
            (0, 400, 50, 450), (450, 400, 500, 450),
        ]

    def is_calibrating(self):
        return False


class _CountingDetector:
    def __init__(self, raws=None):
        self.calls = 0
        self.raws = raws or []

    def detect(self, crop):
        self.calls += 1
        return list(self.raws)


def test_region_budget_caps_detector_passes():
    det = _CountingDetector()
    pipe = DetectPipeline(
        None, _ManyBoxMotion(), det, Tracker((H, W), TrackConfig(fps=5)),
        max_regions=2,
    )
    result = pipe.process_frame(_frame(1))
    assert len(result.regions) == 2
    assert det.calls == 2
    assert result.regions_capped is True


def test_region_budget_zero_is_unbounded():
    det = _CountingDetector()
    pipe = DetectPipeline(
        None, _ManyBoxMotion(), det, Tracker((H, W), TrackConfig(fps=5)),
        max_regions=0,
    )
    result = pipe.process_frame(_frame(1))
    assert len(result.regions) == 5
    assert result.regions_capped is False


def test_label_allowlist_drops_phantom_classes_before_tracking():
    det = _CountingDetector(raws=[
        RawDetection("kite", 0.9, (0.1, 0.1, 0.3, 0.3)),
        RawDetection("banana", 0.8, (0.6, 0.6, 0.8, 0.8)),
        RawDetection("person", 0.9, (0.3, 0.3, 0.7, 0.9)),
    ])
    tracker = Tracker((H, W), TrackConfig(fps=5, min_initialized=1))
    pipe = DetectPipeline(
        None, _ManyBoxMotion(), det, tracker,
        allowed_labels={"person"},
    )
    result = pipe.process_frame(_frame(1))
    assert {d.label for d in result.detections} == {"person"}
    assert {t.label for t in tracker.tracks} == {"person"}


# ── load shedding: never fall so far behind that we lose the stream ──

def test_budget_sheds_under_sustained_overrun_and_recovers():
    """Measured on a live 1080p camera with the shipped defaults: 3.07s of
    inference against a 500ms budget, sustained, with the RTSP session
    dropping every few minutes. Falling behind is not just late detections —
    the worker stops draining ffmpeg's stdout, ffmpeg blocks on the full pipe,
    and MediaMTX drops the reader. The camera then looks flaky when it is us."""
    from detect_pipeline.pipeline import RegionBudgetController

    c = RegionBudgetController(8, fps=2)          # 500ms budget
    assert c.current == 8 and not c.shedding

    for _ in range(40):
        c.observe(3.07, regions_run=c.current)
    assert c.current < 8 and c.shedding
    assert c.current >= 1, "must keep detecting something"

    # Recovery is deliberately cautious now — the budget will not sprint back
    # into a level it has seen fail, and it will not take a quiet frame's word
    # for it either — so full recovery takes real frames doing real work.
    for _ in range(400):
        c.observe(0.05, regions_run=c.current)     # hardware caught up
    assert c.current == 8 and not c.shedding


def test_budget_ignores_isolated_slow_frames():
    """A GOP boundary, a burst of motion, the OS scheduling elsewhere — one
    slow frame must not cut the budget."""
    from detect_pipeline.pipeline import RegionBudgetController

    c = RegionBudgetController(8, fps=2)
    for _ in range(20):
        c.observe(0.05)
    c.observe(4.0)                                 # single spike
    c.observe(0.05)
    assert c.current == 8


def test_budget_is_inert_when_regions_are_unbounded():
    from detect_pipeline.pipeline import RegionBudgetController

    c = RegionBudgetController(0, fps=2)           # 0 = no cap configured
    for _ in range(40):
        assert c.observe(9.0) == 0
    assert c.current == 0


def test_budget_never_sheds_below_its_floor():
    from detect_pipeline.pipeline import RegionBudgetController

    c = RegionBudgetController(8, fps=2, floor=2)
    for _ in range(200):
        c.observe(30.0)                            # hopelessly over budget
    assert c.current == 2


def test_idle_frames_are_not_evidence_of_headroom():
    """A frame on which the detector never ran costs ~0ms. Counting that as
    "we have headroom" let a quiet scene walk the budget straight back to the
    configured maximum on evidence that says nothing about a BUSY frame — so
    the next burst of motion blew the budget again and it sawtoothed.

    Observed in production: 'frame latency 0.00s ... regions cut to 6' —
    stepping the budget UP off zero-cost frames, and mislabelling it too.
    """
    from detect_pipeline.pipeline import RegionBudgetController

    c = RegionBudgetController(8, fps=2)
    for _ in range(40):
        c.observe(3.07, regions_run=8)          # sustained real overrun
    shed_to = c.current
    assert shed_to < 8

    for _ in range(200):
        c.observe(0.0, regions_run=0)           # quiet scene, no detector work
    assert c.current == shed_to, "idle frames must not restore the budget"

    for _ in range(200):
        c.observe(0.05, regions_run=shed_to)    # real frames, real headroom
    assert c.current == 8


def test_budget_change_reports_its_direction():
    """`shedding` is true while RECOVERING too (still below configured), so
    keying the log off it reported a step up as a cut — production logged
    'regions cut to 6' while climbing 5->6, and 'restored to to 8'."""
    from detect_pipeline.pipeline import RegionBudgetController

    c = RegionBudgetController(8, fps=2)
    deltas = [c.observe(3.07, 8) for _ in range(40)]
    assert any(d < 0 for d in deltas), "shedding must report a negative delta"
    assert all(d <= 0 for d in deltas)

    ups = [c.observe(0.05, c.current) for _ in range(200)]
    assert any(d > 0 for d in ups), "recovery must report a positive delta"


def _drive(controller, frames, *, per_region=0.115, busy_every=4, period=50):
    """Drive the controller with a production-shaped load and count budget moves.

    Cost scales with the regions actually RUN, and activity comes and goes: a
    quiet frame runs a couple of regions and is cheap at any budget, a busy one
    runs the full budget and is not. That asymmetry is the whole problem — it
    is why headroom measured while quiet is not evidence about a busy frame.
    """
    moves, dwell = 0, {}
    for i in range(frames):
        demand = 8 if (i // period) % busy_every == busy_every - 1 else 2
        ran = min(controller.current, demand)
        if controller.observe(per_region * ran, ran) != 0:
            moves += 1
        dwell[controller.current] = dwell.get(controller.current, 0) + 1
    return moves, dwell


def test_budget_stops_climbing_back_into_a_level_it_knows_fails():
    """The production sawtooth. In one 35-minute run the budget was cut
    sixteen times and every single cut was from 8: it halved away, climbed
    1 -> 8 in about twenty seconds off cheap quiet frames, blew the budget at 8
    again, and repeated every three minutes. Nothing broke; it just spent its
    life re-learning one fact.

    At 0.115s per region against a 500ms budget, 5 is the largest level a busy
    frame can afford. The controller has to find that and stay there.
    """
    from detect_pipeline.pipeline import RegionBudgetController

    memoryless = RegionBudgetController(
        8, fps=2, probe_factor=1, max_probe_backoff=1, probation_frames=1
    )
    remembering = RegionBudgetController(8, fps=2)

    old_moves, old_dwell = _drive(memoryless, 20000)
    new_moves, new_dwell = _drive(remembering, 20000)

    assert remembering.current == 5, (
        f"should settle on the largest level a busy frame affords, "
        f"got {remembering.current}"
    )
    # The memoryless controller spends most of its life at the level that
    # fails; this one must not go there at all once it has converged.
    assert old_dwell.get(8, 0) > 20000 // 2
    assert new_dwell.get(8, 0) < 20000 // 10
    assert new_moves < old_moves // 4, (
        f"churn should collapse, not merely improve: {new_moves} vs {old_moves}"
    )


def test_budget_churn_stops_once_converged():
    """Converged means converged — no moves at all in the final stretch. A
    controller that still steps every few minutes is still writing WARNING
    lines and still re-paying the cost of being wrong."""
    from detect_pipeline.pipeline import RegionBudgetController

    c = RegionBudgetController(8, fps=2)
    _drive(c, 30000)                                  # converge
    tail_moves, _ = _drive(c, 10000)                  # then watch
    assert tail_moves == 0, f"still churning after convergence: {tail_moves} moves"


def test_a_ceiling_is_never_permanent():
    """One bad minute must not cost detections forever. When the box genuinely
    frees up — a neighbouring camera stopped, the scene changed — the budget
    has to come back."""
    from detect_pipeline.pipeline import RegionBudgetController

    c = RegionBudgetController(8, fps=2)
    _drive(c, 20000)
    assert c.current < 8 and c._ceiling is not None

    for _ in range(2000):                             # everything cheap now
        c.observe(0.02 * min(c.current, 8), min(c.current, 8))
        if c.current == 8:
            break
    assert c.current == 8, "a stale ceiling must not pin the budget down"
    assert c._ceiling is None


def test_the_ceiling_only_lifts_on_evidence_from_frames_that_tested_it():
    """Arriving at a level is not proof it works. A quiet frame is cheap at any
    budget because barely any regions run, so quiet frames must not be able to
    retire the mark — that is exactly the loop being closed here."""
    from detect_pipeline.pipeline import RegionBudgetController

    c = RegionBudgetController(8, fps=2)
    while c.current == 8:
        c.observe(0.9, regions_run=8)                 # 8 is too expensive
    assert c._ceiling == 8

    # Thousands of cheap frames that never run more than 2 regions. They may
    # walk the budget up, but they can never certify the ceiling.
    for _ in range(3000):
        c.observe(0.03, regions_run=2)
    assert c._ceiling == 8, "quiet frames must not retire the ceiling"


def test_the_ceiling_tracks_downwards_when_a_lower_level_also_fails():
    """Cost is monotone in region count, so a failure at 4 also condemns 5-8.
    The mark has to follow the evidence down; when it did not, every shed
    landed somewhere new, the repeat-failure count kept resetting to 1, and the
    probe never got expensive enough to stop the hunting."""
    from detect_pipeline.pipeline import RegionBudgetController

    c = RegionBudgetController(8, fps=2)
    while c.current == 8:
        c.observe(0.9, regions_run=8)
    assert c._ceiling == 8
    at = c.current

    while c.current == at:                            # `at` is too slow as well
        c.observe(2.0, regions_run=at)
    assert c._ceiling <= at, f"ceiling must follow failure down, got {c._ceiling}"


def test_repeated_failure_at_a_level_makes_the_next_probe_dearer():
    """Bounded backoff: a level that keeps failing stops being retried every
    few minutes, but never stops being retried altogether."""
    from detect_pipeline.pipeline import RegionBudgetController

    c = RegionBudgetController(8, fps=2)
    _drive(c, 20000)
    assert c._ceiling_hits > 1, "repeat failures must accumulate"

    # Standing just below the ceiling with a recovery streak running: the cost
    # of the next probe grows with the failures, and stops growing at the cap.
    c._streak = -1
    c.current = c._ceiling - 1
    ordinary = c.settle_frames
    cost = c._evidence_needed()
    assert cost > ordinary, "a probe must cost more than an ordinary step"
    assert cost <= ordinary * c.probe_factor * c.max_probe_backoff, (
        "probe cost must stay bounded so the level is still retried eventually"
    )
