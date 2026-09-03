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

"""Operator alert inbox API — list, acknowledge, ring configuration.

The read side of ``services/alerts_inbox.py``: the UI's alert bell polls
``GET /alerts-inbox`` (unacked first), rings per severity according to
``GET /alerts-inbox/ring-config``, and posts acknowledgements back so
every open browser goes quiet together. Acknowledge is idempotent and
recorded with the acknowledging user — an alarm silenced at 3am should
say by whom.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.database import get_db
from models import AppAlert, SecuritySetting, User
from services.alerts_inbox import (
    DEFAULT_RING_CONFIG,
    RING_CONFIG_KEY,
    RING_MODES,
    normalize_ring_config,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/alerts-inbox", tags=["alerts-inbox"])

_MAX_LIMIT = 200


def _row_out(a: AppAlert) -> dict:
    def _load(text):
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None

    return {
        "id": a.id,
        "alert_id": a.alert_id,
        "fired_at": a.fired_at.isoformat() if a.fired_at else None,
        "severity": a.severity,
        "title": a.title,
        "description": a.description,
        "source_kind": a.source_kind,
        "source_name": a.source_name,
        "camera_id": a.camera_id,
        "correlation_id": a.correlation_id,
        "evidence": _load(a.evidence),
        "tags": _load(a.tags) or [],
        "acknowledged_at": (a.acknowledged_at.isoformat()
                            if a.acknowledged_at else None),
        "acknowledged_by": a.acknowledged_by,
    }


@router.get("")
async def list_alerts(
    unacked: bool = Query(False, description="Only unacknowledged alerts"),
    severity: str | None = Query(None),
    source_name: str | None = Query(None),
    after_id: int | None = Query(
        None, description="Only rows with id > after_id — lets the bell "
        "poll for 'anything new since my last look' cheaply"),
    limit: int = Query(50, ge=1, le=_MAX_LIMIT),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Inbox listing, newest first. ``unacked=true`` is what the bell
    polls; the full list backs the Alerts & Incidents page."""
    q = db.query(AppAlert)
    if unacked:
        q = q.filter(AppAlert.acknowledged_at.is_(None))
    if severity:
        q = q.filter(AppAlert.severity == severity)
    if source_name:
        q = q.filter(AppAlert.source_name == source_name)
    if after_id is not None:
        q = q.filter(AppAlert.id > after_id)
    rows = q.order_by(AppAlert.id.desc()).limit(limit).all()
    unacked_count = (
        db.query(AppAlert.id)
        .filter(AppAlert.acknowledged_at.is_(None))
        .count()
    )
    return {"alerts": [_row_out(a) for a in rows],
            "unacked_count": unacked_count}


class AckIn(BaseModel):
    """Empty body acks ALL unacknowledged alerts; ``ids`` acks a set."""

    ids: list[int] | None = None


@router.post("/ack")
async def acknowledge(
    payload: AckIn,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Silence alerts. Idempotent — an already-acked id is left with its
    ORIGINAL acknowledgement (first silencer wins the audit trail)."""
    q = db.query(AppAlert).filter(AppAlert.acknowledged_at.is_(None))
    if payload.ids is not None:
        if not payload.ids:
            return {"acknowledged": 0}
        q = q.filter(AppAlert.id.in_(payload.ids))
    now = datetime.now(UTC)
    count = 0
    for row in q.all():
        row.acknowledged_at = now
        row.acknowledged_by = current_user.id
        count += 1
    db.commit()
    if count:
        logger.info("alert inbox: %d alert(s) acknowledged by %s",
                    count, current_user.username)
    return {"acknowledged": count}


class TestAlarmIn(BaseModel):
    severity: str = "high"


@router.post("/test")
async def fire_test_alarm(
    payload: TestAlarmIn,
    current_user: User = Depends(get_current_active_user),
):
    """Fire a synthetic alarm through the REAL ingestion path.

    'Is the alarm system working?' deserves a one-click answer that
    exercises the same code a real alert takes — apply_alert, the same
    table, the same poll, the same ring — not a UI-only sound test that
    would pass while the consumer is broken. The alert is clearly
    labelled as a test and acknowledges like any other.
    """
    if payload.severity not in DEFAULT_RING_CONFIG:
        raise HTTPException(status_code=422,
                            detail=f"unknown severity: {payload.severity!r}")
    import uuid
    from datetime import datetime as _dt, timezone as _tz

    from services.alerts_inbox import apply_alert

    envelope = {
        "alert_id": f"alrt_test_{uuid.uuid4().hex[:12]}",
        "fired_at": _dt.now(_tz.utc).replace(microsecond=0).isoformat(),
        "title": f"Test alarm ({payload.severity})",
        "description": (
            f"Fired by {current_user.username} from the Alarms page to "
            "verify the alarm chain end to end."),
        "severity": payload.severity,
        "source": {"kind": "operator", "name": "alarm-test",
                   "version": "1.0.0"},
        "camera_id": None,
        "tags": ["test"],
    }
    status = apply_alert(envelope)
    if status != "stored":
        raise HTTPException(status_code=500,
                            detail=f"test alarm not stored ({status})")
    logger.info("alert inbox: test alarm (%s) fired by %s",
                payload.severity, current_user.username)
    return {"status": "fired", "severity": payload.severity}


class ActionConfigIn(BaseModel):
    """Partial update; twilio.auth_token (plaintext) is encrypted at
    rest and NEVER returned — omit/blank keeps the stored one."""

    min_severity: str | None = None
    twilio: dict | None = None
    webhook: dict | None = None


@router.get("/actions")
async def get_alarm_actions(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Call / SMS / hooter configuration, secret masked (set/unset)."""
    from services.alarm_actions import load_action_config, masked_action_config

    return {"actions": masked_action_config(load_action_config(db))}


@router.put("/actions")
async def put_alarm_actions(
    payload: ActionConfigIn,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    from services.alarm_actions import (
        _SEVERITY_RANK, masked_action_config, save_action_config,
    )

    if payload.min_severity is not None \
            and payload.min_severity not in _SEVERITY_RANK:
        raise HTTPException(status_code=422,
                            detail=f"unknown severity: {payload.min_severity!r}")
    incoming: dict = {}
    if payload.min_severity is not None:
        incoming["min_severity"] = payload.min_severity
    if payload.twilio is not None:
        incoming["twilio"] = payload.twilio
    if payload.webhook is not None:
        incoming["webhook"] = payload.webhook
    merged = save_action_config(db, incoming)
    logger.info("alarm actions config updated by %s", current_user.username)
    return {"actions": masked_action_config(merged)}


@router.post("/actions/test")
async def test_alarm_actions(
    current_user: User = Depends(get_current_active_user),
):
    """Run every ENABLED action once with a synthetic alarm and return
    each action's outcome — validates Twilio credentials and the relay
    URL without waiting for a real alarm. Skips the severity gate on
    purpose (the point is exercising the actions), but a disabled
    action stays disabled."""
    import asyncio as _asyncio

    from services.alarm_actions import dispatch_alarm_actions

    alert = {
        "alert_id": "alrt_action_test", "severity": "critical",
        "title": f"Test alarm action fired by {current_user.username}",
        "description": "Configuration test from the Alarms page.",
        "camera_id": None, "fired_at": "",
    }
    results = await _asyncio.to_thread(
        dispatch_alarm_actions, alert, force=True)
    if not results:
        return {"results": [], "note": "no actions are enabled"}
    return {"results": results}


@router.get("/ring-config")
async def get_ring_config(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Per-severity ring behaviour for the browser bell:
    none | ping | continuous. Stored once, shared by every operator —
    an alarm policy is a site decision, not a per-browser one."""
    row = (
        db.query(SecuritySetting)
        .filter(SecuritySetting.key == RING_CONFIG_KEY)
        .first()
    )
    raw = None
    if row is not None:
        try:
            raw = json.loads(row.json_value)
        except ValueError:
            raw = None
    return {"ring": normalize_ring_config(raw), "modes": list(RING_MODES)}


class RingConfigIn(BaseModel):
    ring: dict[str, str]


@router.put("/ring-config")
async def put_ring_config(
    payload: RingConfigIn,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Replace the ring policy. Unknown severities/modes are rejected
    loudly rather than dropped — a typo'd 'continuos' that silently
    became the default would be discovered at 3am."""
    for sev, mode in payload.ring.items():
        if sev not in DEFAULT_RING_CONFIG:
            raise HTTPException(status_code=422,
                                detail=f"unknown severity: {sev!r}")
        if mode not in RING_MODES:
            raise HTTPException(
                status_code=422,
                detail=f"unknown ring mode {mode!r} for {sev!r} "
                       f"(valid: {', '.join(RING_MODES)})")
    merged = normalize_ring_config(payload.ring)
    row = (
        db.query(SecuritySetting)
        .filter(SecuritySetting.key == RING_CONFIG_KEY)
        .first()
    )
    if row is None:
        row = SecuritySetting(key=RING_CONFIG_KEY,
                              json_value=json.dumps(merged))
        db.add(row)
    else:
        row.json_value = json.dumps(merged)
    db.commit()
    logger.info("alert inbox: ring config updated by %s: %s",
                current_user.username, merged)
    return {"ring": merged}
