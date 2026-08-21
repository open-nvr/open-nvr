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
) -> list[Box]:
    """One square region per motion box and per active track, de-duplicated.

    Bounds how much is ever sent to the detector each frame.
    """
    kept: list[Box] = []
    for b in list(motion_boxes) + list(track_boxes):
        r = calculate_region(frame_shape, b[0], b[1], b[2], b[3], min_region, multiplier)
        if all(intersection_over_union(r, k) < dedup_iou for k in kept):
            kept.append(r)
    return kept


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
        _t = time.monotonic()
        regions = select_regions(
            motion_boxes, track_boxes, frame_shape, self.min_region, self.region_multiplier
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
                raws = self.detector.detect(crop)
                dets.extend(detections_to_frame(raws, region))
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
            skipped_stationary=skipped,
        )

    def run(self, on_tracks: OnTracks | None = None) -> None:
        """Consume the frame source forever (bounded in tests by a finite source)."""
        for frame in self.frame_source.stream():
            result = self.process_frame(frame)
            if on_tracks is not None:
                on_tracks(frame, result.tracks, result.calibrating)
