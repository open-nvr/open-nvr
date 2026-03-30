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
Streams router for MediaMTX integration.

Provides endpoints to retrieve live stream URLs and JWT tokens for cameras
after enforcing user permissions.

Security Architecture:
- All MediaMTX services are bound to localhost only
- Backend is the sole authority for stream access
- JWT tokens are issued per-user, per-camera with short expiry
- MediaMTX validates tokens via JWKS endpoint
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.config import settings
from core.database import get_db
from models import Camera, User
from services.camera_service import CameraService
from services.mediamtx_jwt_service import MediaMtxJwtService
from services.stream_service import _build_stream_name

router = APIRouter(prefix="/streams", tags=["streams"])


def _check_camera_permission(
    db: Session, camera_id: int, user: User, require_manage: bool = False
) -> Camera:
    """
    Check user has permission to access camera.

    Returns camera if authorized, raises HTTPException otherwise.
    """
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    try:
        CameraService.get_camera_by_id(db=db, camera_id=camera_id, user_id=user.id)
    except HTTPException as e:
        if e.status_code == status.HTTP_403_FORBIDDEN:
            allowed = CameraService.user_has_permission(
                db, camera_id, user.id, require_manage=require_manage
            )
            if not allowed:
                raise HTTPException(status_code=403, detail="Not enough permissions")
        else:
            raise

    return camera


@router.get("/token/{camera_id}")
async def get_stream_token(
    camera_id: int,
    expiry_minutes: int | None = 60,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Get a JWT token for accessing a camera stream.

    This token must be included in the Authorization header when
    accessing MediaMTX endpoints (WebRTC, HLS, RTSP, Playback).

    Security:
    - Token is scoped to specific camera
    - Short-lived (default 60 minutes)
    - Includes user identity for audit
    """
    camera = _check_camera_permission(db, camera_id, current_user)

    stream_name = _build_stream_name(
        settings.mediamtx_stream_prefix, camera_id, camera.ip_address
    )

    # Generate JWT token for stream access
    token = MediaMtxJwtService.create_stream_token(
        user_id=current_user.id,
        username=current_user.username,
        camera_id=camera_id,
        camera_path=stream_name,
        actions=["read"],
        expiry_minutes=expiry_minutes,
    )

    return {
        "camera_id": camera_id,
        "token": token,
        "token_type": "Bearer",
        "expires_in_minutes": expiry_minutes,
        "stream_name": stream_name,
        "usage": "Include in Authorization header: Bearer <token>",
    }


@router.get("/playback-token/{camera_id}")
async def get_playback_token(
    camera_id: int,
    expiry_minutes: int | None = 30,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Get a JWT token for accessing camera recordings/playback.

    Security:
    - Includes playback permission
    - Shorter expiry than live streams
    """
    camera = _check_camera_permission(db, camera_id, current_user)

    stream_name = _build_stream_name(
        settings.mediamtx_stream_prefix, camera_id, camera.ip_address
    )

    token = MediaMtxJwtService.create_playback_token(
        user_id=current_user.id,
        username=current_user.username,
        camera_id=camera_id,
        expiry_minutes=expiry_minutes,
    )

    return {
        "camera_id": camera_id,
        "token": token,
        "token_type": "Bearer",
        "expires_in_minutes": expiry_minutes,
        "stream_name": stream_name,
        "usage": "Include in Authorization header: Bearer <token>",
    }


@router.get("/webrtc/{camera_id}")
async def get_whep_url(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Return WebRTC WHEP URL and JWT token for the specified camera.

    Security:
    - JWT token required for MediaMTX access
    - Token scoped to this camera only
    - 60-minute expiry

    Note: MediaMTX is bound to localhost. Frontend must proxy through backend
    or use the token with a backend WebRTC proxy endpoint.
    """
    camera = _check_camera_permission(db, camera_id, current_user)

    stream_name = _build_stream_name(
        settings.mediamtx_stream_prefix, camera_id, camera.ip_address
    )

    # Generate JWT token
    token = MediaMtxJwtService.create_stream_token(
        user_id=current_user.id,
        username=current_user.username,
        camera_id=camera_id,
        camera_path=stream_name,
        actions=["read"],
        expiry_minutes=60,
    )

    # Internal MediaMTX URL (localhost only)
    # Use external URL for browser access if configured
    webrtc_base = (
        settings.mediamtx_external_base_url
        or settings.mediamtx_base_url
        or "http://127.0.0.1:8889"
    )
    whep_url = f"{webrtc_base.rstrip('/')}/{stream_name}/whep"

    return {
        "camera_id": camera_id,
        "whep_url": whep_url,
        "token": token,
        "token_type": "Bearer",
        "stream_name": stream_name,
        "note": "MediaMTX is localhost-only. Use token via backend proxy.",
    }


@router.get("/hls/{camera_id}")
async def get_hls_url(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Return HLS URL and JWT token for the specified camera.

    Security:
    - JWT token required for MediaMTX access
    - Token scoped to this camera only
    """
    camera = _check_camera_permission(db, camera_id, current_user)

    stream_name = _build_stream_name(
        settings.mediamtx_stream_prefix, camera_id, camera.ip_address
    )

    # Generate JWT token
    token = MediaMtxJwtService.create_stream_token(
        user_id=current_user.id,
        username=current_user.username,
        camera_id=camera_id,
        camera_path=stream_name,
        actions=["read"],
        expiry_minutes=60,
    )

    # Internal MediaMTX URL (localhost only)
    # Use external URL for browser access if configured
    hls_base = (
        settings.mediamtx_external_hls_url
        or settings.mediamtx_hls_url
        or "http://127.0.0.1:8888"
    )
    hls_url = f"{hls_base.rstrip('/')}/{stream_name}/index.m3u8"

    return {
        "camera_id": camera_id,
        "hls_url": hls_url,
        "token": token,
        "token_type": "Bearer",
        "stream_name": stream_name,
        "note": "MediaMTX is localhost-only. Use token via backend proxy.",
    }


@router.get("/{camera_id}/info")
async def get_stream_info(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Get comprehensive stream information and JWT token for a camera.

    Returns all stream URLs and a single token valid for all protocols.
    """
    camera = _check_camera_permission(db, camera_id, current_user)

    stream_name = _build_stream_name(
        settings.mediamtx_stream_prefix, camera_id, camera.ip_address
    )

    # Generate JWT token with all read permissions
    token = MediaMtxJwtService.create_stream_token(
        user_id=current_user.id,
        username=current_user.username,
        camera_id=camera_id,
        camera_path=stream_name,
        actions=["read", "playback"],
        expiry_minutes=60,
    )

    # Build internal URLs (localhost only)
    # Use external URLs for browser access, fall back to internal URLs if not configured
    webrtc_base = (
        settings.mediamtx_external_base_url
        or settings.mediamtx_base_url
        or "http://127.0.0.1:8889"
    )
    hls_base = (
        settings.mediamtx_external_hls_url
        or settings.mediamtx_hls_url
        or "http://127.0.0.1:8888"
    )
    rtsp_base = settings.mediamtx_rtsp_url or "rtsp://127.0.0.1:8554"
    playback_base = (
        settings.mediamtx_external_playback_url
        or settings.mediamtx_playback_url
        or "http://127.0.0.1:9996"
    )

    return {
        "camera_id": camera_id,
        "stream_name": stream_name,
        "token": token,
        "token_type": "Bearer",
        "expires_in_minutes": 60,
        "urls": {
            "webrtc": f"{webrtc_base.rstrip('/')}/{stream_name}/whep",
            "hls": f"{hls_base.rstrip('/')}/{stream_name}/index.m3u8",
            "rtsp": f"{rtsp_base.rstrip('/')}/{stream_name}",
            "playback": f"{playback_base.rstrip('/')}/{stream_name}",
        },
        "camera": {
            "name": camera.name,
            "ip_address": camera.ip_address,
            "status": camera.status,
        },
        "security_note": "All MediaMTX services are localhost-only. Access via backend proxy with JWT.",
    }
