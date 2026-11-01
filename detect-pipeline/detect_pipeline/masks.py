# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Per-camera masks: the parts of a picture Tier-0 must ignore.

Two kinds, both polygons normalised to 0..1 of the frame (x right, y down —
the same space as core's zones), drawn by the operator on a snapshot:

* a **motion mask** blinds the motion gate. Pixels under it never count as
  motion, so a burned-in OSD clock, a tree in the wind, a road at the edge
  of the frame or a flag never wakes the object detector. It changes
  nothing about what the detector sees once something else moves — a
  person walking under a masked tree is still found, because the region
  the detector scans is cut from the full frame.
* an **object mask** drops a detection whose bottom-centre — where the
  object stands — falls inside it, before it can start a track: the TV
  screen, the poster, the car parked across the street. Optionally only
  for some labels.

Frigate has the same two (``motion.mask`` and ``objects.filters.*.mask``);
the semantics here match so an operator coming from there finds what they
expect. Polygons are rasterised once per worker at the detect frame's size,
so the per-frame cost is a boolean AND on the motion image and one array
lookup per detection.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import cv2
import numpy as np

log = logging.getLogger(__name__)

#: ``((x, y), ...)`` normalised to 0..1.
Polygon = tuple[tuple[float, float], ...]

MIN_POINTS = 3
MAX_POINTS = 64


@dataclass(frozen=True)
class ObjectMask:
    """One object mask: a polygon, optionally limited to some labels."""

    polygon: Polygon
    labels: frozenset[str] | None = None   # None = every label


def parse_polygon(raw: object) -> Polygon | None:
    """``[[x, y], ...]`` → a normalised polygon, or None when malformed.

    Lenient on shape (lists or tuples, ints or floats) and strict on
    meaning: every coordinate must be a finite number in 0..1 and there
    must be at least three points. A bad polygon is dropped, never
    "fixed" — a mask the operator did not draw must not blind a camera.
    """
    if not isinstance(raw, (list, tuple)) or not (MIN_POINTS <= len(raw) <= MAX_POINTS):
        return None
    pts: list[tuple[float, float]] = []
    for p in raw:
        if not (isinstance(p, (list, tuple)) and len(p) == 2):
            return None
        try:
            x, y = float(p[0]), float(p[1])
        except (TypeError, ValueError):
            return None
        if isinstance(p[0], bool) or isinstance(p[1], bool):
            return None
        if not (np.isfinite(x) and np.isfinite(y) and 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            return None
        pts.append((x, y))
    return tuple(pts)


def parse_motion_masks(raw: object) -> tuple[Polygon, ...]:
    """The ``motion_masks`` field of a camera dict → polygons (bad ones dropped)."""
    if not isinstance(raw, (list, tuple)):
        return ()
    out = []
    for item in raw:
        poly = parse_polygon(item)
        if poly is None:
            log.warning("motion mask ignored: malformed polygon %r", item)
            continue
        out.append(poly)
    return tuple(out)


def parse_object_masks(raw: object) -> tuple[ObjectMask, ...]:
    """The ``object_masks`` field → ObjectMasks. Each item is either a bare
    polygon or ``{"polygon": [...], "labels": [...] | null}``."""
    if not isinstance(raw, (list, tuple)):
        return ()
    out = []
    for item in raw:
        labels = None
        if isinstance(item, dict):
            poly = parse_polygon(item.get("polygon"))
            raw_labels = item.get("labels")
            if isinstance(raw_labels, (list, tuple)):
                labels = frozenset(
                    str(s).strip().lower() for s in raw_labels if str(s).strip()
                ) or None
        else:
            poly = parse_polygon(item)
        if poly is None:
            log.warning("object mask ignored: malformed polygon %r", item)
            continue
        out.append(ObjectMask(poly, labels))
    return tuple(out)


def rasterize(polygons: Iterable[Polygon], shape: tuple[int, int]) -> np.ndarray:
    """A ``(h, w)`` uint8 image, 255 under any of ``polygons``, 0 elsewhere."""
    h, w = int(shape[0]), int(shape[1])
    img = np.zeros((h, w), np.uint8)
    pts = []
    for poly in polygons:
        if len(poly) < MIN_POINTS:
            continue
        # Normalised → pixel, inclusive of the last row/column so a mask
        # drawn to the frame edge really reaches it.
        arr = np.array(
            [[round(x * (w - 1)), round(y * (h - 1))] for x, y in poly], np.int32
        )
        pts.append(arr)
    if pts:
        cv2.fillPoly(img, pts, 255)
    return img


class ObjectMaskFilter:
    """Drop detections standing inside an object mask.

    ``__call__(dets)`` returns the detections to keep and counts the rest
    in ``dropped`` (cumulative; the pipeline reads the per-frame delta).
    The test point is the bottom-centre of the box, clamped into the
    frame — a box that touches the mask with its top edge (a person
    walking in front of a masked wall) is kept.
    """

    def __init__(self, masks: Sequence[ObjectMask], frame_shape: tuple[int, int]) -> None:
        self.frame_shape = (int(frame_shape[0]), int(frame_shape[1]))
        self._any = rasterize(
            [m.polygon for m in masks if m.labels is None], self.frame_shape
        )
        self._by_label: dict[str, np.ndarray] = {}
        per_label: dict[str, list[Polygon]] = {}
        for m in masks:
            for lbl in m.labels or ():
                per_label.setdefault(lbl, []).append(m.polygon)
        for lbl, polys in per_label.items():
            self._by_label[lbl] = rasterize(polys, self.frame_shape)
        self.count = len(masks)
        self.dropped = 0

    def covers(self, label: str, box) -> bool:
        h, w = self.frame_shape
        x1, y1, x2, y2 = box
        x = min(max(int((x1 + x2) // 2), 0), w - 1)
        y = min(max(int(y2), 0), h - 1)
        if self._any[y, x]:
            return True
        img = self._by_label.get(str(label).lower())
        return bool(img is not None and img[y, x])

    def __call__(self, dets):
        kept = []
        for d in dets:
            if self.covers(d.label, d.box):
                self.dropped += 1
            else:
                kept.append(d)
        return kept
