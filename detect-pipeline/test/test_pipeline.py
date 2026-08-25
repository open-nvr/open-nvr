# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for the per-camera Tier-0 worker (fakes, no ffmpeg/model)."""
from __future__ import annotations

from detect_pipeline.detector import RawDetection
from detect_pipeline.ffmpeg_presets import frame_size_bytes
from detect_pipeline.frame_source import Frame
from detect_pipeline.pipeline import DetectPipeline, nms, select_regions
from detect_pipeline.tracking import Detection, TrackConfig, Tracker

W, H = 320, 240


def _frame(seq: int) -> Frame:
    return Frame(bytes(frame_size_bytes(W, H)), W, H, seq, float(seq))


class _FakeSource:
    def __init__(self, n: int):
        self.n = n

    def stream(self):
        for i in range(self.n):
            yield _frame(i)


class _FakeMotion:
    """Returns a fixed motion box; calibrating controllable."""

    def __init__(self, box=(100, 80, 160, 200), calibrating=False):
        self.box = box
        self._calibrating = calibrating

    def detect(self, luma):
        return [self.box]

    def is_calibrating(self):
        return self._calibrating


class _OneBoxDetector:
    def detect(self, crop):
        return [RawDetection("person", 0.9, (0.25, 0.25, 0.75, 0.75))]


# ── unit: NMS + region selection ────────────────────────────────────

def test_nms_suppresses_overlapping_same_label():
    a = Detection("person", (0, 0, 100, 100), 0.9)
    b = Detection("person", (5, 5, 105, 105), 0.6)      # heavy overlap, lower score
    c = Detection("car", (0, 0, 100, 100), 0.8)         # different label, kept
    out = nms([a, b, c])
    assert a in out and c in out and b not in out


def test_select_regions_dedups_overlapping():
    boxes = [(100, 100, 160, 200), (105, 100, 165, 205)]  # near-identical
    regions, capped = select_regions(boxes, [], (H, W), min_region=320)
    assert len(regions) == 1                              # collapsed to one region
    assert capped is False


# ── integration ─────────────────────────────────────────────────────

def test_pipeline_produces_track_from_motion_and_detection():
    tracker = Tracker((H, W), TrackConfig(fps=5, min_initialized=1))
    pipe = DetectPipeline(
        _FakeSource(3), _FakeMotion(calibrating=False), _OneBoxDetector(), tracker,
    )
    seen: list[int] = []

    def on_tracks(frame, tracks, calibrating):
        seen.append(len(tracks))

    pipe.run(on_tracks)
    assert len(seen) == 3                    # one callback per frame
    assert seen[-1] == 1                     # a person track is established
    assert tracker.tracks[0].label == "person"
    assert tracker.tracks[0].best is not None


def test_process_frame_exposes_detections_and_detector_latency():
    # The benchmarking signals: the frame's detections + the pure detector time.
    tracker = Tracker((H, W), TrackConfig(fps=5, min_initialized=1))
    pipe = DetectPipeline(
        _FakeSource(1), _FakeMotion(calibrating=False), _OneBoxDetector(), tracker,
    )
    result = pipe.process_frame(_frame(0))
    assert [d.label for d in result.detections] == ["person"]
    assert result.detect_latency_s is not None and result.detect_latency_s >= 0.0
    # stage breakdown present for a full (non-calibrating) frame
    assert {"motion", "region", "detect", "track"} <= set(result.stage_latency_s)


def test_process_frame_no_detector_latency_while_calibrating():
    tracker = Tracker((H, W), TrackConfig(fps=5, min_initialized=1))
    pipe = DetectPipeline(
        _FakeSource(1), _FakeMotion(calibrating=True), _OneBoxDetector(), tracker,
    )
    result = pipe.process_frame(_frame(0))
    assert result.detections == [] and result.detect_latency_s is None


def test_pipeline_skips_detection_while_calibrating():
    calls = {"n": 0}

    class _CountingDetector:
        def detect(self, crop):
            calls["n"] += 1
            return []

    tracker = Tracker((H, W), TrackConfig(fps=5, min_initialized=1))
    pipe = DetectPipeline(
        _FakeSource(3), _FakeMotion(calibrating=True), _CountingDetector(), tracker,
    )
    results = []
    pipe.run(lambda f, t, c: results.append(c))
    assert calls["n"] == 0                    # detector never called while calibrating
    assert all(c is True for c in results)    # calibrating flag surfaced
    assert tracker.tracks == []


# ── PR B: stationary-track gating ───────────────────────────────────

class _CountingBoxDetector:
    """Detects a person at a fixed frame-space box; counts calls."""

    def __init__(self):
        self.calls = 0

    def detect(self, crop):
        self.calls += 1
        # normalized coords roughly centered — mapped back via the region
        return [RawDetection("person", 0.9, (0.3, 0.3, 0.7, 0.7))]


class _SwitchableMotion:
    """Motion that can be turned on/off per frame."""

    def __init__(self, box=(100, 80, 160, 200)):
        self.box = box
        self.active = True

    def detect(self, luma):
        return [self.box] if self.active else []

    def is_calibrating(self):
        return False


def _make_stationary(pipe, motion, frames=6):
    """Feed frames with motion until a track exists and is stationary."""
    motion.active = True
    for i in range(frames):
        pipe.process_frame(_frame(i))
    motion.active = False


def _gated_pipe(interval=5, stationary_threshold=3):
    tracker = Tracker(
        (H, W),
        TrackConfig(fps=5, min_initialized=1, stationary_threshold=stationary_threshold),
    )
    det = _CountingBoxDetector()
    motion = _SwitchableMotion()
    pipe = DetectPipeline(
        _FakeSource(0), motion, det, tracker, stationary_interval=interval,
    )
    return pipe, det, motion, tracker


def test_stationary_track_stops_feeding_detector_every_frame():
    pipe, det, motion, tracker = _gated_pipe(interval=5)
    _make_stationary(pipe, motion)
    assert tracker.tracks and tracker.tracks[0].stationary
    before = det.calls
    results = [pipe.process_frame(_frame(100 + i)) for i in range(10)]
    ran = det.calls - before
    # 10 still frames, interval 5 → exactly 2 staggered re-verifications
    assert ran == 2, f"expected 2 re-verify detections, detector ran {ran}x"
    assert sum(r.skipped_stationary for r in results) == 8


def test_stationary_track_coasts_instead_of_dying():
    # Skipped frames must not age the track toward deletion: run far past
    # the tracker's disappearance budget (fps*5 = 25) with detection skipped.
    pipe, det, motion, tracker = _gated_pipe(interval=5)
    _make_stationary(pipe, motion)
    tid = tracker.tracks[0].id
    for i in range(60):
        pipe.process_frame(_frame(200 + i))
    assert [t.id for t in tracker.tracks] == [tid], "stationary track was lost while gated"
    assert tracker.tracks[0].stationary


def test_motion_on_stationary_object_reverifies_immediately():
    pipe, det, motion, tracker = _gated_pipe(interval=1000)   # never re-verify by timer
    _make_stationary(pipe, motion)
    before = det.calls
    # a couple of still frames: fully skipped
    pipe.process_frame(_frame(300)); pipe.process_frame(_frame(301))
    assert det.calls == before
    # motion overlapping the tracked object → detector runs THIS frame
    motion.active = True
    r = pipe.process_frame(_frame(302))
    assert det.calls > before
    assert r.skipped_stationary == 0


def test_interval_zero_disables_gating():
    pipe, det, motion, tracker = _gated_pipe(interval=0)
    _make_stationary(pipe, motion)
    before = det.calls
    results = [pipe.process_frame(_frame(400 + i)) for i in range(5)]
    # pre-PR-B behavior: the track feeds a region EVERY frame
    assert det.calls - before == 5
    assert all(r.skipped_stationary == 0 for r in results)


def test_departed_object_still_expires():
    # When the object actually leaves, re-verification frames scan its region,
    # find nothing, and the track must eventually die (not coast forever).
    pipe, det, motion, tracker = _gated_pipe(interval=3)

    class _GoneDetector:
        def detect(self, crop):
            return []

    _make_stationary(pipe, motion)
    pipe.detector = _GoneDetector()
    for i in range(200):
        pipe.process_frame(_frame(500 + i))
        if not tracker.tracks:
            break
    assert not tracker.tracks, "departed stationary track never expired"


def test_adjacent_motion_does_not_erode_a_gated_track():
    # Someone loitering NEXT TO a parked car: their motion region CLIPS the
    # car (~55% coverage at this 720p geometry) but never substantially
    # covers it, and the detector sees nothing car-shaped in those crops.
    # The car's track must coast, not accumulate misses toward deletion —
    # pre-gating it always had its own region, so this failure mode is new
    # with stationary skipping. (Note: FULL coverage + no detection is the
    # opposite case and must still expire the track — that is
    # test_departed_object_still_expires.)
    W2, H2 = 1280, 720
    frame2 = lambda seq: Frame(bytes(frame_size_bytes(W2, H2)), W2, H2, seq, float(seq))
    tracker = Tracker((H2, W2), TrackConfig(fps=5, min_initialized=1, stationary_threshold=3))
    det = _CountingBoxDetector()
    motion = _SwitchableMotion(box=(500, 300, 700, 600))   # the "car"
    pipe = DetectPipeline(_FakeSource(0), motion, det, tracker, stationary_interval=1000)
    for i in range(6):                                     # establish + go stationary
        pipe.process_frame(frame2(i))
    motion.active = False
    assert tracker.tracks and tracker.tracks[0].stationary
    car = tracker.tracks[0]
    tid = car.id

    class _SeesNothing:
        def detect(self, crop):
            return []

    pipe.detector = _SeesNothing()
    # motion box adjacent to the car: its region clips the car partially
    x1, y1, x2, y2 = car.box
    motion.box = (x2 + 20, y1 + 50, x2 + 80, y2 - 50)
    motion.active = True
    budget = tracker.config.disappeared()
    for i in range(budget * 4):
        pipe.process_frame(frame2(700 + i))
    assert [t.id for t in tracker.tracks] == [tid], (
        "gated track eroded by adjacent-motion partial scans")


BIG = (720, 1280)          # this module's H/W are QVGA; these need room


def test_heavy_motion_cannot_starve_track_reverification():
    """Rain, wind in foliage, a busy road — any scene producing max_regions
    motion boxes every frame used to consume the whole budget, so NO confirmed
    track was ever re-verified and they all coasted to DETECT_TRACK_TTL (300s).
    That is precisely when the pipeline most needs to know what is real."""
    from detect_pipeline.regions import calculate_region

    motion = [(i * 140, 0, i * 140 + 40, 40) for i in range(10)]
    tracks = [(500, 400, 560, 480), (900, 300, 960, 380)]
    regions, capped = select_regions(motion, tracks, BIG, 320, max_regions=8)

    assert len(regions) == 8 and capped is True
    for t in tracks:
        r = calculate_region(BIG, t[0], t[1], t[2], t[3], 320)
        assert r in regions, "a live track went un-scanned under heavy motion"


def test_tracks_do_not_hog_the_budget_when_motion_is_light():
    """The reserve must not invert the problem — slack goes back to motion."""
    motion = [(0, 0, 40, 40), (200, 0, 240, 40)]
    tracks = [(500, 400, 560, 480)]
    regions, capped = select_regions(motion, tracks, BIG, 320, max_regions=8)
    assert len(regions) == 3 and capped is False


def test_dedup_alone_is_not_reported_as_capped():
    """capped means the BUDGET hid a candidate, not that dedup merged one —
    it is what tier0_regions_capped_total alerts on."""
    boxes = [(100, 100, 160, 200), (105, 100, 165, 205)]
    regions, capped = select_regions(boxes, [], BIG, min_region=320, max_regions=8)
    assert len(regions) == 1 and capped is False


def test_one_failing_region_does_not_kill_the_frame():
    """detector.detect() was unwrapped, so a model rejecting a crop shape
    raised through process_frame into the worker loop's 'crashed' handler and
    the feed went dark until the next reconcile — every frame, in a loop.
    That is reachable from a documented env knob: the shipped yolov8n.onnx is
    exported at a FIXED 640, so DETECT_ONNX_INPUT=320 made every inference
    raise."""
    class _Flaky:
        def __init__(self): self.n = 0
        def detect(self, crop):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("model rejected this crop shape")
            return [RawDetection("person", 0.9, (0.1, 0.1, 0.5, 0.8))]

    det = _Flaky()
    tracker = Tracker((H, W), TrackConfig(fps=2))
    p = DetectPipeline(_FakeSource(4), _FakeMotion(calibrating=False), det, tracker)

    results = [p.process_frame(f) for f in _FakeSource(4).stream()]   # must not raise
    assert any(r.regions for r in results), "no regions — test would prove nothing"
    assert p.detector_errors == 1
    assert det.n > 1, "pipeline stopped calling the detector after the failure"
    assert any(r.tracks or r.detections for r in results), \
        "later regions must still produce detections"
