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
    plate: str | None = None,
    has_plate: bool = False,
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
    if plate:
        # Normalized like the writer (uppercase, no spaces); substring match
        # so "1234" finds KA01AB1234 — how people actually recall plates.
        norm = "".join(plate.split()).upper()
        q = q.filter(TimelineEvent.plate_text.ilike(f"%{norm}%"))
    elif has_plate:
        # The Vehicles page: every row must BE a plate read (a plate
        # filter implies this already).
        q = q.filter(TimelineEvent.plate_text.isnot(None))
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


def plate_stats(
    db: Session,
    *,
    days: int = 7,
    owner_id: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Aggregates for the Vehicles page: plate reads over the last
    ``days`` (visits whose ``plate_text`` is set), owner-scoped exactly
    like ``query_events``. One grouped pass each for per-camera and
    per-day; portable SQL (sqlite + postgres).
    """
    from datetime import timedelta, timezone

    from sqlalchemy import func

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(1, int(days)))
    base = (
        db.query(TimelineEvent)
        .filter(TimelineEvent.plate_text.isnot(None))
        .filter(TimelineEvent.started_at >= cutoff)
    )
    if owner_id is not None:
        base = base.join(Camera, Camera.id == TimelineEvent.camera_id).filter(
            Camera.owner_id == owner_id
        )

    total = base.count()
    unique_plates = (
        base.with_entities(func.count(func.distinct(TimelineEvent.plate_text)))
        .scalar()
        or 0
    )
    per_camera = [
        {"camera_id": cid, "reads": int(n)}
        for cid, n in (
            base.with_entities(
                TimelineEvent.camera_id, func.count(TimelineEvent.id)
            )
            .group_by(TimelineEvent.camera_id)
            .all()
        )
    ]
    # Day bucketing in SQL is dialect-divergent (date_trunc vs strftime);
    # the window is small (<= a few thousand rows of (id, started_at)),
    # so bucket in Python for portability.
    per_day_counts: dict[str, int] = {}
    for (started_at,) in base.with_entities(TimelineEvent.started_at).all():
        day = started_at.date().isoformat()
        per_day_counts[day] = per_day_counts.get(day, 0) + 1
    per_day = [
        {"day": day, "reads": per_day_counts[day]}
        for day in sorted(per_day_counts)
    ]
    return {
        "days": int(days),
        "total_reads": int(total),
        "unique_plates": int(unique_plates),
        "per_camera": per_camera,
        "per_day": per_day,
    }


def plate_summary(
    db: Session,
    *,
    plate: str,
    owner_id: int | None = None,
) -> dict:
    """Everything the platform knows about ONE plate — the Vehicles
    page's history drill-down ("when did this car last come in?").

    ``plate`` is normalised the same way the producers do (upper, no
    separators) and matched exactly; owner-scoped like ``query_events``.
    All-time on purpose: first_seen is the point of the question.
    """
    from sqlalchemy import func

    normalized = "".join(str(plate).split()).upper()
    base = db.query(TimelineEvent).filter(TimelineEvent.plate_text == normalized)
    if owner_id is not None:
        base = base.join(Camera, Camera.id == TimelineEvent.camera_id).filter(
            Camera.owner_id == owner_id
        )

    total = base.count()
    first_seen, last_seen = (
        base.with_entities(
            func.min(TimelineEvent.started_at), func.max(TimelineEvent.started_at)
        ).one()
        if total
        else (None, None)
    )
    per_camera = [
        {"camera_id": cid, "reads": int(n)}
        for cid, n in (
            base.with_entities(
                TimelineEvent.camera_id, func.count(TimelineEvent.id)
            )
            .group_by(TimelineEvent.camera_id)
            .all()
        )
    ]
    return {
        "plate": normalized,
        "total_reads": int(total),
        "first_seen": first_seen.isoformat() if first_seen else None,
        "last_seen": last_seen.isoformat() if last_seen else None,
        "per_camera": per_camera,
    }


def plate_sessions(
    db: Session,
    *,
    plate: str,
    in_cameras: list[int],
    out_cameras: list[int],
    owner_id: int | None = None,
    limit: int = 50,
) -> dict:
    """Entry/exit pairing for ONE plate — gate in / gate out history.

    Stateless on purpose: which cameras are entry vs exit gates lives
    in the providing app's config (the vertical owns its settings);
    the caller passes both sets and this pairs the plate's reads on
    them chronologically. An entry with no later exit is an OPEN
    session (the vehicle is inside); consecutive entries close the
    earlier one with a missed exit; an exit with no prior entry shows
    as a session with no entry (a missed entry read).
    """
    normalized = "".join(str(plate).split()).upper()
    in_set = {int(c) for c in in_cameras}
    out_set = {int(c) for c in out_cameras} - in_set  # a camera can't be both
    gates = in_set | out_set
    if not gates:
        return {"plate": normalized, "sessions": [], "inside_now": False}

    q = (
        db.query(TimelineEvent)
        .filter(TimelineEvent.plate_text == normalized)
        .filter(TimelineEvent.camera_id.in_(gates))
    )
    if owner_id is not None:
        q = q.join(Camera, Camera.id == TimelineEvent.camera_id).filter(
            Camera.owner_id == owner_id
        )
    reads = q.order_by(TimelineEvent.started_at.asc()).all()

    def _row(entry, exit_) -> dict:
        duration = None
        if entry is not None and exit_ is not None:
            duration = max(
                0, int((exit_.started_at - entry.started_at).total_seconds())
            )
        return {
            "entered_at": entry.started_at.isoformat() if entry else None,
            "entry_camera_id": entry.camera_id if entry else None,
            "exited_at": exit_.started_at.isoformat() if exit_ else None,
            "exit_camera_id": exit_.camera_id if exit_ else None,
            "duration_seconds": duration,
        }

    sessions: list[dict] = []
    open_entry = None
    for r in reads:
        if r.camera_id in in_set:
            if open_entry is not None:
                sessions.append(_row(open_entry, None))  # missed exit
            open_entry = r
        else:
            sessions.append(_row(open_entry, r))
            open_entry = None
    inside_now = open_entry is not None
    if open_entry is not None:
        sessions.append(_row(open_entry, None))  # still inside

    sessions.reverse()  # newest first
    return {
        "plate": normalized,
        "sessions": sessions[: max(1, int(limit))],
        "inside_now": inside_now,
    }


def gate_occupancy(
    db: Session,
    *,
    in_cameras: list[int],
    out_cameras: list[int],
    hours: int = 24,
    owner_id: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Who is inside right now: plates whose LAST gate read within the
    window was on an entry camera. Windowed so a missed exit ages out
    instead of counting a vehicle as inside forever."""
    from datetime import timedelta, timezone

    in_set = {int(c) for c in in_cameras}
    out_set = {int(c) for c in out_cameras} - in_set
    gates = in_set | out_set
    if not in_set or not out_set:
        return {"inside": 0, "plates": []}

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max(1, int(hours)))
    q = (
        db.query(TimelineEvent)
        .filter(TimelineEvent.plate_text.isnot(None))
        .filter(TimelineEvent.camera_id.in_(gates))
        .filter(TimelineEvent.started_at >= cutoff)
    )
    if owner_id is not None:
        q = q.join(Camera, Camera.id == TimelineEvent.camera_id).filter(
            Camera.owner_id == owner_id
        )
    last_by_plate: dict[str, TimelineEvent] = {}
    for r in q.order_by(TimelineEvent.started_at.asc()).all():
        last_by_plate[r.plate_text] = r
    inside = sorted(
        p for p, r in last_by_plate.items() if r.camera_id in in_set
    )
    return {"inside": len(inside), "plates": inside[:200]}


def vehicle_report(
    db: Session,
    *,
    year: int,
    month: int,
    owner_id: int | None = None,
    per_plate_limit: int = 1000,
) -> dict:
    """One calendar month of vehicle movement, aggregated for the
    Vehicles page's printable report: totals, per-plate reads with
    first/last seen and per-camera counts, per-camera totals and a
    per-day series. Owner-scoped like everything else. The registry
    join (which plate belongs to which flat) happens client-side —
    the register lives in the providing app's config, not in core.
    """
    from calendar import monthrange
    from datetime import timedelta, timezone

    from sqlalchemy import func

    year = max(2000, min(int(year), 2100))
    month = max(1, min(int(month), 12))
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=monthrange(year, month)[1])

    base = (
        db.query(TimelineEvent)
        .filter(TimelineEvent.plate_text.isnot(None))
        .filter(TimelineEvent.started_at >= start)
        .filter(TimelineEvent.started_at < end)
    )
    if owner_id is not None:
        base = base.join(Camera, Camera.id == TimelineEvent.camera_id).filter(
            Camera.owner_id == owner_id
        )

    total = base.count()
    per_camera = [
        {"camera_id": cid, "reads": int(n)}
        for cid, n in base.with_entities(
            TimelineEvent.camera_id, func.count(TimelineEvent.id)
        ).group_by(TimelineEvent.camera_id).all()
    ]

    # Per-plate rollup in one grouped pass; day series in Python
    # (dialect portability, same call as plate_stats).
    plate_rows = (
        base.with_entities(
            TimelineEvent.plate_text,
            func.count(TimelineEvent.id),
            func.min(TimelineEvent.started_at),
            func.max(TimelineEvent.started_at),
        )
        .group_by(TimelineEvent.plate_text)
        .order_by(func.count(TimelineEvent.id).desc())
        .limit(max(1, int(per_plate_limit)))
        .all()
    )
    per_plate_cameras: dict[str, dict[int, int]] = {}
    per_day_counts: dict[str, int] = {}
    for plate, cid, started_at in base.with_entities(
        TimelineEvent.plate_text, TimelineEvent.camera_id,
        TimelineEvent.started_at,
    ).all():
        per_plate_cameras.setdefault(plate, {})
        per_plate_cameras[plate][cid] = per_plate_cameras[plate].get(cid, 0) + 1
        day = started_at.date().isoformat()
        per_day_counts[day] = per_day_counts.get(day, 0) + 1

    per_plate = [
        {
            "plate": plate,
            "reads": int(n),
            "first_seen": first.isoformat() if first else None,
            "last_seen": last.isoformat() if last else None,
            "per_camera": [
                {"camera_id": cid, "reads": reads}
                for cid, reads in sorted(
                    per_plate_cameras.get(plate, {}).items())
            ],
        }
        for plate, n, first, last in plate_rows
    ]

    return {
        "year": year,
        "month": month,
        "total_reads": int(total),
        "unique_plates": len(per_plate_cameras),
        "per_camera": per_camera,
        "per_plate": per_plate,
        "per_day": [
            {"day": d, "reads": per_day_counts[d]}
            for d in sorted(per_day_counts)
        ],
    }
