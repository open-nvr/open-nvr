# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The escalation proposal per camera (services/evidence_escalation.py):
read the evaluation and the decision; accept (detect on the main stream),
dismiss for a while, or reset. Seeing needs the camera; deciding needs
``cameras.manage`` and the camera, and is audited.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.database import get_db
from core.permissions import RequirePermission, get_camera_or_403
from models import Camera, User
from services import evidence_escalation as esc
from services.audit_service import audit_request
from services.camera_scope import can_view_camera

router = APIRouter(prefix="/cameras", tags=["evidence-escalation"])

_manage = RequirePermission("cameras.manage")


class DismissIn(BaseModel):
    days: int = Field(esc.DISMISS_DAYS_DEFAULT, ge=1, le=esc.DISMISS_DAYS_MAX)


@router.get("/{camera_id}/escalation")
async def get_escalation(
    camera_id: int,
    fresh: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    cam = db.query(Camera).filter(Camera.id == camera_id, Camera.deleted_at.is_(None)).first()
    if cam is None or not can_view_camera(db, current_user, camera_id):
        raise HTTPException(status_code=404, detail="Camera not found")
    return esc.view(db, camera_id, fresh=fresh)


@router.post("/{camera_id}/escalation/accept")
async def accept_escalation(
    camera_id: int,
    request: Request,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    current_user: User = Depends(_manage),
):
    rec = esc.accept(db, camera, by=current_user.username)
    audit_request(db, request, action="camera.escalation.accept", user_id=current_user.id,
                  entity_type="camera", entity_id=camera.id,
                  details={"camera_id": camera.id, "previous": rec.get("previous"), "to": "main"})
    return esc.view(db, camera_id, fresh=True)


@router.post("/{camera_id}/escalation/dismiss")
async def dismiss_escalation(
    camera_id: int,
    request: Request,
    payload: DismissIn | None = None,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    current_user: User = Depends(_manage),
):
    days = payload.days if payload else esc.DISMISS_DAYS_DEFAULT
    rec = esc.dismiss(db, camera, by=current_user.username, days=days)
    audit_request(db, request, action="camera.escalation.dismiss", user_id=current_user.id,
                  entity_type="camera", entity_id=camera.id,
                  details={"camera_id": camera.id, "until": rec.get("dismissed_until")})
    return esc.view(db, camera_id)


@router.post("/{camera_id}/escalation/reset")
async def reset_escalation(
    camera_id: int,
    request: Request,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    current_user: User = Depends(_manage),
):
    rec = esc.reset(db, camera, by=current_user.username)
    audit_request(db, request, action="camera.escalation.reset", user_id=current_user.id,
                  entity_type="camera", entity_id=camera.id,
                  details={"camera_id": camera.id, "was": (rec or {}).get("status")})
    return esc.view(db, camera_id, fresh=True)
