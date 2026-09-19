# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""``GET /search`` (HA-116): one query over visits/events and app alerts.

What Home Assistant's Assist asks ("was there a car in the driveway this
morning?") and what the ``opennvr.search_events`` service answers. ``q``
matches labels and plates on events, and title, description and source on
alerts. It is also put, as written, to every enabled app that searches
footage in plain language (``services/footage_query.py``, HA-502): their
rows come back as ``footage`` results, and ``semantic`` says whether any app
was asked.

``GET /search/summary`` counts what happened in a period, per camera: events
by label, alerts by severity. Assist's ``summarize_period`` reads it.

Scoped like everything else: events need ``recordings.view``, alerts need
``alerts.view``, both only on cameras the caller can see. A caller holding
neither gets an empty result, not an error.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, cast, func, or_
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.database import get_db
from core.permissions import user_has_permission
from models import AppAlert, Camera, CameraZone, TimelineEvent, User
from services.camera_scope import scope_query, visible_camera_ids

router = APIRouter(tags=["search"])

MAX_LIMIT = 100


def _zone_filter(zone_id: int):
    """``zone_id`` in the JSON list ``events.zone_ids``, dialect-neutral.

    The column holds ``json.dumps`` text (``[1, 4]``) on every backend, so
    four LIKE shapes cover first/only/last/middle without JSON operators
    that SQLite and Postgres spell differently.
    """
    text = cast(TimelineEvent.zone_ids, String)
    z = str(int(zone_id))
    return or_(text.like(f"[{z}]"), text.like(f"[{z},%"),
               text.like(f"%, {z}]"), text.like(f"%, {z},%"))


def _resolve_zone(db: Session, zone: str | None, camera_id: int | None,
                  scope: set[int] | None) -> int | None:
    """A zone id from an id or a name. Names match exactly (case-insensitive,
    no wildcards) and only among cameras the caller can see: another user's
    zone names are not an oracle."""
    from sqlalchemy import func

    if zone is None or zone == "":
        return None
    if zone.isdigit():
        return int(zone)
    q = db.query(CameraZone).filter(func.lower(CameraZone.name) == zone.strip().lower())
    if scope is not None:
        q = q.filter(CameraZone.camera_id.in_(scope or {-1}))
    if camera_id is not None:
        q = q.filter(CameraZone.camera_id == camera_id)
    rows = q.all()
    if not rows:
        raise HTTPException(status_code=404, detail=f"No zone named {zone!r}")
    if len(rows) > 1:
        raise HTTPException(status_code=409,
                            detail=f"Zone name {zone!r} is on several cameras; add camera_id")
    return rows[0].id


def _alert_camera_num(handle: str | None) -> int | None:
    from services.alerts_inbox import _camera_num

    return _camera_num(handle)


@router.get("/search")
async def search(
    q: str | None = Query(None, max_length=200),
    type: Literal["all", "events", "alerts"] = Query("all"),
    camera_id: int | None = None,
    label: str | None = Query(None, max_length=60),
    zone: str | None = Query(None, max_length=60, description="zone id or name"),
    plate: str | None = Query(None, max_length=32),
    source: str | None = Query(None, max_length=100),
    severity: str | None = Query(None, pattern="^(low|medium|high|critical)$"),
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    limit: int = Query(25, ge=1, le=MAX_LIMIT),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Newest first. Each result has ``kind`` (event|alert|footage), ``at``
    and ``camera_id`` plus its own fields; events carry ``evidence_url`` (sign
    it with ``POST /media/sign`` for a phone); footage rows carry ``labels``,
    ``caption`` and the ``source`` app."""
    from routers.alerts_inbox import _scope_alerts
    from routers.timeline_events import _serialize

    scope = visible_camera_ids(db, current_user)
    if camera_id is not None and scope is not None and camera_id not in scope:
        raise HTTPException(status_code=404, detail="Camera not found")
    zone_id = _resolve_zone(db, zone, camera_id, scope)
    text = (q or "").strip()
    results: list[dict] = []

    if type in ("all", "events") and user_has_permission(current_user, "recordings.view"):
        eq = scope_query(db.query(TimelineEvent), TimelineEvent.camera_id, scope)
        if camera_id is not None:
            eq = eq.filter(TimelineEvent.camera_id == camera_id)
        if label:
            eq = eq.filter(TimelineEvent.label == label.strip().lower())
        if source:
            eq = eq.filter(TimelineEvent.source == source)
        if plate:
            norm = re.sub(r"[^A-Za-z0-9]", "", plate).upper()
            eq = eq.filter(TimelineEvent.plate_text.ilike(f"%{norm}%"))
        if zone_id is not None:
            eq = eq.filter(_zone_filter(zone_id))
        if from_ is not None:
            eq = eq.filter(or_(TimelineEvent.ended_at >= from_,
                               TimelineEvent.started_at >= from_))
        if to is not None:
            eq = eq.filter(TimelineEvent.started_at < to)
        if text:
            like = f"%{text}%"
            eq = eq.filter(or_(TimelineEvent.label.ilike(like),
                               TimelineEvent.plate_text.ilike(like)))
        for e in eq.order_by(TimelineEvent.started_at.desc()).limit(limit).all():
            row = _serialize(e)
            results.append({"kind": "event", "at": row["started_at"], **row})

    wants_alerts = (type in ("all", "alerts") and not label and not plate and zone_id is None)
    if wants_alerts and user_has_permission(current_user, "alerts.view"):
        aq = _scope_alerts(db.query(AppAlert), scope)
        if camera_id is not None:
            # In SQL, not after the limit: the same handle forms the inbox
            # stores ("cam3", "cam-3", "3").
            aq = aq.filter(AppAlert.camera_id.in_(
                [f"cam{camera_id}", f"cam-{camera_id}", str(camera_id)]))
        if severity:
            aq = aq.filter(AppAlert.severity == severity)
        if source:
            aq = aq.filter(AppAlert.source_name == source)
        if from_ is not None:
            aq = aq.filter(AppAlert.fired_at >= from_)
        if to is not None:
            aq = aq.filter(AppAlert.fired_at < to)
        if text:
            like = f"%{text}%"
            aq = aq.filter(or_(AppAlert.title.ilike(like), AppAlert.description.ilike(like),
                               AppAlert.source_name.ilike(like)))
        for a in aq.order_by(AppAlert.fired_at.desc()).limit(limit).all():
            cam = _alert_camera_num(a.camera_id)
            results.append({
                "kind": "alert", "at": a.fired_at.isoformat() if a.fired_at else None,
                "id": a.id, "alert_id": a.alert_id, "camera_id": cam, "severity": a.severity,
                "title": a.title, "description": a.description, "source": a.source_name,
                "acknowledged": a.acknowledged_at is not None,
                "correlation_id": a.correlation_id,
            })

    # Plain-language footage search (HA-502): recorded footage, so the same
    # permission as events; zone, plate, source and severity are filters
    # the apps can't apply, so they leave it out.
    asked: list[str] = []
    failed: list[str] = []
    if (text and type in ("all", "events") and zone_id is None and not plate
            and not source and not severity
            and user_has_permission(current_user, "recordings.view")):
        from services import footage_query

        found, asked, failed = await footage_query.search(
            db, current_user, text, limit=limit, scope=scope, camera_id=camera_id,
            label=label, from_=from_, to=to)
        results.extend(found)

    results.sort(key=lambda r: r.get("at") or "", reverse=True)
    return {"results": results[:limit], "semantic": bool(asked),
            "semantic_sources": asked, "semantic_errors": failed,
            "filters": {"q": text or None, "type": type, "camera_id": camera_id,
                        "label": label, "zone_id": zone_id, "plate": plate, "source": source,
                        "severity": severity}}


#: Longest period one summary covers.
MAX_SUMMARY_DAYS = 31


@router.get("/search/summary")
async def search_summary(
    from_: datetime = Query(..., alias="from"),
    to: datetime | None = None,
    camera_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """What happened between ``from`` and ``to`` (default now), per camera:
    events counted by label (and the first and last), alerts by severity.
    Scoped like ``/search``: events need ``recordings.view``, alerts
    ``alerts.view``, on visible cameras only. Cameras with nothing are
    left out."""
    from datetime import UTC, timedelta

    from routers.alerts_inbox import _scope_alerts

    def aware(value: datetime) -> datetime:  # a naive time is UTC
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    start, end = aware(from_), aware(to or datetime.now(UTC))
    if end <= start:
        raise HTTPException(status_code=422, detail="'to' must be after 'from'")
    if end - start > timedelta(days=MAX_SUMMARY_DAYS):
        raise HTTPException(status_code=422,
                            detail=f"A summary covers at most {MAX_SUMMARY_DAYS} days")
    scope = visible_camera_ids(db, current_user)
    if camera_id is not None and scope is not None and camera_id not in scope:
        raise HTTPException(status_code=404, detail="Camera not found")

    cams: dict[int, dict] = {}

    def cam(cid: int) -> dict:
        return cams.setdefault(cid, {"camera_id": cid, "events": {}, "event_count": 0,
                                     "first_event": None, "last_event": None,
                                     "alerts": {}, "alert_count": 0})

    if user_has_permission(current_user, "recordings.view"):
        eq = scope_query(db.query(TimelineEvent.camera_id, TimelineEvent.label,
                                  func.count(TimelineEvent.id),
                                  func.min(TimelineEvent.started_at),
                                  func.max(TimelineEvent.started_at)),
                         TimelineEvent.camera_id, scope)
        eq = eq.filter(TimelineEvent.started_at >= start, TimelineEvent.started_at < end)
        if camera_id is not None:
            eq = eq.filter(TimelineEvent.camera_id == camera_id)
        for cid, label, count, first, last in eq.group_by(
                TimelineEvent.camera_id, TimelineEvent.label).all():
            if cid is None:
                continue
            c = cam(cid)
            c["events"][label or "unknown"] = count
            c["event_count"] += count
            for key, value, pick in (("first_event", first, min), ("last_event", last, max)):
                if value is not None:
                    iso = value.isoformat()
                    c[key] = iso if c[key] is None else pick(c[key], iso)

    site_alerts: dict[str, int] = {}   # alerts about no camera in particular
    if user_has_permission(current_user, "alerts.view"):
        aq = _scope_alerts(db.query(AppAlert.camera_id, AppAlert.severity,
                                    func.count(AppAlert.id)), scope)
        aq = aq.filter(AppAlert.fired_at >= start, AppAlert.fired_at < end)
        for handle, severity, count in aq.group_by(AppAlert.camera_id, AppAlert.severity).all():
            sev = severity or "unknown"
            cid = _alert_camera_num(handle)
            if cid is None:
                if camera_id is None and not handle:
                    site_alerts[sev] = site_alerts.get(sev, 0) + count
                continue
            if camera_id is not None and cid != camera_id:
                continue
            c = cam(cid)
            c["alerts"][sev] = c["alerts"].get(sev, 0) + count
            c["alert_count"] += count

    names = dict(db.query(Camera.id, Camera.name).filter(Camera.id.in_(list(cams) or [-1])).all())
    rows = sorted(cams.values(), key=lambda c: (-(c["event_count"] + c["alert_count"]),
                                                 c["camera_id"]))
    for c in rows:
        c["name"] = names.get(c["camera_id"])
    return {"from": start.isoformat(), "to": end.isoformat(), "cameras": rows,
            "site_alerts": site_alerts,
            "totals": {"events": sum(c["event_count"] for c in rows),
                       "alerts": sum(c["alert_count"] for c in rows)
                       + sum(site_alerts.values())}}
