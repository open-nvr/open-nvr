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
MediaMTX admin control router.

Provides superuser-only control API passthrough for:
- Global configuration (get/patch)
- Path defaults (get/patch)
- Per-path configuration (CRUD)
- Recording control (enable/disable/status)
- Stream pushing and auto-provisioning
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from core.auth import get_current_superuser
from core.config import settings
from core.database import get_db
from models import Camera

# Import hook token verification (shared with mediamtx_hooks router)
from routers.mediamtx_hooks import _verify_hook_token
from services.mediamtx_admin_service import MediaMtxAdminService
from services.mediamtx_config_service import MediaMtxConfigService
from services.storage_service import get_effective_recordings_base_path

mediamtx_logger = logging.getLogger("opennvr.mediamtx")

router = APIRouter(
    prefix="/mediamtx", tags=["mediamtx", "control-api"]
)  # mounted at /api/v1


# ---- Simple health endpoint (no superuser required) ----
@router.get("/health")
async def mediamtx_health():
    """Lightweight health check for MediaMTX Admin API.

    Returns { status: "ok"|"down", admin_api: str | None, http_status?: int }
    """
    try:
        if not MediaMtxAdminService.is_configured():
            return {
                "status": "down",
                "admin_api": settings.mediamtx_admin_api,
                "reason": "no_admin_api",
            }
        # Use global_get as a cheap probe
        res = await MediaMtxAdminService.global_get()
        healthy = (res or {}).get("status") == "ok" and (res or {}).get(
            "http_status"
        ) in (200, 201)
        out = {
            "status": "ok" if healthy else "down",
            "admin_api": settings.mediamtx_admin_api,
        }
        if "http_status" in res:
            out["http_status"] = res["http_status"]
        return out
    except Exception as e:
        return {
            "status": "down",
            "admin_api": settings.mediamtx_admin_api,
            "error": str(e),
        }


# ---- Control API passthrough (superuser) ----


@router.get("/admin/global")
async def mtx_global_get(
    current_user=Depends(get_current_superuser),
):
    return await MediaMtxAdminService.global_get()


@router.patch("/admin/global")
async def mtx_global_patch(
    payload: dict[str, Any],
    current_user=Depends(get_current_superuser),
):
    return await MediaMtxAdminService.global_patch(payload)


@router.get("/admin/pathdefaults")
async def mtx_pathdefaults_get(
    current_user=Depends(get_current_superuser),
):
    return await MediaMtxAdminService.pathdefaults_get()


@router.patch("/admin/pathdefaults")
async def mtx_pathdefaults_patch(
    payload: dict[str, Any],
    current_user=Depends(get_current_superuser),
):
    return await MediaMtxAdminService.pathdefaults_patch(payload)


@router.patch("/admin/paths/{camera_id}")
async def mtx_paths_patch(
    camera_id: int,
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    cam = db.query(Camera).filter(Camera.id == camera_id).first()
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found")
    return await MediaMtxAdminService.patch_path(camera_id, cam.ip_address, payload)


@router.get("/admin/paths/list")
async def mtx_paths_list(
    current_user=Depends(get_current_superuser),
):
    """List all active paths/streams."""
    return await MediaMtxAdminService.list_active_paths()


@router.get("/admin/paths/{camera_id}")
async def mtx_paths_get(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Get active path information for a camera."""
    cam = db.query(Camera).filter(Camera.id == camera_id).first()
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found")
    return await MediaMtxAdminService.get_active_path(camera_id, cam.ip_address)


@router.post("/admin/streams/push/{camera_id}")
async def push_rtsp_stream(
    camera_id: int,
    rtsp_url: str,
    enable_recording: bool = False,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Push RTSP stream to MediaMTX."""
    cam = db.query(Camera).filter(Camera.id == camera_id).first()
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found")

    return await MediaMtxAdminService.push_rtsp_stream(
        camera_id, cam.ip_address, rtsp_url, enable_recording
    )


@router.get("/admin/recordings/list")
async def mtx_recordings_list(
    camera_id: int | None = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """List all recordings or for a specific camera."""
    camera_ip = None
    if camera_id:
        cam = db.query(Camera).filter(Camera.id == camera_id).first()
        if not cam:
            raise HTTPException(status_code=404, detail="Camera not found")
        camera_ip = cam.ip_address

    return await MediaMtxAdminService.list_recordings(camera_id, camera_ip)


@router.get("/admin/recordings/{camera_id}/{segment}")
async def mtx_recording_get(
    camera_id: int,
    segment: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Get recording segment information."""
    cam = db.query(Camera).filter(Camera.id == camera_id).first()
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found")

    return await MediaMtxAdminService.get_recording_segment(
        camera_id, cam.ip_address, segment
    )


@router.delete("/admin/recordings/{camera_id}/{segment}")
async def mtx_recording_delete(
    camera_id: int,
    segment: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Delete a recording segment."""
    cam = db.query(Camera).filter(Camera.id == camera_id).first()
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found")

    return await MediaMtxAdminService.delete_recording_segment(
        camera_id, cam.ip_address, segment
    )


@router.post("/admin/recordings/enable/{camera_id}")
async def enable_recording(
    camera_id: int,
    duration: str | None = "60s",
    segment_duration: str | None = "10s",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Enable recording for a camera stream with configurable duration."""
    cam = db.query(Camera).filter(Camera.id == camera_id).first()
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found")

    # Use effective path from database (user setting) or settings fallback
    base_path = get_effective_recordings_base_path(db)

    # Create recording configuration payload
    recording_config = {
        "record": True,
        "recordPath": f"{base_path}/cam-{camera_id}/%Y/%m/%d/%H-%M-%S-%f",
        "recordFormat": "mp4",
        "recordPartDuration": segment_duration,
        "recordSegmentDuration": duration,
        "recordDeleteAfter": "168h",  # 7 days default
    }

    return await MediaMtxAdminService.patch_path(
        camera_id, cam.ip_address, recording_config
    )


@router.post("/admin/recordings/disable/{camera_id}")
async def disable_recording(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Disable recording for a camera stream."""
    cam = db.query(Camera).filter(Camera.id == camera_id).first()
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found")

    # Disable recording
    recording_config = {"record": False}

    return await MediaMtxAdminService.patch_path(
        camera_id, cam.ip_address, recording_config
    )


@router.get("/admin/recordings/status/{camera_id}")
async def get_recording_status(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Get current recording status for a camera."""
    cam = db.query(Camera).filter(Camera.id == camera_id).first()
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found")

    path_info = await MediaMtxAdminService.get_active_path(camera_id, cam.ip_address)

    # Extract recording status from path configuration
    if path_info and "conf" in path_info:
        conf = path_info["conf"]
        return {
            "camera_id": camera_id,
            "recording_enabled": conf.get("record", False),
            "record_path": conf.get("recordPath"),
            "record_format": conf.get("recordFormat", "mp4"),
            "segment_duration": conf.get("recordPartDuration", "10s"),
            "total_duration": conf.get("recordSegmentDuration", "60s"),
            "delete_after": conf.get("recordDeleteAfter", "168h"),
        }

    return {
        "camera_id": camera_id,
        "recording_enabled": False,
        "message": "Stream not active or configuration not available",
    }


@router.get("/setup")
async def get_mediamtx_setup(
    request: Request,
    current_user=Depends(get_current_superuser),
):
    """Get MediaMTX setup instructions and configuration."""
    base_url = f"{request.url.scheme}://{request.url.netloc}"
    return MediaMtxConfigService.get_setup_instructions(base_url)


# ---- Startup and Auto-Provisioning ----


@router.get("/startup/hook")
async def mediamtx_startup_hook(
    request: Request,
    delay: int = Query(
        default=5, description="Delay in seconds before starting auto-provisioning"
    ),
):
    """
    MediaMTX startup hook endpoint.
    Called by MediaMTX runOnInit hook when server starts.
    Triggers automatic re-provisioning of all cameras from database.
    """
    if not _verify_hook_token(request):
        raise HTTPException(status_code=401, detail="Invalid token")

    import asyncio

    from services.mediamtx_startup_service import MediaMtxStartupService

    # Run auto-provisioning in background to not block MediaMTX startup
    async def run_auto_provision():
        try:
            result = await MediaMtxStartupService.auto_provision_all_cameras(
                delay_seconds=delay
            )
            mediamtx_logger.info(
                f"MediaMTX startup auto-provisioning completed: {result}"
            )
        except Exception as e:
            mediamtx_logger.error(f"MediaMTX startup auto-provisioning failed: {e}")

    # Start background task
    asyncio.create_task(run_auto_provision())

    return {
        "status": "accepted",
        "message": "Auto-provisioning started in background",
        "delay_seconds": delay,
    }


@router.post("/startup/provision-all")
async def provision_all_cameras(
    delay_seconds: int = 0,
    max_retries: int = 3,
    retry_delay: int = 2,
    current_user=Depends(get_current_superuser),
):
    """Manually trigger auto-provisioning of all cameras."""
    from services.mediamtx_startup_service import MediaMtxStartupService

    result = await MediaMtxStartupService.auto_provision_all_cameras(
        delay_seconds=delay_seconds, max_retries=max_retries, retry_delay=retry_delay
    )

    return result


@router.post("/startup/provision-camera/{camera_id}")
async def provision_single_camera(
    camera_id: int,
    force: bool = False,
    current_user=Depends(get_current_superuser),
):
    """Manually provision a specific camera."""
    from services.mediamtx_startup_service import MediaMtxStartupService

    result = await MediaMtxStartupService.provision_camera_by_id(camera_id, force=force)
    return result


@router.get("/startup/status")
async def get_startup_status(
    current_user=Depends(get_current_superuser),
):
    """Get status of all cameras and their provisioning state."""
    from services.mediamtx_startup_service import MediaMtxStartupService

    return MediaMtxStartupService.get_startup_status()


@router.post("/startup/push-path-defaults")
async def push_path_defaults(
    current_user=Depends(get_current_superuser),
):
    """
    Manually push the recording path defaults to MediaMTX.
    This is useful when recording path is changed via UI and you want to apply it
    without restarting MediaMTX or the application server.
    """
    from services.mediamtx_startup_service import MediaMtxStartupService

    result = await MediaMtxStartupService.push_path_defaults()
    return result
