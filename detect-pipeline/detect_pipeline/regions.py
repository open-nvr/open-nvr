# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Geometry primitives ported from Frigate's frigate/util/image.py — MIT licensed,
# Frigate, Inc., reviewed at commit 6f80bcd19 (v0.18-beta). See repo-root NOTICE.
"""
Region geometry for the Tier-0 pipeline.

The detector never runs on the whole frame — it runs on square *regions* around
motion / tracked objects. Getting region sizing right is what keeps detector
inputs stable and small-object recall high (see the design doc's pitfalls). This
module ports the exact sizing/alignment rules:

* ``min_region_size`` — max(model dimension, 320), /4-aligned for sub-320 models.
* ``calculate_region`` — a bounding box → square region: longest edge × multiplier,
  floored to a multiple of 4, never below the model size, centered and clamped
  inside the frame.
* box helpers (``area``, ``intersection``, ``intersection_over_union``, ``clipped``)
  used by clustering and best-frame logic.

``Box`` is ``(x1, y1, x2, y2)`` in pixels.
"""
from __future__ import annotations

Box = tuple[int, int, int, int]

# Any model at or above this size gets a 320px minimum region so larger models
# can still use small regions to detect small objects (upscaled from the crop).
MIN_REGION_FLOOR = 320


def get_min_region_size(model_width: int, model_height: int) -> int:
    """Minimum region edge for a model of the given input size."""
    largest = max(model_width, model_height)
    if largest < MIN_REGION_FLOOR:
        # normalize to a multiple of 4
        if largest % 4 == 0:
            return largest
        return (largest + 3) // 4 * 4
    return MIN_REGION_FLOOR


def calculate_region(
    frame_shape: tuple[int, int],
    xmin: int,
    ymin: int,
    xmax: int,
    ymax: int,
    model_size: int,
    multiplier: float = 2,
) -> Box:
    """Square region around a box: longest edge × multiplier, /4-aligned, clamped.

    ``frame_shape`` is ``(height, width)``.
    """
    # size is the longest edge, scaled, and divisible by 4
    size = int((max(xmax - xmin, ymax - ymin) * multiplier) // 4 * 4)
    # don't go any smaller than the model size
    if size < model_size:
        size = model_size

    # x_offset = midpoint of box minus half the size, clamped into the frame
    x_offset = int((xmax - xmin) / 2.0 + xmin - size / 2.0)
    if x_offset < 0:
        x_offset = 0
    elif x_offset > (frame_shape[1] - size):
        x_offset = max(0, (frame_shape[1] - size))

    y_offset = int((ymax - ymin) / 2.0 + ymin - size / 2.0)
    if y_offset < 0:
        y_offset = 0
    elif y_offset > (frame_shape[0] - size):
        y_offset = max(0, (frame_shape[0] - size))

    return (x_offset, y_offset, x_offset + size, y_offset + size)


def area(box: Box) -> int:
    """Inclusive pixel area of a box (matches Frigate: +1 on each edge)."""
    return (box[2] - box[0] + 1) * (box[3] - box[1] + 1)


def intersection(box_a: Box, box_b: Box) -> Box | None:
    """Overlap box, or ``None`` if the boxes don't intersect."""
    if (
        box_a[2] < box_b[0]
        or box_a[0] > box_b[2]
        or box_a[1] > box_b[3]
        or box_a[3] < box_b[1]
    ):
        return None
    return (
        max(box_a[0], box_b[0]),
        max(box_a[1], box_b[1]),
        min(box_a[2], box_b[2]),
        min(box_a[3], box_b[3]),
    )


def intersection_over_union(box_a: Box, box_b: Box) -> float:
    """IoU of two boxes in ``[0, 1]``."""
    intersect = intersection(box_a, box_b)
    if intersect is None:
        return 0.0
    inter_area = max(0, intersect[2] - intersect[0] + 1) * max(
        0, intersect[3] - intersect[1] + 1
    )
    if inter_area == 0:
        return 0.0
    box_a_area = (box_a[2] - box_a[0] + 1) * (box_a[3] - box_a[1] + 1)
    box_b_area = (box_b[2] - box_b[0] + 1) * (box_b[3] - box_b[1] + 1)
    return inter_area / float(box_a_area + box_b_area - inter_area)


def clipped(box: Box, region: Box, frame_shape: tuple[int, int]) -> bool:
    """True if ``box`` is within 5px of the ``region`` border and that border is
    not the frame edge — i.e. the object is likely cut off by the crop.

    ``frame_shape`` is ``(height, width)``. Used by best-frame selection to
    prefer un-clipped thumbnails.
    """
    return (
        (region[0] > 5 and box[0] - region[0] <= 5)
        or (region[1] > 5 and box[1] - region[1] <= 5)
        or (frame_shape[1] - region[2] > 5 and region[2] - box[2] <= 5)
        or (frame_shape[0] - region[3] > 5 and region[3] - box[3] <= 5)
    )
