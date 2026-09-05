# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The app platform door — what ``opennvr_app_sdk.OpenNVR`` talks to.

``/internal/camera-agent/*`` grew as the OpenNVR Agent's private door and
the SDK reused it for the roster and the events store. This router is
the rest of what a vision app needs from core, on the same credential
model (``services/app_keys``): an app presents its own key and every
route below answers for that app's camera roster and that app's rows;
the deployment's site key answers unscoped (and may name an app with
``?app_id=`` where a route is per app).

Routes (prefix ``/api/v1/internal/app``):

* ``GET  /cameras/{id}/snapshot``             — current JPEG
* ``GET  /recordings/{id}``                   — recorded segments
* ``GET  /recordings/{id}/url``               — playback URL for one segment
* ``GET  /plates/stats|summary|sessions``     — the Vehicles-page aggregates
* ``GET  /alerts``                            — the app's own inbox rows
* ``GET|PUT|DELETE /state[/{key}]``           — durable per-app key/value

Nothing here is reachable with a user JWT: people use the operator API,
apps use this one. Per-camera RBAC for people lives on the operator
routes; per-app roster scoping lives here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from core.config import settings
from core.database import get_db
from models import AppAlert, AppState, Camera, InstalledApp
from routers.internal_camera_agent import (
    _app_roster, _require_internal_key,
)
from services.app_keys import AppPrincipal

router = APIRouter(prefix="/internal/app", tags=["app-platform"])

STATE_KEY_MAX = 200
STATE_VALUE_MAX_BYTES = 256 * 1024
STATE_KEYS_MAX = 2000


def _camera_in_roster(db: Session, principal, camera_id: int) -> Camera:
    """The camera, if it exists and the caller may read it; else 404
    (never 403 — a camera outside the roster is not confirmed)."""
    roster = _app_roster(db, principal)
    cam = db.query(Camera).filter(Camera.id == int(camera_id),
                                  Camera.deleted_at.is_(None)).first()
    if cam is None or (roster is not None and int(cam.id) not in roster):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Camera not found")
    return cam


def _app_id_for(db: Session, principal, app_id: str | None) -> str:
    """Which app a per-app route is about: the key's own app, or the
    ``app_id`` the site key names."""
    if isinstance(principal, AppPrincipal):
        return principal.app_id
    if not app_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="app_id is required with the site key")
    if db.query(InstalledApp.id).filter(InstalledApp.id == app_id).first() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="App not found")
    return app_id


# ── Cameras ─────────────────────────────────────────────────────────


@router.get("/cameras/{camera_id}/snapshot")
async def app_camera_snapshot(
    camera_id: int,
    principal=Depends(_require_internal_key),
    db: Session = Depends(get_db),
):
    """The camera's current frame as JPEG (via the KAI-C capture pool —
    the same path the zone editor's snapshot uses). 503 when no frame
    can be captured."""
    cam = _camera_in_roster(db, principal, camera_id)
    if not cam.rtsp_url:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Camera has no stream URL configured")
    from services.kai_c_service import get_kai_c_service

    jpeg = await get_kai_c_service().capture_frame_bytes(cam.rtsp_url, cam.id)
    if not jpeg:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Could not capture a frame (camera offline?)")
    return Response(content=jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


# ── Recordings ──────────────────────────────────────────────────────


def _playback_path(cam: Camera) -> str:
    from services.camera_identity import path_name_for_camera

    return path_name_for_camera(cam)


@router.get("/recordings/{camera_id}")
async def app_recordings_list(
    camera_id: int,
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    principal=Depends(_require_internal_key),
    db: Session = Depends(get_db),
):
    """Recorded segments for one camera (MediaMTX playback index)."""
    cam = _camera_in_roster(db, principal, camera_id)
    from services import mediamtx_client

    path = _playback_path(cam)
    segments = await mediamtx_client.list_segments(path, start=start, end=end,
                                                   timeout=10.0)
    if segments is None:
        return {"camera_id": cam.id, "path": path, "recordings": [],
                "error": "MediaMTX list unavailable"}
    return {"camera_id": cam.id, "path": path, "recordings": segments,
            "count": len(segments)}


@router.get("/recordings/{camera_id}/url")
async def app_recordings_url(
    camera_id: int,
    start: str = Query(..., description="Segment start, RFC3339"),
    duration: float = Query(..., gt=0, le=24 * 3600),
    principal=Depends(_require_internal_key),
    db: Session = Depends(get_db),
):
    """A direct MediaMTX playback URL for one segment — the internal
    base (apps run on the compose network), unlike the operator route,
    which hands the browser the external one."""
    cam = _camera_in_roster(db, principal, camera_id)
    path = _playback_path(cam)
    base = settings.mediamtx_playback_url or "http://127.0.0.1:9996"
    params = {"path": path, "start": start, "duration": str(duration)}
    return {"camera_id": cam.id, "path": path, "start": start,
            "duration": duration,
            "url": f"{base.rstrip('/')}/get?{urlencode(params)}"}


# ── Plates (the Vehicles-page aggregates, roster-scoped) ────────────


def _roster_ids(db: Session, principal, requested: list[int]) -> list[int]:
    roster = _app_roster(db, principal)
    ids = [int(c) for c in requested]
    return ids if roster is None else [c for c in ids if c in roster]


def _parse_ids(text: str | None) -> list[int]:
    out: list[int] = []
    for part in (text or "").split(","):
        part = part.strip()
        if part.lower().startswith("cam"):
            part = part[3:].lstrip("-")
        if part.isdigit():
            out.append(int(part))
    return out


@router.get("/plates/stats")
async def app_plate_stats(
    days: int = Query(7, ge=1, le=90),
    principal=Depends(_require_internal_key),
    db: Session = Depends(get_db),
):
    from services.timeline_service import plate_stats

    return plate_stats(db, days=days, scope=_app_roster(db, principal))


@router.get("/plates/summary")
async def app_plate_summary(
    plate: str = Query(..., min_length=2, max_length=32),
    principal=Depends(_require_internal_key),
    db: Session = Depends(get_db),
):
    from services.timeline_service import plate_summary

    return plate_summary(db, plate=plate, scope=_app_roster(db, principal))


@router.get("/plates/sessions")
async def app_plate_sessions(
    plate: str = Query(..., min_length=2, max_length=32),
    in_cameras: str = Query("", description="Entry gates: comma-separated ids/handles"),
    out_cameras: str = Query("", description="Exit gates: comma-separated ids/handles"),
    limit: int = Query(50, ge=1, le=200),
    principal=Depends(_require_internal_key),
    db: Session = Depends(get_db),
):
    from services.timeline_service import plate_sessions

    return plate_sessions(
        db, plate=plate,
        in_cameras=_roster_ids(db, principal, _parse_ids(in_cameras)),
        out_cameras=_roster_ids(db, principal, _parse_ids(out_cameras)),
        scope=_app_roster(db, principal), limit=limit)


# ── Alerts: what this app raised ────────────────────────────────────


@router.get("/alerts")
async def app_alerts(
    unacked: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    after_id: int | None = Query(None),
    app_id: str | None = Query(None, description="Site key only: which app"),
    principal=Depends(_require_internal_key),
    db: Session = Depends(get_db),
):
    """The operator inbox rows THIS app raised (``source.name`` == the
    app id), newest first, with their acknowledgement state — so an app
    can tell whether anyone has acted on what it said."""
    q = db.query(AppAlert)
    if isinstance(principal, AppPrincipal):
        q = q.filter(AppAlert.source_name == principal.app_id)
    elif app_id:
        q = q.filter(AppAlert.source_name == app_id)
    if unacked:
        q = q.filter(AppAlert.acknowledged_at.is_(None))
    if after_id is not None:
        q = q.filter(AppAlert.id > after_id)
    rows = q.order_by(AppAlert.id.desc()).limit(limit).all()
    from routers.alerts_inbox import _row_out

    return {"alerts": [_row_out(a) for a in rows]}


# ── Durable per-app state ───────────────────────────────────────────


def _state_out(row: AppState) -> dict[str, Any]:
    return {"key": row.key, "value": row.value,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


@router.get("/state")
async def app_state_list(
    prefix: str = Query("", max_length=STATE_KEY_MAX),
    app_id: str | None = Query(None),
    principal=Depends(_require_internal_key),
    db: Session = Depends(get_db),
):
    owner = _app_id_for(db, principal, app_id)
    q = db.query(AppState).filter(AppState.app_id == owner)
    if prefix:
        q = q.filter(AppState.key.like(prefix.replace("%", r"\%") + "%"))
    rows = q.order_by(AppState.key.asc()).limit(STATE_KEYS_MAX).all()
    return {"app_id": owner, "items": [_state_out(r) for r in rows]}


@router.get("/state/{key}")
async def app_state_get(
    key: str,
    app_id: str | None = Query(None),
    principal=Depends(_require_internal_key),
    db: Session = Depends(get_db),
):
    owner = _app_id_for(db, principal, app_id)
    row = db.query(AppState).filter(AppState.app_id == owner,
                                    AppState.key == key).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No such key")
    return _state_out(row)


@router.put("/state/{key}")
async def app_state_put(
    key: str,
    value: Any = Body(...),
    app_id: str | None = Query(None),
    principal=Depends(_require_internal_key),
    db: Session = Depends(get_db),
):
    """Set one key to any JSON value (last write wins). Keys ≤ 200
    chars, values ≤ 256 KB, ≤ 2000 keys per app — state, not storage."""
    import json

    owner = _app_id_for(db, principal, app_id)
    if not key or len(key) > STATE_KEY_MAX or "/" in key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"key must be 1..{STATE_KEY_MAX} chars without '/'")
    if len(json.dumps(value)) > STATE_VALUE_MAX_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail=f"value over {STATE_VALUE_MAX_BYTES} bytes")
    row = db.query(AppState).filter(AppState.app_id == owner,
                                    AppState.key == key).first()
    if row is None:
        count = db.query(AppState).filter(AppState.app_id == owner).count()
        if count >= STATE_KEYS_MAX:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail=f"app state holds {STATE_KEYS_MAX} keys already")
        row = AppState(app_id=owner, key=key, value=value)
        db.add(row)
    else:
        row.value = value
        row.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(row)
    return _state_out(row)


@router.delete("/state/{key}")
async def app_state_delete(
    key: str,
    app_id: str | None = Query(None),
    principal=Depends(_require_internal_key),
    db: Session = Depends(get_db),
):
    owner = _app_id_for(db, principal, app_id)
    deleted = (db.query(AppState)
               .filter(AppState.app_id == owner, AppState.key == key)
               .delete())
    db.commit()
    return {"deleted": bool(deleted)}
