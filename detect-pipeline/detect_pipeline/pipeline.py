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

Everything runs full-time here — PR A does NOT gate. The gate (skip detection on
stationary tracks) and the Tier-1 dispatch of best crops through KAI-C land in
PR B; this worker only produces tracks + best frames.

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
    ) -> None:
        self.frame_source = frame_source
        self.motion = motion
        self.detector = detector
        self.tracker = tracker
        self.model_size = model_size            # (w, h)
        self.region_multiplier = region_multiplier
        self.min_region = get_min_region_size(model_size[0], model_size[1])

    def process_frame(self, frame) -> FrameResult:
        """Run one frame end-to-end; return tracks + motion boxes + regions."""
        luma = np.frombuffer(frame.y_plane, np.uint8).reshape(frame.height, frame.width)
        motion_boxes = self.motion.detect(luma)

        # While calibrating (warm-up / whole-frame flash) do NOT run the detector;
        # tracks still age. Recording is unaffected (MediaMTX).
        if self.motion.is_calibrating():
            return FrameResult(self.tracker.update([]), motion_boxes, [], True)

        frame_shape = (frame.height, frame.width)
        track_boxes = [t.box for t in self.tracker.tracks]
        regions = select_regions(
            motion_boxes, track_boxes, frame_shape, self.min_region, self.region_multiplier
        )

        dets: list[Detection] = []
        bgr = None
        detect_latency_s: float | None = None
        if regions:
            bgr = to_bgr(frame.data, frame.width, frame.height)
            _t0 = time.monotonic()
            for region in regions:
                crop = crop_and_resize(bgr, region, self.model_size[0], self.model_size[1])
                raws = self.detector.detect(crop)
                dets.extend(detections_to_frame(raws, region))
            dets = nms(dets)
            detect_latency_s = time.monotonic() - _t0   # pure detector time (this model)

        # bgr is passed so the tracker can retain each track's best-frame crop
        # for Tier-1 dispatch (#10); None on frames with no detections.
        return FrameResult(
            self.tracker.update(dets, bgr), motion_boxes, regions, False,
            detections=dets, detect_latency_s=detect_latency_s,
        )

    def run(self, on_tracks: OnTracks | None = None) -> None:
        """Consume the frame source forever (bounded in tests by a finite source)."""
        for frame in self.frame_source.stream():
            result = self.process_frame(frame)
            if on_tracks is not None:
                on_tracks(frame, result.tracks, result.calibrating)
