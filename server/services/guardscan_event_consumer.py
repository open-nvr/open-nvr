# Copyright (c) 2026 OpenNVR
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Core consumes ``guardscan.screening.v1`` into the screening ledger.

The guard-scan app measures; core remembers. Every completed screening —
the clean ones included — lands as a ``guard_screenings`` row, which is
what the compliance page and its reports are built from.

Why the clean ones matter: compliance is complete scans over ALL
screenings. Keeping only the failures would give a page that can say
"nine alerts today" and never "94% of screenings were done properly",
which is the number a manager actually acts on.

Best-effort by design, exactly like the occupancy and plate consumers:
no NATS URL, missing nats-py, or a down broker degrades to "no history
accrues" and the app keeps alerting. Retention is enforced HERE, the
only writer: every ``_PRUNE_EVERY`` applied rows, screenings older than
``RETENTION_DAYS`` are deleted, and the keypoint logs the app writes
alongside them are swept on the same schedule.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

SUBJECT = "opennvr.events.guardscan.screening.v1.>"

RETENTION_DAYS = 90
_PRUNE_EVERY = 200
_RETRY_SECONDS = 60.0

_CAMERA_HANDLE = re.compile(r"^cam-?(\d+)$")
_applies_since_prune = 0

#: Verdicts the app can report. An unknown one is stored as-is rather
#: than dropped — a new grade band in a future app version must not make
#: its screenings vanish from the denominator.
KNOWN_VERDICTS = ("compliant", "partial", "incomplete", "no_scan")


def _camera_num(handle: object) -> int | None:
    m = _CAMERA_HANDLE.match(str(handle or "").strip().lower())
    return int(m.group(1)) if m else None


def _when(value: object, fallback: datetime | None = None) -> datetime | None:
    """An epoch float or ISO string as an aware datetime."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), UTC)
        except (OverflowError, OSError, ValueError):
            return fallback
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return fallback
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return fallback


def apply_screening_event(envelope: object, db=None) -> str:
    """Apply one ``guardscan.screening.v1`` envelope to the ledger.

    Pure-decision core, unit-testable without a bus. Status tokens:

    * ``"applied"``   — a new screening row.
    * ``"duplicate"`` — this session is already recorded (the bus is
      at-least-once, and a redelivery must not inflate the day's count).
    * ``"malformed"`` — no payload, or no session id to key on.
    """
    global _applies_since_prune
    if not isinstance(envelope, dict):
        return "malformed"
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return "malformed"
    session_id = str(payload.get("session") or "").strip()[:40]
    if not session_id:
        return "malformed"

    from models import GuardScreening

    own_session = db is None
    if own_session:
        from core.database import SessionLocal

        db = SessionLocal()
    try:
        exists = (db.query(GuardScreening.id)
                  .filter(GuardScreening.session_id == session_id).first())
        if exists is not None:
            return "duplicate"

        ended = _when(payload.get("ts"), _when(envelope.get("ts"),
                                               datetime.now(UTC)))
        duration = payload.get("duration_s")
        started = None
        if isinstance(duration, (int, float)) and ended is not None:
            started = ended - timedelta(seconds=float(duration))

        row = GuardScreening(
            session_id=session_id,
            camera_id=_camera_num(envelope.get("camera_id")),
            started_at=started,
            ended_at=ended,
            verdict=str(payload.get("verdict") or "no_scan")[:20],
            score=float(payload.get("score") or 0.0),
            coverage=_maybe_float(payload.get("coverage")),
            order_score=_maybe_float(payload.get("order_score")),
            steps_done=_json(payload.get("steps_done")),
            steps_missing=_json(payload.get("steps_missing")),
            flagged=bool(payload.get("flagged")),
            ended_by=str(payload.get("ended_by") or "")[:30] or None,
            duration_s=_maybe_float(duration),
            engaged_s=_maybe_float(payload.get("engaged_s")),
            guard_key=str(payload.get("guard_key") or "")[:64] or None,
            guard_name=_resolve_guard(db, payload, ended),
            images=_json(payload.get("images")),
            alert_id=str(payload.get("alert_id") or "")[:64] or None,
        )
        db.add(row)
        db.commit()

        _applies_since_prune += 1
        if _applies_since_prune >= _PRUNE_EVERY:
            _applies_since_prune = 0
            prune_screenings(db)
        return "applied"
    finally:
        if own_session:
            db.close()


def _maybe_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _json(value: object) -> str | None:
    if value in (None, [], {}):
        return None
    try:
        return json.dumps(value)[:4000]
    except (TypeError, ValueError):
        return None


def _resolve_guard(db, payload: dict, when: datetime | None) -> str | None:
    """Who was on duty, if anyone has said.

    The app can tell one guard from another within a run, but it has no
    way to learn their NAME. That comes from the duty roster, resolved
    here at write time so a screening keeps the name that was true when
    it happened — renaming a shift later must not rewrite history.
    """
    name = payload.get("guard_name")
    if isinstance(name, str) and name.strip():
        return name.strip()[:100]
    return None


def prune_screenings(db) -> int:
    """Delete screenings past the retention window, and the keypoint
    logs recorded with them."""
    cutoff = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)
    from models import GuardScreening

    doomed = (db.query(GuardScreening.session_id)
              .filter(GuardScreening.ended_at < cutoff).all())
    if not doomed:
        return 0
    ids = [row[0] for row in doomed]
    (db.query(GuardScreening)
     .filter(GuardScreening.ended_at < cutoff)
     .delete(synchronize_session=False))
    db.commit()
    _prune_session_logs(ids)
    logger.info("guard screenings: pruned %d rows older than %d days",
                len(ids), RETENTION_DAYS)
    return len(ids)


def _prune_session_logs(session_ids: list[str]) -> None:
    """Delete the per-frame keypoint blobs for pruned screenings.

    These are the training data the app collects as it runs, and they
    are NOT in the evidence store — that sweep only ever deletes .jpg,
    so left there they would accumulate for ever.
    """
    from core.config import settings

    folder = Path(settings.recordings_base_path) / ".guardscan"
    if not folder.is_dir():
        return
    for session_id in session_ids:
        try:
            (folder / f"{session_id}.json").unlink(missing_ok=True)
        except OSError:
            continue


async def _handle_message(msg) -> None:
    """One bus message → one ledger row. Never raises: a bad message is
    a debug line, not a dead subscription."""
    try:
        envelope = json.loads(msg.data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        logger.debug("guard screenings: undecodable message on %s", msg.subject)
        return
    try:
        status = await asyncio.to_thread(apply_screening_event, envelope)
        if status != "applied":
            logger.debug("guard screenings: %s for %s", status, msg.subject)
    except Exception:
        logger.warning("guard screenings: apply failed", exc_info=True)


async def run_consumer_loop() -> None:
    """Subscribe to the screening subject for the process lifetime."""
    from core.config import settings

    url = getattr(settings, "nats_url", None) or ""
    if not url:
        logger.info("guard screenings: no NATS URL — no history will accrue")
        return
    try:
        import nats
    except ImportError:
        logger.info("guard screenings: nats-py not installed — no history")
        return

    # The compose broker runs token auth. Connecting without it is an
    # Authorization Violation, and this loop would retry for ever
    # without ever subscribing — the app keeps alerting while its
    # history quietly never accrues. (Same lesson as the plate and
    # occupancy consumers, which document it too.)
    token = (getattr(settings, "internal_api_key", "") or "").strip() or None

    while True:
        client = sub = None
        try:
            client = await nats.connect(url, connect_timeout=5, token=token,
                                        name="opennvr-guardscan-consumer")
            sub = await client.subscribe(SUBJECT, cb=_handle_message)
            logger.info("guard screenings: subscribed to %s", SUBJECT)
            while client.is_connected:
                await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            await _teardown(sub, client)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("guard screenings: consumer error (%s); retrying in %.0fs",
                           exc, _RETRY_SECONDS)
        await _teardown(sub, client)
        await asyncio.sleep(_RETRY_SECONDS)


async def _teardown(sub, client) -> None:
    for closer in (getattr(sub, "unsubscribe", None),
                   getattr(client, "close", None)):
        if closer is None:
            continue
        try:
            await closer()
        except Exception:  # noqa: BLE001
            continue
