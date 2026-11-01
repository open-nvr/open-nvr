# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Per-camera detection settings: read and replace a camera's overrides of
the detect-pipeline's global knobs (services/detection_settings.py).

Same rule as zones and masks: seeing needs what seeing the camera needs;
changing needs ``cameras.manage`` and the camera. PUT replaces the whole
record; an empty body clears every override. The detect-pipeline picks a
change up on its next reconcile tick (the camera's worker restarts).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.database import get_db
from core.permissions import RequirePermission, get_camera_or_403
from models import Camera, User
from services import detection_settings as ds
from services.audit_service import audit_request
from services.camera_scope import can_view_camera

router = APIRouter(prefix="/cameras", tags=["detection-settings"])

_manage = RequirePermission("cameras.manage")


@router.get("/{camera_id}/detection-settings")
async def get_detection_settings(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    cam = db.query(Camera).filter(Camera.id == camera_id, Camera.deleted_at.is_(None)).first()
    if cam is None or not can_view_camera(db, current_user, camera_id):
        raise HTTPException(status_code=404, detail="Camera not found")
    return ds.serialize(cam)


@router.put("/{camera_id}/detection-settings")
async def put_detection_settings(
    camera_id: int,
    payload: ds.DetectionSettingsIn,
    request: Request,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    current_user: User = Depends(_manage),
):
    before = ds.normalize(camera.detection_settings)
    camera.detection_settings = payload.stored()
    db.commit()
    db.refresh(camera)
    after = ds.normalize(camera.detection_settings)
    audit_request(db, request, action="camera.detection_settings", user_id=current_user.id,
                  entity_type="camera", entity_id=camera.id,
                  details={"camera_id": camera.id, "from": before, "to": after})
    return ds.serialize(camera)
