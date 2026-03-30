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
MediaMTX webhook endpoints.

Receives recording segment events from MediaMTX hooks and handles:
- Segment creation acknowledgement
- Segment completion (DB recording, cloud mirror, fast-start optimisation)

All endpoints are protected with X-MTX-Secret header verification.
"""

import json
import logging
import os
import pathlib
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any

import requests
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from core.config import settings
from core.database import get_db
from models import Camera

# Logger for faststart operations
faststart_logger = logging.getLogger("opennvr.faststart")


def validate_segment_path(segment_path: str) -> str:
    """
    Validate that the segment path is safe and free of directory traversal attacks.
    """
    if not segment_path:
        return ""

    try:
        # Prevent Directory Traversal
        if ".." in segment_path:
            raise ValueError("Path traversal sequence detected")

        # Resolve absolute path to canonicalize
        path_obj = pathlib.Path(segment_path).resolve()

        # Ensure it is treated as a file path
        return str(path_obj)
    except Exception as e:
        faststart_logger.error(
            f"Security: Path validation failed for '{segment_path}': {e}"
        )
        raise HTTPException(status_code=400, detail="Invalid file path")


router = APIRouter(prefix="/mediamtx", tags=["mediamtx", "hooks"])  # mounted at /api/v1


# ---- FastStart MP4 Background Processing ----


def apply_faststart_to_segment(segment_path: str) -> dict[str, Any]:
    """
    Apply MP4 fast-start (moov atom at beginning) to a recording segment.
    Uses FFmpeg remux (no re-encoding) for instant processing.

    Args:
        segment_path: Full path to the MP4 file

    Returns:
        Dict with status, original_size, new_size, and any error message
    """
    result = {
        "segment_path": segment_path,
        "faststart_applied": False,
        "original_size": None,
        "new_size": None,
        "error": None,
    }

    # Validate file exists
    if not os.path.exists(segment_path):
        result["error"] = f"File not found: {segment_path}"
        faststart_logger.error(result["error"])
        return result

    # Skip non-MP4 files
    if not segment_path.lower().endswith(".mp4"):
        result["error"] = f"Not an MP4 file: {segment_path}"
        faststart_logger.warning(result["error"])
        return result

    try:
        # Get original file size
        result["original_size"] = os.path.getsize(segment_path)

        # Create temporary output path
        temp_path = segment_path + ".faststart.tmp"

        # Build FFmpeg command for remux with faststart
        # -y: overwrite output without asking
        # -i: input file
        # -c copy: copy streams without re-encoding (instant)
        # -movflags faststart: move moov atom to beginning
        ffmpeg_cmd = [
            "ffmpeg",
            "-y",
            "-i",
            segment_path,
            "-c",
            "copy",
            "-movflags",
            "faststart",
            temp_path,
        ]

        faststart_logger.info(f"Applying faststart to: {segment_path}")

        # Run FFmpeg subprocess
        process = subprocess.run(
            ffmpeg_cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout for large files
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

        if process.returncode != 0:
            result["error"] = f"FFmpeg failed: {process.stderr[:500]}"
            faststart_logger.error(result["error"])
            # Clean up temp file if it exists
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            return result

        # Verify temp file was created and has content
        if not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
            result["error"] = "FFmpeg produced empty or no output file"
            faststart_logger.error(result["error"])
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            return result

        # Atomic replacement: rename temp to original
        # On Windows, we need to remove original first
        try:
            # Get new file size before replacement
            result["new_size"] = os.path.getsize(temp_path)

            # Remove original file
            os.remove(segment_path)

            # Rename temp to original
            shutil.move(temp_path, segment_path)

            result["faststart_applied"] = True

            # Calculate size reduction
            if result["original_size"] and result["new_size"]:
                reduction = (
                    (result["original_size"] - result["new_size"])
                    / result["original_size"]
                ) * 100
                faststart_logger.info(
                    f"Faststart applied successfully: {segment_path} "
                    f"({result['original_size']} -> {result['new_size']} bytes, "
                    f"{reduction:.1f}% reduction)"
                )
            else:
                faststart_logger.info(f"Faststart applied successfully: {segment_path}")

        except Exception as e:
            result["error"] = f"Failed to replace original file: {e!s}"
            faststart_logger.error(result["error"])
            # Try to clean up temp file
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            return result

    except subprocess.TimeoutExpired:
        result["error"] = "FFmpeg timed out after 5 minutes"
        faststart_logger.error(result["error"])
    except FileNotFoundError:
        result["error"] = "FFmpeg not found. Ensure FFmpeg is installed and in PATH."
        faststart_logger.error(result["error"])
    except Exception as e:
        result["error"] = f"Unexpected error: {e!s}"
        faststart_logger.exception(result["error"])

    return result


# ---- Webhook authentication ----


def _verify_hook_token(request: Request) -> bool:
    """
    Verify MediaMTX webhook authentication.

    Security: Checks X-MTX-Secret header against configured secret.
    Falls back to query param 't' for backwards compatibility.

    IMPORTANT: In production, only X-MTX-Secret header should be used.
    """
    import secrets

    # Primary: Check X-MTX-Secret header (secure method)
    header_secret = request.headers.get("X-MTX-Secret")
    if header_secret and settings.mediamtx_secret:
        return secrets.compare_digest(header_secret, settings.mediamtx_secret)

    # Fallback: Check query parameter (legacy, less secure)
    t = request.query_params.get("t")
    if settings.mediamtx_webhook_token and t:
        return secrets.compare_digest(t, settings.mediamtx_webhook_token)

    # If mediamtx_secret is configured, require it
    if settings.mediamtx_secret:
        return False

    # No security configured - reject in production
    return False


# ---- Webhook endpoints ----


@router.get("/hooks/segment-create")
async def hook_segment_create(
    request: Request,
    path: str = Query(..., description="MediaMTX path name (MTX_PATH)"),
    segment_path: str = Query(..., description="Segment file path (MTX_SEGMENT_PATH)"),
):
    if not _verify_hook_token(request):
        raise HTTPException(status_code=401, detail="Invalid token")

    # SECURITY FIX: Validate path for directory traversal
    if segment_path:
        validate_segment_path(segment_path)

    # For create event, we can just acknowledge; details are available on complete
    return {
        "status": "ok",
        "event": "segment-create",
        "path": path,
        "segment_path": segment_path,
    }


@router.get("/hooks/segment-complete")
async def hook_segment_complete(
    request: Request,
    background_tasks: BackgroundTasks,
    path: str = Query(..., description="MediaMTX path name (MTX_PATH)"),
    segment_path: str = Query(..., description="Segment file path (MTX_SEGMENT_PATH)"),
    segment_duration: str | None = Query(
        None, description="Segment duration (MTX_SEGMENT_DURATION)"
    ),
    db: Session = Depends(get_db),
):
    if not _verify_hook_token(request):
        raise HTTPException(status_code=401, detail="Invalid token")

    # SECURITY FIX: Validate path for directory traversal
    if segment_path:
        segment_path = validate_segment_path(segment_path)

    # Import required modules at the top of the function
    from models import Camera as _Camera, Recording, User

    # Try to map path to camera by our naming convention
    # Path may be like "cam-<id>" or "cam-<ip>"; we first try id form
    cam: Camera | None = None
    if path.startswith(("cam-", "CAM-", "Cam-")):
        try:
            cid_str = path.split("-", 1)[1]
            cid = int(cid_str)
            cam = db.query(Camera).filter(Camera.id == cid).first()
        except Exception:
            cam = None
    if cam is None:
        # Try to match by ip pattern embedded in path if it's ip-mode
        # We normalize camera ip by replacing '.' with '_'
        # This is a best-effort; if not found, we still accept the webhook
        all_cams = db.query(_Camera).all()
        for c in all_cams:
            ip_tag = c.ip_address.replace(".", "_") if c.ip_address else ""
            if path == f"cam-{ip_tag}":
                cam = c
                break

    # Parse duration
    duration_sec: float | None = None
    if segment_duration:
        # MediaMTX passes a number as string; if contains 's', strip
        s = str(segment_duration).strip().lower().rstrip("s")
        try:
            duration_sec = float(s)
        except Exception:
            duration_sec = None

    # Store recording in the database
    if cam:
        try:
            # Get file size if possible
            file_size = None
            try:
                if os.path.exists(segment_path):
                    file_size = os.path.getsize(segment_path)
            except Exception:
                pass

            # Find a user to attribute this recording to (system recordings need a user)
            # Try to get the first available user ID
            system_user = db.query(User).first()
            created_by_id = system_user.id if system_user else None

            # For now, use simple timing - we can improve this later
            # Use current time as end time, calculate start time from duration
            end_time = datetime.now(UTC)
            if duration_sec and duration_sec > 0:
                start_time = end_time - timedelta(seconds=duration_sec)
            else:
                start_time = end_time

            # Create recording entry only if we have a user to assign it to
            if created_by_id:
                # Convert absolute path to relative path
                # MediaMTX sends: /app/recordings/cam-1/2026/02/26/07-03-24-036694.mp4
                # We need: cam-1/2026/02/26/07-03-24-036694.mp4
                relative_path = segment_path
                norm_path = segment_path.replace("\\", "/")
                lower_path = norm_path.lower()
                marker = "/recordings/"
                idx = lower_path.find(marker)
                if idx >= 0:
                    relative_path = norm_path[idx + len(marker):]
                elif norm_path.startswith("/app/recordings/"):
                    relative_path = norm_path.replace("/app/recordings/", "", 1)
                
                recording = Recording(
                    camera_id=cam.id,
                    filename=os.path.basename(segment_path),
                    file_path=relative_path,
                    file_size=file_size,
                    duration=duration_sec,
                    recording_type="continuous",
                    start_time=start_time,
                    end_time=end_time,
                    is_processed=True,
                    created_by_id=created_by_id,
                )

                db.add(recording)
                db.commit()
                db.refresh(recording)

                # Also keep the legacy event log for now (for debugging/audit)
                record = {
                    "camera_id": cam.id,
                    "recording_id": recording.id,
                    "filename": recording.filename,
                    "file_path": segment_path,
                    "file_size": file_size,
                    "duration": duration_sec,
                    "recording_type": "continuous",
                    "start_time": recording.start_time.isoformat(),
                    "end_time": recording.end_time.isoformat(),
                }
            else:
                # No user found, create basic record for debugging
                record = {
                    "camera_id": cam.id,
                    "recording_id": None,
                    "filename": os.path.basename(segment_path),
                    "file_path": segment_path,
                    "file_size": file_size,
                    "duration": duration_sec,
                    "recording_type": "continuous",
                    "start_time": start_time.isoformat(),
                    "end_time": end_time.isoformat(),
                    "error": "No user found to assign recording to",
                }

        except Exception as e:
            # Log error but don't fail the webhook
            faststart_logger.error(f"Error storing recording: {e}")
            # Create basic record for debugging
            end_time = datetime.now(UTC)
            if duration_sec and duration_sec > 0:
                start_time = end_time - timedelta(seconds=duration_sec)
            else:
                start_time = end_time
            record = {
                "camera_id": cam.id,
                "filename": os.path.basename(segment_path),
                "file_path": segment_path,
                "file_size": None,
                "duration": duration_sec,
                "recording_type": "continuous",
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "error": str(e),
            }
    else:
        # No camera found, store basic info
        record = {
            "camera_id": None,
            "filename": os.path.basename(segment_path),
            "file_path": segment_path,
            "file_size": None,
            "duration": duration_sec,
            "recording_type": "continuous",
            "start_time": datetime.now(UTC).isoformat(),
            "error": "Camera not found",
        }

    # Store in legacy event log for debugging
    try:
        from models import SecuritySetting

        key = "mtx_segment_events"
        row = db.query(SecuritySetting).filter(SecuritySetting.key == key).first()
        if not row:
            row = SecuritySetting(key=key, json_value=json.dumps({"items": []}))
            db.add(row)
            db.commit()
            db.refresh(row)
        payload = json.loads(row.json_value or "{}")
        items = payload.get("items") or []
        items.insert(0, {"type": "segment-complete", "path": path, **record})
        # keep last 100
        items = items[:100]
        row.json_value = json.dumps({"items": items})
        db.commit()
    except Exception:
        pass

    # Mirror to cloud recording server if configured (with BYOK TLS support)
    try:
        from services.cloud_recording_service import CloudRecordingService
        
        if segment_path and os.path.exists(segment_path):
            # Build relative path under Recordings root
            def _extract_rel(full_path: str) -> str:
                if not full_path:
                    return os.path.basename(full_path)
                norm = full_path.replace("\\", "/")
                parts = norm.split("/Recordings/")
                if len(parts) > 1 and parts[1]:
                    return parts[1]
                return os.path.basename(full_path)

            rel = _extract_rel(segment_path)
            camera_id = getattr(cam, "id", None) if cam else None
            
            # Queue upload for async processing (supports S3 and NVR-to-NVR with BYOK)
            cloud_service = CloudRecordingService.get_instance()
            background_tasks.add_task(
                cloud_service.queue_upload,
                segment_path,
                camera_id,
                rel,
            )
            faststart_logger.info(f"Queued cloud upload for: {segment_path}")
    except Exception as e:
        faststart_logger.warning(f"Cloud mirror step error: {e}")

    # Schedule faststart optimization in background
    # This converts fragmented MP4 (fMP4) to fast-start MP4 for instant web playback
    faststart_scheduled = False
    if (
        segment_path
        and segment_path.lower().endswith(".mp4")
        and os.path.exists(segment_path)
    ):
        background_tasks.add_task(apply_faststart_to_segment, segment_path)
        faststart_scheduled = True
        faststart_logger.info(f"Scheduled faststart processing for: {segment_path}")

    return {
        "status": "ok",
        "event": "segment-complete",
        "path": path,
        "segment_path": segment_path,
        "camera_id": getattr(cam, "id", None),
        "faststart_applied": faststart_scheduled,
    }
