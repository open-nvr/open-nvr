# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Ported from Frigate's is_better_thumbnail / on_edge / has_better_attr in
# frigate/util/image.py — MIT licensed, Frigate, Inc., reviewed at commit
# 6f80bcd19 (v0.18-beta). See repo-root NOTICE.
"""
Best-frame (thumbnail) selection for a tracked object.

The gate feeds the *expensive* Tier-1 model one crop per tracked object, so which
crop matters: a plain "highest score" pick hands the VLM edge-clipped, face-less
frames. This ports Frigate's attribute-aware priority:

1. person → a better (or first) ``face`` attribute wins; once the current thumb
   has a face, only a better face replaces it.
2. car / motorcycle → same for ``license_plate``.
3. else: a new frame clipped on a frame edge (when the current isn't) loses.
4. else: a frame much blurrier than the current one loses (the blur veto,
   ours: the recording is asked for the best frame's moment, so a blurred
   pick fetches a blurred main frame).
5. else: score better by > 0.05 wins.
6. else: area > 10% larger wins.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .regions import Box, area

# A candidate whose sharpness is below this fraction of the current best's
# does not replace it on score or area alone. 0 turns the veto off.
BLUR_VETO = 0.5
# Sharpness is measured on the crop scaled (up or down) to this longest
# side, so a subject walking closer (a larger box of the same pixels) is
# compared at the same scale rather than looking softer as it grows.
SHARPNESS_SIDE_PX = 96


@dataclass(frozen=True)
class Attribute:
    """A recognized sub-feature of an object (e.g. a face on a person)."""

    label: str
    box: Box


@dataclass(frozen=True)
class ThumbCandidate:
    """One candidate best-frame for a tracked object."""

    label: str                       # object class: "person", "car", ...
    box: Box                         # object box in full-frame coords
    score: float
    attributes: tuple[Attribute, ...] = field(default_factory=tuple)
    sharpness: float = 0.0           # variance of the Laplacian at SHARPNESS_SIDE_PX; 0 = unmeasured

    @property
    def area(self) -> int:
        return area(self.box)


def crop_sharpness(frame, box: Box) -> float:
    """Scale-normalised sharpness of ``box`` in ``frame``: the crop is
    resized so its longest side is SHARPNESS_SIDE_PX, then the variance of
    the Laplacian is taken. 0.0 for anything degenerate; never raises."""
    try:
        import cv2
        x1, y1, x2, y2 = (int(round(v)) for v in box)
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
        if x2 - x1 < 4 or y2 - y1 < 4:
            return 0.0
        crop = frame[y1:y2, x1:x2]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
        scale = SHARPNESS_SIDE_PX / float(max(gray.shape[:2]))
        size = (max(4, int(round(gray.shape[1] * scale))), max(4, int(round(gray.shape[0] * scale))))
        if size != (gray.shape[1], gray.shape[0]):
            gray = cv2.resize(gray, size, interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR)
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())
    except Exception:
        return 0.0


def blur_vetoed(current: ThumbCandidate, new: ThumbCandidate, blur_veto: float = BLUR_VETO) -> bool:
    """True when ``new`` is too blurred, next to ``current``, to replace it
    on score or area alone."""
    return (blur_veto > 0 and current.sharpness > 0 and new.sharpness > 0
            and new.sharpness < current.sharpness * blur_veto)


def on_edge(box: Box, frame_shape: tuple[int, int]) -> bool:
    """True if the box touches any frame edge. ``frame_shape`` is ``(h, w)``."""
    return (
        box[0] == 0
        or box[1] == 0
        or box[2] == frame_shape[1] - 1
        or box[3] == frame_shape[0] - 1
    )


def _max_attr_area(attrs: tuple[Attribute, ...], label: str) -> int:
    return max([0] + [area(a.box) for a in attrs if a.label == label])


def has_better_attr(current: ThumbCandidate, new: ThumbCandidate, label: str) -> bool:
    """True if ``new`` has a larger recognized ``label`` attribute than ``current``."""
    return _max_attr_area(new.attributes, label) > _max_attr_area(current.attributes, label)


def is_better_thumbnail(
    current: ThumbCandidate,
    new: ThumbCandidate,
    frame_shape: tuple[int, int],
    blur_veto: float = BLUR_VETO,
) -> bool:
    """Should ``new`` replace ``current`` as the object's best frame?"""
    label = new.label

    # attribute-bearing crops are preferred, and sticky once acquired
    if label == "person":
        if has_better_attr(current, new, "face"):
            return True
        if any(a.label == "face" for a in current.attributes):
            return False

    if label in ("car", "motorcycle"):
        if has_better_attr(current, new, "license_plate"):
            return True
        if any(a.label == "license_plate" for a in current.attributes):
            return False

    # a newly edge-clipped crop loses to a non-clipped current one
    if on_edge(new.box, frame_shape) and not on_edge(current.box, frame_shape):
        return False

    # much blurrier than the current best: not on score or area alone
    if blur_vetoed(current, new, blur_veto):
        return False

    # score better by more than 5%
    if new.score > current.score + 0.05:
        return True

    # area more than 10% larger
    if new.area > current.area * 1.1:
        return True

    return False
