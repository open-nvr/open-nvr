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
4. else: score better by > 0.05 wins.
5. else: area > 10% larger wins.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .regions import Box, area


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

    @property
    def area(self) -> int:
        return area(self.box)


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

    # score better by more than 5%
    if new.score > current.score + 0.05:
        return True

    # area more than 10% larger
    if new.area > current.area * 1.1:
        return True

    return False
