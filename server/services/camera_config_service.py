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
Camera configuration service.
Manages per-camera configuration (protocol, source, recording) and provisions
MediaMTX via the admin service.
"""

from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import Camera, CameraConfig, CameraPermission, User
from schemas import CameraConfigCreate, CameraConfigUpdate
from services.mediamtx_admin_service import MediaMtxAdminService


class CameraConfigService:
    """Business logic for camera configuration and MediaMTX provisioning."""

    @staticmethod
    def _ensure_camera_access(
        db: Session, camera_id: int, user: User, manage_required: bool = True
    ) -> Camera:
        cam = db.query(Camera).filter(Camera.id == camera_id).first()
        if not cam:
            raise HTTPException(status_code=404, detail="Camera not found")
        if user.is_superuser or cam.owner_id == user.id:
            return cam
        if manage_required:
            perm = (
                db.query(CameraPermission)
                .filter(
                    CameraPermission.camera_id == camera_id,
                    CameraPermission.user_id == user.id,
                    CameraPermission.can_manage == True,
                )
                .first()
            )
            if perm:
                return cam
            raise HTTPException(status_code=403, detail="Not enough permissions")
        else:
            # read-only access requires can_view
            perm = (
                db.query(CameraPermission)
                .filter(
                    CameraPermission.camera_id == camera_id,
                    CameraPermission.user_id == user.id,
                    CameraPermission.can_view == True,
                )
                .first()
            )
            if perm:
                return cam
            raise HTTPException(status_code=403, detail="Not enough permissions")

    @staticmethod
    def get_config(db: Session, camera_id: int) -> CameraConfig | None:
        return (
            db.query(CameraConfig).filter(CameraConfig.camera_id == camera_id).first()
        )

    @staticmethod
    def upsert_config(
        db: Session,
        payload: CameraConfigCreate | CameraConfigUpdate,
        user: User,
        camera_id: int | None = None,
    ) -> tuple[CameraConfig, bool, bool]:
        # Prefer explicit camera_id passed by caller (e.g., router's path param for update)
        if camera_id is None:
            camera_id = getattr(payload, "camera_id", None)
        if camera_id is None:
            raise HTTPException(status_code=400, detail="camera_id required")
        cam = CameraConfigService._ensure_camera_access(
            db, camera_id, user, manage_required=True
        )
        cfg = db.query(CameraConfig).filter(CameraConfig.camera_id == camera_id).first()
        if cfg is None:
            if not isinstance(payload, CameraConfigCreate):
                raise HTTPException(
                    status_code=400, detail="Config doesn't exist; create first"
                )
            cfg = CameraConfig(camera_id=camera_id)
            db.add(cfg)
        prev_enabled = bool(getattr(cfg, "recording_enabled", False))
        data = payload.dict(exclude_unset=True)
        # Ensure camera_id is not overwritten on the record
        data.pop("camera_id", None)
        for k, v in data.items():
            setattr(cfg, k, v)
        db.commit()
        db.refresh(cfg)
        new_enabled = bool(getattr(cfg, "recording_enabled", False))
        return cfg, prev_enabled, new_enabled

    @staticmethod
    async def provision(db: Session, camera_id: int, user: User) -> dict[str, Any]:
        cam = CameraConfigService._ensure_camera_access(
            db, camera_id, user, manage_required=True
        )
        cfg = db.query(CameraConfig).filter(CameraConfig.camera_id == camera_id).first()
        if not cfg:
            raise HTTPException(status_code=400, detail="No configuration to provision")
        payload = {
            "protocol": cfg.stream_protocol,
            "source_url": cfg.source_url,
            "recording": {
                "enabled": cfg.recording_enabled,
                "path": cfg.recording_path,
                "segment_seconds": cfg.recording_segment_seconds,
            },
            "publishers": {
                "webrtc": cfg.webrtc_publisher,
                "rtmp": cfg.rtmp_publisher,
            },
            "rtsp_transport": cfg.rtsp_transport,
        }
        result = await MediaMtxAdminService.provision_path(
            cam.id, cam.ip_address, payload
        )
        return result

    @staticmethod
    async def unprovision(db: Session, camera_id: int, user: User) -> dict[str, Any]:
        cam = CameraConfigService._ensure_camera_access(
            db, camera_id, user, manage_required=True
        )
        return await MediaMtxAdminService.unprovision_path(cam.id, cam.ip_address)

    @staticmethod
    async def status(db: Session, camera_id: int, user: User) -> dict[str, Any]:
        cam = CameraConfigService._ensure_camera_access(
            db, camera_id, user, manage_required=False
        )
        return await MediaMtxAdminService.path_status(cam.id, cam.ip_address)

    # FFmpeg RTSP proxy controls removed
