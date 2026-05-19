# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Point-in-polygon + bbox-center tests."""
from __future__ import annotations

import pytest

from zone import Point, Zone, bbox_center


def _square() -> Zone:
    """A unit-square zone for quick inside/outside checks."""
    return Zone(
        name="unit-square",
        polygon=[Point(0, 0), Point(10, 0), Point(10, 10), Point(0, 10)],
    )


def _concave_arrow() -> Zone:
    """A concave (arrow-shape) zone — exercises the odd-crossings rule."""
    return Zone(
        name="arrow",
        polygon=[
            Point(0, 0),
            Point(10, 5),   # tip
            Point(0, 10),
            Point(3, 5),    # inner concave point
        ],
    )


# ── Construction ───────────────────────────────────────────────────


def test_zone_requires_three_vertices():
    with pytest.raises(ValueError, match="3 vertices"):
        Zone(name="bad", polygon=[Point(0, 0), Point(1, 1)])


def test_zone_from_config_parses_list_of_pairs():
    z = Zone.from_config("z1", [[0, 0], [10, 0], [10, 10]])
    assert z.name == "z1"
    assert len(z.polygon) == 3
    assert z.polygon[0] == Point(0, 0)


# ── Inside / outside (convex) ──────────────────────────────────────


def test_point_clearly_inside():
    assert _square().contains(Point(5, 5))


def test_point_clearly_outside():
    z = _square()
    assert not z.contains(Point(15, 5))
    assert not z.contains(Point(5, 15))
    assert not z.contains(Point(-1, 5))


def test_point_on_edge_treated_as_inside():
    """We deliberately treat edge points as inside (intrusion bias)."""
    z = _square()
    assert z.contains(Point(5, 0))   # bottom edge
    assert z.contains(Point(10, 5))  # right edge
    assert z.contains(Point(0, 0))   # corner


# ── Inside / outside (concave) ─────────────────────────────────────


def test_concave_arrow_inside_legitimate():
    z = _concave_arrow()
    # Inside the body of the arrow (not the notch)
    assert z.contains(Point(2, 2))


def test_concave_arrow_outside_in_notch():
    z = _concave_arrow()
    # The notch cuts inward at (3, 5) → a point slightly to the
    # right of the notch but still in the "indentation" should be
    # OUTSIDE the polygon.
    assert not z.contains(Point(2, 5))  # in the notch — outside arrow shape


# ── bbox_center ────────────────────────────────────────────────────


def test_bbox_center_normalizes_to_pixels():
    # Normalized bbox covers (x=0.2, y=0.3, w=0.4, h=0.2) → center at
    # (0.2 + 0.2, 0.3 + 0.1) = (0.4, 0.4) in normalized space.
    # On 1920x1080 → (768, 432).
    center = bbox_center({"x": 0.2, "y": 0.3, "w": 0.4, "h": 0.2}, 1920, 1080)
    assert center.x == pytest.approx(768.0)
    assert center.y == pytest.approx(432.0)


def test_bbox_center_at_origin():
    center = bbox_center({"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5}, 1000, 1000)
    assert center.x == pytest.approx(250.0)
    assert center.y == pytest.approx(250.0)


# ── Combined: detection-in-zone flow ───────────────────────────────


def test_normalized_detection_inside_polygon():
    # Detection bbox center (768, 432) on 1920x1080 frame; zone is the
    # rectangle (500, 300) - (1000, 600) — center is inside.
    zone = Zone.from_config("z", [[500, 300], [1000, 300], [1000, 600], [500, 600]])
    center = bbox_center({"x": 0.3, "y": 0.35, "w": 0.1, "h": 0.1}, 1920, 1080)
    assert zone.contains(center)


def test_normalized_detection_outside_polygon():
    zone = Zone.from_config("z", [[500, 300], [1000, 300], [1000, 600], [500, 600]])
    # Center deep in the top-left corner of the frame.
    center = bbox_center({"x": 0.0, "y": 0.0, "w": 0.05, "h": 0.05}, 1920, 1080)
    assert not zone.contains(center)


# ── Defensive bbox parsing (regression for self-review SR-4) ──────


def test_bbox_center_treats_missing_keys_as_zero():
    """If a detection has a partial bbox (programmer error or spec
    drift), bbox_center returns Point(0, 0) instead of crashing."""
    center = bbox_center({}, 1920, 1080)
    assert center.x == 0.0
    assert center.y == 0.0


def test_bbox_center_handles_non_numeric_values():
    """String or None values in the bbox dict default to 0 rather
    than raising — the detector then filters the (0, 0) corner case
    via the zone check."""
    center = bbox_center({"x": "not-a-number", "y": None, "w": 0.1, "h": 0.1}, 1000, 1000)
    # x defaults to 0, w=0.1 → center.x = 50; y defaults to 0, h=0.1 → center.y = 50
    assert center.x == pytest.approx(50.0)
    assert center.y == pytest.approx(50.0)
