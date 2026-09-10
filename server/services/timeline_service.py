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

from sqlalchemy import func as _func
from sqlalchemy.orm import Session

from models import TimelineEvent
from services.camera_scope import can_view_camera, scope_query

#: When a PLATE READ happened, for the aggregations below.
#:
#: ``observed_at`` is the capture time of the look the read won on;
#: ``started_at`` is the VISIT's start, which is a different moment and,
#: on a merged track, can belong to a different vehicle entirely. Reads
#: written before observed_at existed have only the fallback (#451).
#:
#: This matters most where reads are SUBTRACTED. A dwell time built from
#: two started_at values is off by the difference between two visits'
#: OCR lag, and that lag is largest exactly when a gate is busiest — so
#: the number was least trustworthy when it mattered most.
#:
#: Only for plate-filtered queries. ``query_events`` deliberately keeps
#: filtering by started_at: it ranges over EVERY visit (people, vehicles,
#: alarms), most of which have no read to be dated by, and the visit's
#: start is the honest answer to "who was here between 3 and 4".
SEEN_AT = _func.coalesce(TimelineEvent.observed_at, TimelineEvent.started_at)


def seen_at_of(row) -> datetime | None:
    """The Python-side twin of :data:`SEEN_AT`, for rows already loaded."""
    return getattr(row, "observed_at", None) or row.started_at


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
    scene_evidence_path: str | None = None,
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
        scene_evidence_path=scene_evidence_path,
        payload={"stationary": stationary} if stationary is not None else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _events_query(
    db: Session,
    *,
    camera_id: int | None = None,
    label: str | None = None,
    source: str | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
    scope: set[int] | None = None,
    plate: str | None = None,
    has_plate: bool = False,
):
    """Scope + filters, with NO ordering, offset or limit.

    The one place the /events predicate lives, so a page and its total
    can never disagree. A total built from a SECOND, similar query is
    how a row count for a camera the caller cannot see leaks out — the
    two must be the same query object or the scoping is decorative.

    Deliberately unordered: ``.count()`` wraps this in
    ``SELECT count(*) FROM (...)``, and an ORDER BY riding along into
    that subquery makes the database sort rows it is only going to
    count. Ordering belongs to :func:`query_events`, which is the only
    caller that returns rows.
    """
    q = db.query(TimelineEvent)
    q = scope_query(q, TimelineEvent.camera_id, scope)
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
    return q


def query_events(
    db: Session,
    *,
    limit: int = 100,
    skip: int = 0,
    **filters,
) -> list[TimelineEvent]:
    """Newest-first visits/alarms/alerts intersecting [from, to).

    ``scope`` (in ``filters``) is the caller's visible camera set from
    ``camera_scope.visible_camera_ids`` — own cameras plus can_view
    grants. Pass None ONLY for superusers (unrestricted).

    The ordering carries an ``id`` tiebreaker because ``started_at`` is
    NOT unique: ``uq_events_visit`` is on the triple (camera, track,
    start), and Tier-0 emits visits in bursts, so ties cluster exactly
    when a page boundary is most likely to land in one. Without a total
    order, paging across a tie set silently repeats or drops rows.

    The clamp stays here even though the router validates: this is a
    public surface (``internal_camera_agent`` calls it with an
    unvalidated int), and one line of defence in depth is cheap.
    """
    limit = max(1, min(500, limit))
    skip = max(0, int(skip))
    return (
        _events_query(db, **filters)
        .order_by(TimelineEvent.started_at.desc(), TimelineEvent.id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def count_events(db: Session, **filters) -> int:
    """How many rows the SAME filters and scope match, ignoring paging."""
    return _events_query(db, **filters).count()


def can_access_event(db: Session, event: TimelineEvent, *, user) -> bool:
    """Visibility check for a single event — the camera_scope rule
    (owner or can_view grant; superusers see everything)."""
    return can_view_camera(db, user, event.camera_id)


def plate_stats(
    db: Session,
    *,
    days: int = 7,
    scope: set[int] | None = None,
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
        .filter(SEEN_AT >= cutoff)
    )
    base = scope_query(base, TimelineEvent.camera_id, scope)

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
    # the window is small (<= a few thousand rows of one timestamp),
    # so bucket in Python for portability.
    per_day_counts: dict[str, int] = {}
    for (seen_at,) in base.with_entities(SEEN_AT).all():
        day = seen_at.date().isoformat()
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
    scope: set[int] | None = None,
) -> dict:
    """Everything the platform knows about ONE plate — the Vehicles
    page's history drill-down ("when did this car last come in?").

    ``plate`` is normalised the same way the producers do (upper, no
    separators) and matched exactly; scoped like ``query_events``.
    All-time on purpose: first_seen is the point of the question.
    """
    from sqlalchemy import func

    normalized = "".join(str(plate).split()).upper()
    base = db.query(TimelineEvent).filter(TimelineEvent.plate_text == normalized)
    base = scope_query(base, TimelineEvent.camera_id, scope)

    total = base.count()
    first_seen, last_seen = (
        base.with_entities(func.min(SEEN_AT), func.max(SEEN_AT)).one()
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
    scope: set[int] | None = None,
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
    q = scope_query(q, TimelineEvent.camera_id, scope)
    reads = q.order_by(SEEN_AT.asc()).all()

    def _row(entry, exit_) -> dict:
        entered = seen_at_of(entry) if entry is not None else None
        exited = seen_at_of(exit_) if exit_ is not None else None
        duration = None
        if entered is not None and exited is not None:
            # Both ends are READ times, so the difference is how long the
            # vehicle was inside — not that plus the difference between
            # two visits' OCR lag.
            duration = max(0, int((exited - entered).total_seconds()))
        return {
            "entered_at": entered.isoformat() if entered else None,
            "entry_camera_id": entry.camera_id if entry else None,
            "exited_at": exited.isoformat() if exited else None,
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
    scope: set[int] | None = None,
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
        .filter(SEEN_AT >= cutoff)
    )
    q = scope_query(q, TimelineEvent.camera_id, scope)
    last_by_plate: dict[str, TimelineEvent] = {}
    for r in q.order_by(SEEN_AT.asc()).all():
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
    scope: set[int] | None = None,
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
        .filter(SEEN_AT >= start)
        .filter(SEEN_AT < end)
    )
    base = scope_query(base, TimelineEvent.camera_id, scope)

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
            func.min(SEEN_AT),
            func.max(SEEN_AT),
        )
        .group_by(TimelineEvent.plate_text)
        .order_by(func.count(TimelineEvent.id).desc())
        .limit(max(1, int(per_plate_limit)))
        .all()
    )
    per_plate_cameras: dict[str, dict[int, int]] = {}
    per_day_counts: dict[str, int] = {}
    for plate, cid, seen_at in base.with_entities(
        TimelineEvent.plate_text, TimelineEvent.camera_id, SEEN_AT,
    ).all():
        per_plate_cameras.setdefault(plate, {})
        per_plate_cameras[plate][cid] = per_plate_cameras[plate].get(cid, 0) + 1
        day = seen_at.date().isoformat()
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
