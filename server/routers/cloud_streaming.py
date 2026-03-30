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
Cloud Streaming Router

API endpoints for managing external stream publishing to custom streaming servers
(AntMedia, Nginx-RTMP, Wowza, OBS, mobile app backends, etc.).

Supports multiple stream targets per camera - each camera can stream to multiple
destinations simultaneously.
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.auth import get_current_superuser
from core.database import get_db
from models import Camera
from services.audit_service import write_audit_log
from services.cloud_streaming_service import (
    SERVER_PRESETS,
    CloudStreamingService,
    CloudStreamTarget,
    delete_cloud_stream_target,
    get_cloud_stream_targets,
    save_cloud_stream_target,
)

router = APIRouter(prefix="/cloud-streaming", tags=["cloud-streaming"])


class CloudStreamTargetCreate(BaseModel):
    """Schema for creating/updating a cloud stream target."""
    target_id: str | None = None  # If provided, updates existing; if None, creates new
    camera_id: int
    enabled: bool = False
    server_url: str = ""  # e.g., rtmp://myserver.com/live
    stream_key: str = ""  # Stream key or path
    protocol: Literal["rtmp", "rtmps", "srt"] = "rtmp"
    use_tls: bool = False
    use_custom_ca: bool = False  # Use BYOK CA certificate
    video_codec: Literal["copy", "libx264", "libx265"] = "copy"
    audio_codec: Literal["copy", "aac"] = "aac"
    video_bitrate: str | None = None
    audio_bitrate: str | None = "128k"
    max_reconnect_attempts: int = Field(5, ge=0, le=20)
    reconnect_delay_seconds: int = Field(5, ge=1, le=60)


class CloudStreamTargetResponse(BaseModel):
    """Response schema for cloud stream target."""
    target_id: str
    camera_id: int
    camera_name: str | None = None
    enabled: bool
    server_url: str
    stream_key_set: bool  # Don't expose actual key
    protocol: str
    use_tls: bool
    use_custom_ca: bool
    video_codec: str
    audio_codec: str
    video_bitrate: str | None
    audio_bitrate: str | None
    max_reconnect_attempts: int
    reconnect_delay_seconds: int
    status: str | None = None
    running: bool = False


class StreamStatusResponse(BaseModel):
    """Response schema for stream status."""
    target_id: str
    camera_id: int
    status: str
    running: bool
    started_at: str | None = None
    server_url: str | None = None
    error_message: str | None = None
    reconnect_attempts: int = 0


class ServerPresetResponse(BaseModel):
    """Response schema for server preset."""
    id: str
    name: str
    description: str
    protocol: str
    default_port: int
    video_codec: str
    audio_codec: str


@router.get("/presets")
async def get_server_presets(
    current_user=Depends(get_current_superuser),
):
    """Get available server type presets (RTMP, RTMPS, SRT)."""
    return {
        "presets": [
            ServerPresetResponse(
                id=preset_id,
                name=preset.get("name", preset_id),
                description=preset.get("description", ""),
                protocol=preset.get("protocol", "rtmp"),
                default_port=preset.get("default_port", 1935),
                video_codec=preset.get("video_codec", "copy"),
                audio_codec=preset.get("audio_codec", "copy"),
            )
            for preset_id, preset in SERVER_PRESETS.items()
        ]
    }


@router.get("/targets")
async def list_stream_targets(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """List all configured cloud stream targets."""
    targets = get_cloud_stream_targets(db)
    service = CloudStreamingService.get_instance()
    
    # Get camera names for display
    camera_names = {}
    cameras = db.query(Camera).all()
    for cam in cameras:
        camera_names[cam.id] = cam.name
    
    result = []
    for target_id, target in targets.items():
        status = await service.get_stream_status(target_id)
        result.append(CloudStreamTargetResponse(
            target_id=target_id,
            camera_id=target.camera_id,
            camera_name=camera_names.get(target.camera_id),
            enabled=target.enabled,
            server_url=target.server_url,
            stream_key_set=bool(target.stream_key),
            protocol=target.protocol,
            use_tls=target.use_tls,
            use_custom_ca=target.use_custom_ca,
            video_codec=target.video_codec,
            audio_codec=target.audio_codec,
            video_bitrate=target.video_bitrate,
            audio_bitrate=target.audio_bitrate,
            max_reconnect_attempts=target.max_reconnect_attempts,
            reconnect_delay_seconds=target.reconnect_delay_seconds,
            status=status.get("status"),
            running=status.get("running", False),
        ))
    
    return {"targets": result}


@router.get("/targets/{target_id}")
async def get_stream_target(
    target_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Get a specific cloud stream target."""
    targets = get_cloud_stream_targets(db)
    target = targets.get(target_id)
    
    if not target:
        raise HTTPException(status_code=404, detail="Stream target not found")
    
    # Get camera name
    camera = db.query(Camera).filter(Camera.id == target.camera_id).first()
    camera_name = camera.name if camera else None
    
    service = CloudStreamingService.get_instance()
    status = await service.get_stream_status(target_id)
    
    return CloudStreamTargetResponse(
        target_id=target_id,
        camera_id=target.camera_id,
        camera_name=camera_name,
        enabled=target.enabled,
        server_url=target.server_url,
        stream_key_set=bool(target.stream_key),
        protocol=target.protocol,
        use_tls=target.use_tls,
        use_custom_ca=target.use_custom_ca,
        video_codec=target.video_codec,
        audio_codec=target.audio_codec,
        video_bitrate=target.video_bitrate,
        audio_bitrate=target.audio_bitrate,
        max_reconnect_attempts=target.max_reconnect_attempts,
        reconnect_delay_seconds=target.reconnect_delay_seconds,
        status=status.get("status"),
        running=status.get("running", False),
    )


@router.post("/targets")
async def create_or_update_stream_target(
    payload: CloudStreamTargetCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    """Create or update a cloud stream target. Multiple targets per camera are supported."""
    # Verify camera exists
    camera = db.query(Camera).filter(Camera.id == payload.camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    
    # Generate new target_id if not provided (create), otherwise use provided (update)
    target_id = payload.target_id or str(uuid.uuid4())[:8]

    # If updating an existing target and stream_key is blank, preserve existing key
    existing = None
    if payload.target_id:
        existing = get_cloud_stream_targets(db).get(target_id)
        if existing and (payload.stream_key is None or payload.stream_key == ""):
            payload.stream_key = existing.stream_key
    
    target = CloudStreamTarget(
        target_id=target_id,
        camera_id=payload.camera_id,
        enabled=payload.enabled,
        server_url=payload.server_url,
        stream_key=payload.stream_key,
        protocol=payload.protocol,
        use_tls=payload.use_tls,
        use_custom_ca=payload.use_custom_ca,
        video_codec=payload.video_codec,
        audio_codec=payload.audio_codec,
        video_bitrate=payload.video_bitrate,
        audio_bitrate=payload.audio_bitrate,
        max_reconnect_attempts=payload.max_reconnect_attempts,
        reconnect_delay_seconds=payload.reconnect_delay_seconds,
    )
    
    save_cloud_stream_target(db, target)
    
    # If enabled, start the stream
    service = CloudStreamingService.get_instance()
    if target.enabled:
        result = await service.start_stream(target_id, target, db)
    else:
        # Stop if running
        result = await service.stop_stream(target_id)
    
    try:
        write_audit_log(
            db,
            action="cloud_streaming.configure",
            user_id=current_user.id,
            entity_type="stream_target",
            entity_id=target_id,
            details={
                "camera_id": payload.camera_id,
                "server_url": payload.server_url,
                "protocol": payload.protocol,
                "enabled": payload.enabled,
            },
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    
    return {
        "status": "saved",
        "target_id": target_id,
        "camera_id": payload.camera_id,
        "stream_result": result,
    }


@router.delete("/targets/{target_id}")
async def delete_stream_target(
    target_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    """Delete a cloud stream target and stop streaming."""
    service = CloudStreamingService.get_instance()
    
    # Get target info before deleting
    targets = get_cloud_stream_targets(db)
    target = targets.get(target_id)
    camera_id = target.camera_id if target else None
    
    # Stop the stream first
    await service.stop_stream(target_id)
    
    # Delete from database
    delete_cloud_stream_target(db, target_id)
    
    try:
        write_audit_log(
            db,
            action="cloud_streaming.delete",
            user_id=current_user.id,
            entity_type="stream_target",
            entity_id=target_id,
            details={"camera_id": camera_id},
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    
    return {"status": "deleted", "target_id": target_id, "camera_id": camera_id}


@router.post("/targets/{target_id}/start")
async def start_stream(
    target_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    """Start streaming to a configured target."""
    targets = get_cloud_stream_targets(db)
    target = targets.get(target_id)
    
    if not target:
        raise HTTPException(
            status_code=404,
            detail="Stream target not found"
        )
    
    if not target.server_url:
        raise HTTPException(
            status_code=400,
            detail="Server URL is required"
        )
    
    service = CloudStreamingService.get_instance()
    result = await service.start_stream(target_id, target, db)
    
    # Update enabled status
    target.enabled = True
    save_cloud_stream_target(db, target)
    
    try:
        write_audit_log(
            db,
            action="cloud_streaming.start",
            user_id=current_user.id,
            entity_type="stream_target",
            entity_id=target_id,
            details={"camera_id": target.camera_id, "server_url": target.server_url},
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    
    return result


@router.post("/targets/{target_id}/stop")
async def stop_stream(
    target_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    """Stop streaming to a target."""
    service = CloudStreamingService.get_instance()
    result = await service.stop_stream(target_id)
    
    # Update enabled status in database
    targets = get_cloud_stream_targets(db)
    target = targets.get(target_id)
    if target:
        target.enabled = False
        save_cloud_stream_target(db, target)
    
    try:
        write_audit_log(
            db,
            action="cloud_streaming.stop",
            user_id=current_user.id,
            entity_type="stream_target",
            entity_id=target_id,
            details={"camera_id": target.camera_id if target else None},
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    
    return result


@router.get("/targets/{target_id}/status")
async def get_stream_status(
    target_id: str,
    current_user=Depends(get_current_superuser),
):
    """Get the current status of a stream target."""
    service = CloudStreamingService.get_instance()
    status = await service.get_stream_status(target_id)
    return status


@router.get("/status")
async def get_all_stream_statuses(
    current_user=Depends(get_current_superuser),
):
    """Get status of all active streams."""
    service = CloudStreamingService.get_instance()
    statuses = await service.get_all_stream_statuses()
    return {"streams": statuses}
