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
from services.stream_service import _build_stream_name, substream_name

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

    # When the low-res agent substream is enabled, scope the token to BOTH the
    # main path and its "-sub" sibling so the same token authorizes either WHEP
    # (the camera-agent's live view uses the sub; the main UI uses the main).
    # Off → an exact-match scope on the main path, unchanged.
    if settings.agent_live_use_substream:
        import re

        token_path = f"~^{re.escape(stream_name)}(-sub)?$"
    else:
        token_path = stream_name

    # Generate JWT token with all read permissions
    token = MediaMtxJwtService.create_stream_token(
        user_id=current_user.id,
        username=current_user.username,
        camera_id=camera_id,
        camera_path=token_path,
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
    # RTSPS URL uses the same external/internal fallback as HLS/WebRTC so the
    # value handed to browsers is resolvable on their network. See V-019.
    rtsps_base = (
        settings.mediamtx_external_rtsps_url
        or settings.mediamtx_rtsps_url
        or "rtsps://127.0.0.1:8322"
    )
    playback_base = (
        settings.mediamtx_external_playback_url
        or settings.mediamtx_playback_url
        or "http://127.0.0.1:9996"
    )

    # Plaintext RTSP is not exposed to external clients regardless of
    # whether MediaMTX is configured in "strict" or "optional" mode:
    #
    #   * strict mode — plaintext listener doesn't bind at all.
    #   * optional mode — plaintext :8554 binds but is internal-only:
    #     not port-mapped to the host in bridge compose, pinned to
    #     127.0.0.1 in host-mode compose. It exists solely so KAI-C's
    #     inference tap can read from MediaMTX over loopback. Browser
    #     clients and external tools never see it.
    #
    # We emit ``urls.rtsp: None`` either way for backwards compatibility
    # (clients keying into ['urls']['rtsp'] get an explicit signal
    # rather than a KeyError) and direct them at ``urls.rtsps`` instead.
    # ``rtsp_disabled_reason`` makes the intent discoverable from the
    # API itself. See docs/SECURITY_ARCHITECTURE.md §"RTSP encryption
    # posture" for the trust-boundary rationale.
    return {
        "camera_id": camera_id,
        "stream_name": stream_name,
        "token": token,
        "token_type": "Bearer",
        "expires_in_minutes": 60,
        "urls": {
            "webrtc": f"{webrtc_base.rstrip('/')}/{stream_name}/whep",
            # Low-res agent live view: present only when substreams are enabled.
            # The camera-agent prefers this to cut WebRTC decode CPU; the main
            # UI ignores it and keeps using the full-res "webrtc" above.
            **(
                {"webrtc_sub": f"{webrtc_base.rstrip('/')}/{substream_name(stream_name)}/whep"}
                if settings.agent_live_use_substream else {}
            ),
            "hls": f"{hls_base.rstrip('/')}/{stream_name}/index.m3u8",
            "rtsps": f"{rtsps_base.rstrip('/')}/{stream_name}",
            "rtsp": None,
            "rtsp_disabled_reason": (
                "plaintext RTSP is internal-only (used by the in-host "
                "inference tap); use urls.rtsps for external clients"
            ),
            "playback": f"{playback_base.rstrip('/')}/{stream_name}",
        },
        "camera": {
            "name": camera.name,
            "ip_address": camera.ip_address,
            "status": camera.status,
        },
        "security_note": "All MediaMTX services are localhost-only. Access via backend proxy with JWT.",
    }
