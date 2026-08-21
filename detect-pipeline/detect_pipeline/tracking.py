# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Object tracking for the Tier-0 pipeline.

Design intent (see docs/design/compute-gated-inference.md) and the Frigate source
review (frigate/track/norfair_tracker.py, MIT 6f80bcd19) call out one load-bearing
property: matching uses a **size-aware bottom-center distance**, NOT centroid-IoU,
because centroid-IoU swaps IDs between nearby same-class objects. This module
implements that metric with greedy nearest-match assignment plus the track
lifecycle (initialization delay, max-disappeared) and a ``motionless_count`` so a
later stage (PR B) can treat settled objects as stationary and skip re-detecting
them.

Deliberately lean: this is greedy matching over the size-aware distance, not a
full Norfair/Kalman port. Kalman motion prediction (Frigate's per-class R/Q
tuning) is a worthwhile follow-up but is not required for the metric that
prevents ID churn. Kept dependency-light on purpose.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import count

from .regions import Box, intersection_over_union
from .thumbnail import ThumbCandidate, is_better_thumbnail


@dataclass(frozen=True)
class Detection:
    """A detector result for one object in one frame."""

    label: str
    box: Box
    score: float


@dataclass
class TrackConfig:
    """Lifecycle + matching tunables. Frigate ties these to detect fps."""

    fps: int = 5
    # consecutive hits before a track is "confirmed" (Frigate: min_initialized = fps//2)
    min_initialized: int | None = None
    # frames a confirmed track survives unmatched (Frigate: ~fps*5)
    max_disappeared: int | None = None
    # size-aware distance below which a detection matches a track (Frigate ~2.5)
    distance_threshold: float = 2.5
    # per-class distance override (Frigate tunes car/license_plate/person)
    per_class_threshold: dict[str, float] = field(default_factory=dict)
    # IoU above which a matched box counts as "not moved" this frame
    motionless_iou: float = 0.9
    # motionless frames before a track is considered stationary (Frigate: 50)
    stationary_threshold: int = 50

    def initialized(self) -> int:
        return self.min_initialized if self.min_initialized is not None else max(1, self.fps // 2)

    def disappeared(self) -> int:
        return self.max_disappeared if self.max_disappeared is not None else max(1, self.fps * 5)

    def threshold_for(self, label: str) -> float:
        return self.per_class_threshold.get(label, self.distance_threshold)


@dataclass
class Track:
    id: int
    label: str
    box: Box
    score: float
    hits: int = 1
    misses: int = 0
    age: int = 1
    motionless_count: int = 0
    confirmed: bool = False
    best: ThumbCandidate | None = None
    stationary_threshold: int = 50
    # BGR crop of the best frame, retained only when the tracker is fed pixels
    # (the Tier-1 gate dispatches THIS on escalation — see gate/dispatch, PR B #10).
    best_crop: object | None = field(default=None, repr=False, compare=False)

    @property
    def stationary(self) -> bool:
        """True once the object has been motionless long enough (PR B skips
        re-detecting stationary tracks; here we only track the count)."""
        return self.motionless_count >= self.stationary_threshold


def bottom_center_distance(a: Box, b: Box) -> float:
    """Size-aware distance between two boxes.

    Compares bottom-center position (normalized by the target box's size) plus
    width/height ratios — the same shape as Frigate's custom Norfair distance, so
    a car passing a parked car does not get merged.
    """
    a_cx = (a[0] + a[2]) / 2.0
    b_cx = (b[0] + b[2]) / 2.0
    a_bottom, b_bottom = float(a[3]), float(b[3])
    a_w, a_h = max(a[2] - a[0], 1), max(a[3] - a[1], 1)
    b_w, b_h = max(b[2] - b[0], 1), max(b[3] - b[1], 1)

    dx = (a_cx - b_cx) / b_w
    dy = (a_bottom - b_bottom) / b_h
    dw = abs(a_w - b_w) / b_w
    dh = abs(a_h - b_h) / b_h
    return math.sqrt(dx * dx + dy * dy + dw * dw + dh * dh)


def _coverage(inner: Box, outer: Box) -> float:
    """Fraction of ``inner``'s area that lies inside ``outer`` (0..1)."""
    ix1, iy1 = max(inner[0], outer[0]), max(inner[1], outer[1])
    ix2, iy2 = min(inner[2], outer[2]), min(inner[3], outer[3])
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    area = max((inner[2] - inner[0]) * (inner[3] - inner[1]), 1)
    return inter / area


def _crop_bgr(bgr, box: Box):
    """Clamp ``box`` to the frame and return a copied BGR crop, or None if empty."""
    h, w = bgr.shape[:2]
    x1 = max(0, min(int(box[0]), w - 1))
    y1 = max(0, min(int(box[1]), h - 1))
    x2 = max(x1 + 1, min(int(box[2]), w))
    y2 = max(y1 + 1, min(int(box[3]), h))
    crop = bgr[y1:y2, x1:x2]
    return crop.copy() if crop.size else None


class Tracker:
    """Greedy, size-aware multi-object tracker."""

    def __init__(self, frame_shape: tuple[int, int], config: TrackConfig | None = None) -> None:
        self.frame_shape = frame_shape  # (h, w)
        self.config = config or TrackConfig()
        self._tracks: list[Track] = []
        self._ids = count(1)

    @property
    def tracks(self) -> list[Track]:
        """Confirmed tracks only (past the initialization delay)."""
        return [t for t in self._tracks if t.confirmed]

    def update(
        self, detections: list[Detection], bgr=None,
        scanned_regions: list[Box] | None = None,
    ) -> list[Track]:
        # ``bgr`` (the full-frame BGR image) is optional: when given, the best
        # frame's crop pixels are retained on the track for Tier-1 dispatch.
        #
        # ``scanned_regions`` is the PR B coasting contract: the regions the
        # detector actually LOOKED AT this frame. An unmatched track only
        # counts a miss if its box intersects a scanned region — being
        # missed where we looked is evidence of absence; being unmatched
        # because its region was skipped (stationary gating) is not. None
        # (the default, and every pre-PR-B caller) preserves the original
        # behavior: every unmatched track counts a miss.
        cfg = self.config
        unmatched_tracks = set(range(len(self._tracks)))
        unmatched_dets = set(range(len(detections)))

        # candidate pairs (same label, within per-class distance threshold)
        pairs: list[tuple[float, int, int]] = []
        for ti, tr in enumerate(self._tracks):
            for di, det in enumerate(detections):
                if det.label != tr.label:
                    continue
                dist = bottom_center_distance(tr.box, det.box)
                if dist <= cfg.threshold_for(det.label):
                    pairs.append((dist, ti, di))
        pairs.sort()

        for _dist, ti, di in pairs:
            if ti in unmatched_tracks and di in unmatched_dets:
                self._match(self._tracks[ti], detections[di], bgr)
                unmatched_tracks.discard(ti)
                unmatched_dets.discard(di)

        # unmatched detections -> new tentative tracks
        for di in unmatched_dets:
            self._spawn(detections[di], bgr)

        # unmatched tracks -> age out (or coast, if never scanned this frame)
        survivors: list[Track] = []
        for ti, tr in enumerate(self._tracks):
            if ti in unmatched_tracks:
                tr.age += 1
                if scanned_regions is not None and not any(
                    _coverage(tr.box, r) >= 0.7 for r in scanned_regions
                ):
                    # Coast: no scanned region substantially covered this
                    # object, so its absence from ``detections`` is not
                    # evidence it left. Mere partial overlap coasts too —
                    # motion NEXT TO a gated stationary object scans a
                    # region that clips it; a half-visible car at a crop
                    # edge going undetected must not accumulate misses, or
                    # someone loitering beside a parked car would get its
                    # track deleted. Re-verification frames still expire a
                    # departed object: the track's own region fully
                    # contains its box (coverage 1.0).
                    survivors.append(tr)
                    continue
                tr.misses += 1
                # tentative tracks die on a single miss; confirmed survive to max_disappeared
                if not tr.confirmed or tr.misses > cfg.disappeared():
                    continue
            survivors.append(tr)
        self._tracks = survivors
        return self.tracks

    def _match(self, tr: Track, det: Detection, bgr=None) -> None:
        iou = intersection_over_union(tr.box, det.box)
        if iou >= self.config.motionless_iou:
            tr.motionless_count += 1
        else:
            tr.motionless_count = 0
        tr.box = det.box
        tr.score = det.score
        tr.hits += 1
        tr.age += 1
        tr.misses = 0
        if not tr.confirmed and tr.hits >= self.config.initialized():
            tr.confirmed = True
        self._update_best(tr, det, bgr)

    def _spawn(self, det: Detection, bgr=None) -> None:
        tr = Track(
            id=next(self._ids),
            label=det.label,
            box=det.box,
            score=det.score,
            stationary_threshold=self.config.stationary_threshold,
        )
        tr.confirmed = self.config.initialized() <= 1
        self._update_best(tr, det, bgr)
        self._tracks.append(tr)

    def _update_best(self, tr: Track, det: Detection, bgr=None) -> None:
        cand = ThumbCandidate(label=det.label, box=det.box, score=det.score)
        if tr.best is None or is_better_thumbnail(tr.best, cand, self.frame_shape):
            tr.best = cand
            if bgr is not None:                       # retain the best frame's pixels
                crop = _crop_bgr(bgr, det.box)
                if crop is not None:
                    tr.best_crop = crop
