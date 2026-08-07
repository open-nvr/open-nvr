# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
#
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""
Timeline query API — the read side of the canonical event & evidence store.

"Who came between 3 and 4pm?" is `GET /events?label=person&from=&to=` — a
range scan over visits, each row linking its best-frame evidence JPEG. This
is the API the agent's search tools, the UI timeline, and every app query
instead of keeping private stores (RFC-0001 Challenge 1).

Distinct from routers/events.py, which is the LIVE WebSocket feed; this is
history. Same nouns, different tense.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.database import get_db
from models import TimelineEvent, User

router = APIRouter(tags=["timeline"])


def _serialize(e: TimelineEvent) -> dict:
    return {
        "id": e.id,
        "camera_id": e.camera_id,
        "source": e.source,
        "event_type": e.event_type,
        "label": e.label,
        "score": e.score,
        "track_id": e.track_id,
        "started_at": e.started_at.isoformat() if e.started_at else None,
        "ended_at": e.ended_at.isoformat() if e.ended_at else None,
        "recording_ref": e.recording_ref,
        "plate_text": e.plate_text,
        "has_evidence": bool(e.evidence_path),
        "evidence_url": f"/api/v1/events/{e.id}/evidence" if e.evidence_path else None,
        "payload": e.payload,
    }


@router.get("/events")
async def list_events(
    camera_id: int | None = None,
    label: str | None = None,
    source: str | None = None,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    limit: int = 100,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Query visits/alarms/alerts, newest first.

    Time filters use the OVERLAP rule — an event counts if any part of it
    intersects [from, to) — because "who was here 3-4pm" must include the
    visit that started 14:58 and left 15:03.
    """
    from services.timeline_service import query_events

    rows = query_events(
        db, camera_id=camera_id, label=label, source=source,
        from_=from_, to=to, limit=limit,
    )
    return {"events": [_serialize(e) for e in rows], "count": len(rows)}


@router.get("/events/{event_id}/evidence")
async def get_event_evidence(
    event_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """The visit's best-frame JPEG — the sharpest look Tier-0 had at it."""
    e = db.query(TimelineEvent).filter(TimelineEvent.id == event_id).first()
    if e is None or not e.evidence_path:
        raise HTTPException(status_code=404, detail="no evidence for this event")
    from services.evidence_store import resolve_evidence

    path = resolve_evidence(e.evidence_path)
    if path is None:
        raise HTTPException(status_code=404, detail="evidence file missing")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=86400"})
