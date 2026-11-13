# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Camera masks CRUD: what Tier-0 must ignore in a camera's picture.

Same rule as zones: seeing a camera's masks needs what seeing the camera
needs; changing them needs ``cameras.manage`` and the camera. The
detect-pipeline picks a change up on its next reconcile tick (the camera's
worker restarts with the mask rasterised); nothing here touches recording.
"""

from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.database import get_db
from core.permissions import RequirePermission, get_camera_or_403
from models import Camera, CameraMask, User
from services import masks as mask_service
from services import zones as zone_service
from services.audit_service import audit_request
from services.camera_scope import can_view_camera

router = APIRouter(prefix="/cameras", tags=["masks"])

_manage = RequirePermission("cameras.manage")
_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9 _.-]{0,59}$")


class MaskIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    kind: Literal["motion", "object"] = "motion"
    polygon: list[list[float]]
    #: Object masks only: drop only these labels; omit for all. Ignored
    #: (stored as null) on a motion mask, which has no labels to filter.
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

    @property
    def stored_labels(self) -> list[str] | None:
        return self.labels if self.kind == "object" else None


def _mask_or_404(db: Session, camera_id: int, mask_id: int) -> CameraMask:
    m = (db.query(CameraMask)
         .filter(CameraMask.id == mask_id, CameraMask.camera_id == camera_id).first())
    if m is None:
        raise HTTPException(status_code=404, detail="Mask not found")
    return m


@router.get("/{camera_id}/masks")
async def list_masks(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    cam = db.query(Camera).filter(Camera.id == camera_id, Camera.deleted_at.is_(None)).first()
    if cam is None or not can_view_camera(db, current_user, camera_id):
        raise HTTPException(status_code=404, detail="Camera not found")
    rows = (db.query(CameraMask).filter(CameraMask.camera_id == camera_id)
            .order_by(CameraMask.kind, CameraMask.name).all())
    return {"camera_id": camera_id, "masks": [mask_service.serialize(m) for m in rows]}


@router.post("/{camera_id}/masks", status_code=201)
async def create_mask(
    camera_id: int,
    payload: MaskIn,
    request: Request,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    current_user: User = Depends(_manage),
):
    count = db.query(CameraMask).filter(CameraMask.camera_id == camera.id).count()
    if count >= mask_service.MAX_MASKS_PER_CAMERA:
        raise HTTPException(status_code=409,
                            detail=f"At most {mask_service.MAX_MASKS_PER_CAMERA} masks per camera")
    mask = CameraMask(camera_id=camera.id, name=payload.name, kind=payload.kind,
                      polygon=payload.polygon, labels=payload.stored_labels)
    db.add(mask)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="A mask with that name exists") from exc
    db.refresh(mask)
    audit_request(db, request, action="mask.create", user_id=current_user.id,
                  entity_type="camera_mask", entity_id=mask.id,
                  details={"camera_id": camera.id, "name": mask.name, "kind": mask.kind})
    return mask_service.serialize(mask)


@router.put("/{camera_id}/masks/{mask_id}")
async def update_mask(
    camera_id: int,
    mask_id: int,
    payload: MaskIn,
    request: Request,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    current_user: User = Depends(_manage),
):
    mask = _mask_or_404(db, camera.id, mask_id)
    mask.name, mask.kind, mask.polygon = payload.name, payload.kind, payload.polygon
    mask.labels = payload.stored_labels
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="A mask with that name exists") from exc
    db.refresh(mask)
    audit_request(db, request, action="mask.update", user_id=current_user.id,
                  entity_type="camera_mask", entity_id=mask.id,
                  details={"camera_id": camera.id, "name": mask.name, "kind": mask.kind})
    return mask_service.serialize(mask)


@router.delete("/{camera_id}/masks/{mask_id}")
async def delete_mask(
    camera_id: int,
    mask_id: int,
    request: Request,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    current_user: User = Depends(_manage),
):
    mask = _mask_or_404(db, camera.id, mask_id)
    name, kind = mask.name, mask.kind
    db.delete(mask)
    db.commit()
    audit_request(db, request, action="mask.delete", user_id=current_user.id,
                  entity_type="camera_mask", entity_id=mask_id,
                  details={"camera_id": camera.id, "name": name, "kind": kind})
    return {"deleted": mask_id}
