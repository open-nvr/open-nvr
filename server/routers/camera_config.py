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

"""
Camera configuration and MediaMTX provisioning endpoints.

- Create/Update per-camera config (protocol, source URL, recording options)
- Provision/Unprovision path in MediaMTX through admin API (or no-op if disabled)
- Get status of MediaMTX path
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.database import get_db
from models import User
from schemas import (
    CameraConfigCreate,
    CameraConfigResponse,
    CameraConfigUpdate,
    ProvisionResult,
)
from services.audit_service import write_audit_log
from services.camera_config_service import CameraConfigService

router = APIRouter(prefix="/camera-config", tags=["camera-config"])


@router.post("/", response_model=CameraConfigResponse)
async def create_config(
    payload: CameraConfigCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    request: Request = None,
):
    cfg, prev_enabled, new_enabled = CameraConfigService.upsert_config(
        db, payload, current_user, camera_id=payload.camera_id
    )
    # Recorder integration removed; no automatic start/stop
    try:
        write_audit_log(
            db,
            action="camera_config.update",
            user_id=current_user.id,
            entity_type="camera",
            entity_id=cfg.camera_id,
            details=payload.dict(),
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return cfg


@router.put("/{camera_id}", response_model=CameraConfigResponse)
async def update_config(
    camera_id: int,
    payload: CameraConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    request: Request = None,
):
    # Attach camera_id and call upsert (requires existing config)
    payload_data = payload.dict(exclude_unset=True)
    payload_data["camera_id"] = camera_id
    cfg, prev_enabled, new_enabled = CameraConfigService.upsert_config(
        db, CameraConfigUpdate(**payload_data), current_user, camera_id=camera_id
    )
    # Recorder integration removed; no automatic start/stop
    try:
        write_audit_log(
            db,
            action="camera_config.update",
            user_id=current_user.id,
            entity_type="camera",
            entity_id=camera_id,
            details=payload.dict(exclude_unset=True),
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return cfg


@router.get("/{camera_id}", response_model=CameraConfigResponse)
async def get_config(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    cfg = CameraConfigService.get_config(db, camera_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Config not found")
    return cfg


@router.post("/{camera_id}/provision", response_model=ProvisionResult)
async def provision(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    request: Request = None,
):
    result = await CameraConfigService.provision(db, camera_id, current_user)
    try:
        write_audit_log(
            db,
            action="camera.provision",
            user_id=current_user.id,
            entity_type="camera",
            entity_id=camera_id,
            details=result,
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return ProvisionResult(
        camera_id=camera_id,
        path=result.get("path"),
        status=result.get("status"),
        details=result.get("details"),
    )


@router.post("/{camera_id}/unprovision", response_model=ProvisionResult)
async def unprovision(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    request: Request = None,
):
    result = await CameraConfigService.unprovision(db, camera_id, current_user)
    try:
        write_audit_log(
            db,
            action="camera.unprovision",
            user_id=current_user.id,
            entity_type="camera",
            entity_id=camera_id,
            details=result,
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return ProvisionResult(
        camera_id=camera_id,
        path=result.get("path"),
        status=result.get("status"),
        details=result.get("details"),
    )


@router.get("/{camera_id}/status")
async def path_status(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    return await CameraConfigService.status(db, camera_id, current_user)
