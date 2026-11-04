# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Per-camera tuning: the knobs that used to be one env value for every camera.

A road camera and an indoor camera want different analysis rates, motion
thresholds and object sizes, and the ``DETECT_*`` env knobs are one value
for the whole pipeline. Core keeps a ``detection_settings`` record per
camera and hands it to the pipeline in the sources listing as
``detection``; this module turns it into a ``Tuning`` on the camera spec.
Every field is optional and ``None`` means "inherit the global env value",
so a camera with nothing set behaves exactly as before.

Object filters are per label (Frigate's ``objects.filters``): a detection
of that label is dropped when its box area is outside ``min_area`` /
``max_area`` (fractions of the frame area, so the same numbers hold on
the substream and the main stream), its width/height ratio is outside
``min_ratio`` / ``max_ratio``, or its score is below ``min_score``. They
run after NMS and the object mask, before tracking, and what they drop is
counted in ``tier0_object_filtered_total``.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace

from .motion import MotionConfig

log = logging.getLogger(__name__)

FPS_RANGE = (1, 30)
MOTION_THRESHOLD_RANGE = (1, 255)
CONTOUR_AREA_RANGE = (1, 100_000)
STATIONARY_INTERVAL_RANGE = (0, 600)
RATIO_RANGE = (0.01, 100.0)


@dataclass(frozen=True)
class ObjectFilter:
    """Size, shape and score limits for one label. ``None`` = no limit."""

    min_area: float | None = None     # fraction of the frame area, 0..1
    max_area: float | None = None
    min_ratio: float | None = None    # width / height
    max_ratio: float | None = None
    min_score: float | None = None    # 0..1

    @property
    def empty(self) -> bool:
        return all(v is None for v in (self.min_area, self.max_area, self.min_ratio,
                                       self.max_ratio, self.min_score))


@dataclass(frozen=True)
class Tuning:
    """One camera's overrides. Hashable, so a change is a spec change and
    the worker restarts on the next reconcile tick."""

    fps: int | None = None
    motion_enabled: bool | None = None
    motion_threshold: int | None = None
    motion_contour_area: int | None = None
    min_spawn_score: float | None = None
    stationary_interval: int | None = None
    # Which stream this camera detects on: "auto" (the sub when it is
    # tall enough for the detector, else the main), "sub" (the sub even
    # when small — the operator's call), "main" (full-res decode, full-res
    # evidence from the same frame). None = auto.
    detect_stream: str | None = None
    object_filters: tuple[tuple[str, ObjectFilter], ...] = ()

    @property
    def is_default(self) -> bool:
        return self == Tuning()

    def motion_config(self, base: MotionConfig) -> MotionConfig:
        """``base`` (the env-derived config) with this camera's overrides."""
        changes = {}
        if self.motion_enabled is not None:
            changes["enabled"] = self.motion_enabled
        if self.motion_threshold is not None:
            changes["threshold"] = self.motion_threshold
        if self.motion_contour_area is not None:
            changes["contour_area"] = self.motion_contour_area
        return replace(base, **changes) if changes else base

    def filters(self) -> dict[str, ObjectFilter]:
        return dict(self.object_filters)

    def describe(self) -> str:
        """The overrides, for the worker's start-up log line."""
        bits = []
        for name in ("fps", "motion_enabled", "motion_threshold", "detect_stream",
                     "motion_contour_area", "min_spawn_score", "stationary_interval"):
            v = getattr(self, name)
            if v is not None:
                bits.append(f"{name}={v}")
        if self.object_filters:
            bits.append(f"object_filters={','.join(lbl for lbl, _ in self.object_filters)}")
        return " ".join(bits) or "-"


# ── parsing ──────────────────────────────────────────────────────────

def _num(v, lo, hi, *, integer=False):
    """``v`` as a number within ``[lo, hi]``, else None (never raises; a
    bad value is ignored, not "fixed")."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or not (lo <= f <= hi):
        return None
    return int(f) if integer else f


def _bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    return None


def parse_object_filter(raw) -> ObjectFilter | None:
    if not isinstance(raw, Mapping):
        return None
    f = ObjectFilter(
        min_area=_num(raw.get("min_area"), 0.0, 1.0),
        max_area=_num(raw.get("max_area"), 0.0, 1.0),
        min_ratio=_num(raw.get("min_ratio"), *RATIO_RANGE),
        max_ratio=_num(raw.get("max_ratio"), *RATIO_RANGE),
        min_score=_num(raw.get("min_score"), 0.0, 1.0),
    )
    return None if f.empty else f


def parse_tuning(raw) -> Tuning:
    """The ``detection`` field of a camera dict → Tuning. Anything absent,
    malformed or out of range inherits the global value."""
    if not isinstance(raw, Mapping):
        return Tuning()
    filters = []
    raw_filters = raw.get("object_filters")
    if isinstance(raw_filters, Mapping):
        for label, spec in raw_filters.items():
            f = parse_object_filter(spec)
            lbl = str(label).strip().lower()
            if f is not None and lbl:
                filters.append((lbl, f))
            elif spec:
                log.warning("object filter for %r ignored: %r", label, spec)
    return Tuning(
        fps=_num(raw.get("fps"), *FPS_RANGE, integer=True),
        detect_stream=(raw.get("detect_stream") if raw.get("detect_stream") in ("auto", "sub", "main") else None),
        motion_enabled=_bool(raw.get("motion_enabled")),
        motion_threshold=_num(raw.get("motion_threshold"), *MOTION_THRESHOLD_RANGE,
                              integer=True),
        motion_contour_area=_num(raw.get("motion_contour_area"), *CONTOUR_AREA_RANGE,
                                 integer=True),
        min_spawn_score=_num(raw.get("min_spawn_score"), 0.0, 1.0),
        stationary_interval=_num(raw.get("stationary_interval"),
                                 *STATIONARY_INTERVAL_RANGE, integer=True),
        object_filters=tuple(sorted(filters)),
    )


# ── the filter ───────────────────────────────────────────────────────

class ObjectSizeFilter:
    """Drop detections outside their label's size, shape or score limits.

    ``__call__(dets)`` returns the detections to keep; ``dropped`` counts
    the rest (cumulative; the pipeline reads the per-frame delta).
    """

    def __init__(self, filters: Mapping[str, ObjectFilter], frame_shape: tuple[int, int]) -> None:
        self.filters = {str(k).lower(): v for k, v in filters.items()}
        h, w = int(frame_shape[0]), int(frame_shape[1])
        self._frame_area = float(max(1, h * w))
        self.dropped = 0

    def reason(self, label: str, box, score: float) -> str | None:
        """Why this detection is dropped, or None when it passes."""
        f = self.filters.get(str(label).lower())
        if f is None:
            return None
        x1, y1, x2, y2 = box
        bw, bh = max(0.0, float(x2 - x1)), max(0.0, float(y2 - y1))
        area = bw * bh / self._frame_area
        if f.min_area is not None and area < f.min_area:
            return "min_area"
        if f.max_area is not None and area > f.max_area:
            return "max_area"
        if f.min_ratio is not None or f.max_ratio is not None:
            ratio = bw / bh if bh > 0 else math.inf
            if f.min_ratio is not None and ratio < f.min_ratio:
                return "min_ratio"
            if f.max_ratio is not None and ratio > f.max_ratio:
                return "max_ratio"
        if f.min_score is not None and float(score) < f.min_score:
            return "min_score"
        return None

    def __call__(self, dets):
        kept = []
        for d in dets:
            if self.reason(d.label, d.box, d.score) is None:
                kept.append(d)
            else:
                self.dropped += 1
        return kept
