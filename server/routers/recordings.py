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

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from core.auth import get_current_superuser, get_current_user, verify_token
from core.config import settings
from core.database import get_db
from core.logging_config import recording_logger
from core.policy import require_outbound_allowed
from models import Camera, SecuritySetting, User
from schemas import (
    RecordingRetentionSettings,
    RecordingScheduleSettings,
    RecordingStorageSettings,
)
from services.mediamtx_admin_service import MediaMtxAdminService
from services import mediamtx_client
from services.cloud_recording_service import CloudRecordingService
from services.retention_service import RetentionService
from services.storage_service import (
    RecordingPathError,
    safe_recording_path,
    storage_service,
)
from services.stream_service import _build_stream_name

router = APIRouter(prefix="/recordings", tags=["recordings"])

# How far behind "now" the playable edge of a still-recording file sits. Only the
# unfinished tail is withheld: MediaMTX flushes fragments continuously, so
# footage older than this is complete and plays as ordinary VOD. One minute is a
# deliberately generous margin over the sub-second write cadence — it costs the
# viewer nothing noticeable and leaves no chance of serving a half-written tail.
LIVE_EDGE_LAG_SECONDS = 60


def _live_edge_iso(
    file_start: datetime, now: datetime, lag_seconds: int = LIVE_EDGE_LAG_SECONDS
) -> str:
    """The instant from which footage is treated as LIVE (not yet playable).

    ``lag_seconds`` behind ``now``, but never earlier than ``file_start`` — a
    recording that just began is live in its entirety. Accepts tz-aware
    datetimes (preferred; emitted as proper UTC) or the legacy naive-UTC pair
    (emitted with a bare ``Z`` suffix, matching the old behaviour).
    """
    edge = max(file_start, now - timedelta(seconds=lag_seconds))
    if edge.tzinfo is None:
        return edge.isoformat() + "Z"
    return edge.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_byte_range(range_header: str | None, file_size: int) -> tuple[int, int] | None:
    """Parse a single HTTP ``Range: bytes=start-end`` header.

    Returns an inclusive ``(start, end)`` byte range clamped to the file, or
    None when the header is absent/unparseable/unsatisfiable (caller then
    serves the whole file with 200). Only the first range of a set is honoured,
    which is all hls.js ever requests.
    """
    if not range_header or not range_header.startswith("bytes="):
        return None
    spec = range_header[len("bytes="):].split(",")[0].strip()
    if "-" not in spec:
        return None
    lo, hi = spec.split("-", 1)
    try:
        if lo == "":
            # Suffix form: bytes=-N -> last N bytes.
            n = int(hi)
            if n <= 0:
                return None
            start = max(0, file_size - n)
            end = file_size - 1
        else:
            start = int(lo)
            end = int(hi) if hi else file_size - 1
    except ValueError:
        return None
    if start < 0 or start >= file_size or start > end:
        return None
    return start, min(end, file_size - 1)


@router.post(
    "/cloud-upload/day",
    # Queueing to the cloud opens an outbound HTTP path (remote NVR or S3);
    # refused in offline mode. See V-009.
    dependencies=[Depends(require_outbound_allowed)],
)
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
    skipped_unsafe = 0

    for rec in rows:
        raw_path = rec.file_path or rec.filename or ""
        norm = raw_path.replace("\\", "/")
        lower = norm.lower()

        # Refuse any absolute path that isn't inside the recordings subtree —
        # otherwise a DB-poisoned "/etc/passwd" could fall through to a lookup
        # under the recordings base (which MediaMTX can write to). See V-005.
        is_absolute = os.path.isabs(norm) or (
            len(norm) >= 2 and norm[1] == ":"  # Windows drive letter
        )
        recordings_marker_present = (
            "/recordings/" in lower
            or lower.startswith("recordings/")
            or lower.startswith("/app/recordings/")
        )
        if is_absolute and not recordings_marker_present:
            skipped_unsafe += 1
            recording_logger.warning(
                "V-005/H-1: refusing to upload recording whose DB-stored "
                "file_path is absolute and outside the recordings subtree "
                "(record id=%s camera_id=%s raw=%r)",
                getattr(rec, "id", None),
                camera_id,
                raw_path,
            )
            continue

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
        destination_key = (
            f"recordings/{rel_suffix}"
            if rel_suffix
            else f"recordings/{os.path.basename(raw_path)}"
        )

        # V-005 (Zenodo 17261761 §3.4 / customer-controlled storage):
        # ``file_path`` is DB-stored and could in principle be poisoned. We
        # require every recording we read off disk to resolve under the
        # configured recordings base, with symlinks chased. A poisoned record
        # pointing at ``/etc/passwd`` was already rejected above by H-1; this
        # handles the relative-path attack surface (``../`` traversal,
        # symlink-escape, etc.).
        try:
            full_path = safe_recording_path(rel_suffix or raw_path, db)
        except RecordingPathError as exc:
            skipped_unsafe += 1
            recording_logger.warning(
                "V-005: refusing to upload recording with unsafe path "
                "(record id=%s camera_id=%s raw=%r): %s",
                getattr(rec, "id", None),
                camera_id,
                raw_path,
                exc,
            )
            continue

        if not full_path.exists():
            skipped_missing += 1
            continue

        await service.queue_upload(str(full_path), camera_id, destination_key)
        queued += 1

    return {
        "status": "queued",
        "camera_id": camera_id,
        "date": date,
        "queued": queued,
        "skipped_missing": skipped_missing,
        "skipped_unsafe": skipped_unsafe,
    }


@router.get("/cloud-upload/status")
async def get_cloud_upload_status(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Get current cloud upload worker and queue status."""
    service = CloudRecordingService.get_instance()
    status = service.get_queue_status()
    status["configured"] = service.is_cloud_configured(db)
    return status


async def _check_mediamtx_available() -> bool:
    """Check if MediaMTX playback server is available.

    Async + TTL-cached (see services.mediamtx_client): the old version ran a
    blocking ``requests.get`` on the event loop for every listing request.
    """
    return await mediamtx_client.check_available()


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


def _can_view_camera(user: User, camera: Camera, db: Session) -> bool:
    """Camera-level view permission for recordings/playback.

    Mirrors the ownership rule the camera routes enforce (superuser bypass,
    else owner), extended with CameraPermission.can_view grants. Cameras with
    no owner (legacy rows) stay visible to any authenticated user — matching
    their pre-existing behaviour instead of suddenly locking them away.
    """
    if getattr(user, "is_superuser", False):
        return True
    owner_id = getattr(camera, "owner_id", None)
    if owner_id is None or owner_id == user.id:
        return True
    from models import CameraPermission

    return (
        db.query(CameraPermission.id)
        .filter(
            CameraPermission.user_id == user.id,
            CameraPermission.camera_id == camera.id,
            CameraPermission.can_view == True,  # noqa: E712
        )
        .first()
        is not None
    )


def _require_camera_view(user: User, camera: Camera, db: Session) -> None:
    if not _can_view_camera(user, camera, db):
        raise HTTPException(
            status_code=403, detail="Not permitted to view this camera"
        )


def _viewable_active_cameras(db: Session, user: User) -> list[Camera]:
    """Active cameras the user may view (superuser: all; else owner or granted).

    Used by the listing endpoints so one user can never enumerate another
    owner's cameras.
    """
    cameras = db.query(Camera).filter(Camera.is_active == True).all()  # noqa: E712
    return [c for c in cameras if _can_view_camera(user, c, db)]


def _camera_for_playback_path(db: Session, path: str) -> Camera | None:
    """Resolve a MediaMTX playback path (``cam-57`` or ``cam-192_168_1_9``) to
    its Camera by rebuilding each active camera's canonical path.

    Rebuilding (not parsing) works for both id- and ip-based path modes.
    Returns ``None`` when no active camera owns the path.
    """
    for cam in db.query(Camera).filter(Camera.is_active == True).all():  # noqa: E712
        if (
            _build_stream_name(
                settings.mediamtx_stream_prefix, cam.id, cam.ip_address
            )
            == path
        ):
            return cam
    return None


async def _authenticate_request(request: Request, db: Session) -> User | None:
    """Authenticate a request from the Authorization: Bearer header.

    The JWT is taken ONLY from the header — never from a ?token= query param,
    which would leak the long-lived token into access logs / browser history.
    """
    user_obj = None

    if request:
        auth_header = request.headers.get("authorization")
        if auth_header and auth_header.lower().startswith("bearer "):
            tok = auth_header.split(" ", 1)[1]
            td = verify_token(tok)
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
    new_record_path = f"{effective_path}/%path/%Y-%m-%d/%H/%M-%S-%f"
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


# DISABLED — recording is automatic on this NVR and cannot be started/stopped
# on demand, including via the API. Route intentionally commented out so a
# direct `curl` cannot control recording. Recording is enabled at camera-
# configure time (see CameraService). Re-enable the decorator only if the
# product decision changes. See also cameras.toggle_camera_recording.
# @router.post("/start/{camera_id}")
async def start_recording(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Start recording for a camera via MediaMTX. (Route disabled — see above.)"""
    try:
        result = await MediaMtxAdminService.enable_recording(camera_id)
        recording_logger.info(
            f"Started recording for camera {camera_id}", extra={"camera_id": camera_id}
        )
        return result
    except Exception as e:
        recording_logger.error(f"Failed to start recording for camera {camera_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# DISABLED — recording is automatic and cannot be stopped on demand, including
# via the API. Route intentionally commented out so a direct `curl` cannot stop
# recording. Re-enable the decorator only if the product decision changes.
# @router.post("/stop/{camera_id}")
async def stop_recording(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Stop recording for a camera via MediaMTX. (Route disabled — see above.)"""
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
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    List available recordings from MediaMTX playback server.
    Returns segments with direct playback URLs.
    """
    user_obj = await _authenticate_request(request, db)
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

    # Authorize: the path must resolve to a camera this user may view.
    camera = _camera_for_playback_path(db, path)
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    _require_camera_view(user_obj, camera, db)

    recordings = await mediamtx_client.list_segments(path, timeout=10.0)
    if recordings is None:
        return {"recordings": [], "error": "MediaMTX list unavailable"}

    # Returned to the browser, so use the external fallback chain
    # (matches get_playback_url / get_playback_config).
    browser_playback_base = (
        settings.mediamtx_external_playback_url
        or settings.mediamtx_playback_url
        or "http://127.0.0.1:9996"
    )
    return {
        "recordings": recordings,
        "count": len(recordings),
        "playback_base_url": browser_playback_base,
        "path": path,
    }


@router.get("/playback/cameras")
async def list_recording_cameras(
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    List all cameras that have recordings.
    Queries MediaMTX for each active camera.
    """
    user_obj = await _authenticate_request(request, db)
    if not user_obj:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Only cameras this user may view — never the whole estate.
    cameras = _viewable_active_cameras(db, user_obj)
    result = []

    path_by_cam = {
        cam.id: _build_stream_name(
            settings.mediamtx_stream_prefix, cam.id, cam.ip_address
        )
        for cam in cameras
    }
    listings = await mediamtx_client.list_segments_many(
        list(path_by_cam.values()), timeout=5.0
    )

    for cam in cameras:
        path = path_by_cam[cam.id]
        recordings = listings.get(path)
        if recordings:
            total_duration = sum(r.get("duration", 0) for r in recordings)
            result.append(
                {
                    "camera_id": cam.id,
                    "camera_name": cam.name or f"Camera {cam.id}",
                    "path": path,
                    "recording_count": len(recordings),
                    "total_duration": total_duration,
                    "earliest": recordings[0].get("start") if recordings else None,
                    "latest": recordings[-1].get("start") if recordings else None,
                }
            )

    return {"cameras": result, "count": len(result)}


@router.get("/playback/url")
async def get_playback_url(
    path: str = Query(..., description="Camera path (e.g., cam-57)"),
    start: str = Query(..., description="Start time in RFC3339 format"),
    duration: float = Query(..., description="Duration in seconds"),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    Get a direct MediaMTX playback URL for a recording.
    The URL can be used directly in a video player.
    """
    user_obj = await _authenticate_request(request, db)
    if not user_obj:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Authorize: the path must resolve to a camera this user may view.
    camera = _camera_for_playback_path(db, path)
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    _require_camera_view(user_obj, camera, db)

    # Build the playback URL the BROWSER fetches — use the external fallback
    # chain (the browser can't reach the Docker-internal mediamtx host; the
    # external URL is nginx-TLS-fronted). Mirrors streams.py's pattern.
    playback_base = (
        settings.mediamtx_external_playback_url
        or settings.mediamtx_playback_url
        or "http://127.0.0.1:9996"
    )
    params = {"path": path, "start": start, "duration": str(duration)}
    playback_url = f"{playback_base.rstrip('/')}/get?{urlencode(params)}"

    return {"url": playback_url, "path": path, "start": start, "duration": duration}


# =============================================================================
# Flag protection — flagged recordings survive retention (protect_flagged)
# =============================================================================


@router.put("/flag")
async def set_recording_flag(
    camera_id: int = Query(..., description="Camera ID"),
    start: str = Query(..., description="Range start (ISO 8601)"),
    end: str = Query(..., description="Range end (ISO 8601)"),
    flagged: bool = Query(..., description="Set or clear the protection flag"),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """Flag (or unflag) every recording clip in a time range.

    Flagged clips are skipped by retention's age and disk-pressure sweeps
    while ``protect_flagged`` is enabled — this is what makes "keep this
    incident's footage" survive the retention window.
    """
    from datetime import datetime

    from models import Recording

    user_obj = await _authenticate_request(request, db)
    if not user_obj:
        raise HTTPException(status_code=401, detail="Unauthorized")

    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    _require_camera_view(user_obj, camera, db)

    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid start/end format")
    if end_dt <= start_dt:
        raise HTTPException(status_code=400, detail="End must be after start")

    updated = (
        db.query(Recording)
        .filter(
            Recording.camera_id == camera_id,
            Recording.start_time >= start_dt,
            Recording.start_time < end_dt,
        )
        .update({Recording.is_flagged: flagged}, synchronize_session=False)
    )
    db.commit()
    return {"camera_id": camera_id, "flagged": flagged, "updated_clips": updated}


# =============================================================================
# Authenticated clip export (streams through the backend)
# =============================================================================

# One-time export tickets: minted by an authenticated POST, consumed by the
# browser's <a href> download (which cannot carry an Authorization header).
# In-memory is fine — a ticket lives seconds and downloads are single-process.
_EXPORT_TICKET_TTL_SECONDS = 300
_export_tickets: dict[str, dict] = {}


@router.post("/export/ticket")
async def create_export_ticket(
    camera_id: int = Query(..., description="Camera ID"),
    start: str = Query(..., description="Clip start (RFC3339)"),
    duration: float = Query(..., gt=0, le=4 * 3600, description="Clip length (s)"),
    filename: str | None = Query(default=None, description="Download filename"),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """Mint a short-lived, single-use ticket for a clip download."""
    import time as _time
    import uuid as _uuid

    user_obj = await _authenticate_request(request, db)
    if not user_obj:
        raise HTTPException(status_code=401, detail="Unauthorized")

    camera = (
        db.query(Camera)
        .filter(Camera.id == camera_id, Camera.is_active == True)
        .first()
    )
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    _require_camera_view(user_obj, camera, db)

    # Sanitize the download filename (header-injection / path chars).
    safe_name = "clip.mp4"
    if filename:
        cleaned = "".join(
            c for c in filename if c.isalnum() or c in ("-", "_", ".")
        ).strip(".")
        if cleaned:
            safe_name = cleaned if cleaned.endswith(".mp4") else f"{cleaned}.mp4"

    # Purge expired tickets opportunistically.
    now = _time.time()
    for t in [t for t, v in _export_tickets.items() if v["expires"] < now]:
        _export_tickets.pop(t, None)

    ticket = _uuid.uuid4().hex
    _export_tickets[ticket] = {
        "expires": now + _EXPORT_TICKET_TTL_SECONDS,
        "path": _build_stream_name(
            settings.mediamtx_stream_prefix, camera.id, camera.ip_address
        ),
        "start": start,
        "duration": duration,
        "filename": safe_name,
    }
    return {
        "ticket": ticket,
        "download_url": f"{settings.api_prefix}/recordings/export?ticket={ticket}",
        "expires_in_seconds": _EXPORT_TICKET_TTL_SECONDS,
    }


@router.get("/export")
async def export_clip(
    ticket: str = Query(..., description="Export ticket"),
):
    """Stream a recording clip to the browser as a file download.

    The whole clip is STREAMED (chunked) from MediaMTX through the backend to
    the browser's disk — never buffered in browser memory (the old client-side
    export blob was multi-GB for long clips) and never fetched from an
    unauthenticated port.
    """
    import time as _time

    from fastapi.responses import StreamingResponse

    entry = _export_tickets.pop(ticket, None)  # single-use
    if not entry or entry["expires"] < _time.time():
        raise HTTPException(status_code=403, detail="Invalid or expired ticket")

    url = f"{settings.mediamtx_playback_url}/get"  # url-internal-ok: server-side clip fetch from mediamtx playback server
    params = {
        "path": entry["path"],
        "start": entry["start"],
        "duration": str(entry["duration"]),
    }

    import httpx as _httpx

    client = mediamtx_client.get_client()
    httpx_timeout = _httpx.Timeout(30.0, read=600.0)

    async def _stream():
        async with client.stream(
            "GET", url, params=params, timeout=httpx_timeout
        ) as resp:
            if resp.status_code != 200:
                recording_logger.warning(
                    f"Clip export: MediaMTX returned {resp.status_code}"
                )
                return
            async for chunk in resp.aiter_bytes(chunk_size=256 * 1024):
                yield chunk

    return StreamingResponse(
        _stream(),
        media_type="video/mp4",
        headers={
            "Content-Disposition": f'attachment; filename="{entry["filename"]}"',
            "Cache-Control": "no-store",
        },
    )


# =============================================================================
# HLS VOD Playback - Backend-generated manifests with 5s segments
# =============================================================================


@router.get("/playback/hls")
async def create_hls_session(
    camera_id: int = Query(..., description="Camera ID"),
    start: str = Query(..., description="Start time in RFC3339 format"),
    end: str = Query(..., description="End time in RFC3339 format"),
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

    user_obj = await _authenticate_request(request, db)
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
    _require_camera_view(user_obj, camera, db)

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
            db=db,
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

    # H.265 (hev1) recordings can't play through hls.js/MSE as recorded (browser
    # rejects the hev1 tag + PCM audio). For those, the client should use the
    # browser-remux endpoint instead of the HLS manifest.
    from services.hevc_remux_service import is_browser_incompatible_video

    needs_remux = is_browser_incompatible_video(session.video_codec)
    browser_mp4_url = (
        f"{settings.api_prefix}/recordings/playback/hls/{session.session_id}/browser.mp4"
        if needs_remux
        else None
    )

    return {
        "session_id": session.session_id,
        "manifest_url": manifest_url,
        "browser_mp4_url": browser_mp4_url,
        "video_codec": session.video_codec,
        "needs_remux": needs_remux,
        "file_offset_seconds": session.file_offset_seconds,
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
            # Same-origin only: no ACAO wildcard — a leaked session id
            # must not be usable cross-origin from an arbitrary site.
            "Cache-Control": "no-cache, no-store, must-revalidate",
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
        headers={"Cache-Control": "max-age=3600"},
    )


def _serve_file_ranges(path: Path, request: Request | None):
    """Range-request file serving shared by the HLS media endpoints."""
    from fastapi.responses import StreamingResponse

    if not path.is_file():
        raise HTTPException(status_code=404, detail="Recording file not available")

    file_size = path.stat().st_size
    range_header = request.headers.get("range") if request else None

    start = 0
    end = file_size - 1
    status_code = 200
    parsed = _parse_byte_range(range_header, file_size)
    if parsed is not None:
        start, end = parsed
        status_code = 206

    length = end - start + 1

    def _iter_file():
        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = length
            chunk = 64 * 1024
            while remaining > 0:
                data = fh.read(min(chunk, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Cache-Control": "max-age=86400",
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    return StreamingResponse(
        _iter_file(),
        status_code=status_code,
        media_type="video/mp4",
        headers=headers,
    )


@router.get("/playback/hls/{session_id}/media")
async def get_hls_media(
    session_id: str,
    request: Request = None,
):
    """
    Serve byte ranges of the session's FIRST on-disk clip file (legacy path).

    Kept for older manifests; new multi-file manifests address clips as
    ``media/{n}`` (see get_hls_media_file).

    Security: session_id acts as the auth token (same as the other playback
    routes). The file was resolved under the recordings base at session
    creation (V-005), so no request-supplied path reaches the filesystem here.
    """
    from services.hls_playback_service import HlsPlaybackService

    session = await HlsPlaybackService.get_session(session_id)
    if not session or not session.file_path:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    return _serve_file_ranges(Path(session.file_path), request)


@router.get("/playback/hls/{session_id}/media/{file_index}")
async def get_hls_media_file(
    session_id: str,
    file_index: int,
    request: Request = None,
):
    """
    Serve byte ranges of one clip file in the session's playback window.

    The multi-file byte-range manifest addresses each contiguous clip as
    ``media/{n}``; hls.js fetches init segments and media segments as HTTP
    Ranges against it, so playback crosses clip boundaries with zero session
    churn and every seek is a single ranged read.

    Security: session_id is the auth token; the file list was resolved under
    the recordings base at session creation (V-005) — ``file_index`` selects
    from that frozen list, no request-supplied path reaches the filesystem.
    """
    from services.hls_playback_service import HlsPlaybackService

    session = await HlsPlaybackService.get_session(session_id)
    if not session or not session.files:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    if file_index < 0 or file_index >= len(session.files):
        raise HTTPException(status_code=404, detail="No such file in session")

    return _serve_file_ranges(Path(session.files[file_index].path), request)


@router.get("/playback/hls/{session_id}/browser.mp4")
async def get_browser_playable_recording(
    session_id: str,
    request: Request = None,
):
    """Serve an H.265 recording as a browser-playable ``hvc1`` video-only MP4.

    The MP4 is VIRTUAL: a small in-RAM index (built sub-second, headers-only
    scan) maps every output byte to the original recording, and each HTTP
    Range request is answered by streaming the mapped bytes straight from the
    source file — no ffmpeg, no re-encode, no on-disk copy. H.264 recordings
    never hit this route (they use the HLS byte-range path).
    """
    from fastapi.responses import StreamingResponse

    from services.hevc_remux_service import get_remux_index, iter_index_range
    from services.hls_playback_service import HlsPlaybackService

    session = await HlsPlaybackService.get_session(session_id)
    if not session or not session.file_path:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    # Prefer the snapshot frozen when the session was created: a still-recording
    # file grows, and re-indexing mid-playback would move every byte offset under
    # the player. Falling back to a fresh index keeps older sessions working.
    index = session.remux_index or await get_remux_index(session.file_path)
    if index is None:
        raise HTTPException(
            status_code=500, detail="Could not prepare this recording for playback"
        )

    file_size = index.total_size
    range_header = request.headers.get("range") if request else None

    start, end, status_code = 0, file_size - 1, 200
    parsed = _parse_byte_range(range_header, file_size)
    if parsed is not None:
        start, end = parsed
        status_code = 206
    length = end - start + 1

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Cache-Control": "max-age=86400",
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    return StreamingResponse(
        iter_index_range(index, start, end),
        status_code=status_code,
        media_type="video/mp4",
        headers=headers,
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
        },
    )


@router.delete("/playback/hls/{session_id}")
async def delete_hls_session(
    session_id: str,
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    Explicitly delete/invalidate an HLS session.

    Called when user stops playback to free resources.
    """
    from services.hls_playback_service import HlsPlaybackService

    user_obj = await _authenticate_request(request, db)
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

    # Returned to the browser, so use the external fallback chain (not the
    # Docker-internal mediamtx host). Locked by test_url_fallback_chain.py.
    playback_url = (
        settings.mediamtx_external_playback_url
        or settings.mediamtx_playback_url
        or "http://127.0.0.1:9996"
    )

    return {
        "playback_url": playback_url,
        "stream_prefix": settings.mediamtx_stream_prefix,
        "hls_enabled": True,
        "hls_segment_duration": HlsPlaybackService.SEGMENT_DURATION,
        "hls_session_ttl": HlsPlaybackService.SESSION_TTL_SECONDS,
    }


@router.get("/segments/{camera_id}")
async def get_day_segments(
    camera_id: int,
    date: str | None = Query(
        default=None,
        description="Day in YYYY-MM-DD (interpreted in `tz`). Defaults to today.",
    ),
    start: str | None = Query(
        default=None, description="Explicit range start (ISO 8601), overrides date"
    ),
    end: str | None = Query(
        default=None, description="Explicit range end (ISO 8601), overrides date"
    ),
    tz: str | None = Query(
        default=None,
        description="IANA timezone for interpreting `date` (default: server local)",
    ),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    Get the continuous recording segments for a camera on a given day.

    Unlike ``/list`` (which aggregates a whole day into one entry), this returns
    every continuous recording run as its own segment with an absolute
    ``start``, ``duration`` and a browser-reachable ``playback_url``. The DVR
    playback timeline uses this to draw footage/gap blocks and to seek to any
    wall-clock instant via ``playback_base_url``/``path``.

    The day window is LOCAL midnight → next local midnight (in ``tz``), or the
    explicit ``start``/``end`` instants when provided. Served from the
    recordings DB index; falls back to MediaMTX for unindexed cameras.
    """
    from datetime import datetime
    from urllib.parse import quote

    user_obj = await _authenticate_request(request, db)
    if not user_obj:
        raise HTTPException(status_code=401, detail="Unauthorized")

    camera = (
        db.query(Camera)
        .filter(Camera.id == camera_id, Camera.is_active == True)
        .first()
    )
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    _require_camera_view(user_obj, camera, db)

    from services.recording_query_service import (
        camera_has_rows,
        day_segments,
        local_day_bounds,
        resolve_query_tz,
    )

    query_tz = resolve_query_tz(tz)

    # Resolve the requested window: explicit instants win, else the LOCAL day.
    range_start: datetime | None = None
    range_end: datetime | None = None
    if start and end:
        try:
            range_start = datetime.fromisoformat(start.replace("Z", "+00:00"))
            range_end = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid start/end format")
    target_date = date or datetime.now(query_tz).strftime("%Y-%m-%d")
    if range_start is None or range_end is None:
        try:
            range_start, range_end = local_day_bounds(target_date, query_tz)
        except (ValueError, IndexError):
            raise HTTPException(status_code=400, detail="Invalid date format")

    path = _build_stream_name(
        settings.mediamtx_stream_prefix, camera.id, camera.ip_address
    )

    # The LIST call stays internal; every URL handed to the browser uses the
    # external fallback chain (matches the other playback routes).
    browser_playback_base = (
        settings.mediamtx_external_playback_url
        or settings.mediamtx_playback_url
        or "http://127.0.0.1:9996"
    ).rstrip("/")

    # Live edge: the instant after which footage is NOT yet safely playable.
    # Only the unfinished tail of a still-being-written file qualifies — the
    # footage already flushed to it plays fine — so the edge sits
    # LIVE_EDGE_LAG_SECONDS behind now rather than at the file's start. Anything
    # older is served as normal VOD; the UI paints the remainder as LIVE and
    # sends the user to Live View for it. Clamped to the file start, so a file
    # only seconds old is entirely live.
    #
    # Resolution globs only the current/previous hour directories (see
    # find_latest_recording_file) instead of walking the whole archive, and
    # runs off the event loop.
    live_edge_start = None
    try:
        from pathlib import Path as _Path

        from services.recording_paths import find_latest_recording_file
        from services.storage_service import get_effective_recordings_base_path

        now = datetime.now(UTC)
        root = _Path(get_effective_recordings_base_path(db))

        def _resolve_live_edge():
            latest = find_latest_recording_file(camera_id, root, now)
            if latest is None:
                return None
            live_path, file_start = latest
            if now.timestamp() - live_path.stat().st_mtime < 60:
                return _live_edge_iso(file_start, now)
            return None

        live_edge_start = await asyncio.to_thread(_resolve_live_edge)
    except Exception:
        live_edge_start = None

    empty = {
        "segments": [],
        "camera_id": camera_id,
        "camera_name": camera.name or f"Camera {camera_id}",
        "path": path,
        "date": target_date,
        "total_duration": 0,
        "segment_count": 0,
        "playback_base_url": browser_playback_base,
        "live_edge_start": live_edge_start,
    }

    def _seg_payload(start_iso: str, duration: float) -> dict:
        encoded_start = quote(start_iso, safe="")
        return {
            "start": start_iso,
            "duration": duration,
            "playback_url": (
                f"{browser_playback_base}/get?path={path}"
                f"&start={encoded_start}&duration={duration}"
            ),
        }

    try:
        # Primary: the recordings DB index — one indexed range query, clips
        # merged into continuous segments server-side.
        if settings.use_db_recordings_index and camera_has_rows(db, camera_id):
            merged = day_segments(db, camera_id, range_start, range_end)
            segments = [
                _seg_payload(
                    m["start"].astimezone(UTC).isoformat().replace("+00:00", "Z"),
                    m["duration"],
                )
                for m in merged
            ]
            result = dict(empty)
            result["segments"] = segments
            result["total_duration"] = sum(m["duration"] for m in merged)
            result["segment_count"] = len(segments)
            return result

        # Fallback (unindexed camera / flag off): date-bounded MediaMTX query.
        # A one-hour margin catches segments spanning the window start.
        all_segments = await mediamtx_client.list_segments(
            path,
            start=range_start - timedelta(hours=1),
            end=range_end,
            timeout=10.0,
        )
        if all_segments is None:
            return empty

        fallback_segments = []
        for seg in all_segments:
            start_str = seg.get("start", "")
            if not start_str:
                continue
            try:
                dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            except Exception:
                continue
            if dt < range_start or dt >= range_end:
                continue
            fallback_segments.append(_seg_payload(start_str, seg.get("duration", 0)))

        fallback_segments.sort(key=lambda s: s.get("start", ""))
        result = dict(empty)
        result["segments"] = fallback_segments
        result["total_duration"] = sum(
            s.get("duration", 0) for s in fallback_segments
        )
        result["segment_count"] = len(fallback_segments)
        return result
    except Exception as e:
        recording_logger.error(
            f"Failed to get day segments for camera {camera_id}: {e}"
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

        # URL-encode the start time. Returned to the browser, so use the
        # external fallback chain.
        encoded_start = quote(first_start, safe="")
        browser_playback_base = (
            settings.mediamtx_external_playback_url
            or settings.mediamtx_playback_url
            or "http://127.0.0.1:9996"
        )
        playback_url = f"{browser_playback_base}/get?path={path}&start={encoded_start}&duration={total_duration}"

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
    tz: str | None = Query(
        default=None,
        description="IANA timezone for day grouping (default: server local)",
    ),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    List recordings grouped by camera and LOCAL date (in ``tz``).
    Returns user-friendly recording counts (1 recording = 1 day per camera).

    Served from the recordings DB index (one aggregate query); cameras with
    no indexed rows fall back to MediaMTX, and the whole endpoint falls back
    to MediaMTX/filesystem when the index is disabled.
    """
    user_obj = await _authenticate_request(request, db)
    if not user_obj:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Check if MediaMTX is available (async + TTL-cached)
    mediamtx_available = await _check_mediamtx_available()

    # Get cameras to query
    if camera_id:
        cameras = (
            db.query(Camera)
            .filter(Camera.id == camera_id, Camera.is_active == True)
            .all()
        )
    else:
        cameras = db.query(Camera).filter(Camera.is_active == True).all()

    # Camera-level scoping: non-superusers only list cameras they may view.
    cameras = [c for c in cameras if _can_view_camera(user_obj, c, db)]

    result = []
    total_recordings = 0
    total_duration = 0

    # ---- Primary path: recordings DB index --------------------------------
    if settings.use_db_recordings_index:
        from services.recording_query_service import day_summaries, resolve_query_tz

        query_tz = resolve_query_tz(tz)
        browser_playback_base = (
            settings.mediamtx_external_playback_url
            or settings.mediamtx_playback_url
            or "http://127.0.0.1:9996"
        ).rstrip("/")

        summaries = day_summaries(db, [c.id for c in cameras], query_tz)
        unindexed = [c for c in cameras if c.id not in summaries]

        for cam in cameras:
            days = summaries.get(cam.id)
            if not days:
                continue
            path = _build_stream_name(
                settings.mediamtx_stream_prefix, cam.id, cam.ip_address
            )
            daily_recordings = []
            cam_duration = 0.0
            for d in days:
                first_iso = (
                    d["first_start"].astimezone(UTC).isoformat().replace("+00:00", "Z")
                )
                from urllib.parse import quote as _quote

                daily_recordings.append(
                    {
                        "date": d["date"],
                        "total_duration": d["total_duration"],
                        "segment_count": d["segment_count"],
                        "first_start": first_iso,
                        "playback_url": (
                            f"{browser_playback_base}/get?path={path}"
                            f"&start={_quote(first_iso, safe='')}"
                            f"&duration={d['total_duration']}"
                        ),
                    }
                )
                cam_duration += d["total_duration"]

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

        # Bootstrap fallback: cameras not indexed yet (fresh migration, the
        # reconciler hasn't caught up) still list via MediaMTX.
        if unindexed and mediamtx_available:
            cameras = unindexed
        else:
            return {
                "cameras": result,
                "total_recordings": total_recordings,
                "total_duration": total_duration,
                "total_cameras": len(result),
                "mediamtx_available": mediamtx_available,
            }

    if mediamtx_available:
        # Use MediaMTX playback server (preferred - accurate durations).
        # Parallel, date-bounded fan-out: one concurrent request per camera
        # (semaphore-capped) limited to the retention window, instead of the
        # old sequential full-history fetch that froze the event loop for up
        # to cameras × 10s.
        retention_days = RetentionService._get_retention_settings(db).get(
            "retention_days", 30
        )
        window_start = datetime.now(UTC) - timedelta(days=retention_days + 1)

        path_by_cam = {
            cam.id: _build_stream_name(
                settings.mediamtx_stream_prefix, cam.id, cam.ip_address
            )
            for cam in cameras
        }
        listings = await mediamtx_client.list_segments_many(
            list(path_by_cam.values()), start=window_start, timeout=10.0
        )

        for cam in cameras:
            path = path_by_cam[cam.id]
            segments = listings.get(path)
            if not segments:
                continue
            # Group by date
            daily_recordings = _group_segments_by_date(segments, path)
            cam_duration = sum(d["total_duration"] for d in daily_recordings)

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
                # Use storage service to list recordings from filesystem —
                # a full directory walk, so keep it off the event loop.
                fs_data = await asyncio.to_thread(
                    storage_service.list_recordings, db, cam.id, None, None, 10000
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
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    Get recording statistics for dashboard.
    Returns counts and durations in user-friendly format.
    Falls back to filesystem listing if MediaMTX is unavailable.
    """
    user_obj = await _authenticate_request(request, db)
    if not user_obj:
        raise HTTPException(status_code=401, detail="Unauthorized")

    cameras = db.query(Camera).filter(Camera.is_active == True).all()
    cameras = [c for c in cameras if _can_view_camera(user_obj, c, db)]
    mediamtx_available = await _check_mediamtx_available()

    total_recordings = 0  # Camera-days
    total_duration = 0
    cameras_with_recordings = 0

    # ---- Primary path: recordings DB index (one aggregate query) -----------
    if settings.use_db_recordings_index:
        from services.recording_query_service import day_summaries, resolve_query_tz

        summaries = day_summaries(
            db, [c.id for c in cameras], resolve_query_tz(None)
        )
        if summaries or not mediamtx_available:
            for days in summaries.values():
                total_recordings += len(days)
                total_duration += sum(d["total_duration"] for d in days)
                cameras_with_recordings += 1
            return {
                "total_recordings": total_recordings,
                "total_duration": total_duration,
                "total_duration_formatted": _format_duration(total_duration),
                "cameras_with_recordings": cameras_with_recordings,
                "total_cameras": len(cameras),
                "mediamtx_available": mediamtx_available,
            }

    if mediamtx_available:
        retention_days = RetentionService._get_retention_settings(db).get(
            "retention_days", 30
        )
        window_start = datetime.now(UTC) - timedelta(days=retention_days + 1)

        path_by_cam = {
            cam.id: _build_stream_name(
                settings.mediamtx_stream_prefix, cam.id, cam.ip_address
            )
            for cam in cameras
        }
        listings = await mediamtx_client.list_segments_many(
            list(path_by_cam.values()), start=window_start, timeout=5.0
        )

        for cam in cameras:
            segments = listings.get(path_by_cam[cam.id])
            if segments:
                daily_recordings = _group_segments_by_date(
                    segments, path_by_cam[cam.id]
                )
                total_recordings += len(daily_recordings)
                total_duration += sum(
                    d["total_duration"] for d in daily_recordings
                )
                cameras_with_recordings += 1
    else:
        # Fallback to filesystem listing
        for cam in cameras:
            path = _build_stream_name(
                settings.mediamtx_stream_prefix, cam.id, cam.ip_address
            )
            try:
                fs_data = await asyncio.to_thread(
                    storage_service.list_recordings, db, cam.id, None, None, 10000
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
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    Get recording sessions grouped by camera and date, specifically for AI processing.
    Sessions are continuous recording periods (with small gaps allowed).
    Falls back to filesystem listing if MediaMTX is unavailable.
    """
    user_obj = await _authenticate_request(request, db)
    if not user_obj:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Check if MediaMTX is available
    mediamtx_available = await _check_mediamtx_available()

    # Get cameras to query — scoped to what this user may view.
    if camera_id:
        camera = (
            db.query(Camera)
            .filter(Camera.id == camera_id, Camera.is_active == True)
            .first()
        )
        if not camera:
            raise HTTPException(status_code=404, detail="Camera not found")
        _require_camera_view(user_obj, camera, db)
        cameras = [camera]
    else:
        cameras = _viewable_active_cameras(db, user_obj)

    result = []

    if mediamtx_available:
        # Use MediaMTX playback server (preferred - accurate durations).
        # Parallel, retention-bounded fan-out (see /list for rationale).
        retention_days = RetentionService._get_retention_settings(db).get(
            "retention_days", 30
        )
        window_start = datetime.now(UTC) - timedelta(days=retention_days + 1)

        path_by_cam = {
            cam.id: _build_stream_name(
                settings.mediamtx_stream_prefix, cam.id, cam.ip_address
            )
            for cam in cameras
        }
        listings = await mediamtx_client.list_segments_many(
            list(path_by_cam.values()), start=window_start, timeout=10.0
        )

        for cam in cameras:
            segments = listings.get(path_by_cam[cam.id])
            if not segments:
                continue
            try:
                # Group segments by date first
                from collections import defaultdict

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
                # Use storage service to list recordings from filesystem —
                # a full directory walk, so keep it off the event loop.
                fs_data = await asyncio.to_thread(
                    storage_service.list_recordings, db, cam.id, None, None, 10000
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
