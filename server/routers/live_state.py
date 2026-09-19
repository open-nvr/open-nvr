# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""``GET /live-state``: what each camera sees right now (HA-110).

Counts come from memory (services/live_state.py, fed by Tier-0 frames); the
last plate read and the last finished visit come from the event store. Home
Assistant polls this on start and then follows ``live_state`` events.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.database import get_db
from core.permissions import user_has_permission
from models import Camera, TimelineEvent, User
from services.camera_scope import visible_camera_ids
from services.live_state import get_live_state

router = APIRouter(tags=["live-state"])


#: How far back "last plate" / "last object" look. Bounded and ordered by
#: (camera_id, started_at) so the ix_events_cam_start index serves it; an
#: unbounded "newest with a plate" scan walks the whole table on a camera
#: that never read one.
LOOKBACK = timedelta(days=7)


def _recent(db: Session, camera_id: int):
    return (db.query(TimelineEvent)
            .filter(TimelineEvent.camera_id == camera_id,
                    TimelineEvent.started_at >= datetime.now(UTC) - LOOKBACK)
            .order_by(TimelineEvent.started_at.desc()))


def _last_plate(db: Session, camera_id: int) -> dict | None:
    row = _recent(db, camera_id).filter(TimelineEvent.plate_text.isnot(None)).first()
    if row is None:
        return None
    seen = row.observed_at or row.started_at
    return {"text": row.plate_text, "event_id": row.id,
            "at": seen.isoformat() if seen else None}


def _last_object(db: Session, camera_id: int) -> dict | None:
    row = _recent(db, camera_id).filter(TimelineEvent.event_type == "track").first()
    if row is None:
        return None
    return {
        "label": row.label, "event_id": row.id,
        "at": (row.ended_at or row.started_at).isoformat()
        if (row.ended_at or row.started_at) else None,
        "zone_ids": row.zone_ids,
        "evidence_url": f"/api/v1/events/{row.id}/evidence" if row.evidence_path else None,
    }


@router.get("/live-state")
async def get_live_states(
    camera_id: int | None = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Per camera: ``objects`` ({label: {total, active}}), ``zones``
    (the same per zone), ``motion``, ``stale`` (no Tier-0 frame for a few
    seconds, i.e. nothing detected lately), ``last_plate`` and
    ``last_object`` (the most recent finished visit in the last 7 days, with
    its best-frame image; both null without ``recordings.view``). Only
    cameras the caller can see."""
    scope = visible_camera_ids(db, current_user)
    q = db.query(Camera.id).filter(Camera.deleted_at.is_(None), Camera.is_active.is_(True))
    if camera_id is not None:
        q = q.filter(Camera.id == camera_id)
    ids = [cid for (cid,) in q.order_by(Camera.id).all() if scope is None or cid in scope]
    if camera_id is not None and not ids:
        raise HTTPException(status_code=404, detail="Camera not found")
    live = get_live_state()
    # Plates and visit images are recorded history: the same right as
    # GET /events (and the last_plate/last_object entities) needs.
    history = user_has_permission(current_user, "recordings.view")
    cameras = []
    for cid in ids:
        state = live.camera(cid)
        state["last_plate"] = _last_plate(db, cid) if history else None
        state["last_object"] = _last_object(db, cid) if history else None
        cameras.append(state)
    return {"stale_after_s": live.stale_s, "motion_off_after_s": live.motion_off_after_s,
            "cameras": cameras}
