# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Camera zones: named areas of a camera's picture (HA-109).

Coordinates are normalised 0..1 of the frame, x right, y down, the same space
as the overlay boxes. A visit belongs to a zone when a point of its path
(where the object stood: the bottom-centre of its box) lies inside the zone's
polygon, and the zone either has no label filter or lists the visit's label.
"""

from __future__ import annotations

import math
from typing import Any

from sqlalchemy.orm import Session

#: Polygon size limits. Three points is a triangle; 32 is plenty for a
#: hand-drawn area and keeps point-in-polygon cheap at ingest.
MIN_POINTS = 3
MAX_POINTS = 32
#: Most path points accepted from the pipeline for one visit.
MAX_PATH_POINTS = 64
MAX_ZONES_PER_CAMERA = 32


def _is_unit(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) \
        and math.isfinite(v) and 0.0 <= v <= 1.0


def valid_polygon(poly: Any) -> list[list[float]]:
    """The polygon as ``[[x, y], ...]`` floats, or ValueError."""
    if not isinstance(poly, list) or not (MIN_POINTS <= len(poly) <= MAX_POINTS):
        raise ValueError(f"polygon needs {MIN_POINTS}..{MAX_POINTS} points")
    out = []
    for p in poly:
        if not (isinstance(p, (list, tuple)) and len(p) == 2 and all(_is_unit(c) for c in p)):
            raise ValueError("each polygon point is [x, y] with 0 <= x, y <= 1")
        out.append([float(p[0]), float(p[1])])
    if _area(out) < 1e-6:
        raise ValueError("polygon has no area")
    return out


def _area(poly: list[list[float]]) -> float:
    s = 0.0
    for i, (x1, y1) in enumerate(poly):
        x2, y2 = poly[(i + 1) % len(poly)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def point_in_polygon(x: float, y: float, poly: list[list[float]]) -> bool:
    """Even-odd ray cast. Points exactly on an edge may land either side,
    which is fine for "was the object in the zone"."""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def clean_path(path: Any) -> list[tuple[float, float]] | None:
    """The pipeline's path as points, or None if absent or malformed (a bad
    path is dropped, never a reason to refuse the visit)."""
    if not isinstance(path, list) or not path:
        return None
    pts = []
    for p in path[:MAX_PATH_POINTS]:
        if not (isinstance(p, (list, tuple)) and len(p) == 2 and all(_is_unit(c) for c in p)):
            return None
        pts.append((float(p[0]), float(p[1])))
    return pts


def zone_matches(zone, label: str | None, path: list[tuple[float, float]]) -> bool:
    labels = zone.labels
    if labels and (label or "").lower() not in {str(lbl).lower() for lbl in labels}:
        return False
    poly = zone.polygon or []
    return any(point_in_polygon(x, y, poly) for x, y in path)


def zones_for_path(
    db: Session, camera_id: int, label: str | None, path: list[tuple[float, float]]
) -> list[int]:
    """Sorted ids of this camera's zones the path passes through."""
    from models import CameraZone

    zones = db.query(CameraZone).filter(CameraZone.camera_id == camera_id).all()
    return sorted(z.id for z in zones if zone_matches(z, label, path))


def serialize(zone) -> dict[str, Any]:
    return {
        "id": zone.id,
        "camera_id": zone.camera_id,
        "name": zone.name,
        "polygon": zone.polygon,
        "labels": zone.labels,
        "created_at": zone.created_at.isoformat() if zone.created_at else None,
        "updated_at": zone.updated_at.isoformat() if zone.updated_at else None,
    }
