# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Attendance's clock and verdict: timetable → sessions → marks.

Core owns both. The attendance app only reports what each frame showed
(services/attendance_event_consumer.py); everything that decides WHO WAS
THERE happens here, so the answer does not depend on the app being up,
on its version, or on it agreeing with itself after a restart.

One tick (every ``TICK_SECONDS``):

1. **materialise** today's and tomorrow's sessions from the active
   timetable, in each group's timezone. Unique per (slot, local date):
   ticking twice, or a restart, can never create a class twice. A session
   that has not started follows edits to its slot; one that has started
   keeps the times and cameras it ran with. A session whose slot was
   switched off, moved to another day or ended its term is cancelled
   while it is still only scheduled.
2. **advance** each session: scheduled → running at its start,
   → finalizing at its end, → final ``finalize_grace_minutes`` later
   (the app's last attempts may still be on the bus at the bell).
3. **watch coverage**: a running session with no frame looked at for
   ``GAP_MINUTES`` raises one alert per gap — the app or every camera is
   down, and the people taking attendance should hear it now, not find
   an empty report at the end of the day.

Finalising cuts a session into buckets of ``bucket_minutes``. A bucket
COUNTS only if at least one ``ok`` attempt fell in it, on any of the
session's cameras: a camera outage is a gap, not a class marked absent
(attendly counted the slot regardless and marked everyone ABSENT). A
member recognised in at least ``present_ratio`` of the counted buckets
is PRESENT, or LATE if the first sighting came after
``late_grace_minutes``; everyone else is ABSENT. Marks set by a person
(``source='manual'``) are never overwritten, and re-finalising
recomputes only the automatic ones.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from services import attendance_policy

logger = logging.getLogger(__name__)

TICK_SECONDS = 30.0
#: A running session with no frame looked at for this long raises an alert.
GAP_MINUTES = 10
#: Days ahead to materialise: today and tomorrow, so tomorrow's first
#: period exists before midnight in every timezone.
_HORIZON_DAYS = 2

#: Statuses a session can still change from.
OPEN = ("scheduled", "running", "finalizing")


def _utc(dt: datetime) -> datetime:
    """Aware UTC. SQLite hands timezone-aware columns back naive."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _local_window(day: date, start: str, end: str, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """A slot's local wall-clock times on ``day``, as UTC. A time that does
    not exist that day (a DST spring-forward gap) resolves the way
    zoneinfo does, to the instant it would have been; the class is not
    skipped."""
    def at(hhmm: str) -> datetime:
        h, m = (int(x) for x in hhmm.split(":"))
        if (h, m) == (24, 0):       # an end of "24:00" is the next midnight
            return datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=tz).astimezone(UTC)
        return datetime.combine(day, time(h, m), tzinfo=tz).astimezone(UTC)
    return at(start), at(end)


def _slot_runs_on(slot, day: date) -> bool:
    if not slot.active or slot.weekday != day.weekday():
        return False
    if slot.valid_from and day < slot.valid_from:
        return False
    return not (slot.valid_to and day > slot.valid_to)


# ── 1. materialise ───────────────────────────────────────────────────


def materialize(db, now: datetime) -> dict[str, int]:
    """Create, refresh and cancel scheduled sessions from the timetable."""
    from models import AttendanceGroup, AttendanceSession, AttendanceTimetableSlot

    now = _utc(now)
    out = {"created": 0, "updated": 0, "cancelled": 0}
    slots = (db.query(AttendanceTimetableSlot, AttendanceGroup)
             .join(AttendanceGroup, AttendanceGroup.id == AttendanceTimetableSlot.group_id)
             .all())
    for slot, group in slots:
        try:
            tz = ZoneInfo(group.timezone)
        except Exception:
            logger.warning("attendance: group %s has unknown timezone %r", group.id, group.timezone)
            continue
        today = now.astimezone(tz).date()
        for offset in range(_HORIZON_DAYS):
            day = today + timedelta(days=offset)
            existing = (db.query(AttendanceSession)
                        .filter(AttendanceSession.slot_id == slot.id,
                                AttendanceSession.local_date == day).first())
            wanted = group.active and _slot_runs_on(slot, day)
            if existing is not None:
                if existing.status == "cancelled" and wanted:
                    # Switched off and on again (or the group deactivated and
                    # reactivated) before it started: it is wanted again. The
                    # unique (slot, date) row is this one, so it is revived,
                    # not recreated. Once its start has passed it stays
                    # cancelled — nothing watched it.
                    start, end = _local_window(day, slot.start_time, slot.end_time, tz)
                    if start > now and end > start:
                        existing.status = "scheduled"
                        out["updated"] += 1
                    else:
                        continue
                if existing.status != "scheduled":
                    continue        # started, finished or cancelled: history
                if not wanted:
                    existing.status = "cancelled"
                    out["cancelled"] += 1
                    continue
                start, end = _local_window(day, slot.start_time, slot.end_time, tz)
                if end <= start:
                    # A DST spring-forward gap swallowed the period.
                    existing.status = "cancelled"
                    out["cancelled"] += 1
                    continue
                cams = list(slot.camera_ids or [])
                if (_utc(existing.start_at), _utc(existing.end_at), existing.camera_ids,
                        existing.group_id) != (start, end, cams, slot.group_id):
                    existing.start_at, existing.end_at = start, end
                    existing.camera_ids, existing.group_id = cams, slot.group_id
                    out["updated"] += 1
                continue
            if not wanted:
                continue
            start, end = _local_window(day, slot.start_time, slot.end_time, tz)
            if end <= now:
                # Booting at noon must not invent this morning's classes:
                # nothing watched them, so they would all be no-coverage.
                continue
            if end <= start:
                # The local times fall in a DST spring-forward gap that day
                # (02:00-03:00 in New York in March): there is no such hour.
                logger.info("attendance: slot %s skipped on %s, inside a DST gap", slot.id, day)
                continue
            db.add(AttendanceSession(slot_id=slot.id, group_id=slot.group_id, local_date=day,
                                     start_at=start, end_at=end,
                                     camera_ids=list(slot.camera_ids or []),
                                     status="scheduled"))
            out["created"] += 1
    db.commit()
    return out


# ── 2. advance and finalise ──────────────────────────────────────────


def session_policy(db, session) -> dict:
    from models import AttendanceGroup

    group = db.get(AttendanceGroup, session.group_id)
    return attendance_policy.effective_policy(attendance_policy.site_policy(db),
                                              group.policy if group else None)


def advance(db, now: datetime) -> dict[str, int]:
    from models import AttendanceSession

    now = _utc(now)
    out = {"started": 0, "ended": 0, "finalized": 0}
    rows = (db.query(AttendanceSession)
            .filter(AttendanceSession.status.in_(OPEN)).all())
    for s in rows:
        start, end = _utc(s.start_at), _utc(s.end_at)
        if s.status == "scheduled" and start <= now < end:
            s.status = "running"
            out["started"] += 1
        if s.status in ("scheduled", "running") and now >= end:
            # A session core never saw start (it was down) still ends.
            s.status = "finalizing"
            out["ended"] += 1
        if s.status == "finalizing":
            grace = timedelta(minutes=session_policy(db, s)["finalize_grace_minutes"])
            if now >= end + grace:
                finalize(db, s, now=now)
                out["finalized"] += 1
    db.commit()
    return out


def bucket_of(start: datetime, at: datetime, bucket_s: int) -> int:
    return int((_utc(at) - _utc(start)).total_seconds() // bucket_s)


def evidence_pruned(session, now: datetime | None = None) -> bool:
    """Whether the session's attempts and sightings may already have been
    pruned (services/attendance_event_consumer.py RETENTION_DAYS). Past
    that, its marks are the only record: recomputing them from what is
    left would turn every one into ABSENT."""
    from services.attendance_event_consumer import RETENTION_DAYS

    cutoff = _utc(now or datetime.now(UTC)) - timedelta(days=RETENTION_DAYS)
    return _utc(session.end_at) < cutoff


def finalize(db, session, *, now: datetime | None = None,
             only_person: int | None = None) -> dict[str, int]:
    """Compute the session's automatic marks from its attempts and
    sightings. Safe to run again: manual marks are left alone and the
    automatic ones are recomputed from the same rows.

    ``only_person`` recomputes that one person's mark and nothing else —
    handing one mark back to the cameras must not re-mark the whole past
    session against today's roster. Callers must not recompute a session
    whose evidence is pruned (``evidence_pruned``)."""
    from models import (
        AttendanceAttempt,
        AttendanceAttemptSession,
        AttendanceGroupMember,
        AttendanceMark,
        AttendancePerson,
        AttendanceSighting,
    )

    policy = session_policy(db, session)
    bucket_s = int(policy["bucket_minutes"]) * 60
    start, end = _utc(session.start_at), _utc(session.end_at)
    total = max(1, math.ceil((end - start).total_seconds() / bucket_s))

    attempts = (db.query(AttendanceAttempt.at)
                .join(AttendanceAttemptSession,
                      AttendanceAttemptSession.attempt_id == AttendanceAttempt.id)
                .filter(AttendanceAttemptSession.session_id == session.id,
                        AttendanceAttempt.ok.is_(True)).all())
    counted = {b for (at,) in attempts if 0 <= (b := bucket_of(start, at, bucket_s)) < total}

    seen: dict[int, tuple[set[int], datetime]] = {}
    rows = (db.query(AttendanceSighting.person_id, AttendanceAttempt.at)
            .join(AttendanceAttempt, AttendanceAttempt.id == AttendanceSighting.attempt_id)
            .filter(AttendanceSighting.session_id == session.id).all())
    for person_id, at in rows:
        b = bucket_of(start, at, bucket_s)
        if not (0 <= b < total):
            continue
        buckets, first = seen.get(person_id, (set(), _utc(at)))
        buckets.add(b)
        seen[person_id] = (buckets, min(first, _utc(at)))

    existing = {m.person_id: m for m in db.query(AttendanceMark)
                .filter(AttendanceMark.session_id == session.id).all()}
    if only_person is not None:
        members = [only_person]
    elif existing:
        # A recompute is for the roster the session was decided for, not
        # today's: a student who joined since was not in that class.
        members = list(existing)
    else:
        # Marked are the people the cameras could have recognised: in
        # the group before the class ended, active, consented and with a
        # face enrolled. Anyone else would be ABSENT for being unknown to
        # the camera, which is not evidence they were away. Somebody seen
        # (a sighting) is marked whatever their state is now.
        members = [pid for (pid, added_at) in db.query(AttendanceGroupMember.person_id,
                                                       AttendanceGroupMember.added_at)
                   .join(AttendancePerson, AttendancePerson.id == AttendanceGroupMember.person_id)
                   .filter(AttendanceGroupMember.group_id == session.group_id,
                           AttendancePerson.deleted_at.is_(None),
                           AttendancePerson.active.is_(True),
                           AttendancePerson.consent_at.isnot(None),
                           AttendancePerson.face_samples > 0).all()
                   if added_at is None or _utc(added_at) <= end]
        members += [pid for pid in seen if pid not in set(members)]
    late_after = start + timedelta(minutes=int(policy["late_grace_minutes"]))
    out = {"PRESENT": 0, "LATE": 0, "ABSENT": 0, "manual": 0}
    for pid in members:
        mark = existing.get(pid)
        if mark is not None and mark.source == "manual":
            out["manual"] += 1
            continue
        buckets, first = seen.get(pid, (set(), None))
        present = len(buckets & counted)
        if counted and present / len(counted) >= float(policy["present_ratio"]):
            status = "LATE" if first is not None and first > late_after else "PRESENT"
        else:
            status = "ABSENT"
        out[status] += 1
        if mark is None:
            mark = AttendanceMark(session_id=session.id, person_id=pid, source="auto")
            db.add(mark)
        mark.status, mark.buckets_present, mark.buckets_total = status, present, len(counted)
        mark.first_seen_at = first

    if only_person is not None:
        return out
    session.total_buckets, session.counted_buckets = total, len(counted)
    session.no_coverage = (not counted
                           or len(counted) / total < float(policy.get("min_coverage_ratio", 0.0)))
    session.status = "final"
    session.finalized_at = _utc(now or datetime.now(UTC))
    return out


# ── 3. coverage ──────────────────────────────────────────────────────


def check_coverage(db, now: datetime) -> int:
    """One alert per running session per gap of ``GAP_MINUTES`` with no
    frame looked at. The alert id carries the gap's window, so a session
    silent for an hour raises a handful, not one a tick."""
    from models import (
        AttendanceAttempt,
        AttendanceAttemptSession,
        AttendanceGroup,
        AttendanceSession,
    )
    from services.alerts_inbox import apply_alert

    now = _utc(now)
    gap = timedelta(minutes=GAP_MINUTES)
    raised = 0
    for s in db.query(AttendanceSession).filter(AttendanceSession.status == "running").all():
        if now - _utc(s.start_at) < gap:
            continue
        last = (db.query(AttendanceAttempt.at)
                .join(AttendanceAttemptSession,
                      AttendanceAttemptSession.attempt_id == AttendanceAttempt.id)
                .filter(AttendanceAttemptSession.session_id == s.id,
                        AttendanceAttempt.ok.is_(True))
                .order_by(AttendanceAttempt.at.desc()).first())
        if last is not None and now - _utc(last[0]) < gap:
            continue
        group = db.get(AttendanceGroup, s.group_id)
        name = group.name if group else f"group {s.group_id}"
        window = int(now.timestamp() // gap.total_seconds())
        status = apply_alert({
            "alert_id": f"attendance-gap-{s.id}-{window}",
            "severity": "medium",
            "title": f"Attendance for {name} is not being taken",
            "description": (
                f"No frame from the cameras of {name} has been looked at for "
                f"{GAP_MINUTES} minutes during a running session. The attendance "
                "app, its face adapter or every camera of the room is down. Minutes "
                "with no frame are left out of everyone's ratio rather than counted "
                "as absence, but nobody can be marked present for them either."),
            "source": {"kind": "platform", "name": "attendance"},
            "tags": ["attendance", "coverage"],
            "evidence": {"session_id": s.id, "group_id": s.group_id,
                         "camera_ids": list(s.camera_ids or [])},
            "fired_at": now.isoformat(),
        }, db=db)
        raised += status == "stored"
    return raised


def tick(db, now: datetime | None = None) -> dict:
    now = _utc(now or datetime.now(UTC))
    out = {"materialize": materialize(db, now), "advance": advance(db, now)}
    out["coverage_alerts"] = check_coverage(db, now)
    return out


def _tick_once() -> None:
    from core.database import SessionLocal

    db = SessionLocal()
    try:
        tick(db)
    finally:
        db.close()


async def run_scheduler_loop() -> None:
    """The process-lifetime loop. A failing tick is logged and retried at
    the next one; it never stops the loop."""
    while True:
        try:
            await asyncio.to_thread(_tick_once)
        except Exception:
            logger.warning("attendance: scheduler tick failed", exc_info=True)
        await asyncio.sleep(TICK_SECONDS)
