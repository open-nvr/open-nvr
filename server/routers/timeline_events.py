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
    plate: str | None = None,
    has_plate: bool = False,
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
        db, camera_id=camera_id, label=label, source=source, plate=plate,
        has_plate=has_plate, from_=from_, to=to, limit=limit,
        # Camera data is owner-scoped everywhere in OpenNVR; history and
        # evidence photos are the MOST sensitive camera data, so the same
        # rule applies here. Superusers see the fleet.
        owner_id=None if current_user.is_superuser else current_user.id,
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
    from services.timeline_service import can_access_event

    if not can_access_event(db, e, user=current_user):
        # 404, not 403: don't confirm the event exists on someone else's camera.
        raise HTTPException(status_code=404, detail="no evidence for this event")
    from services.evidence_store import resolve_evidence

    path = resolve_evidence(e.evidence_path)
    if path is None:
        raise HTTPException(status_code=404, detail="evidence file missing")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=86400"})


@router.get("/events/plate-stats")
async def get_plate_stats(
    days: int = 7,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Aggregates for the Vehicles page (plate reads over the last
    ``days``): totals, unique plates, per-camera and per-day counts.
    Owner-scoped like /events; superusers see the fleet."""
    from services.timeline_service import plate_stats

    return plate_stats(
        db,
        days=max(1, min(int(days), 90)),
        owner_id=None if current_user.is_superuser else current_user.id,
    )


@router.get("/events/plate-summary")
async def get_plate_summary(
    plate: str,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """All-time history for ONE plate — the Vehicles page drill-down:
    first seen, last seen, total reads, per-camera counts. Owner-scoped
    like /events; superusers see the fleet."""
    from services.timeline_service import plate_summary

    if not plate or not plate.strip():
        raise HTTPException(status_code=422, detail="plate is required")
    return plate_summary(
        db,
        plate=plate,
        owner_id=None if current_user.is_superuser else current_user.id,
    )


def _parse_camera_ids(raw: str) -> list[int]:
    out = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part:
            try:
                out.append(int(part))
            except ValueError:
                raise HTTPException(status_code=422,
                                    detail=f"bad camera id: {part!r}")
    return out


@router.get("/events/plate-sessions")
async def get_plate_sessions(
    plate: str,
    in_cameras: str = "",
    out_cameras: str = "",
    limit: int = 50,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Gate in / gate out history for one plate. ``in_cameras`` /
    ``out_cameras`` are comma-separated camera ids — which camera is
    which gate lives in the providing app's config, so the caller
    passes the sets and this stays stateless. Owner-scoped."""
    from services.timeline_service import plate_sessions

    if not plate or not plate.strip():
        raise HTTPException(status_code=422, detail="plate is required")
    return plate_sessions(
        db,
        plate=plate,
        in_cameras=_parse_camera_ids(in_cameras),
        out_cameras=_parse_camera_ids(out_cameras),
        owner_id=None if current_user.is_superuser else current_user.id,
        limit=max(1, min(int(limit), 200)),
    )


@router.get("/events/gate-occupancy")
async def get_gate_occupancy(
    in_cameras: str = "",
    out_cameras: str = "",
    hours: int = 24,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """How many vehicles are inside right now (last gate read within
    the window was an entry). Windowed so missed exits age out."""
    from services.timeline_service import gate_occupancy

    return gate_occupancy(
        db,
        in_cameras=_parse_camera_ids(in_cameras),
        out_cameras=_parse_camera_ids(out_cameras),
        hours=max(1, min(int(hours), 24 * 7)),
        owner_id=None if current_user.is_superuser else current_user.id,
    )


@router.get("/events/vehicle-report")
async def get_vehicle_report(
    year: int,
    month: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """One calendar month of vehicle movement, aggregated for the
    printable monthly report. Owner-scoped like /events."""
    from services.timeline_service import vehicle_report

    if not (1 <= int(month) <= 12):
        raise HTTPException(status_code=422, detail="month must be 1..12")
    return vehicle_report(
        db,
        year=year,
        month=month,
        owner_id=None if current_user.is_superuser else current_user.id,
    )
