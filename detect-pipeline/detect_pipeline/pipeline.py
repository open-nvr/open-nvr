# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
The per-camera Tier-0 worker — ties the pieces together.

    frame source ──► motion ──► region select ──► detector (per region)
                                     │                    │
                                     └── (calibrating? skip detection) ──┐
                                                          ▼              │
                                                NMS ──► tracker ──► on_tracks
                                                                         ▲
                                     recording is MediaMTX's job ────────┘  (never gated)

Detection is DOUBLY gated. The first gate has always been region selection:
no motion boxes and no active tracks → no regions → the detector never runs.
The second gate is stationary-track skipping (PR B, this file): a track that
has not moved for ``stationary_threshold`` frames stops feeding a region
every frame and is only re-verified every ``stationary_interval``-th frame
(staggered per track id so multiple parked objects don't re-verify on the
same frame), or IMMEDIATELY when a motion box intersects it — a parked car
no longer pins the detector, but something moving next to it re-checks it
at once. Skipped tracks coast in the tracker (see Tracker.update's
scanned_regions contract) instead of aging toward deletion.

Region selection here is the simple form: a region per motion box and per active
track, de-duplicated by IoU. The learned per-camera region grid is a documented
follow-up (it needs OpenNVR-side storage, not Frigate's DB models).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from .detector import (
    DetectorAdapter,
    crop_and_resize,
    detections_to_frame,
    to_bgr,
)
from .regions import Box, calculate_region, get_min_region_size, intersection_over_union
from .tracking import Detection, Track, Tracker

log = logging.getLogger("detect_pipeline.pipeline")

# on_tracks(frame, tracks, calibrating)
OnTracks = Callable[[object, list[Track], bool], None]


@dataclass
class FrameResult:
    """Everything one frame produced — for downstream use, audit, and the demo
    overlay (motion boxes + regions + tracks)."""

    tracks: list[Track]
    motion_boxes: list[Box] = field(default_factory=list)
    regions: list[Box] = field(default_factory=list)
    calibrating: bool = False
    # The frame's NMS'd detections and the pure detector time (region loop only,
    # excluding decode/motion/track). These are the model-benchmarking signals:
    # per-class output volume and per-model inference speed. Empty/None on frames
    # where the detector didn't run (calibrating / no motion).
    detections: list[Detection] = field(default_factory=list)
    detect_latency_s: float | None = None
    # Stationary tracks whose region was NOT scanned this frame (the PR B
    # saving, made visible for metrics/tests).
    skipped_stationary: int = 0
    track_population: int = 0
    # True when the per-frame region budget dropped at least one candidate
    # this frame (exported as tier0_regions_capped_total — no silent caps).
    regions_capped: bool = False
    # Per-stage wall time for the frame (decode/motion/region/detect/track) — lets
    # the test-bed see *where* time goes, not just the end-to-end total.
    stage_latency_s: dict[str, float] = field(default_factory=dict)


def nms(dets: list[Detection], iou_threshold: float = 0.5) -> list[Detection]:
    """Greedy per-label non-max suppression; keeps the highest score."""
    kept: list[Detection] = []
    for d in sorted(dets, key=lambda x: x.score, reverse=True):
        if all(
            not (k.label == d.label and intersection_over_union(d.box, k.box) > iou_threshold)
            for k in kept
        ):
            kept.append(d)
    return kept


def select_regions(
    motion_boxes: list[Box],
    track_boxes: list[Box],
    frame_shape: tuple[int, int],
    min_region: int,
    multiplier: float = 1.35,
    dedup_iou: float = 0.7,
    max_regions: int | None = None,
) -> tuple[list[Box], bool]:
    """One square region per motion box and per active track, de-duplicated.

    Bounds how much is ever sent to the detector each frame. ``max_regions``
    is the hard per-frame budget (#track-explosion): motion boxes are listed
    first so live activity always wins a slot over track re-verification;
    candidates beyond the budget are simply not scanned this frame — the
    tracker's ``scanned_regions`` contract makes their tracks coast, and the
    caller rotates ``track_boxes`` so the budget round-robins across frames.

    Returns ``(regions, capped)`` — ``capped`` is True when at least one
    candidate was dropped by the budget (never silently: the service exports
    it as ``tier0_regions_capped_total``).
    """
    kept: list[Box] = []
    # Candidates we actually EVALUATED. A box that was evaluated and then
    # deduped away is not "capped" — only one the budget never let us look
    # at is, which is what tier0_regions_capped_total is meant to report.
    attempted: set[tuple[int, int]] = set()

    def _take(boxes, budget: int, group: int) -> None:
        added = 0
        for i, b in enumerate(boxes):
            if added >= budget or (max_regions is not None and len(kept) >= max_regions):
                continue
            attempted.add((group, i))
            r = calculate_region(
                frame_shape, b[0], b[1], b[2], b[3], min_region, multiplier
            )
            if all(intersection_over_union(r, k) < dedup_iou for k in kept):
                kept.append(r)
                added += 1

    motion, tracks = list(motion_boxes), list(track_boxes)
    if max_regions is None:
        _take(motion, len(motion), 0)
        _take(tracks, len(tracks), 1)
    else:
        # Reserve part of the budget for track re-verification. Motion boxes
        # used to be taken first and the loop simply broke at the cap, so a
        # scene generating max_regions motion boxes every frame — rain, wind
        # in foliage, a busy road — meant NO confirmed track was ever
        # re-verified. They all coasted to DETECT_TRACK_TTL (300s default),
        # which is exactly when the pipeline most needs to know what is real.
        reserve = min(len(tracks), max(1, max_regions // 2)) if tracks else 0
        _take(motion, max_regions - reserve, 0)   # motion first, minus the reserve
        _take(tracks, max_regions, 1)             # tracks claim the reserve
        _take(motion, max_regions, 0)             # any slack goes back to motion
    return kept, len(attempted) < (len(motion) + len(tracks))


class DetectPipeline:
    """Full-time Tier-0 detection for one camera substream."""

    def __init__(
        self,
        frame_source,
        motion,
        detector: DetectorAdapter,
        tracker: Tracker,
        *,
        model_size: tuple[int, int] = (320, 320),
        region_multiplier: float = 1.35,
        stationary_interval: int = 10,
        max_regions: int = 8,
        allowed_labels: frozenset[str] | set[str] | None = None,
    ) -> None:
        self.frame_source = frame_source
        self.motion = motion
        self.detector = detector
        self.tracker = tracker
        self.model_size = model_size            # (w, h)
        self.region_multiplier = region_multiplier
        self.min_region = get_min_region_size(model_size[0], model_size[1])
        # Re-verify a stationary track every Nth frame (staggered by track
        # id). 0 disables the skip — every track feeds a region every frame,
        # the pre-PR-B behavior. Kept well below the tracker's disappearance
        # budget so a skipped track can never age out between verifications.
        self.stationary_interval = max(0, int(stationary_interval))
        # Per-frame detector budget (#track-explosion): the worst-case number
        # of region crops YOLO ever runs on one frame, no matter how many
        # tracks accumulate. 0 disables (unbounded, the pre-guard behavior).
        self.max_regions = max(0, int(max_regions))
        self.detector_errors = 0
        # Labels worth TRACKING. An NVR watching a cluttered desk has no
        # business confirming tracks for "kite"/"banana" wire false
        # positives — every phantom is a standing re-verify cost. None = all.
        self.allowed_labels = frozenset(allowed_labels) if allowed_labels else None
        self._frame_idx = 0

    def process_frame(self, frame) -> FrameResult:
        """Run one frame end-to-end; return tracks + motion boxes + regions."""
        stages: dict[str, float] = {}
        _t = time.monotonic()
        luma = np.frombuffer(frame.y_plane, np.uint8).reshape(frame.height, frame.width)
        motion_boxes = self.motion.detect(luma)
        stages["motion"] = time.monotonic() - _t

        # While calibrating (warm-up / whole-frame flash) do NOT run the detector;
        # tracks still age. Recording is unaffected (MediaMTX).
        if self.motion.is_calibrating():
            _t = time.monotonic()
            tracks = self.tracker.update([])
            stages["track"] = time.monotonic() - _t
            return FrameResult(tracks, motion_boxes, [], True, stage_latency_s=stages)

        frame_shape = (frame.height, frame.width)
        self._frame_idx += 1
        track_boxes: list[Box] = []
        skipped = 0
        for t in self.tracker.tracks:
            if (
                self.stationary_interval > 0
                and t.stationary
                # staggered re-verify frame for THIS track?
                and (self._frame_idx + t.id) % self.stationary_interval != 0
                # motion touching the object forces an immediate re-check —
                # the car may be leaving, or someone is standing next to it.
                and not any(
                    intersection_over_union(t.box, m) > 0 for m in motion_boxes
                )
            ):
                skipped += 1
                continue
            track_boxes.append(t.box)
        # Rotate the track-region candidates by frame index so the per-frame
        # budget round-robins across tracks instead of starving the tail.
        if self.max_regions > 0 and len(track_boxes) > 1:
            off = self._frame_idx % len(track_boxes)
            track_boxes = track_boxes[off:] + track_boxes[:off]
        _t = time.monotonic()
        regions, regions_capped = select_regions(
            motion_boxes, track_boxes, frame_shape, self.min_region, self.region_multiplier,
            max_regions=self.max_regions or None,
        )
        stages["region"] = time.monotonic() - _t

        dets: list[Detection] = []
        bgr = None
        detect_latency_s: float | None = None
        if regions:
            _t = time.monotonic()
            bgr = to_bgr(frame.data, frame.width, frame.height)
            stages["decode"] = time.monotonic() - _t
            _t = time.monotonic()
            for region in regions:
                crop = crop_and_resize(bgr, region, self.model_size[0], self.model_size[1])
                try:
                    raws = self.detector.detect(crop)
                except Exception:
                    # One bad inference must cost one region, not the camera.
                    # This was unwrapped, so a model that rejected the crop
                    # shape raised straight through process_frame into the
                    # worker loop's "crashed" handler and the feed went dark
                    # until the next reconcile — every frame, in a loop.
                    self.detector_errors += 1
                    log.debug("detector failed on one region", exc_info=True)
                    continue
                dets.extend(detections_to_frame(raws, region))
            if self.allowed_labels is not None:
                dets = [d for d in dets if d.label in self.allowed_labels]
            dets = nms(dets)
            detect_latency_s = time.monotonic() - _t   # pure detector time (this model)
            stages["detect"] = detect_latency_s

        # bgr is passed so the tracker can retain each track's best-frame crop
        # for Tier-1 dispatch (#10); None on frames with no detections.
        _t = time.monotonic()
        tracks = self.tracker.update(dets, bgr, scanned_regions=regions)
        stages["track"] = time.monotonic() - _t
        return FrameResult(
            tracks, motion_boxes, regions, False,
            detections=dets, detect_latency_s=detect_latency_s, stage_latency_s=stages,
            skipped_stationary=skipped, regions_capped=regions_capped,
            track_population=self.tracker.population,
        )

    def run(self, on_tracks: OnTracks | None = None) -> None:
        """Consume the frame source forever (bounded in tests by a finite source)."""
        for frame in self.frame_source.stream():
            result = self.process_frame(frame)
            if on_tracks is not None:
                on_tracks(frame, result.tracks, result.calibrating)


class RegionBudgetController:
    """Shrink the per-frame detector budget when a camera can't keep up.

    Tier-0's cost is dominated by inference, and the region budget is what
    multiplies it: ``max_regions`` crops per frame. On hardware where a single
    inference is slower than the whole frame budget, the worker simply falls
    further behind every frame — and it has no way to notice. The consequence
    is not just late detections: the worker stops draining ffmpeg's stdout,
    ffmpeg blocks on the full pipe, MediaMTX drops the reader, and the session
    dies. The camera then looks flaky ("Failed reading RTSP data: End of file")
    when the real cause is us.

    Measured on a live 1080p camera with the shipped defaults (8 regions,
    640px input, 2 fps): 3.07s of inference against a 500ms budget — six times
    over, sustained, with the stream dropping every few minutes.

    So: watch actual frame latency against the budget and spend fewer regions
    when we are over it, recovering when there is room again. Detecting less
    per frame is a real cost, but it is strictly better than decoding a stream
    we cannot finish and losing the session with it.

    Recovery remembers. Plain additive-increase/multiplicative-decrease is
    memoryless, and on a real camera that showed: it halved away from 8
    regions, climbed back to 8 in about twenty seconds off cheap quiet frames,
    blew the budget at 8 again, and repeated every three minutes — sixteen cuts
    in one 35-minute run, every one of them from 8. So the level that failed is
    remembered (TCP's slow-start threshold), growth below it stays free, and
    growth back INTO it has to be paid for in sustained evidence, more each
    time it fails again. The mark only lifts on frames that genuinely ran that
    many regions — a quiet frame is cheap at any budget and proves nothing —
    so a busy scene can no longer be talked into a level it cannot afford, and
    a box that really does free up still gets its full budget back.

    Pure and clock-injected — no I/O, no threads.
    """

    def __init__(
        self,
        configured: int,
        fps: int,
        *,
        floor: int = 1,
        over_budget: float = 1.2,
        recover_at: float = 0.6,
        settle_frames: int = 5,
        smoothing: float = 0.3,
        probe_factor: int = 4,
        max_probe_backoff: int = 8,
        probation_frames: int = 20,
    ) -> None:
        self.configured = max(0, int(configured))
        self.budget_s = 1.0 / max(1, int(fps))
        self.floor = max(1, int(floor))
        self.over_budget = over_budget
        self.recover_at = recover_at
        self.settle_frames = max(1, int(settle_frames))
        self.smoothing = smoothing
        self.probe_factor = max(1, int(probe_factor))
        self.max_probe_backoff = max(1, int(max_probe_backoff))
        self.probation_frames = max(1, int(probation_frames))
        self.current = self.configured
        self._ema: float | None = None
        self._streak = 0
        # The lowest region count observed to blow the budget on this box, or
        # None if we have no such evidence. TCP's slow-start threshold: the
        # point below which growth is free and above which it must be earned.
        self._ceiling: int | None = None
        # Consecutive failures at that same level. Each one makes the next
        # probe cost more evidence, so a level that keeps failing stops being
        # retried every few minutes — bounded, so it is never retried *never*.
        self._ceiling_hits = 0
        # Frames that have genuinely exercised the ceiling since we climbed
        # back to it. Only these count towards clearing it.
        self._held = 0

    @property
    def shedding(self) -> bool:
        return self.configured > 0 and self.current < self.configured

    def observe(self, latency_s: float, regions_run: int = 1) -> int:
        """Feed one frame. Returns the DELTA applied to the budget (0 = none).

        A single slow frame means nothing — a GOP boundary, a burst of motion,
        the OS scheduling elsewhere. Only a sustained trend moves the budget,
        so this smooths and requires a streak in one direction.

        ``regions_run`` is what makes the signal honest. A frame on which the
        detector never ran costs ~0ms, and treating that as "we have headroom"
        let a quiet scene walk the budget straight back up to the configured
        maximum on evidence that says nothing about the cost of a BUSY frame —
        so the next burst of motion immediately blew the budget again and it
        sawtoothed. Idle frames are simply not evidence either way.
        """
        if self.configured <= 0 or regions_run <= 0:
            return 0
        self._ema = (
            latency_s if self._ema is None
            else (self.smoothing * latency_s + (1 - self.smoothing) * self._ema)
        )
        over = self._ema > self.budget_s * self.over_budget
        under = self._ema < self.budget_s * self.recover_at
        self._serve_probation(regions_run, over)

        if over and self.current > self.floor:
            self._streak = self._streak + 1 if self._streak >= 0 else 1
        elif under and self.current < self.configured:
            self._streak = self._streak - 1 if self._streak <= 0 else -1
        else:
            self._streak = 0
            return 0

        if abs(self._streak) < self._evidence_needed():
            return 0
        self._streak = 0
        # Halve on the way down (the overrun is usually large), step back up
        # one at a time so recovery cannot immediately re-saturate.
        before = self.current
        if over:
            # This level does not work here — remember it, so recovery does
            # not sprint straight back into it. Cost is monotone in region
            # count (if `before` regions is too slow, so is anything above it),
            # so while the mark stands it only ever moves DOWN. Shedding from
            # at-or-above it means a probe we already paid for has failed
            # again: charge more for the next one.
            if self._ceiling is not None and before >= self._ceiling:
                self._ceiling_hits += 1
            else:
                self._ceiling_hits = 1
            self._ceiling = (
                before if self._ceiling is None else min(self._ceiling, before)
            )
            self._held = 0
            self.current = max(self.floor, self.current // 2)
        else:
            self.current = min(self.configured, self.current + 1)
        return self.current - before

    def _serve_probation(self, regions_run: int, over: bool) -> None:
        """Clear the remembered ceiling once the level has actually proved out.

        Arriving at the level is not proof. A quiet frame costs almost nothing
        at *any* budget because barely any regions run, so a quiet stretch will
        happily carry the budget back to a level that the next burst of motion
        cannot afford — which is precisely the loop this class exists to break.
        Only a frame that really ran ``ceiling`` regions and came in on time
        tests the ceiling.

        Sustain enough of those and the evidence is genuinely stale: the box
        freed up, the scene changed, whatever. Then the mark comes off and
        ordinary growth resumes. Nothing is pinned permanently.
        """
        if self._ceiling is None or self.current < self._ceiling:
            self._held = 0
            return
        if regions_run < self._ceiling or over:
            return                              # proves nothing about this level
        self._held += 1
        if self._held >= self.probation_frames:
            self._ceiling = None
            self._ceiling_hits = 0
            self._held = 0

    def _evidence_needed(self) -> int:
        """Frames of agreeing evidence required before the next step.

        Shedding is urgent and stepping up into room we have no reason to
        distrust is cheap, so both take the ordinary streak. Stepping up to a
        level that has already blown the budget is a *probe*, and a wrong probe
        is expensive: the overrun stalls the worker, ffmpeg's pipe fills and
        MediaMTX drops the reader. So a probe has to be paid for in evidence.

        Without this the controller was memoryless — it halved away from 8,
        climbed 1 -> 8 in about twenty seconds, blew the budget at 8 again, and
        repeated every three minutes forever (sixteen such cuts in one 35-minute
        run, every one of them from 8). Same additive-increase/multiplicative-
        decrease law, but now it converges on the largest level that actually
        works instead of orbiting the one that does not.
        """
        if self._streak > 0:                                   # shedding
            return self.settle_frames
        if self._ceiling is None or self.current + 1 < self._ceiling:
            return self.settle_frames
        backoff = min(self._ceiling_hits, self.max_probe_backoff)
        return self.settle_frames * self.probe_factor * max(1, backoff)
