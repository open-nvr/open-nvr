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
from .regions import Box, calculate_region, contained_share, get_min_region_size, intersection_over_union
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
    # Detections dropped THIS frame because they stood inside an object
    # mask (exported as tier0_object_masked_total; see masks.py).
    masked_detections: int = 0
    # Detections dropped THIS frame by the camera's per-label size, shape
    # or score filters (exported as tier0_object_filtered_total; tuning.py).
    filtered_detections: int = 0
    # Motion-gate state, for metrics. ``motion_forced_exit`` is a per-frame
    # EDGE (did the calibration deadline fire on this frame?), not the
    # detector's running total — a cumulative value fed to a Prometheus
    # ``inc`` would compound every frame. ``motion_latched_open`` says the
    # gate gave up on a scene with no static background.
    motion_forced_exit: bool = False
    motion_latched_open: bool = False
    # Per-stage wall time for the frame (decode/motion/region/detect/track) — lets
    # the test-bed see *where* time goes, not just the end-to-end total.
    stage_latency_s: dict[str, float] = field(default_factory=dict)


#: IoU above which two boxes of DIFFERENT labels are the same object.
#: Well above the per-label threshold: a person standing in front of a car
#: overlaps it partially and must keep both boxes; a vehicle the detector
#: reads as "car" AND "truck" produces two near-identical boxes and must
#: not — that double box was the most visible overlay inaccuracy, and the
#: tracker (which matches per label) spawned two tracks for it.
CROSS_LABEL_IOU = 0.85
#: A same-label box with at least this share of its own area inside a larger
#: kept box is a PART of that object (a head-and-shoulders box on a person
#: whose full body is already boxed), not a second one. IoU never catches
#: it: a part is small, so its IoU with the whole is low. 0 or 1 = off.
CONTAINMENT = 0.85
#: A part spans (nearly) the width of its whole: an upper body is as wide
#: as the person. A PERSON inside a wider box is not a part of it, it is
#: one of several people the detector boxed together as a group (three
#: people crossing side by side, Ventuno Q 2026-11-05: the group box
#: swallowed the one in the middle). The inner box must be at least this
#: share of the outer box's width to count as a part.
PART_MIN_WIDTH_SHARE = 0.6


def nms(
    dets: list[Detection],
    iou_threshold: float = 0.5,
    cross_label_iou: float | None = CROSS_LABEL_IOU,
    containment: float | None = CONTAINMENT,
) -> list[Detection]:
    """Greedy non-max suppression; keeps the highest score.

    Same-label boxes are suppressed above ``iou_threshold``. Boxes of a
    different label are suppressed only above ``cross_label_iou`` — near
    coincidence, i.e. one object the model labelled two ways. ``None``,
    ``<= 0`` or ``>= 1`` disables the cross-label rule; a value below
    ``iou_threshold`` is raised to it, since suppressing ACROSS labels
    more eagerly than within one is never the intent.

    A same-label box that is smaller than a kept box, lies at least
    ``containment`` of its own area inside it AND spans at least
    PART_MIN_WIDTH_SHARE of its width is a part of that object — the
    detector's second box on one person's upper body — and is dropped
    whatever its IoU, which for a part is always low. Measured on the
    Ventuno Q (2026-11-05): with IoU alone one walk past the camera posted
    two or three visits per pass, each with its own recording fetch and
    photo. The width test keeps a person who stands inside a wider box the
    detector drew around a group: a part is as wide as its whole, a person
    in a group is a third of it. ``None``, ``<= 0`` or ``>= 1`` disables
    the rule. A person in front of a car is a different label and never
    affected.

    The losing label is gone from ``FrameResult.detections`` too, so a
    benchmark scoring per-label recall (evalcmp) measures this policy as
    well as the model; run such comparisons with the rule off
    (``DETECT_NMS_CROSS_LABEL_IOU=0``).
    """
    cross: float | None = None
    if cross_label_iou is not None and 0.0 < cross_label_iou < 1.0:
        cross = max(cross_label_iou, iou_threshold)
    contain: float | None = None
    if containment is not None and 0.0 < containment < 1.0:
        contain = containment
    kept: list[Detection] = []
    for d in sorted(dets, key=lambda x: x.score, reverse=True):
        duplicate = False
        for k in kept:
            limit = iou_threshold if k.label == d.label else cross
            if limit is not None and intersection_over_union(d.box, k.box) > limit:
                duplicate = True
                break
        if not duplicate:
            kept.append(d)
    if contain is None or len(kept) < 2:
        return kept
    # The part rule, by SIZE rather than score: the detector often scores
    # the upper body higher than the whole person (the face is the clearest
    # part), so a score-ordered pass would keep the part and let the whole
    # through. Largest first; a same-label box mostly inside a larger
    # survivor is a part of it.
    # Two shapes of containment, told apart by COUNT. A box with two or
    # more same-label boxes inside it is a GROUP box: the detector drew one
    # box around people crossing together (three of them, Ventuno Q
    # 2026-11-05) beside the box per person; the group box is the duplicate
    # and the people are kept. A box with exactly one same-label box inside
    # it that is about as wide is a whole with its PART (an upper body); the
    # part is the duplicate. One narrow box inside another is left alone: a
    # person in a group box whose other members were missed, or a sliver,
    # and the two cannot be told apart from geometry.
    by_size = sorted(kept, key=lambda x: _area(x.box), reverse=True)
    dropped: set[int] = set()
    for i, outer in enumerate(by_size):
        if i in dropped:
            continue
        inside = [j for j in range(i + 1, len(by_size))
                  if j not in dropped and by_size[j].label == outer.label
                  and contained_share(by_size[j].box, outer.box) >= contain]
        if len(inside) >= 2:
            dropped.add(i)                            # a group box
        elif len(inside) == 1 and is_part_of(by_size[inside[0]].box, outer.box, contain):
            dropped.add(inside[0])                    # a part of this one
    whole = [d for j, d in enumerate(by_size) if j not in dropped]
    # back in score order, as callers expect
    return [d for d in kept if any(d is w for w in whole)]


def _area(b: Box) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def is_part_of(inner: Box, outer: Box, containment: float = CONTAINMENT) -> bool:
    """Is ``inner`` a part of the object in ``outer``: mostly inside it and
    about as wide (see PART_MIN_WIDTH_SHARE)?"""
    if contained_share(inner, outer) < containment:
        return False
    iw, ow = max(0.0, inner[2] - inner[0]), max(0.0, outer[2] - outer[0])
    return ow > 0 and iw / ow >= PART_MIN_WIDTH_SHARE


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
        cross_label_iou: float | None = CROSS_LABEL_IOU,
        containment: float | None = CONTAINMENT,
        object_mask: Callable[[list[Detection]], list[Detection]] | None = None,
        object_filter: Callable[[list[Detection]], list[Detection]] | None = None,
    ) -> None:
        self.frame_source = frame_source
        # The camera's per-label size/shape/score filters (tuning.py), run
        # after NMS and the object mask so one object is judged once.
        self.object_filter = object_filter
        # The operator's object masks (masks.ObjectMaskFilter): detections
        # standing inside one are dropped before they can start a track.
        self.object_mask = object_mask
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
        # See nms(): one object the model labels two ways is one box.
        # DETECT_NMS_CROSS_LABEL_IOU; 0 or 1 turns the rule off.
        self.cross_label_iou = cross_label_iou
        # See nms(): a same-label box mostly inside a larger one is a part.
        # DETECT_NMS_CONTAINMENT; 0 or 1 turns the rule off.
        self.containment = containment
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
            return FrameResult(
                tracks, motion_boxes, [], True, stage_latency_s=stages,
                motion_forced_exit=getattr(
                    self.motion, "forced_exit_this_frame", False),
                motion_latched_open=getattr(
                    self.motion, "latched_open", False),
            )

        frame_shape = (frame.height, frame.width)
        self._frame_idx += 1
        # (last_matched, id, box): the order the budget will consider them in.
        candidates: list[tuple[float, int, Box]] = []
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
            candidates.append((getattr(t, "last_matched", 0.0), t.id, t.box))
        # Under a region budget the reserve goes to the tracks that have
        # WAITED LONGEST since their last positive match. This used to be a
        # round-robin by frame index (`frame_idx % len(track_boxes)`), but
        # the candidate count changes every frame as tracks spawn, expire
        # and get stationary-skipped, so the modulo aliased and some tracks
        # were skipped again and again: measured on a busy dashcam scene
        # with the budget shed to 4 regions, present objects went 6–11 s
        # between re-verifications while the median was under 3 s. Oldest
        # first is deterministic and bounds the wait at ~(tracks / reserve)
        # frames for everyone; the id breaks ties so equal ages stay stable.
        if self.max_regions > 0 and len(candidates) > 1:
            candidates.sort(key=lambda c: (c[0], c[1]))
        track_boxes: list[Box] = [c[2] for c in candidates]
        _t = time.monotonic()
        regions, regions_capped = select_regions(
            motion_boxes, track_boxes, frame_shape, self.min_region, self.region_multiplier,
            max_regions=self.max_regions or None,
        )
        stages["region"] = time.monotonic() - _t

        dets: list[Detection] = []
        bgr = None
        masked = 0
        filtered = 0
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
            if self.object_mask is not None and dets:
                before = len(dets)
                dets = self.object_mask(dets)
                masked = before - len(dets)
            dets = nms(dets, cross_label_iou=self.cross_label_iou, containment=self.containment)
            if self.object_filter is not None and dets:
                before = len(dets)
                dets = self.object_filter(dets)
                filtered = before - len(dets)
            detect_latency_s = time.monotonic() - _t   # pure detector time (this model)
            stages["detect"] = detect_latency_s

        # bgr is passed so the tracker can retain each track's best-frame crop
        # for Tier-1 dispatch (#10); None on frames with no detections.
        _t = time.monotonic()
        tracks = self.tracker.update(
            dets, bgr, scanned_regions=regions,
            frame_ts=getattr(frame, "ts", None),   # the evidence lookup's key
        )
        stages["track"] = time.monotonic() - _t
        return FrameResult(
            tracks, motion_boxes, regions, False,
            detections=dets, detect_latency_s=detect_latency_s, stage_latency_s=stages,
            skipped_stationary=skipped, regions_capped=regions_capped,
            masked_detections=masked, filtered_detections=filtered,
            track_population=self.tracker.population,
            motion_forced_exit=getattr(
                self.motion, "forced_exit_this_frame", False),
            motion_latched_open=getattr(self.motion, "latched_open", False),
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
    further behind every frame — and it has no way to notice. Until #507 the
    consequence was not just late detections: the worker stopped draining
    ffmpeg's stdout, ffmpeg blocked on the full pipe, MediaMTX dropped the
    reader, and the session died — a camera that looked flaky ("Failed
    reading RTSP data: End of file") when the real cause was us. The decoder
    is drained on its own thread now (frame_source._FrameSlot): a slow
    detector drops frames, counted in ``tier0_frames_dropped_total``, and
    never the session. What this controller buys is detecting on MORE of the
    frames the camera decodes, and not spending CPU on regions the box
    cannot afford.

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
