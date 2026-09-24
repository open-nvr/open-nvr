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

from datetime import datetime, timedelta, timezone

from sqlalchemy import func as _func, or_
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


#: ``TimelineEvent.event_type`` of one object's stay on one camera.
#:
#: "track", not "visit", and the two must never be guessed at
#: separately. RFC-0003's resolve_visit was written filtering
#: ``event_type == "visit"`` — the word the RFC, the route and every
#: docstring use — while record_track_visit has always written "track".
#: Nothing in production matched, so every bind returned "no visit": the
#: doorbell wrote no face_id at all, and its counters described a site
#: where nobody was ever seen.
#:
#: The tests passed because their fixtures BUILT rows with
#: event_type="visit" by hand — the suite was the only producer of the
#: value the code looked for. That is the defect this constant exists to
#: prevent, and tests/test_visit_binding.py goes through
#: record_track_visit now for exactly that reason.
TRACK = "track"


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
    zone_ids: list[int] | None = None,
) -> TimelineEvent:
    """Persist one finished visit (source=tier0, event_type=track)."""
    row = TimelineEvent(
        camera_id=camera_id,
        source="tier0",
        event_type=TRACK,
        label=(label or "")[:60].lower() or None,
        score=score,
        track_id=(track_id or "")[:40] or None,
        started_at=started_at,
        ended_at=ended_at,
        evidence_path=evidence_path,
        scene_evidence_path=scene_evidence_path,
        payload={"stationary": stationary} if stationary is not None else None,
        zone_ids=zone_ids,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


#: TimelineEvent.source / event_type of an event someone created by hand
#: (Home Assistant automation, operator, API client), HA-107.
MANUAL = "manual"


def record_manual_event(
    db: Session,
    *,
    camera_id: int,
    label: str,
    started_at: datetime,
    ended_at: datetime | None = None,
    note: str | None = None,
    actor: str | None = None,
) -> TimelineEvent:
    """Persist a manual event. ``ended_at=None`` leaves it open until
    :func:`end_manual_event`."""
    payload: dict = {}
    if note:
        payload["note"] = note
    if actor:
        payload["created_by"] = actor
    row = TimelineEvent(
        camera_id=camera_id,
        source=MANUAL,
        event_type=MANUAL,
        label=(label or MANUAL)[:60].lower(),
        started_at=started_at,
        ended_at=ended_at,
        payload=payload or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def end_manual_event(db: Session, row: TimelineEvent, ended_at: datetime) -> TimelineEvent:
    row.ended_at = ended_at
    db.commit()
    db.refresh(row)
    return row


def zone_filter(zone_id: int):
    """``zone_id`` in the JSON list ``events.zone_ids`` (HA-109), dialect-neutral.

    The column holds ``json.dumps`` text (``[1, 4]``) on every backend, so
    four LIKE shapes cover first/only/last/middle without JSON operators
    that SQLite and Postgres spell differently, and never a prefix match
    (zone 1 is not zone 11).

    Serializer assumption: the patterns spell the list exactly as
    SQLAlchemy's ``JSON`` type serialises it, ``json.dumps`` with the
    default separators (``", "`` between items, no space inside the
    brackets). ``record_track_visit`` writes through that type; an engine
    with a custom ``json_serializer`` (compact separators) would silently
    make every filter miss its middle and last items, so
    tests/test_zones.py pins the stored text form.
    """
    from sqlalchemy import String, cast, or_

    text = cast(TimelineEvent.zone_ids, String)
    z = str(int(zone_id))
    return or_(text.like(f"[{z}]"), text.like(f"[{z},%"),
               text.like(f"%, {z}]"), text.like(f"%, {z},%"))


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
    zone_id: int | None = None,
    attrs: list[tuple[str | None, str]] | None = None,
    has_descriptor: bool = False,
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
    if zone_id is not None:
        q = q.filter(zone_filter(zone_id))
    if label:
        q = q.filter(TimelineEvent.label == label.strip().lower())
    # What a skill SAID about the visit — "blue", "van", "hi-vis" — as
    # opposed to what the detector classified it as. ANDed, like the
    # search API's: two attributes narrow, they do not widen.
    #
    # This is what a camera agent needs to answer "did you see a blue
    # car", and without it the word "blue" was dropped before the query
    # was built and the answer described a different question
    # confidently. The predicate is the search API's, imported rather
    # than rewritten, so the two cannot drift into disagreeing about
    # what an attribute match means.
    for kind, value in (attrs or []):
        if not (value or "").strip():
            continue
        from services.search_service import db_exists_claim

        q = q.filter(db_exists_claim(kind, value))
    # "Was anything here described AT ALL?" — the question that tells an
    # empty attribute search which kind of empty it is. Not a filter
    # anyone asks for directly; it is how a caller distinguishes "no
    # blue cars" from "nothing looked at any of them".
    if has_descriptor:
        from models import VisitDescriptor
        from sqlalchemy import select as _select

        q = q.filter(
            _select(VisitDescriptor.id)
            .where(VisitDescriptor.event_id == TimelineEvent.id)
            .exists()
        )
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
    # `entries` carries WHEN each vehicle came in, which `plates` cannot.
    # A count answers "how busy is the site"; "has this visitor been here
    # too long?" needs the entry time, and without it every caller has to
    # keep its own ledger of arrivals to subtract from — which is exactly
    # what license-plate-recognition was doing, in memory, losing every
    # open visit on restart.
    #
    # `plates` is left exactly as it was. The Vehicles page reads it as a
    # list of strings, and widening a field in place is how a dashboard
    # starts rendering "[object Object]".
    entries = [
        {
            "plate": p,
            "entered_at": (seen_at_of(last_by_plate[p]).isoformat()
                           if seen_at_of(last_by_plate[p]) else None),
            "camera_id": last_by_plate[p].camera_id,
        }
        for p in inside[:200]
    ]
    return {"inside": len(inside), "plates": inside[:200], "entries": entries}


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


# ── Subject binding: which visit is this frame looking at? ──────────
#
# RFC-0003. A frame-polling app has a camera, some bytes and an
# instant; it has no event_id, so whatever it works out from the frame
# has nowhere to go. That single gap is why smart-doorbell kept a
# parallel visit log, why `face_id` had no producer, and why person
# journeys were unreachable code.
#
# The objection to closing it was never that matching is impossible.
# It was that a guessed subject becomes indistinguishable from a
# measured one once written down. So the answer is not to match more
# cleverly; it is to say which kind of match happened, every time, in
# the row itself.

#: How far outside every visit's span an instant may fall and still
#: bind. Deliberately small: a doorbell frame is taken within a second
#: or two of the person being there, and a window wide enough to be
#: "safe" is wide enough to attach a name to the wrong visitor.
DEFAULT_BIND_TOLERANCE_S = 5.0

#: How many overlapping visits an ``ambiguous`` reply will name.
#:
#: Ambiguity is decided by the SECOND row — one binds, two refuse — so
#: this number never changes an outcome. It only bounds how much of the
#: crowd the caller gets told about, because "ambiguous, and here is
#: which visits" is debuggable and a bare refusal is not.
_AMBIGUITY_REPORT_N = 8


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _gap_to(row, at: datetime) -> float | None:
    """Seconds between ``at`` and the nearer edge of ``row``'s span.

    Only ever called on a row the containment query REJECTED, so the row
    lies strictly on one side of ``at`` and there are exactly two cases.
    Saying so explicitly matters, because the previous version wrote the
    general ``min(|at-start|, |at-end|)`` and treated an open visit's end
    as ``at`` itself — which made the second term exactly zero, so EVERY
    open visit scored a gap of 0.0 no matter how far away it started. An
    open visit beginning four seconds after the instant beat a closed one
    ending half a second before it, and the reply said "nearest within
    0.0s" about a visit that had not started yet.
    """
    start = _utc(getattr(row, "started_at", None))
    if start is None:
        return None
    if start > at:
        return (start - at).total_seconds()
    # Started at or before `at` and did not contain it, so it must have
    # ended first. An open visit that started before `at` IS containing
    # and cannot reach here; if one somehow does, it is not a near miss.
    end = _utc(row.ended_at)
    if end is None:
        return None
    return (at - end).total_seconds()


def resolve_visit(
    db: Session,
    *,
    camera_id: int,
    at: datetime,
    scope: set[int] | None = None,
    tolerance_s: float = DEFAULT_BIND_TOLERANCE_S,
    label: str | None = None,
) -> dict:
    """Which visit was happening on this camera at this instant.

    Returns ``{"event_id": int|None, "binding": str|None, "reason":
    str}``. The three outcomes are deliberately distinct:

    * exactly one visit's span CONTAINS the instant → ``window``. Not a
      guess: the span is core's own record of when that object was
      present, and the lookup is made by the component that owns it.
    * nothing contains it, but one visit starts or ends within
      ``tolerance_s`` → ``nearest``. This IS a guess, it is named one,
      and a caller that must not act on a guess can refuse it.
    * two or more visits contain it → NOTHING binds.

    That last case is the one worth being stubborn about. A doorbell
    frame taken while two people are at the door genuinely does not
    identify which visit the face belongs to, and picking the closer
    one would be inventing a fact. Ambiguity is an answer here, not an
    error — the caller is told ``ambiguous`` and writes no claim.

    ``label`` narrows to visits of one kind, which is how a face app
    avoids binding to the delivery van parked behind the visitor.
    """
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)

    def candidates():
        """The scoped, camera-and-label-narrowed visit query."""
        q = (
            db.query(TimelineEvent)
            .filter(TimelineEvent.camera_id == int(camera_id))
            .filter(TimelineEvent.event_type == TRACK)
        )
        q = scope_query(q, TimelineEvent.camera_id, scope)
        if label:
            q = q.filter(TimelineEvent.label == label.strip().lower())
        return q

    window = timedelta(seconds=max(0.0, float(tolerance_s)))

    # CONTAINMENT IS A SQL PREDICATE, not a filter over a page of rows.
    #
    # This used to take the 64 most recent visits within the tolerance
    # and test containment in Python. That is wrong in the one place it
    # matters: on a busy camera the visit actually covering the instant
    # can sit outside the newest 64 — several objects with overlapping
    # spans, or one long visit with short ones layered over it — and the
    # function then reported "no visit covered the instant". The caller
    # could not tell that from a true miss, so a real `window` binding
    # silently became a `nearest` guess or no claim at all. It degraded
    # exactly on the cameras with the most traffic, and it degraded
    # quietly, which is the worst combination available.
    #
    # An open visit (ended_at IS NULL) is still in progress, so it
    # contains any instant at or after its start.
    #
    # THE SPAN IS started_at..ended_at, not SEEN_AT..ended_at. SEEN_AT is
    # coalesce(observed_at, started_at), and observed_at is the capture
    # time of the look a PLATE READ won on — normally later than the
    # visit's start, sometimes much later on a busy gate. Using it as the
    # start while using the raw ended_at as the end made the effective
    # span strictly narrower than the real visit: a visit running
    # 10:00:00-10:00:30 whose plate was read at 10:00:10 would not bind
    # an instant at 10:00:02, which is plainly inside it. Worse, a row
    # where observed_at fell after ended_at could never bind at ANY
    # instant, because the two halves of the predicate were unsatisfiable
    # together.
    #
    # SEEN_AT exists for the plate AGGREGATIONS, where "when was this
    # read taken" is the question. "Was this object present at this
    # instant" is a different question and the visit's own span is its
    # answer.
    contains = candidates().filter(
        TimelineEvent.started_at <= at,
        or_(TimelineEvent.ended_at.is_(None), TimelineEvent.ended_at >= at),
    )
    # Two is the whole question — one binds, more than one refuses. A few
    # more are fetched only so the `ambiguous` reply can NAME the
    # candidates, which is what makes it debuggable rather than just a
    # refusal.
    containing = (contains.order_by(TimelineEvent.started_at.desc())
                  .limit(_AMBIGUITY_REPORT_N).all())

    near: list[tuple[float, TimelineEvent]] = []
    if not containing:
        # Nothing covered the instant, so every candidate is strictly on
        # one side of it, and the nearest on each side is reachable with
        # an ORDER BY the index can serve. No scan, no ceiling.
        #
        #   before: ended_at < at, and within tolerance
        #   after:  it starts after at, and within tolerance
        #
        # A visit with no end cannot be "before" — it would have been
        # containing — so the before-arm needs no NULL branch.
        #
        # Both arms key off started_at/ended_at, the same columns
        # containment uses. When they disagreed — before on ended_at,
        # after on SEEN_AT — one row could satisfy BOTH (ended before
        # `at`, plate read after it), appear twice in `near` with
        # identical gaps, and trip the equally-near tie check into
        # reporting `ambiguous` with the same event id listed twice, for
        # a single unambiguous candidate.
        before = (
            candidates()
            .filter(TimelineEvent.ended_at.isnot(None),
                    TimelineEvent.ended_at < at,
                    TimelineEvent.ended_at >= at - window)
            .order_by(TimelineEvent.ended_at.desc())
            .limit(2)
            .all()
        )
        after = (
            candidates()
            .filter(TimelineEvent.started_at > at,
                    TimelineEvent.started_at <= at + window)
            .order_by(TimelineEvent.started_at.asc())
            .limit(2)
            .all()
        )
        seen_ids: set[int] = set()
        for row in before + after:
            if row.id in seen_ids:
                continue
            seen_ids.add(row.id)
            gap = _gap_to(row, at)
            if gap is not None and gap <= window.total_seconds():
                near.append((gap, row))

    if len(containing) == 1:
        _count_binding("window")
        return {"event_id": containing[0].id, "binding": "window",
                "reason": "one visit was in progress"}
    if len(containing) > 1:
        _count_binding("ambiguous")
        return {"event_id": None, "binding": None, "reason": "ambiguous",
                "candidates": sorted(r.id for r in containing)}
    if near:
        near.sort(key=lambda pair: pair[0])
        # Two candidates equally close is the same ambiguity as two
        # containing visits, and gets the same refusal.
        if len(near) > 1 and abs(near[0][0] - near[1][0]) < 1e-6:
            _count_binding("ambiguous")
            return {"event_id": None, "binding": None, "reason": "ambiguous",
                    "candidates": sorted(r.id for _, r in near)}
        # The GAP is recorded, not just the outcome. A `nearest` share
        # that is stable but creeping towards the tolerance is the
        # warning; by the time it crosses, the binding stops happening
        # at all and the symptom changes shape from "occasionally wrong"
        # to "silently nothing".
        _count_binding("nearest", gap=near[0][0])
        return {"event_id": near[0][1].id, "binding": "nearest",
                "reason": f"no visit covered the instant; nearest within "
                          f"{near[0][0]:.1f}s"}
    _count_binding("none")
    return {"event_id": None, "binding": None, "reason": "no visit"}


def _count_binding(outcome: str, *, gap: float | None = None) -> None:
    """Record one bind attempt. Never raises.

    Metrics are an observation of the system, not part of it: a
    misconfigured or missing collector must not be able to stop a
    doorbell attaching a name. The import is local for the same reason
    the rest of this module's are — services importing each other at
    module scope is how this tree grew its import cycles.
    """
    try:
        from services import search_metrics as _metrics

        _metrics.BINDINGS.inc({"outcome": outcome})
        if gap is not None:
            _metrics.BIND_GAP.observe(gap)
    except Exception:                              # noqa: BLE001
        pass
