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
Timeline service — write and read the canonical event store (RFC-0001 C1).

Routes stay thin; the semantics live here where tests can reach them:
* one row per visit (track lifecycle), alarm, or alert;
* the OVERLAP rule for time filters ("who was here 3-4pm" includes the
  visit that started 14:58 and left 15:03).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from models import Camera, TimelineEvent


def record_track_visit(
    db: Session,
    *,
    camera_id: int,
    label: str,
    started_at: datetime,
    ended_at: datetime | None = None,
    score: float | None = None,
    track_id: str | None = None,
    stationary: bool | None = None,
    evidence_path: str | None = None,
) -> TimelineEvent:
    """Persist one finished visit (source=tier0, event_type=track)."""
    row = TimelineEvent(
        camera_id=camera_id,
        source="tier0",
        event_type="track",
        label=(label or "")[:60].lower() or None,
        score=score,
        track_id=(track_id or "")[:40] or None,
        started_at=started_at,
        ended_at=ended_at,
        evidence_path=evidence_path,
        payload={"stationary": stationary} if stationary is not None else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def query_events(
    db: Session,
    *,
    camera_id: int | None = None,
    label: str | None = None,
    source: str | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
    limit: int = 100,
    owner_id: int | None = None,
) -> list[TimelineEvent]:
    """Newest-first visits/alarms/alerts intersecting [from, to).

    ``owner_id`` scopes results to that user's cameras — the same ownership
    rule every camera route enforces. Pass None ONLY for superusers.
    """
    limit = max(1, min(500, limit))
    q = db.query(TimelineEvent)
    if owner_id is not None:
        q = q.join(Camera, Camera.id == TimelineEvent.camera_id).filter(
            Camera.owner_id == owner_id
        )
    if camera_id is not None:
        q = q.filter(TimelineEvent.camera_id == camera_id)
    if label:
        q = q.filter(TimelineEvent.label == label.strip().lower())
    if source:
        q = q.filter(TimelineEvent.source == source)
    if to is not None:
        q = q.filter(TimelineEvent.started_at < to)
    if from_ is not None:
        # Overlap: an event with an end must end at/after `from`; an
        # instantaneous event (no end) must start at/after `from`.
        q = q.filter(
            ((TimelineEvent.ended_at.isnot(None)) & (TimelineEvent.ended_at >= from_))
            | ((TimelineEvent.ended_at.is_(None)) & (TimelineEvent.started_at >= from_))
        )
    return q.order_by(TimelineEvent.started_at.desc()).limit(limit).all()


def can_access_event(db: Session, event: TimelineEvent, *, user) -> bool:
    """Ownership check for a single event — mirrors get_camera_or_403."""
    if getattr(user, "is_superuser", False):
        return True
    cam = db.query(Camera).filter(Camera.id == event.camera_id).first()
    return bool(cam and cam.owner_id == user.id)
