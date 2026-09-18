# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Per-camera health numbers for API clients (HA-105).

One place that joins the three sources a camera's health lives in:

* MediaMTX path info: is the stream up, and the ``bytesReceived`` counter,
  which becomes a bitrate by differencing two reads;
* the detect-pipeline's metrics (services/tier0_metrics): processing fps,
  target fps, mean inference time, skipped frames, active tracks;
* the database: recording health (the same derivation the camera list
  uses, so they never disagree) and how many days of footage are kept.

Every source may be absent (pipeline not deployed, MediaMTX admin API off,
camera paused). A missing source yields ``None`` fields, never an error:
Home Assistant shows "unknown" rather than a failed poll.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import Camera, CameraConfig, Recording

# path -> (monotonic time, bytesReceived) of the previous read, for bitrate.
_last_bytes: dict[str, tuple[float, int]] = {}
#: Two reads further apart than this are too stale to average over.
_BITRATE_MAX_GAP_S = 300.0


def _bitrate_kbps(path: str, bytes_received: int | None, now: float) -> float | None:
    """kbit/s since the previous read of this path, or None on the first read.

    Differencing on demand avoids a background sampler: Home Assistant polls
    every 30 s, so from its second poll on there is always a recent reading.
    A counter that went backwards (MediaMTX restarted) restarts the series.
    """
    if bytes_received is None:
        return None
    prev = _last_bytes.get(path)
    _last_bytes[path] = (now, bytes_received)
    if prev is None:
        return None
    dt = now - prev[0]
    delta = bytes_received - prev[1]
    if dt <= 0 or dt > _BITRATE_MAX_GAP_S or delta < 0:
        return None
    return round(delta * 8 / dt / 1000.0, 1)


def _days_retained(db: Session, camera_id: int, now: datetime) -> float | None:
    oldest = (
        db.query(func.min(Recording.start_time))
        .filter(Recording.camera_id == camera_id)
        .scalar()
    )
    if oldest is None:
        return None
    if oldest.tzinfo is None:  # SQLite returns naive UTC
        oldest = oldest.replace(tzinfo=UTC)
    return round(max(0.0, (now - oldest).total_seconds()) / 86400.0, 2)


def _recording_state(db: Session, camera: Camera, now: datetime) -> str:
    from routers.cameras import _derive_recording_state
    from services.camera_status_service import get_camera_status_service

    latest = (
        db.query(func.max(Recording.start_time))
        .filter(Recording.camera_id == camera.id)
        .scalar()
    )
    if latest is not None and latest.tzinfo is None:
        latest = latest.replace(tzinfo=UTC)
    enabled = (
        db.query(CameraConfig.recording_enabled)
        .filter(CameraConfig.camera_id == camera.id)
        .scalar()
    )
    status = get_camera_status_service()
    live = status.snapshot([camera.id]).get(camera.id) if camera.is_active else None
    return _derive_recording_state(
        camera, bool(enabled), latest, now, live,
        status.online_since([camera.id]).get(camera.id),
    )


def _tier0_row(metrics: dict[str, Any], camera_id: int) -> dict[str, Any] | None:
    if not metrics.get("available", True):
        return None
    for row in metrics.get("cameras") or []:
        if row.get("camera") == f"cam{camera_id}":
            return row
    return None


async def get_camera_stats(db: Session, camera: Camera) -> dict[str, Any]:
    """The stats payload for GET /cameras/{id}/stats."""
    from services.mediamtx_admin_service import MediaMtxAdminService
    from services.stream_service import _build_stream_name
    from services.tier0_metrics import get_tier0_metrics
    from core.config import settings

    now_wall = datetime.now(UTC)
    path = _build_stream_name(settings.mediamtx_stream_prefix, camera.id,
                              camera.ip_address)

    stream_ready: bool | None = None
    bitrate: float | None = None
    if camera.is_active:
        info = await MediaMtxAdminService.get_active_path_info(path)
        details = info.get("details") if info.get("status") == "ok" else None
        if isinstance(details, dict):
            stream_ready = bool(details.get("ready"))
            received = details.get("bytesReceived")
            bitrate = _bitrate_kbps(
                path, int(received) if isinstance(received, (int, float)) else None,
                time.monotonic(),
            )

    row = _tier0_row(await get_tier0_metrics(), camera.id)
    return {
        "camera_id": camera.id,
        "is_active": bool(camera.is_active),
        "stream_ready": stream_ready,
        "bitrate_kbps": bitrate,
        "detect_up": row.get("up") if row else None,
        "detect_fps": row.get("fps") if row else None,
        "target_fps": row.get("target_fps") if row else None,
        "inference_ms": row.get("inference_ms") if row else None,
        "skipped_total": row.get("skipped_total") if row else None,
        "tracks_active": row.get("tracks_active") if row else None,
        "frame_age_s": row.get("frame_age_s") if row else None,
        "recording_state": _recording_state(db, camera, now_wall),
        "days_retained": _days_retained(db, camera.id, now_wall),
    }
