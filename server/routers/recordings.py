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
Recordings router - Simplified to use MediaMTX playback server directly.

MediaMTX handles all playback complexity:
- Segment stitching
- Continuous playback
- fMP4 streaming

This router provides:
- Settings endpoints (schedule, storage, retention)
- Recording control (start/stop via MediaMTX)
- Proxy to MediaMTX playback server (with auth)
"""

import json
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import requests as http_client
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from core.auth import get_current_superuser, get_current_user, verify_token
from core.config import settings
from core.database import get_db
from core.logging_config import recording_logger
from models import Camera, SecuritySetting, User
from schemas import (
    RecordingRetentionSettings,
    RecordingScheduleSettings,
    RecordingStorageSettings,
)
from services.mediamtx_admin_service import MediaMtxAdminService
from services.cloud_recording_service import CloudRecordingService
from services.storage_service import storage_service
from services.stream_service import _build_stream_name

router = APIRouter(prefix="/recordings", tags=["recordings"])


@router.post("/cloud-upload/day")
async def queue_cloud_upload_for_day(
    camera_id: int = Query(..., description="Camera ID"),
    date: str = Query(..., description="Date in YYYY-MM-DD format"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Queue cloud upload for all DB recordings of a camera for a selected day."""
    from models import Recording

    # Validate cloud recording settings up front to provide clear UX.
    cloud_row = db.query(SecuritySetting).filter(SecuritySetting.key == "cloud").first()
    cloud_payload = {}
    if cloud_row and cloud_row.json_value:
        try:
            cloud_payload = json.loads(cloud_row.json_value)
        except Exception:
            cloud_payload = {}

    recording_cfg = cloud_payload.get("recording") or {}
    if not recording_cfg.get("enabled"):
        raise HTTPException(
            status_code=400,
            detail="Cloud recording upload is disabled. Enable and configure Cloud Recording Server first.",
        )

    required_fields = [
        recording_cfg.get("server_url"),
        recording_cfg.get("bucket"),
        recording_cfg.get("access_key"),
        recording_cfg.get("secret_key"),
    ]
    if not all(required_fields):
        raise HTTPException(
            status_code=400,
            detail="Cloud recording server is not fully configured. Please add endpoint, bucket, access key, and secret key.",
        )

    try:
        day_start = datetime.fromisoformat(date).replace(tzinfo=UTC)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    day_end = day_start + timedelta(days=1)

    camera = (
        db.query(Camera)
        .filter(Camera.id == camera_id, Camera.is_active == True)
        .first()
    )
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    rows = (
        db.query(Recording)
        .filter(
            Recording.camera_id == camera_id,
            Recording.start_time >= day_start,
            Recording.start_time < day_end,
        )
        .order_by(Recording.start_time.asc())
        .all()
    )

    if not rows:
        return {
            "status": "no_recordings",
            "camera_id": camera_id,
            "date": date,
            "queued": 0,
            "skipped_missing": 0,
        }

    service = CloudRecordingService.get_instance()

    queued = 0
    skipped_missing = 0
    base_path = settings.recordings_base_path

    for rec in rows:
        raw_path = rec.file_path or rec.filename or ""
        norm = raw_path.replace("\\", "/")
        lower = norm.lower()

        # Keep only recording-relative suffix (strip host/container absolute prefixes)
        rel_suffix = norm
        marker = "/recordings/"
        idx = lower.find(marker)
        if idx >= 0:
            rel_suffix = norm[idx + len(marker):]
        elif lower.startswith("recordings/"):
            rel_suffix = norm[len("recordings/"):]
        elif lower.startswith("/app/recordings/"):
            rel_suffix = norm[len("/app/recordings/"):]

        rel_suffix = rel_suffix.lstrip("/")
        destination_key = f"recordings/{rel_suffix}" if rel_suffix else f"recordings/{os.path.basename(raw_path)}"

        # Resolve absolute path robustly for legacy/container-style paths.
        if rec.file_path and os.path.isabs(rec.file_path):
            full_path = rec.file_path
        elif rec.file_path and rec.file_path.startswith("/app/recordings/"):
            suffix = rec.file_path.replace("/app/recordings/", "", 1)
            full_path = os.path.join(base_path, suffix)
        else:
            full_path = os.path.join(base_path, rel_suffix or raw_path)

        if not os.path.exists(full_path):
            skipped_missing += 1
            continue

        await service.queue_upload(full_path, camera_id, destination_key)
        queued += 1

    return {
        "status": "queued",
        "camera_id": camera_id,
        "date": date,
        "queued": queued,
        "skipped_missing": skipped_missing,
    }


@router.get("/cloud-upload/status")
async def get_cloud_upload_status(
    current_user=Depends(get_current_superuser),
):
    """Get current cloud upload worker and queue status."""
    service = CloudRecordingService.get_instance()
    return service.get_queue_status()


def _check_mediamtx_available() -> bool:
    """Check if MediaMTX playback server is available."""
    if not settings.mediamtx_playback_url:
        return False
    try:
        # Quick health check - just try to connect
        # MediaMTX returns various status codes:
        # - 200: valid path with recordings
        # - 400: invalid request
        # - 401: JWT auth required (server is running!)
        # - 404: path not found
        # - 500: server error
        response = http_client.get(
            f"{settings.mediamtx_playback_url}/list?path=__health__", timeout=2
        )
        # Any response means the server is responding (including 401 for JWT auth)
        return response.status_code in (200, 400, 401, 404, 500)
    except Exception:
        return False


# =============================================================================
# Helper Functions
# =============================================================================


def _get_or_init(db: Session, key: str, default_obj) -> SecuritySetting:
    """Get or initialize a security setting."""
    row = db.query(SecuritySetting).filter(SecuritySetting.key == key).first()
    if not row:
        row = SecuritySetting(key=key, json_value=json.dumps(default_obj.model_dump()))
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


async def _authenticate_request(
    request: Request, token: str | None, db: Session
) -> User | None:
    """Authenticate request from either Authorization header or token query param."""
    user_obj = None

    if request:
        auth_header = request.headers.get("authorization")
        if auth_header and auth_header.lower().startswith("bearer "):
            tok = auth_header.split(" ", 1)[1]
            td = verify_token(tok)
            if td:
                user_obj = db.query(User).filter(User.username == td.username).first()

    if not user_obj and token:
        td = verify_token(token)
        if td:
            user_obj = db.query(User).filter(User.username == td.username).first()

    if user_obj and user_obj.is_active:
        return user_obj
    return None


# =============================================================================
# Settings Endpoints
# =============================================================================


@router.get("/schedule")
async def get_schedule(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Get recording schedule settings."""
    row = _get_or_init(db, "recordings_schedule", RecordingScheduleSettings())
    try:
        val = json.loads(row.json_value or "{}")
    except Exception:
        val = {}
    obj = RecordingScheduleSettings(
        **{**RecordingScheduleSettings().model_dump(), **val}
    )
    return obj.model_dump()


@router.put("/schedule")
async def update_schedule(
    payload: RecordingScheduleSettings,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Update recording schedule settings."""
    row = _get_or_init(db, "recordings_schedule", RecordingScheduleSettings())
    obj = RecordingScheduleSettings(**payload.model_dump())
    row.json_value = json.dumps(obj.model_dump())
    db.commit()
    return obj.model_dump()


@router.get("/storage")
async def get_storage(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Get recording storage settings."""
    row = _get_or_init(db, "recordings_storage", RecordingStorageSettings())
    try:
        val = json.loads(row.json_value or "{}")
    except Exception:
        val = {}
    obj = RecordingStorageSettings(**{**RecordingStorageSettings().model_dump(), **val})
    result = obj.model_dump()
    if (
        not result.get("recordings_base_path")
        or result.get("recordings_base_path") == "recordings"
    ):
        result["recordings_base_path"] = settings.recordings_base_path
    return result


async def _sync_storage_to_mediamtx(db: Session, effective_path: str) -> dict:
    """Sync storage settings to MediaMTX."""
    new_record_path = f"{effective_path}/%path/%Y/%m/%d/%H-%M-%S-%f"
    result = {"mediamtx_record_path": new_record_path}

    try:
        # Update pathdefaults for new cameras
        await MediaMtxAdminService.pathdefaults_patch({"recordPath": new_record_path})

        # Update all active camera paths
        cameras = db.query(Camera).filter(Camera.is_active == True).all()
        for cam in cameras:
            path_name = _build_stream_name(
                settings.mediamtx_stream_prefix, cam.id, cam.ip_address
            )
            try:
                await MediaMtxAdminService.patch_path_by_name(
                    path_name, {"recordPath": new_record_path}
                )
            except Exception as e:
                # Log error but continue
                recording_logger.error(
                    f"Error updating recording path for camera {cam.id}: {e}"
                )
                pass
    except Exception as e:
        result["mediamtx_error"] = str(e)

    return result


@router.put("/storage")
async def update_storage(
    payload: RecordingStorageSettings,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Update recording storage settings and sync to MediaMTX."""
    # Update DB
    row = _get_or_init(db, "recordings_storage", RecordingStorageSettings())
    obj = RecordingStorageSettings(**payload.model_dump())
    row.json_value = json.dumps(obj.model_dump())
    db.commit()

    result = obj.model_dump()

    # Calculate effective path
    effective_path = result.get("recordings_base_path")
    if not effective_path or effective_path == "recordings":
        effective_path = settings.recordings_base_path
    result["recordings_base_path"] = effective_path

    # Sync to MediaMTX if path changed or just to ensure consistency
    if payload.recordings_base_path:
        sync_result = await _sync_storage_to_mediamtx(db, effective_path)
        result.update(sync_result)

    return result


@router.get("/retention")
async def get_retention(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Get recording retention settings."""
    row = _get_or_init(db, "recordings_retention", RecordingRetentionSettings())
    try:
        val = json.loads(row.json_value or "{}")
    except Exception:
        val = {}
    obj = RecordingRetentionSettings(
        **{**RecordingRetentionSettings().model_dump(), **val}
    )
    return obj.model_dump()


@router.put("/retention")
async def update_retention(
    payload: RecordingRetentionSettings,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Update recording retention settings."""
    row = _get_or_init(db, "recordings_retention", RecordingRetentionSettings())
    obj = RecordingRetentionSettings(**payload.model_dump())
    row.json_value = json.dumps(obj.model_dump())
    db.commit()
    return obj.model_dump()


# =============================================================================
# Recording Control (MediaMTX)
# =============================================================================


@router.post("/start/{camera_id}")
async def start_recording(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Start recording for a camera via MediaMTX."""
    try:
        result = await MediaMtxAdminService.enable_recording(camera_id)
        recording_logger.info(
            f"Started recording for camera {camera_id}", extra={"camera_id": camera_id}
        )
        return result
    except Exception as e:
        recording_logger.error(f"Failed to start recording for camera {camera_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/stop/{camera_id}")
async def stop_recording(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Stop recording for a camera via MediaMTX."""
    try:
        result = await MediaMtxAdminService.disable_recording(camera_id)
        recording_logger.info(
            f"Stopped recording for camera {camera_id}", extra={"camera_id": camera_id}
        )
        return result
    except Exception as e:
        recording_logger.error(f"Failed to stop recording for camera {camera_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status/{camera_id}")
async def recording_status(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get recording status for a camera."""
    try:
        status = await MediaMtxAdminService.get_recording_status(camera_id, db)
        return {
            "camera_id": camera_id,
            "recording_enabled": status.get("recording_enabled", False),
            "status": "active" if status.get("recording_enabled", False) else "stopped",
        }
    except Exception as e:
        return {
            "camera_id": camera_id,
            "recording_enabled": False,
            "status": "error",
            "error": str(e),
        }


# =============================================================================
# MediaMTX Playback - Direct URLs (no proxy needed)
# =============================================================================


@router.get("/playback/list")
async def list_recordings(
    path: str = Query(..., description="Camera path (e.g., cam-57)"),
    token: str | None = Query(default=None),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    List available recordings from MediaMTX playback server.
    Returns segments with direct playback URLs.
    """
    user_obj = await _authenticate_request(request, token, db)
    if not user_obj:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Sanitize path to prevent traversal attacks against MediaMTX
    # Force alphanumeric and dashes/underscores only for camera path
    if (
        not path
        or ".." in path
        or path.startswith("/")
        or any(c in path for c in [":", "\\"])
    ):
        raise HTTPException(status_code=400, detail="Invalid path format")

    try:
        url = f"{settings.mediamtx_playback_url}/list?path={path}"
        response = http_client.get(url, timeout=10)

        if response.status_code != 200:
            return {
                "recordings": [],
                "error": f"MediaMTX returned {response.status_code}",
            }

        recordings = response.json()

        return {
            "recordings": recordings,
            "count": len(recordings),
            "playback_base_url": settings.mediamtx_playback_url,
            "path": path,
        }
    except Exception as e:
        recording_logger.error(f"Failed to list recordings: {e}")
        return {"recordings": [], "error": str(e)}


@router.get("/playback/cameras")
async def list_recording_cameras(
    token: str | None = Query(default=None),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    List all cameras that have recordings.
    Queries MediaMTX for each active camera.
    """
    user_obj = await _authenticate_request(request, token, db)
    if not user_obj:
        raise HTTPException(status_code=401, detail="Unauthorized")

    cameras = db.query(Camera).filter(Camera.is_active == True).all()
    result = []

    for cam in cameras:
        path = _build_stream_name(
            settings.mediamtx_stream_prefix, cam.id, cam.ip_address
        )
        try:
            url = f"{settings.mediamtx_playback_url}/list?path={path}"
            response = http_client.get(url, timeout=5)

            if response.status_code == 200:
                recordings = response.json()
                if recordings:
                    total_duration = sum(r.get("duration", 0) for r in recordings)
                    result.append(
                        {
                            "camera_id": cam.id,
                            "camera_name": cam.name or f"Camera {cam.id}",
                            "path": path,
                            "recording_count": len(recordings),
                            "total_duration": total_duration,
                            "earliest": recordings[0].get("start")
                            if recordings
                            else None,
                            "latest": recordings[-1].get("start")
                            if recordings
                            else None,
                        }
                    )
        except Exception as e:
            recording_logger.error(
                f"Error processing recordings for camera path {path}: {e}"
            )
            pass

    return {"cameras": result, "count": len(result)}


@router.get("/playback/url")
async def get_playback_url(
    path: str = Query(..., description="Camera path (e.g., cam-57)"),
    start: str = Query(..., description="Start time in RFC3339 format"),
    duration: float = Query(..., description="Duration in seconds"),
    token: str | None = Query(default=None),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    Get a direct MediaMTX playback URL for a recording.
    The URL can be used directly in a video player.
    """
    user_obj = await _authenticate_request(request, token, db)
    if not user_obj:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Build direct MediaMTX URL
    params = {"path": path, "start": start, "duration": str(duration)}
    playback_url = f"{settings.mediamtx_playback_url}/get?{urlencode(params)}"

    return {"url": playback_url, "path": path, "start": start, "duration": duration}


# =============================================================================
# HLS VOD Playback - Backend-generated manifests with 5s segments
# =============================================================================


@router.get("/playback/hls")
async def create_hls_session(
    camera_id: int = Query(..., description="Camera ID"),
    start: str = Query(..., description="Start time in RFC3339 format"),
    end: str = Query(..., description="End time in RFC3339 format"),
    token: str | None = Query(default=None),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    Create an HLS playback session for VOD-style recording playback.

    Returns a session with manifest URL that can be used with HLS.js.
    Sessions are time-limited and automatically expire.

    Security:
    - Requires authentication (JWT token)
    - Enforces camera-level permissions
    - Session-based auth for subsequent requests
    """
    from datetime import datetime

    from services.hls_playback_service import HlsPlaybackService

    user_obj = await _authenticate_request(request, token, db)
    if not user_obj:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Validate camera exists and user has access
    camera = (
        db.query(Camera)
        .filter(Camera.id == camera_id, Camera.is_active == True)
        .first()
    )
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    # Parse time range
    try:
        start_time = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_time = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid time format: {e}")

    if end_time <= start_time:
        raise HTTPException(status_code=400, detail="End time must be after start time")

    # Build camera path
    camera_path = _build_stream_name(
        settings.mediamtx_stream_prefix, camera.id, camera.ip_address
    )

    # Create HLS session
    try:
        session = await HlsPlaybackService.create_session(
            user_id=user_obj.id,
            username=user_obj.username,
            camera_id=camera_id,
            camera_path=camera_path,
            start_time=start_time,
            end_time=end_time,
        )
    except Exception as e:
        recording_logger.error(f"Failed to create HLS session: {e}")
        raise HTTPException(status_code=500, detail="Failed to create playback session")

    if session.total_duration <= 0:
        raise HTTPException(
            status_code=404, detail="No recordings found in the specified time range"
        )

    # Build manifest URL (relative to API)
    manifest_url = (
        f"{settings.api_prefix}/recordings/playback/hls/{session.session_id}/index.m3u8"
    )

    return {
        "session_id": session.session_id,
        "manifest_url": manifest_url,
        "camera_id": camera_id,
        "camera_name": camera.name or f"Camera {camera_id}",
        "start": start,
        "end": end,
        "duration": session.total_duration,
        "segment_count": int(
            session.total_duration / HlsPlaybackService.SEGMENT_DURATION
        )
        + 1,
        "expires_in_seconds": HlsPlaybackService.SESSION_TTL_SECONDS,
    }


@router.get("/playback/hls/{session_id}/index.m3u8")
async def get_hls_manifest(
    session_id: str,
):
    """
    Serve the HLS VOD manifest for a session.

    Security: Session ID acts as authentication token.
    """
    from services.hls_playback_service import HlsPlaybackService

    session = await HlsPlaybackService.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    manifest = HlsPlaybackService.generate_manifest(session)

    return Response(
        content=manifest,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.get("/playback/hls/{session_id}/init.mp4")
async def get_hls_init_segment(
    session_id: str,
):
    """
    Serve the fMP4 initialization segment.

    Contains codec info needed to start playback.
    """
    from services.hls_playback_service import HlsPlaybackService

    session = await HlsPlaybackService.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    init_data = await HlsPlaybackService.get_init_segment(session)
    if not init_data:
        raise HTTPException(
            status_code=500, detail="Failed to get initialization segment"
        )

    return Response(
        content=init_data,
        media_type="video/mp4",
        headers={"Cache-Control": "max-age=3600", "Access-Control-Allow-Origin": "*"},
    )


@router.get("/playback/hls/{session_id}/segment-{segment_index}.m4s")
async def get_hls_segment(
    session_id: str,
    segment_index: int,
):
    """
    Serve an HLS media segment.

    Proxies segment data from MediaMTX /get endpoint.
    Uses streaming response for efficient delivery.
    """
    from fastapi.responses import StreamingResponse

    from services.hls_playback_service import HlsPlaybackService

    session = await HlsPlaybackService.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    # Validate segment index
    max_segments = int(session.total_duration / HlsPlaybackService.SEGMENT_DURATION) + 1
    if segment_index < 0 or segment_index >= max_segments:
        raise HTTPException(status_code=404, detail="Segment not found")

    return StreamingResponse(
        HlsPlaybackService.stream_segment(session, segment_index),
        media_type="video/mp4",
        headers={
            "Cache-Control": "max-age=86400",  # Cache segments for 24h
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.delete("/playback/hls/{session_id}")
async def delete_hls_session(
    session_id: str,
    token: str | None = Query(default=None),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    Explicitly delete/invalidate an HLS session.

    Called when user stops playback to free resources.
    """
    from services.hls_playback_service import HlsPlaybackService

    user_obj = await _authenticate_request(request, token, db)
    if not user_obj:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Verify session belongs to user
    session = await HlsPlaybackService.get_session(session_id)
    if session and session.user_id != user_obj.id:
        raise HTTPException(
            status_code=403, detail="Not authorized to delete this session"
        )

    success = await HlsPlaybackService.invalidate_session(session_id)

    return {"deleted": success, "session_id": session_id}


@router.get("/config")
async def get_playback_config(
    current_user=Depends(get_current_user),
):
    """
    Get playback configuration for frontend.
    Returns the MediaMTX playback server URL and HLS settings.
    """
    from services.hls_playback_service import HlsPlaybackService

    return {
        "playback_url": settings.mediamtx_playback_url,
        "stream_prefix": settings.mediamtx_stream_prefix,
        "hls_enabled": True,
        "hls_segment_duration": HlsPlaybackService.SEGMENT_DURATION,
        "hls_session_ttl": HlsPlaybackService.SESSION_TTL_SECONDS,
    }


@router.get("/today/{camera_id}")
async def get_today_segments(
    camera_id: int,
    token: str | None = Query(default=None),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    Get today's recording segments for a specific camera.
    Used for DVR-style timeline scrubbing in live view.
    Returns segments with start times and durations for timeline visualization.
    """
    from datetime import datetime
    from urllib.parse import quote

    user_obj = await _authenticate_request(request, token, db)
    if not user_obj:
        raise HTTPException(status_code=401, detail="Unauthorized")

    camera = (
        db.query(Camera)
        .filter(Camera.id == camera_id, Camera.is_active == True)
        .first()
    )
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    path = _build_stream_name(
        settings.mediamtx_stream_prefix, camera.id, camera.ip_address
    )

    try:
        url = f"{settings.mediamtx_playback_url}/list?path={path}"
        response = http_client.get(url, timeout=10)

        if response.status_code != 200:
            return {
                "segments": [],
                "camera_id": camera_id,
                "path": path,
                "today": datetime.now().strftime("%Y-%m-%d"),
            }

        all_segments = response.json()
        if not all_segments:
            return {
                "segments": [],
                "camera_id": camera_id,
                "path": path,
                "today": datetime.now().strftime("%Y-%m-%d"),
            }

        # Filter to today's segments only
        today = datetime.now().strftime("%Y-%m-%d")
        today_segments = []

        for seg in all_segments:
            start_str = seg.get("start", "")
            if not start_str:
                continue
            try:
                dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                if dt.strftime("%Y-%m-%d") == today:
                    # Add playback URL for this segment
                    encoded_start = quote(start_str, safe="")
                    seg["playback_url"] = (
                        f"{settings.mediamtx_playback_url}/get?path={path}&start={encoded_start}&duration={seg.get('duration', 300)}"
                    )
                    today_segments.append(seg)
            except Exception:
                continue

        # Sort by start time
        today_segments.sort(key=lambda s: s.get("start", ""))

        # Calculate total duration
        total_duration = sum(s.get("duration", 0) for s in today_segments)

        # Build a "play from here to live" URL helper
        first_start = today_segments[0].get("start") if today_segments else None

        return {
            "segments": today_segments,
            "camera_id": camera_id,
            "camera_name": camera.name or f"Camera {camera_id}",
            "path": path,
            "today": today,
            "total_duration": total_duration,
            "segment_count": len(today_segments),
            "first_start": first_start,
            "playback_base_url": settings.mediamtx_playback_url,
        }
    except Exception as e:
        recording_logger.error(
            f"Failed to get today's segments for camera {camera_id}: {e}"
        )
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Date-Grouped Recordings - User-friendly aggregation
# =============================================================================


def _group_segments_by_date(segments: list, path: str) -> list:
    """
    Group recording segments by date.
    Returns one entry per day with aggregated duration and playback URL.
    """
    from collections import defaultdict
    from datetime import datetime
    from urllib.parse import quote

    if not segments:
        return []

    # Group by date
    by_date = defaultdict(list)
    for seg in segments:
        start_str = seg.get("start", "")
        if not start_str:
            continue
        try:
            # Parse ISO datetime and extract date
            dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            date_key = dt.strftime("%Y-%m-%d")
            by_date[date_key].append(seg)
        except Exception:
            continue

    # Build aggregated results
    result = []
    for date_key in sorted(by_date.keys(), reverse=True):
        day_segments = by_date[date_key]

        # Sort segments by start time
        day_segments.sort(key=lambda s: s.get("start", ""))

        # Calculate totals
        total_duration = sum(s.get("duration", 0) for s in day_segments)
        first_start = day_segments[0].get("start")
        last_segment = day_segments[-1]

        # Build playback URL for the entire day - URL-encode the start time
        encoded_start = quote(first_start, safe="")
        playback_url = f"{settings.mediamtx_playback_url}/get?path={path}&start={encoded_start}&duration={total_duration}"

        result.append(
            {
                "date": date_key,
                "total_duration": total_duration,
                "segment_count": len(day_segments),
                "first_start": first_start,
                "playback_url": playback_url,
            }
        )

    return result


def _group_filesystem_recordings_by_date(
    items: list, camera_id: int, camera_name: str, path: str
) -> dict:
    """
    Group filesystem recording items by date for a specific camera.
    Returns camera data structure compatible with MediaMTX format.
    """
    from collections import defaultdict
    from datetime import datetime

    if not items:
        return None

    # Group by date
    by_date = defaultdict(list)
    for item in items:
        start_str = item.get("start_time", "")
        if not start_str:
            continue
        try:
            dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            date_key = dt.strftime("%Y-%m-%d")
            by_date[date_key].append(item)
        except Exception:
            continue

    if not by_date:
        return None

    # Build aggregated results
    daily_recordings = []
    total_duration = 0

    for date_key in sorted(by_date.keys(), reverse=True):
        day_items = by_date[date_key]
        day_items.sort(key=lambda s: s.get("start_time", ""))

        # Estimate duration from file count (assume segment_seconds from config, default 60s)
        estimated_duration = len(day_items) * 60  # Default 60 seconds per segment
        first_start = day_items[0].get("start_time")

        daily_recordings.append(
            {
                "date": date_key,
                "total_duration": estimated_duration,
                "segment_count": len(day_items),
                "first_start": first_start,
                "playback_url": None,  # Playback unavailable when MediaMTX is down
            }
        )
        total_duration += estimated_duration

    return {
        "camera_id": camera_id,
        "camera_name": camera_name,
        "path": path,
        "recording_count": len(daily_recordings),
        "total_duration": total_duration,
        "recordings": daily_recordings,
    }


@router.get("/list")
async def list_recordings_by_date(
    camera_id: int | None = Query(default=None, description="Filter by camera ID"),
    token: str | None = Query(default=None),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    List recordings grouped by camera and date.
    Returns user-friendly recording counts (1 recording = 1 day per camera).
    Falls back to filesystem listing if MediaMTX is unavailable.
    """
    user_obj = await _authenticate_request(request, token, db)
    if not user_obj:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Check if MediaMTX is available
    mediamtx_available = _check_mediamtx_available()

    # Get cameras to query
    if camera_id:
        cameras = (
            db.query(Camera)
            .filter(Camera.id == camera_id, Camera.is_active == True)
            .all()
        )
    else:
        cameras = db.query(Camera).filter(Camera.is_active == True).all()

    result = []
    total_recordings = 0
    total_duration = 0

    if mediamtx_available:
        # Use MediaMTX playback server (preferred - accurate durations)
        for cam in cameras:
            path = _build_stream_name(
                settings.mediamtx_stream_prefix, cam.id, cam.ip_address
            )
            try:
                url = f"{settings.mediamtx_playback_url}/list?path={path}"
                response = http_client.get(url, timeout=10)

                if response.status_code == 200:
                    segments = response.json()
                    if segments:
                        # Group by date
                        daily_recordings = _group_segments_by_date(segments, path)

                        cam_duration = sum(
                            d["total_duration"] for d in daily_recordings
                        )

                        result.append(
                            {
                                "camera_id": cam.id,
                                "camera_name": cam.name or f"Camera {cam.id}",
                                "path": path,
                                "recording_count": len(daily_recordings),
                                "total_duration": cam_duration,
                                "recordings": daily_recordings,
                            }
                        )

                        total_recordings += len(daily_recordings)
                        total_duration += cam_duration
            except Exception as e:
                recording_logger.error(
                    f"Failed to get recordings for camera {cam.id}: {e}"
                )
    else:
        # Fallback to filesystem listing
        recording_logger.warning(
            "MediaMTX unavailable, falling back to filesystem listing"
        )

        for cam in cameras:
            path = _build_stream_name(
                settings.mediamtx_stream_prefix, cam.id, cam.ip_address
            )
            try:
                # Use storage service to list recordings from filesystem
                fs_data = storage_service.list_recordings(
                    db, camera_id=cam.id, limit=10000
                )
                items = fs_data.get("items", [])

                if items:
                    cam_data = _group_filesystem_recordings_by_date(
                        items, cam.id, cam.name or f"Camera {cam.id}", path
                    )
                    if cam_data:
                        result.append(cam_data)
                        total_recordings += cam_data["recording_count"]
                        total_duration += cam_data["total_duration"]
            except Exception as e:
                recording_logger.error(
                    f"Failed to get filesystem recordings for camera {cam.id}: {e}"
                )

    return {
        "cameras": result,
        "total_recordings": total_recordings,
        "total_duration": total_duration,
        "total_cameras": len(result),
        "mediamtx_available": mediamtx_available,
    }


@router.get("/stats")
async def get_recording_stats(
    token: str | None = Query(default=None),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    Get recording statistics for dashboard.
    Returns counts and durations in user-friendly format.
    Falls back to filesystem listing if MediaMTX is unavailable.
    """
    user_obj = await _authenticate_request(request, token, db)
    if not user_obj:
        raise HTTPException(status_code=401, detail="Unauthorized")

    cameras = db.query(Camera).filter(Camera.is_active == True).all()
    mediamtx_available = _check_mediamtx_available()

    total_recordings = 0  # Camera-days
    total_duration = 0
    cameras_with_recordings = 0

    if mediamtx_available:
        for cam in cameras:
            path = _build_stream_name(
                settings.mediamtx_stream_prefix, cam.id, cam.ip_address
            )
            try:
                url = f"{settings.mediamtx_playback_url}/list?path={path}"
                response = http_client.get(url, timeout=5)

                if response.status_code == 200:
                    segments = response.json()
                    if segments:
                        daily_recordings = _group_segments_by_date(segments, path)
                        total_recordings += len(daily_recordings)
                        total_duration += sum(
                            d["total_duration"] for d in daily_recordings
                        )
                        cameras_with_recordings += 1
            except Exception as e:
                recording_logger.warning(
                    f"Failed to fetch playback stats for camera {cam.id}: {e}"
                )
                pass
    else:
        # Fallback to filesystem listing
        for cam in cameras:
            path = _build_stream_name(
                settings.mediamtx_stream_prefix, cam.id, cam.ip_address
            )
            try:
                fs_data = storage_service.list_recordings(
                    db, camera_id=cam.id, limit=10000
                )
                items = fs_data.get("items", [])
                if items:
                    cam_data = _group_filesystem_recordings_by_date(
                        items, cam.id, cam.name or f"Camera {cam.id}", path
                    )
                    if cam_data:
                        total_recordings += cam_data["recording_count"]
                        total_duration += cam_data["total_duration"]
                        cameras_with_recordings += 1
            except Exception as e:
                recording_logger.warning(
                    f"Failed to list filesystem recordings for camera {cam.id}: {e}"
                )
                pass

    return {
        "total_recordings": total_recordings,
        "total_duration": total_duration,
        "total_duration_formatted": _format_duration(total_duration),
        "cameras_with_recordings": cameras_with_recordings,
        "total_cameras": len(cameras),
        "mediamtx_available": mediamtx_available,
    }


def _format_duration(seconds: float) -> str:
    """Format duration in human-readable format."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _group_segments_into_sessions(
    segments: list, max_gap_seconds: int = 300, camera_id: int = None
) -> list:
    """
    Group recording segments into continuous sessions.
    A new session starts when there's a gap > max_gap_seconds between segments.

    Args:
        segments: List of segments from MediaMTX with 'start' and 'duration'
        max_gap_seconds: Maximum gap between segments to still be in same session
        camera_id: Camera ID for constructing file paths (if MediaMTX segments don't have 'path')
    """
    import uuid
    from datetime import datetime

    if not segments:
        return []

    # Sort segments by start time
    sorted_segments = sorted(segments, key=lambda s: s.get("start", ""))

    sessions = []
    current_session = None

    for seg in sorted_segments:
        start_str = seg.get("start", "")
        duration = seg.get("duration", 0)

        if not start_str:
            continue

        try:
            seg_start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            seg_end = seg_start.timestamp() + duration

            # Construct file path if not provided (MediaMTX doesn't include it)
            seg_path = seg.get("path", "")
            if not seg_path and camera_id is not None:
                # Format: cam-{id}/YYYY/MM/DD/HH-MM-SS-ffffff.mp4
                seg_path = (
                    f"cam-{camera_id}/"
                    f"{seg_start.year:04d}/"
                    f"{seg_start.month:02d}/"
                    f"{seg_start.day:02d}/"
                    f"{seg_start.hour:02d}-{seg_start.minute:02d}-{seg_start.second:02d}-{seg_start.microsecond:06d}.mp4"
                )

            # Check if we should start a new session
            if current_session is None:
                # First segment - start new session
                current_session = {
                    "session_id": str(uuid.uuid4()),
                    "start_time": start_str,
                    "end_time": start_str,
                    "end_timestamp": seg_end,
                    "segments": [],
                }
            else:
                # Check gap from last segment
                last_end = current_session["end_timestamp"]
                gap = seg_start.timestamp() - last_end

                if gap > max_gap_seconds:
                    # Gap too large - finalize current session and start new one
                    sessions.append(current_session)
                    current_session = {
                        "session_id": str(uuid.uuid4()),
                        "start_time": start_str,
                        "end_time": start_str,
                        "end_timestamp": seg_end,
                        "segments": [],
                    }

            # Add segment to current session
            end_str = datetime.fromtimestamp(seg_end, tz=UTC).isoformat()
            current_session["segments"].append(
                {
                    "path": seg_path,
                    "start_time": start_str,
                    "end_time": end_str,
                    "duration_seconds": duration,
                    "size_bytes": seg.get(
                        "size", 0
                    ),  # May be provided by MediaMTX or filesystem
                }
            )
            current_session["end_time"] = end_str
            current_session["end_timestamp"] = seg_end

        except Exception as e:
            recording_logger.warning(f"Failed to process segment: {e}")
            continue

    # Add the last session
    if current_session and current_session["segments"]:
        sessions.append(current_session)

    # Calculate session statistics and validate segments
    for session in sessions:
        start = datetime.fromisoformat(session["start_time"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(session["end_time"].replace("Z", "+00:00"))
        duration_seconds = (end - start).total_seconds()

        # Mark segments as complete if they have valid duration
        # MediaMTX provides duration in the API, so segments with duration > 0 are complete
        complete_segments = []
        incomplete_segments = []

        for seg in session["segments"]:
            # A segment is complete if it has a valid duration from MediaMTX
            seg["is_complete"] = seg["duration_seconds"] > 0
            if seg["is_complete"]:
                complete_segments.append(seg)
            else:
                incomplete_segments.append(seg)

        complete_duration = sum(s["duration_seconds"] for s in complete_segments)

        session["duration_seconds"] = duration_seconds
        session["duration_formatted"] = _format_duration(duration_seconds)
        session["size_bytes"] = sum(s["size_bytes"] for s in session["segments"])
        session["size_formatted"] = "N/A"
        session["segment_count"] = len(session["segments"])
        session["complete_segment_count"] = len(complete_segments)
        session["incomplete_segment_count"] = len(incomplete_segments)
        session["is_in_progress"] = len(incomplete_segments) > 0
        session["complete_duration_seconds"] = complete_duration
        session["complete_duration_formatted"] = _format_duration(complete_duration)

        # Remove temporary timestamp field
        session.pop("end_timestamp", None)

    return sessions


def _group_filesystem_items_into_sessions(
    items: list, camera_id: int, camera_name: str, segment_seconds: int = 60
) -> dict:
    """
    Group filesystem recording items into sessions by date.
    Returns camera data with dates and sessions.
    """
    from collections import defaultdict
    from datetime import datetime

    if not items:
        return None

    # Group by date first
    by_date = defaultdict(list)
    for item in items:
        start_str = item.get("start_time", "")
        if not start_str:
            continue
        try:
            dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            date_key = dt.strftime("%Y-%m-%d")
            by_date[date_key].append(
                {
                    "start": start_str,
                    "duration": segment_seconds,  # Estimate from config
                    "path": item.get("relpath", ""),
                    "size": item.get("size", 0),
                }
            )
        except Exception:
            continue

    if not by_date:
        return None

    # Convert segments to sessions for each date
    dates = []
    for date_key in sorted(by_date.keys(), reverse=True):
        day_items = by_date[date_key]
        # Use the same session grouping logic
        sessions = _group_segments_into_sessions(
            day_items, max_gap_seconds=300, camera_id=camera_id
        )

        if sessions:
            total_duration = sum(s["duration_seconds"] for s in sessions)
            dates.append(
                {
                    "date": date_key,
                    "session_count": len(sessions),
                    "total_duration_seconds": total_duration,
                    "sessions": sessions,
                }
            )

    if not dates:
        return None

    return {
        "camera_id": camera_id,
        "camera_name": camera_name,
        "camera_location": None,
        "dates": dates,
    }


@router.get("/sessions-for-ai")
async def get_recording_sessions_for_ai(
    camera_id: int | None = Query(default=None, description="Filter by camera ID"),
    token: str | None = Query(default=None),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    Get recording sessions grouped by camera and date, specifically for AI processing.
    Sessions are continuous recording periods (with small gaps allowed).
    Falls back to filesystem listing if MediaMTX is unavailable.
    """
    user_obj = await _authenticate_request(request, token, db)
    if not user_obj:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Check if MediaMTX is available
    mediamtx_available = _check_mediamtx_available()

    # Get cameras to query
    if camera_id:
        cameras = (
            db.query(Camera)
            .filter(Camera.id == camera_id, Camera.is_active == True)
            .all()
        )
    else:
        cameras = db.query(Camera).filter(Camera.is_active == True).all()

    result = []

    if mediamtx_available:
        # Use MediaMTX playback server (preferred - accurate durations)
        for cam in cameras:
            path = _build_stream_name(
                settings.mediamtx_stream_prefix, cam.id, cam.ip_address
            )
            try:
                url = f"{settings.mediamtx_playback_url}/list?path={path}"
                response = http_client.get(url, timeout=10)

                if response.status_code == 200:
                    segments = response.json()
                    if segments:
                        # Group segments by date first
                        from collections import defaultdict
                        from datetime import datetime

                        by_date = defaultdict(list)
                        for seg in segments:
                            start_str = seg.get("start", "")
                            if not start_str:
                                continue
                            try:
                                dt = datetime.fromisoformat(
                                    start_str.replace("Z", "+00:00")
                                )
                                date_key = dt.strftime("%Y-%m-%d")
                                by_date[date_key].append(seg)
                            except Exception:
                                continue

                        # For each date, create sessions
                        dates = []
                        for date_key in sorted(by_date.keys(), reverse=True):
                            day_segments = by_date[date_key]
                            sessions = _group_segments_into_sessions(
                                day_segments, camera_id=cam.id
                            )

                            if sessions:
                                total_duration = sum(
                                    s["duration_seconds"] for s in sessions
                                )
                                dates.append(
                                    {
                                        "date": date_key,
                                        "session_count": len(sessions),
                                        "total_duration_seconds": total_duration,
                                        "sessions": sessions,
                                    }
                                )

                        if dates:
                            result.append(
                                {
                                    "camera_id": cam.id,
                                    "camera_name": cam.name or f"Camera {cam.id}",
                                    "camera_location": getattr(cam, "location", None),
                                    "dates": dates,
                                }
                            )
            except Exception as e:
                recording_logger.error(
                    f"Failed to get sessions for camera {cam.id}: {e}"
                )
    else:
        # Fallback to filesystem listing
        recording_logger.warning(
            "MediaMTX unavailable, falling back to filesystem listing for AI sessions"
        )

        # Get segment duration from storage config
        from services.storage_service import _load_storage_config

        store_cfg = _load_storage_config(db)
        segment_seconds = store_cfg.segment_seconds

        for cam in cameras:
            try:
                # Use storage service to list recordings from filesystem
                fs_data = storage_service.list_recordings(
                    db, camera_id=cam.id, limit=10000
                )
                items = fs_data.get("items", [])

                if items:
                    cam_data = _group_filesystem_items_into_sessions(
                        items, cam.id, cam.name or f"Camera {cam.id}", segment_seconds
                    )
                    if cam_data:
                        result.append(cam_data)
            except Exception as e:
                recording_logger.error(
                    f"Failed to get filesystem sessions for camera {cam.id}: {e}"
                )

    return {
        "cameras": result,
        "total_cameras": len(result),
        "source": "mediamtx" if mediamtx_available else "filesystem",
    }
