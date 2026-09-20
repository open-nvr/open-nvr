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

Scope: an operator sees, counts and acknowledges only the alerts raised
on cameras they may view (``services.camera_scope``); alerts with no
camera (a site-wide notice, a test alarm) reach everyone. The alarm
POLICY — ring modes, call/SMS/hooter actions, test alarms — is a site
decision and superuser-only.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from core.auth import get_current_active_user, get_current_superuser
from core.database import get_db, release
from models import AppAlert, SecuritySetting, User
from core.pagination import resolve_total
from services.alerts_inbox import (
    DEFAULT_RING_CONFIG,
    RING_CONFIG_KEY,
    RING_MODES,
    normalize_ring_config,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/alerts-inbox", tags=["alerts-inbox"])

_MAX_LIMIT = 200


def _scope_alerts(q, scope: set[int] | None):
    """Restrict an AppAlert query to ``scope`` (the caller's visible
    camera ids from ``camera_scope.visible_camera_ids``).

    ``AppAlert.camera_id`` is the wire handle (``cam3``) — or NULL /
    blank for an alert about nothing in particular, which every operator
    gets. Superusers (scope None) see the lot; a user granted no cameras
    sees only the camera-less rows.
    """
    if scope is None:
        return q
    handles: list[str] = []
    for cam_id in sorted(scope):
        handles += [f"cam{cam_id}", f"cam-{cam_id}", str(cam_id)]
    camera_less = or_(AppAlert.camera_id.is_(None), AppAlert.camera_id == "")
    if not handles:
        return q.filter(camera_less)
    return q.filter(or_(camera_less,
                        func.lower(AppAlert.camera_id).in_(handles)))


def _parse_bound(value: str) -> datetime | None:
    """An ISO-8601 filter bound as an aware datetime, or None.

    A bound we cannot read is dropped rather than rejected: a filter
    typo must not 500 the alert list an operator is trying to read.
    """
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


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
        # When the thing happened, as opposed to when the app decided.
        # The UI shows this and falls back to fired_at; fired_at stays
        # the ORDER (see the model) so the inbox is append-only.
        "observed_at": a.observed_at.isoformat() if a.observed_at else None,
        "severity": a.severity,
        "title": a.title,
        "description": a.description,
        "source_kind": a.source_kind,
        "source_name": a.source_name,
        "camera_id": a.camera_id,
        "correlation_id": a.correlation_id,
        "evidence": _load(a.evidence),
        "tags": _load(a.tags) or [],
        "alert_type": a.alert_type,
        # Names only. The bytes come from the image route below, which
        # applies the caller's camera scope the same way this listing
        # does — a path in this list is not itself an authorisation.
        "images": sorted((_load(a.images) or {}).keys()),
        "acknowledged_at": (a.acknowledged_at.isoformat()
                            if a.acknowledged_at else None),
        "acknowledged_by": a.acknowledged_by,
    }


@router.get("")
async def list_alerts(
    unacked: bool = Query(False, description="Only unacknowledged alerts"),
    severity: str | None = Query(None),
    source_name: str | None = Query(None),
    alert_type: str | None = Query(
        None, description="The producer's kind of alert, e.g. scanner_flag"),
    camera_id: str | None = Query(
        None, description="Camera handle as the producer sent it (cam3)"),
    from_: str | None = Query(
        None, alias="from", description="ISO-8601 lower bound on fired_at"),
    to: str | None = Query(None, description="ISO-8601 upper bound on fired_at"),
    q_text: str | None = Query(
        None, alias="q",
        description="Substring of the title or description"),
    after_id: int | None = Query(
        None, description="Only rows with id > after_id — lets the bell "
        "poll for 'anything new since my last look' cheaply"),
    before_id: int | None = Query(
        None, description="Only rows with id <= before_id — pins a paged "
        "table to the set it first saw, so arrivals cannot shuffle it"),
    skip: int = Query(0, ge=0, le=100_000),
    limit: int = Query(50, ge=1, le=_MAX_LIMIT),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Inbox listing, newest first. ``unacked=true`` is what the bell
    polls; the full list backs the Alerts & Incidents page.

    TWO counts come back, and they answer different questions. Reading
    one as the other is the mistake this docstring exists to prevent:

    ``unacked_count`` is the BELL BADGE — how many unacknowledged alerts
    exist in the caller's scope, ignoring every filter on this request
    (including ``unacked`` itself). It is why the bell can show "12"
    while the page below it shows three rows.

    ``total`` is how many rows THIS request's filters match, for the
    pager's "1-25 of 312". So ``?severity=critical`` can legitimately
    return ``unacked_count: 40`` beside ``total: 3``.
    """
    from services.camera_scope import visible_camera_ids

    scope = visible_camera_ids(db, current_user)
    q = _scope_alerts(db.query(AppAlert), scope)
    if unacked:
        q = q.filter(AppAlert.acknowledged_at.is_(None))
    if severity:
        q = q.filter(AppAlert.severity == severity)
    if source_name:
        q = q.filter(AppAlert.source_name == source_name)
    if alert_type:
        q = q.filter(AppAlert.alert_type == alert_type)
    if camera_id:
        q = q.filter(func.lower(AppAlert.camera_id) == camera_id.strip().lower())
    if from_ and (lower := _parse_bound(from_)) is not None:
        q = q.filter(AppAlert.fired_at >= lower)
    if to and (upper := _parse_bound(to)) is not None:
        q = q.filter(AppAlert.fired_at <= upper)
    if text := (q_text or "").strip():
        like = f"%{text}%"
        q = q.filter(or_(AppAlert.title.ilike(like),
                         AppAlert.description.ilike(like)))
    if after_id is not None:
        q = q.filter(AppAlert.id > after_id)
    if before_id is not None:
        q = q.filter(AppAlert.id <= before_id)
    # id is unique, so id DESC is already a total order — unlike /events,
    # this endpoint needs no tiebreaker. Ordering is applied HERE and not
    # to `q`, so the counts below do not drag it into their subquery.
    rows = q.order_by(AppAlert.id.desc()).offset(skip).limit(limit).all()
    # Same `q` — same scope, same filters. A total assembled from a
    # separately-written query is how a count for someone else's camera
    # leaks; reusing the object makes that impossible rather than
    # merely unlikely.
    total = resolve_total(len(rows), skip, limit, q.count)
    unacked_count = _unacked_count(
        db, scope,
        # The bell's own request shape answers its badge for free: an
        # unfiltered first page of unacked rows that came back SHORT is
        # already the whole unacked set. That deletes a COUNT this
        # endpoint pays today, on every poll, in every open tab.
        rows if (unacked and skip == 0 and not severity and not source_name
                 and after_id is None and before_id is None
                 and len(rows) < limit) else None,
    )
    return {"alerts": [_row_out(a) for a in rows],
            "unacked_count": unacked_count,
            "total": total}


@router.get("/{alert_id}/images/{name}")
async def get_alert_image(
    alert_id: int,
    name: str,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """One evidence photo off an alert — the face, the body, the scene.

    ``name`` is the key the producer used, not a path: the path comes
    from the row, so a caller cannot ask for a file the alert does not
    reference.
    """
    from services.camera_scope import visible_camera_ids

    scope = visible_camera_ids(db, current_user)
    row = (_scope_alerts(db.query(AppAlert), scope)
           .filter(AppAlert.id == alert_id).first())
    if row is None or not row.images:
        # 404 rather than 403 throughout this router: whether an alert
        # exists on a camera you cannot see is itself not your business.
        raise HTTPException(status_code=404, detail="no such alert image")
    try:
        rel = (json.loads(row.images) or {}).get(name)
    except ValueError:
        rel = None
    if not isinstance(rel, str) or not rel:
        raise HTTPException(status_code=404, detail="no such alert image")

    from services.evidence_store import resolve_evidence

    path = resolve_evidence(rel)
    if path is None:
        raise HTTPException(status_code=404, detail="evidence file missing")
    # Don't hold a pooled connection for a client-paced JPEG transfer.
    release(db)
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=86400"})


def _unacked_count(db, scope, proven_rows) -> int:
    """The bell badge: unacknowledged alerts in scope, filter-blind.

    ``proven_rows`` short-circuits the query when the caller already
    holds the complete unacked set (see the call site); None means
    count.
    """
    if proven_rows is not None:
        return len(proven_rows)
    return (
        _scope_alerts(db.query(AppAlert.id), scope)
        .filter(AppAlert.acknowledged_at.is_(None))
        .count()
    )


class AckIn(BaseModel):
    """What to silence. Three shapes, and the caller must pick one:

    * ``ids`` — exactly these alerts ("acknowledge these 25").
    * ``source_name`` and/or ``severity`` — every unacknowledged alert in
      the caller's scope matching them ("acknowledge all 39 vehicle
      alarms"). Added so a paged table's "all" button can mean the whole
      filtered set rather than whichever page happens to be loaded.
    * empty — every unacknowledged alert in scope. Unchanged.

    ``ids`` and the filters are mutually exclusive: a body carrying both
    reads as two different intentions, and guessing which one an operator
    meant is not a thing to do with a control that silences alarms.
    """

    ids: list[int] | None = None
    source_name: str | None = None
    severity: str | None = None

    def filters(self) -> dict[str, str]:
        return {k: v for k, v in (("source_name", self.source_name),
                                  ("severity", self.severity)) if v}


@router.post("/ack")
async def acknowledge(
    payload: AckIn,
    request: Request,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Silence alerts. Idempotent — an already-acked id is left with its
    ORIGINAL acknowledgement (first silencer wins the audit trail).
    Only alerts the caller can see are touched: "ack all" silences MY
    inbox, and an id on someone else's camera is simply not found."""
    from services.camera_scope import visible_camera_ids

    filters = payload.filters()
    if payload.ids is not None and filters:
        raise HTTPException(
            status_code=400,
            detail="Send ids OR a filter, not both — they are two "
                   "different intentions.",
        )
    q = _scope_alerts(db.query(AppAlert),
                      visible_camera_ids(db, current_user)).filter(
        AppAlert.acknowledged_at.is_(None))
    if payload.ids is not None:
        if not payload.ids:
            return {"acknowledged": 0}
        q = q.filter(AppAlert.id.in_(payload.ids))
    else:
        # Scope is applied above and is NOT negotiable here: a filtered
        # "acknowledge all" must still be unable to touch a camera the
        # caller cannot open.
        if filters.get("source_name"):
            q = q.filter(AppAlert.source_name == filters["source_name"])
        if filters.get("severity"):
            q = q.filter(AppAlert.severity == filters["severity"])
    now = datetime.now(UTC)
    acked_ids: list[int] = []
    for row in q.all():
        row.acknowledged_at = now
        row.acknowledged_by = current_user.id
        acked_ids.append(row.id)
    db.commit()
    count = len(acked_ids)
    if count:
        logger.info("alert inbox: %d alert(s) acknowledged by %s",
                    count, current_user.username)
        # Silencing an alarm is exactly the kind of action an investigator
        # needs to attribute later (who, when, from where, which automation).
        from services.audit_service import audit_request

        audit_request(
            db, request, action="alerts.ack", user_id=current_user.id,
            entity_type="app_alert",
            details={"count": count, "ids": acked_ids[:200],
                     **({"filter": filters} if filters else {})},
        )
    return {"acknowledged": count}


class TestAlarmIn(BaseModel):
    severity: str = "high"


@router.post("/test")
async def fire_test_alarm(
    payload: TestAlarmIn,
    current_user: User = Depends(get_current_superuser),
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
    current_user: User = Depends(get_current_superuser),
    db: Session = Depends(get_db),
):
    """Call / SMS / hooter configuration, secret masked (set/unset)."""
    from services.alarm_actions import load_action_config, masked_action_config

    return {"actions": masked_action_config(load_action_config(db))}


@router.put("/actions")
async def put_alarm_actions(
    payload: ActionConfigIn,
    current_user: User = Depends(get_current_superuser),
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
    current_user: User = Depends(get_current_superuser),
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
    current_user: User = Depends(get_current_superuser),
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
