# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""``GET/PUT /site-mode`` (HA-118). See services/site_mode.py for what a mode does."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.database import get_db
from core.permissions import RequirePermission
from models import User
from services import site_mode
from services.audit_service import audit_request

router = APIRouter(tags=["site-mode"])


class SiteModeIn(BaseModel):
    mode: str
    #: Why, for the audit log (e.g. the Home Assistant automation).
    reason: str | None = None


@router.get("/site-mode")
async def get_site_mode(
    db: Session = Depends(get_db),
    current_user: User = Depends(RequirePermission("settings.view")),
):
    return {**site_mode.get(db), "modes": list(site_mode.MODES)}


@router.put("/site-mode")
async def put_site_mode(
    payload: SiteModeIn,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(RequirePermission("settings.manage")),
):
    from core.request_context import current as current_ctx
    from services.event_bus_service import publish_site_mode

    ctx = current_ctx()
    actor = (ctx.actor if ctx is not None and ctx.actor else None) or f"user:{current_user.username}"
    before = site_mode.current_mode(db)
    try:
        value = site_mode.set_mode(db, payload.mode, actor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit_request(db, request, action="site_mode.set", user_id=current_user.id,
                  entity_type="setting", entity_id=None,
                  details={"from": before, "to": payload.mode,
                           **({"reason": payload.reason[:200]} if payload.reason else {})})
    await publish_site_mode(value)
    return {**value, "modes": list(site_mode.MODES)}
