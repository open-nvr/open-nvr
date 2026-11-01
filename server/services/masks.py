# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Camera masks: the parts of a camera's picture Tier-0 must ignore.

Two kinds, both polygons in the zones' coordinate space (0..1 of the frame,
x right, y down):

* ``motion`` — pixels under it never count as motion. A burned-in OSD clock,
  a tree in the wind, a road at the edge of the frame or a flag no longer
  wakes the object detector; the detector still sees the whole frame once
  something else moves. This is the fix the detect-pipeline README used to
  ask the operator to make in the camera ("disable the overlay or mask it").
* ``object`` — a detection standing inside it (the bottom-centre of its box)
  is dropped before it can start a track: the TV screen, the poster, the
  car parked across the street. ``labels`` optionally limits it to some
  object labels.

The detect-pipeline reads them from the camera-agent sources listing
(``motion_masks`` / ``object_masks`` per camera) and restarts the camera's
worker on its next reconcile tick when they change.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from sqlalchemy.orm import Session

from services.zones import valid_polygon  # noqa: F401 - the same polygon rule

KINDS = ("motion", "object")
MAX_MASKS_PER_CAMERA = 32


def serialize(mask) -> dict[str, Any]:
    return {
        "id": mask.id,
        "camera_id": mask.camera_id,
        "name": mask.name,
        "kind": mask.kind,
        "polygon": mask.polygon,
        "labels": mask.labels if mask.kind == "object" else None,
        "created_at": mask.created_at.isoformat() if mask.created_at else None,
        "updated_at": mask.updated_at.isoformat() if mask.updated_at else None,
    }


def masks_for_cameras(db: Session, camera_ids: Iterable[int]) -> dict[int, dict[str, list]]:
    """``{camera_id: {"motion_masks": [polygon, ...], "object_masks":
    [{"polygon": ..., "labels": ...}, ...]}}`` for the sources listing —
    one query for the whole fleet, and every listed camera gets an entry
    (empty lists for a camera with no masks) so the pipeline never has to
    tell "no masks" from "an older core"."""
    from models import CameraMask

    ids = [int(i) for i in camera_ids]
    out: dict[int, dict[str, list]] = {
        i: {"motion_masks": [], "object_masks": []} for i in ids
    }
    if not ids:
        return out
    rows = (db.query(CameraMask).filter(CameraMask.camera_id.in_(ids))
            .order_by(CameraMask.id).all())
    grouped: dict[int, list] = defaultdict(list)
    for m in rows:
        grouped[int(m.camera_id)].append(m)
    for cid, masks in grouped.items():
        for m in masks:
            if m.kind == "object":
                out[cid]["object_masks"].append(
                    {"polygon": m.polygon, "labels": m.labels or None})
            else:
                out[cid]["motion_masks"].append(m.polygon)
    return out
