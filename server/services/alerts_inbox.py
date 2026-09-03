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

"""Alerts-inbox consumer — lands §11.5 app alerts in the operator inbox.

Every app built on the SDK publishes its alerts to
``opennvr.alerts.{source.kind}.{source.name}.{camera_id}``; the LPR
app's shipped config even points ``nats_alerts_url`` at the compose
broker "so the operator-UI alerts inbox ... pick[s] them up". Until this
module, nothing did: alerts reached stdout and the bus and stopped, so
an armed "alarm on unknown vehicle" fired into a log nobody watches.

One consumer, wildcard subscription, one row per alert in ``app_alerts``
(the inbox the UI polls, rings, and acknowledges). Dedup is the
producer's ``alert_id`` — NATS is at-least-once and this process
reconnects, so a redelivered alert must never become a second ringing
row.

Same best-effort posture as ``plate_event_consumer``: no NATS URL or no
nats-py degrades to "alerts stay in the app logs", and the loop never
raises into the task runner.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC

logger = logging.getLogger(__name__)

SUBJECT = "opennvr.alerts.>"

#: Reconnect cadence after a connect/subscribe failure (see
#: plate_event_consumer for the rationale — slow on purpose).
_RETRY_SECONDS = 60.0

_SEVERITIES = ("low", "medium", "high", "critical")

#: Ring configuration lives in SecuritySetting under this key. The UI
#: reads it to decide what each severity does in the browser:
#:   "none"       — badge only, no sound
#:   "ping"       — one chime when the alert arrives
#:   "continuous" — rings until every alert of that severity is
#:                  acknowledged (the camera-agent's "siren" behaviour)
RING_CONFIG_KEY = "alert_ring_config"
RING_MODES = ("none", "ping", "continuous")
DEFAULT_RING_CONFIG = {
    "low": "none",
    "medium": "ping",
    "high": "continuous",
    "critical": "continuous",
}


def normalize_ring_config(raw: object) -> dict[str, str]:
    """Overlay ``raw`` on the defaults, dropping unknown severities and
    unknown modes — a corrupt stored value must never silence critical
    alerts OR make 'low' scream; it degrades to the defaults."""
    merged = dict(DEFAULT_RING_CONFIG)
    if isinstance(raw, dict):
        for sev, mode in raw.items():
            if sev in _SEVERITIES and mode in RING_MODES:
                merged[sev] = mode
    return merged


def apply_alert(envelope: object) -> str:
    """Store one §11.5 alert envelope into the inbox.

    Pure-decision core, unit-testable without a bus. Status tokens:

    * ``"stored"``     — new row in ``app_alerts``.
    * ``"duplicate"``  — this ``alert_id`` is already in the inbox
      (at-least-once redelivery); left untouched.
    * ``"malformed"``  — not a dict, or missing/empty ``title`` or
      ``alert_id``; dropped with a debug line.
    """
    if not isinstance(envelope, dict):
        return "malformed"
    alert_id = envelope.get("alert_id")
    title = envelope.get("title")
    if not isinstance(alert_id, str) or not alert_id.strip():
        return "malformed"
    if not isinstance(title, str) or not title.strip():
        return "malformed"

    severity = envelope.get("severity")
    if severity not in _SEVERITIES:
        # Unknown severity must still SURFACE — an app bug in one field
        # cannot be allowed to hide a fired alert. High, not critical:
        # never escalate by accident either.
        severity = "high"

    fired_at = _parse_fired_at(envelope.get("fired_at"))
    source = envelope.get("source")
    source_kind = source_name = None
    if isinstance(source, dict):
        source_kind = _clip(source.get("kind"), 30)
        source_name = _clip(source.get("name"), 100)

    from datetime import datetime

    from core.database import SessionLocal
    from models import AppAlert

    db = SessionLocal()
    try:
        exists = (
            db.query(AppAlert.id)
            .filter(AppAlert.alert_id == alert_id.strip()[:64])
            .first()
        )
        if exists is not None:
            return "duplicate"
        row = AppAlert(
            alert_id=alert_id.strip()[:64],
            fired_at=fired_at or datetime.now(UTC),
            severity=severity,
            title=title.strip()[:200],
            description=_clip(envelope.get("description"), 4000),
            source_kind=source_kind,
            source_name=source_name,
            camera_id=_clip(envelope.get("camera_id"), 60),
            correlation_id=_clip(envelope.get("correlation_id"), 64),
            evidence=_json_or_none(envelope.get("evidence")),
            tags=_json_or_none(envelope.get("tags")),
        )
        db.add(row)
        db.commit()
        logger.info(
            "alert inbox: [%s] %s (camera=%s source=%s alert_id=%s)",
            severity.upper(), row.title, row.camera_id,
            source_name or "?", row.alert_id,
        )
        return "stored"
    finally:
        db.close()


def _clip(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if value else None


def _json_or_none(value: object) -> str | None:
    if value in (None, {}, []):
        return None
    try:
        return json.dumps(value)[:8000]
    except (TypeError, ValueError):
        return None


def _parse_fired_at(value: object):
    from datetime import datetime

    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _handle_message(msg) -> None:
    """One bus message → one ``apply_alert``. Never raises — a bad
    message is a debug line, not a dead subscription."""
    try:
        envelope = json.loads(msg.data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        logger.debug("alert inbox: undecodable message on %s", msg.subject)
        return
    try:
        status = await asyncio.to_thread(apply_alert, envelope)
        if status != "stored":
            logger.debug("alert inbox: %s for %s", status, msg.subject)
    except Exception:
        logger.warning("alert inbox: apply failed", exc_info=True)


async def run_consumer_loop() -> None:
    """Subscribe to ``opennvr.alerts.>`` for the process lifetime.

    Returns immediately when no NATS URL is configured; retries
    (slowly) on every other failure — mirrors plate_event_consumer."""
    from core.config import settings

    url = (getattr(settings, "nats_url", "") or "").strip()
    if not url:
        logger.info("alert inbox consumer disabled (no NATS_URL) — "
                    "app alerts stay in the app containers' logs")
        return
    try:
        import nats
    except ImportError:
        logger.warning("alert inbox consumer disabled: nats-py not installed")
        return

    # Compose broker runs token auth (--auth $INTERNAL_API_KEY) — same
    # lesson as every other consumer in this stack.
    token = (getattr(settings, "internal_api_key", "") or "").strip() or None

    while True:
        try:
            client = await nats.connect(url, connect_timeout=5, token=token)
            sub = await client.subscribe(SUBJECT, cb=_handle_message)
            logger.info("alert inbox consumer subscribed to %s", SUBJECT)
            try:
                await asyncio.Event().wait()
            finally:
                try:
                    await sub.unsubscribe()
                    await client.drain()
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "alert inbox consumer: connect/subscribe failed (%s); "
                "retrying in %.0fs", exc, _RETRY_SECONDS)
            await asyncio.sleep(_RETRY_SECONDS)
