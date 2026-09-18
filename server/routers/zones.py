# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Camera zones CRUD (HA-109).

Seeing a camera's zones needs what seeing the camera needs; changing them
needs ``cameras.manage`` and the camera (the same rule as editing it).
Detect-pipeline visits are tagged with the zones they passed through at
ingest (``TimelineEvent.zone_ids``); Home Assistant turns zones into
per-zone occupancy sensors.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.database import get_db
from core.permissions import RequirePermission, get_camera_or_403
from models import Camera, CameraZone, User
from services import zones as zone_service
from services.audit_service import audit_request
from services.camera_scope import can_view_camera

router = APIRouter(prefix="/cameras", tags=["zones"])

_manage = RequirePermission("cameras.manage")
_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9 _.-]{0,59}$")


class ZoneIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    polygon: list[list[float]]
    #: Only these object labels count in the zone; omit for all.
    labels: list[str] | None = Field(None, max_length=20)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name is empty")
        return v

    @field_validator("polygon")
    @classmethod
    def _polygon(cls, v: list[list[float]]) -> list[list[float]]:
        return zone_service.valid_polygon(v)

    @field_validator("labels")
    @classmethod
    def _labels(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        out = sorted({s.strip().lower() for s in v if s.strip()})
        for s in out:
            if not _LABEL_RE.fullmatch(s):
                raise ValueError(f"bad label {s!r}")
        return out or None


def _zone_or_404(db: Session, camera_id: int, zone_id: int) -> CameraZone:
    z = (db.query(CameraZone)
         .filter(CameraZone.id == zone_id, CameraZone.camera_id == camera_id).first())
    if z is None:
        raise HTTPException(status_code=404, detail="Zone not found")
    return z


@router.get("/{camera_id}/zones")
async def list_zones(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    cam = db.query(Camera).filter(Camera.id == camera_id, Camera.deleted_at.is_(None)).first()
    if cam is None or not can_view_camera(db, current_user, camera_id):
        raise HTTPException(status_code=404, detail="Camera not found")
    rows = (db.query(CameraZone).filter(CameraZone.camera_id == camera_id)
            .order_by(CameraZone.name).all())
    return {"camera_id": camera_id, "zones": [zone_service.serialize(z) for z in rows]}


@router.post("/{camera_id}/zones", status_code=201)
async def create_zone(
    camera_id: int,
    payload: ZoneIn,
    request: Request,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    current_user: User = Depends(_manage),
):
    count = db.query(CameraZone).filter(CameraZone.camera_id == camera.id).count()
    if count >= zone_service.MAX_ZONES_PER_CAMERA:
        raise HTTPException(status_code=409,
                            detail=f"At most {zone_service.MAX_ZONES_PER_CAMERA} zones per camera")
    zone = CameraZone(camera_id=camera.id, name=payload.name, polygon=payload.polygon,
                      labels=payload.labels)
    db.add(zone)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="A zone with that name exists") from exc
    db.refresh(zone)
    audit_request(db, request, action="zone.create", user_id=current_user.id,
                  entity_type="camera_zone", entity_id=zone.id,
                  details={"camera_id": camera.id, "name": zone.name})
    return zone_service.serialize(zone)


@router.put("/{camera_id}/zones/{zone_id}")
async def update_zone(
    camera_id: int,
    zone_id: int,
    payload: ZoneIn,
    request: Request,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    current_user: User = Depends(_manage),
):
    zone = _zone_or_404(db, camera.id, zone_id)
    zone.name, zone.polygon, zone.labels = payload.name, payload.polygon, payload.labels
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="A zone with that name exists") from exc
    db.refresh(zone)
    audit_request(db, request, action="zone.update", user_id=current_user.id,
                  entity_type="camera_zone", entity_id=zone.id,
                  details={"camera_id": camera.id, "name": zone.name})
    return zone_service.serialize(zone)


@router.delete("/{camera_id}/zones/{zone_id}")
async def delete_zone(
    camera_id: int,
    zone_id: int,
    request: Request,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    current_user: User = Depends(_manage),
):
    zone = _zone_or_404(db, camera.id, zone_id)
    name = zone.name
    db.delete(zone)
    db.commit()
    audit_request(db, request, action="zone.delete", user_id=current_user.id,
                  entity_type="camera_zone", entity_id=zone_id,
                  details={"camera_id": camera.id, "name": name})
    return {"deleted": zone_id}
