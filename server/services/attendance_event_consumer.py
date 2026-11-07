# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Core consumes ``attendance.attempt.v1`` into attempts and sightings.

The attendance app publishes one event per camera per look: when, whether
it got a frame, how many faces it saw, and which enrolled people it
recognised (by gallery id). The app measures; core remembers and decides
— services/attendance_scheduler.py turns these rows into marks.

Core, not the app, decides which sessions an attempt belongs to: every
session running on that camera at that moment. One frame of a room two
groups share is therefore one attempt for both, and a session list sent
by the app is ignored rather than trusted. A match is kept only for a
member of that session's group, at or above the group's match threshold.

Envelope (EVENT_CONTRACTS.md): ``{id, schema, camera_id: "cam<N>", ts,
producer, payload}`` with payload ``{at, ok, faces_detected,
matches: [{person_id: <gallery uuid>, similarity, bbox}], evidence_path?}``.

Best-effort like the other consumers: no NATS, no history, never a failed
boot. Idempotent on the envelope id (the bus is at-least-once).

``producer`` is not checked: it is free text the publisher writes about
itself ("app" by default in the SDK), so a check would prove nothing and
would only drop events from an app that spelt itself differently. Who may
publish on this subject is the apps bus's business (services/nats_users.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

SUBJECT = "opennvr.events.attendance.attempt.v1.>"
SCHEMA = "attendance.attempt.v1"
_RETRY_SECONDS = 60.0
#: Attempts and their sightings are pruned past this; marks are kept (they
#: are the record). The evidence frames an attempt points at live in the
#: evidence store and follow ITS retention, not this one. Once pruned, a
#: session cannot be recomputed (attendance_scheduler.evidence_pruned).
RETENTION_DAYS = 90
_PRUNE_EVERY = 500
_applies_since_prune = 0
#: An attempt's ``at`` may not be in the future by more than this (clock skew).
_MAX_SKEW = timedelta(minutes=2)
_MAX_MATCHES = 500

_CAMERA_HANDLE = re.compile(r"^cam-?(\d+)$")


def _camera_num(handle: object) -> int | None:
    if isinstance(handle, int) and not isinstance(handle, bool):
        return handle
    m = _CAMERA_HANDLE.match(str(handle or "").strip().lower())
    return int(m.group(1)) if m else None


def _when(value: object) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def apply_attempt_event(envelope: object, db=None, *, now: datetime | None = None) -> str:
    """Apply one envelope. Status tokens: ``applied``, ``duplicate``,
    ``malformed``, ``foreign`` (another schema), ``future``
    (dated ahead of core's clock), ``no_session`` (nothing ran on that
    camera then — stored nowhere, it could count toward nothing)."""
    global _applies_since_prune
    if not isinstance(envelope, dict):
        return "malformed"
    payload = envelope.get("payload")
    event_id = str(envelope.get("id") or "").strip()[:64]
    if not isinstance(payload, dict) or not event_id:
        return "malformed"
    if envelope.get("schema") not in (None, SCHEMA):
        return "foreign"
    camera_id = _camera_num(envelope.get("camera_id"))
    at = _when(payload.get("at")) or _when(envelope.get("ts"))
    if camera_id is None or at is None:
        return "malformed"
    now = _utc(now or datetime.now(UTC))
    if at > now + _MAX_SKEW:
        return "future"

    from models import (
        AttendanceAttempt,
        AttendanceAttemptSession,
        AttendanceGroupMember,
        AttendancePerson,
        AttendanceSession,
        AttendanceSighting,
    )
    from services.attendance_scheduler import session_policy

    own = db is None
    if own:
        from core.database import SessionLocal

        db = SessionLocal()
    try:
        if db.query(AttendanceAttempt.id).filter(
                AttendanceAttempt.event_id == event_id).first() is not None:
            return "duplicate"

        # Sessions this camera was serving at ``at``. Final ones are
        # closed: an attempt that arrives after the verdict changes nothing.
        candidates = (db.query(AttendanceSession)
                      .filter(AttendanceSession.status.in_(("scheduled", "running",
                                                            "finalizing")))
                      .all())
        sessions = [s for s in candidates
                    if camera_id in (s.camera_ids or [])
                    and _utc(s.start_at) <= at < _utc(s.end_at)]
        if not sessions:
            return "no_session"

        # A real boolean only: bool("false") is True, and a look that got
        # no frame must never count as one that did.
        ok = payload.get("ok", True) is True
        matches = payload.get("matches") if ok else []
        matches = matches if isinstance(matches, list) else []
        attempt = AttendanceAttempt(
            event_id=event_id, camera_id=camera_id, at=at, ok=ok,
            faces_detected=max(0, int(payload.get("faces_detected") or 0)),
            matched=0,
            evidence_path=(str(payload.get("evidence_path"))[:500]
                           if payload.get("evidence_path") else None))
        db.add(attempt)
        db.flush()

        best: dict[str, tuple[float, object]] = {}
        for m in matches[:_MAX_MATCHES]:
            if not isinstance(m, dict):
                continue
            uuid = str(m.get("person_id") or "").strip()
            sim = m.get("similarity")
            if not uuid or isinstance(sim, bool) or not isinstance(sim, (int, float)):
                continue
            if uuid not in best or float(sim) > best[uuid][0]:
                best[uuid] = (float(sim), m.get("bbox"))
        people = {}
        if best:
            people = {u: pid for pid, u in db.query(AttendancePerson.id, AttendancePerson.uuid)
                      .filter(AttendancePerson.uuid.in_(list(best)),
                              AttendancePerson.deleted_at.is_(None)).all()}

        kept = 0
        for s in sessions:
            db.add(AttendanceAttemptSession(attempt_id=attempt.id, session_id=s.id))
            if not people:
                continue
            threshold = float(session_policy(db, s)["match_threshold"])
            members = {pid for (pid,) in db.query(AttendanceGroupMember.person_id).filter(
                AttendanceGroupMember.group_id == s.group_id,
                AttendanceGroupMember.person_id.in_(list(people.values()))).all()}
            for uuid, (sim, bbox) in best.items():
                pid = people.get(uuid)
                if pid is None or pid not in members or sim < threshold:
                    continue
                db.add(AttendanceSighting(attempt_id=attempt.id, session_id=s.id,
                                          person_id=pid, similarity=sim,
                                          bbox=bbox if isinstance(bbox, list) else None))
                kept += 1
        attempt.matched = kept
        db.commit()

        _applies_since_prune += 1
        if _applies_since_prune >= _PRUNE_EVERY:
            _applies_since_prune = 0
            prune(db, now=now)
        return "applied"
    except Exception:
        db.rollback()
        raise
    finally:
        if own:
            db.close()


def prune(db, *, now: datetime | None = None) -> int:
    """Delete attempts (and, by cascade, their links and sightings) older
    than the retention window. Marks stay: they are the record."""
    from models import AttendanceAttempt, AttendanceAttemptSession, AttendanceSighting

    cutoff = _utc(now or datetime.now(UTC)) - timedelta(days=RETENTION_DAYS)
    old = [i for (i,) in db.query(AttendanceAttempt.id)
           .filter(AttendanceAttempt.at < cutoff).all()]
    if not old:
        return 0
    # Explicitly, not only by the foreign keys: SQLite cascades only with
    # foreign_keys on.
    for model in (AttendanceSighting, AttendanceAttemptSession):
        db.query(model).filter(model.attempt_id.in_(old)).delete(synchronize_session=False)
    db.query(AttendanceAttempt).filter(AttendanceAttempt.id.in_(old)).delete(
        synchronize_session=False)
    db.commit()
    logger.info("attendance: pruned %d attempts older than %d days", len(old), RETENTION_DAYS)
    return len(old)


async def _handle_message(msg) -> None:
    try:
        envelope = json.loads(msg.data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        logger.debug("attendance: undecodable message on %s", msg.subject)
        return
    try:
        status = await asyncio.to_thread(apply_attempt_event, envelope)
        if status != "applied":
            logger.debug("attendance: %s for %s", status, msg.subject)
    except Exception:
        logger.warning("attendance: apply failed", exc_info=True)


async def run_consumer_loop() -> None:
    """Subscribe for the process lifetime (same shape as guard-scan's)."""
    from core.config import settings

    url = getattr(settings, "nats_url", None) or ""
    if not url:
        logger.info("attendance: no NATS URL — no attendance will be taken")
        return
    try:
        import nats
    except ImportError:
        logger.info("attendance: nats-py not installed — no attendance will be taken")
        return
    # Token auth on the compose broker; without it this loop would retry
    # for ever and never subscribe (deployment-only failure).
    token = (getattr(settings, "internal_api_key", "") or "").strip() or None
    while True:
        client = sub = None
        try:
            client = await nats.connect(url, connect_timeout=5, token=token,
                                        name="opennvr-attendance-consumer")
            sub = await client.subscribe(SUBJECT, cb=_handle_message)
            logger.info("attendance: subscribed to %s", SUBJECT)
            while client.is_connected:
                await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            await _teardown(sub, client)
            raise
        except Exception as exc:
            logger.warning("attendance: consumer error (%s); retrying in %.0fs",
                           exc, _RETRY_SECONDS)
        await _teardown(sub, client)
        await asyncio.sleep(_RETRY_SECONDS)


async def _teardown(sub, client) -> None:
    for closer in (getattr(sub, "unsubscribe", None), getattr(client, "close", None)):
        if closer is None:
            continue
        try:
            await closer()
        except Exception:
            continue
