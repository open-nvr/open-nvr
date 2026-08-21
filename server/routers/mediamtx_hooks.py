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
- Segment completion (recordings-index upsert with filename-derived
  timestamps + codec probe, cloud mirror)

All endpoints are protected with X-MTX-Secret header verification.
"""

import logging
import os
import pathlib
from datetime import UTC, datetime, timedelta
from typing import Any

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


def compute_segment_end_time(
    start_time: datetime,
    duration_sec: float | None,
    mtime: float | None,
) -> datetime:
    """Wall-clock-anchored end of a completed segment.

    ``start_time + duration`` alone is a MEDIA-clock end. Cameras whose RTP
    timestamps run slower than real time (counter-stamped frames: delivered
    fps below the declared fps) produce segments whose media duration
    under-counts the wall time they actually cover — a "60s" segment can
    span 72 real seconds, and media-clock ends left phantom 10-15s gaps
    before each next segment on an otherwise continuous recording.

    The file's mtime is the wall-clock instant the last byte landed (the
    webhook fires right after MediaMTX closes the file), so it bounds the
    segment's real coverage: take the later of the two ends. The mtime is
    only trusted up to 2x the media duration so a bogus one (copied file,
    host clock jump) can't smear a segment across hours. ``duration``
    stays the true playable length — only timeline placement changes.
    """
    media_end = (
        start_time + timedelta(seconds=duration_sec)
        if duration_sec and duration_sec > 0
        else start_time
    )
    if mtime is None:
        return media_end
    wall_end = datetime.fromtimestamp(mtime, tz=UTC)
    if duration_sec and duration_sec > 0:
        wall_end = min(wall_end, start_time + timedelta(seconds=2 * duration_sec))
    return max(media_end, wall_end)


# NOTE: the old apply_faststart_to_segment ffmpeg pass is GONE, deliberately.
# fMP4 already carries ftyp+moov at the file head — "faststart" is a
# progressive-MP4 concept that bought nothing here. Worse, the remux emitted a
# NON-fragmented MP4, which silently broke the byte-range fast playback path
# (no moof boxes → _scan_fmp4 returns None → every processed file degraded to
# proxying through MediaMTX), and burned a full-file ffmpeg pass per segment —
# untenable at one segment per minute per camera. Historical already-remuxed
# files still play via the MediaMTX proxy fallback.


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


def _camera_from_path(db: Session, path: str) -> Camera | None:
    """Map a MediaMTX path name back to a Camera row.

    Path may be like "cam-<id>" (id mode) or "cam-<ip with _ for .>" (ip
    mode); id form first, then one indexed ip lookup.
    """
    if not path.lower().startswith("cam-"):
        return None
    tag = path.split("-", 1)[1]
    # Binned (deleted) cameras are excluded defensively: their path is torn
    # down at delete time, and any straggler segment must not re-index under
    # a camera that officially no longer exists. Inactive (paused) cameras
    # stay resolvable — footage that arrives is never dropped.
    try:
        return (
            db.query(Camera)
            .filter(Camera.id == int(tag), Camera.deleted_at.is_(None))
            .first()
        )
    except ValueError:
        return (
            db.query(Camera)
            .filter(
                Camera.ip_address == tag.replace("_", "."),
                Camera.deleted_at.is_(None),
            )
            .first()
        )


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


async def _handle_path_status_hook(
    request: Request, path: str, db: Session, online: bool
) -> dict[str, Any]:
    if not _verify_hook_token(request):
        raise HTTPException(status_code=401, detail="Invalid token")

    from services.stream_service import SUBSTREAM_SUFFIX

    # Substreams mirror the main path's source; __startup__ is the provisioning
    # sentinel path. Neither represents a camera of its own.
    if path == "__startup__" or path.endswith(SUBSTREAM_SUFFIX):
        return {"status": "ignored", "path": path}

    cam = _camera_from_path(db, path)
    if cam is None:
        return {"status": "unknown_path", "path": path}

    from services.camera_status_service import get_camera_status_service

    await get_camera_status_service().handle_signal(
        camera_id=cam.id,
        camera_name=cam.name or f"Camera {cam.id}",
        path=path,
        online=online,
        reason="hook",
    )
    return {"status": "ok", "path": path}


@router.get("/hooks/path-ready")
async def hook_path_ready(
    request: Request,
    path: str = Query(..., description="MediaMTX path name (MTX_PATH)"),
    db: Session = Depends(get_db),
):
    return await _handle_path_status_hook(request, path, db, online=True)


@router.get("/hooks/path-not-ready")
async def hook_path_not_ready(
    request: Request,
    path: str = Query(..., description="MediaMTX path name (MTX_PATH)"),
    db: Session = Depends(get_db),
):
    return await _handle_path_status_hook(request, path, db, online=False)


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

    from models import Recording

    # Best-effort; if not found, we still accept the webhook
    cam = _camera_from_path(db, path)

    # Identity gate (TTL-cached): after a DB wipe the id sequence restarts,
    # so the camera this path resolves to may not own the directory the
    # segment landed in. A conflicted segment must be neither indexed NOR
    # cloud-mirrored — both would attribute another camera's footage to this
    # one. Fail closed: on any error, skip indexing (the reconciler is the
    # authoritative pass and will converge).
    identity_ok = True
    if cam:
        try:
            from services.camera_identity import verify_segment_identity
            from services.storage_service import get_effective_recordings_base_path

            root = pathlib.Path(get_effective_recordings_base_path(db))
            identity_ok = verify_segment_identity(cam, path, root)
        except Exception as e:
            faststart_logger.error(f"Identity check failed for path {path}: {e}")
            identity_ok = False

    # Parse duration
    duration_sec: float | None = None
    if segment_duration:
        # MediaMTX passes a number as string; if contains 's', strip
        s = str(segment_duration).strip().lower().rstrip("s")
        try:
            duration_sec = float(s)
        except Exception:
            duration_sec = None

    recording_id: int | None = None
    if cam and identity_ok:
        try:
            file_size = None
            mtime = None
            try:
                st = os.stat(segment_path)
                file_size = st.st_size
                mtime = st.st_mtime
            except OSError:
                pass

            # start_time comes from the FILENAME (the instant MediaMTX opened
            # the file), parsed by the shared layout/timezone-aware helper —
            # never from webhook receipt time, which is skewed by delivery
            # latency. Every read path parses filenames the same way, so DB
            # and filesystem always agree.
            from services.recording_paths import parse_recording_time

            start_time = parse_recording_time(segment_path, mtime=mtime)
            if start_time is None:
                # Unrecognized name: fall back to receipt-time minus duration.
                start_time = datetime.now(UTC) - timedelta(seconds=duration_sec or 0)
            end_time = compute_segment_end_time(start_time, duration_sec, mtime)

            # Codec, read once here (cheap: only the moov box, which fMP4
            # keeps at the file head) so playback never has to probe.
            codec = None
            try:
                from services.hevc_remux_service import probe_video_codec

                codec = probe_video_codec(segment_path)
            except Exception:
                pass

            # Convert absolute path to relative path
            # MediaMTX sends: /app/recordings/cam-1/2026-02-26/07/03-24-036694.mp4
            # We store:       cam-1/2026-02-26/07/03-24-036694.mp4
            relative_path = segment_path
            norm_path = segment_path.replace("\\", "/")
            idx = norm_path.lower().find("/recordings/")
            if idx >= 0:
                relative_path = norm_path[idx + len("/recordings/"):]

            # A wall-clock end can overshoot the next segment's open instant
            # by the part-flush lag (a second or two of mtime trailing the
            # handoff). Clamp the PREVIOUS segment's end_time back to this
            # segment's start so the timeline never overlaps; commits with
            # the upsert below.
            prev = (
                db.query(Recording)
                .filter(
                    Recording.camera_id == cam.id,
                    Recording.start_time < start_time,
                )
                .order_by(Recording.start_time.desc())
                .first()
            )
            if prev is not None and prev.end_time is not None:
                prev_end = prev.end_time
                if prev_end.tzinfo is None:
                    prev_end = prev_end.replace(tzinfo=UTC)
                if prev_end > start_time:
                    prev.end_time = start_time

            # Upsert on (camera_id, file_path): webhook retries and reconciler
            # overlap must never duplicate. Portable get-then-write; the unique
            # index backstops the rare true race.
            existing = (
                db.query(Recording)
                .filter(
                    Recording.camera_id == cam.id,
                    Recording.file_path == relative_path,
                )
                .first()
            )
            if existing:
                existing.file_size = file_size
                existing.duration = duration_sec
                existing.end_time = end_time
                if codec:
                    existing.codec = codec
                existing.source = "webhook"
                db.commit()
                recording_id = existing.id
            else:
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
                    codec=codec,
                    source="webhook",
                    created_by_id=None,
                )
                db.add(recording)
                try:
                    db.commit()
                    recording_id = recording.id
                except Exception:
                    # Unique-index race with a concurrent insert: the row
                    # exists now, which is all we needed.
                    db.rollback()
        except Exception as e:
            # Log error but don't fail the webhook
            faststart_logger.error(f"Error storing recording: {e}")
            db.rollback()

    # Mirror to cloud recording server if configured (with BYOK TLS support)
    try:
        from services.cloud_recording_service import CloudRecordingService

        if segment_path and os.path.exists(segment_path) and identity_ok:
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

    return {
        "status": "ok",
        "event": "segment-complete",
        "path": path,
        "segment_path": segment_path,
        "camera_id": getattr(cam, "id", None),
        "recording_id": recording_id,
    }
