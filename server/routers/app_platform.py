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
* ``GET  /cameras/{id}/stream``               — scoped RTSP URL for frames
* ``GET  /recordings/{id}``                   — recorded segments
* ``GET  /recordings/{id}/url``               — playback URL for one segment
* ``GET  /plates/stats|summary|sessions``     — the Vehicles-page aggregates
* ``POST /evidence``                          — store a JPEG, get its path
* ``GET  /alerts``                            — the app's own inbox rows
* ``GET  /site-mode``                         — the site's arming state (read-only)
* ``GET|PUT|DELETE /state[/{key}]``           — durable per-app key/value

Nothing here is reachable with a user JWT: people use the operator API,
apps use this one. Per-camera RBAC for people lives on the operator
routes; per-app roster scoping lives here.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote as urlquote
from urllib.parse import urlencode

from fastapi import (APIRouter, Body, Depends, HTTPException, Query, Request,
                     status)
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.config import settings
from core.database import get_db
from models import AppAlert, AppState, Camera, InstalledApp
from routers.internal_camera_agent import (
    _app_roster, _require_internal_key,
)
from services.app_keys import AppPrincipal

logger = logging.getLogger(__name__)

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


#: How long a stream grant lasts. Long enough that an app is not
#: re-minting every minute, short enough that a leaked URL dies on its
#: own. The SDK renews well before this.
STREAM_TOKEN_MINUTES = 60


@router.get("/cameras/{camera_id}/stream")
async def app_camera_stream(
    camera_id: int,
    principal=Depends(_require_internal_key),
    db: Session = Depends(get_db),
):
    """An RTSP URL this app may read, for continuous frames.

    Snapshots answer "what is there now"; some apps must WATCH — a
    gesture, a sweep, a fall is a shape in time, and at one still every
    few seconds it has already happened. Those apps need the stream.

    The token minted here is scoped to THIS camera's path, not the
    wildcard the platform's own components carry. That is the whole
    point of the route: apps sit on a shared network, so handing one a
    bare ``rtsp://mediamtx:8554/...`` would quietly grant it every
    camera in the building and undo the per-app roster. A grant an app
    cannot widen is worth the extra endpoint.
    """
    cam = _camera_in_roster(db, principal, camera_id)
    if not settings.mediamtx_rtsp_url:
        # No MediaMTX configured: the camera's own URL is all there is,
        # and it is not ours to hand out scoped.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="No MediaMTX stream base configured")

    from services.camera_identity import path_name_for_camera
    from services.stream_service import substream_name

    stream_name = path_name_for_camera(cam)
    # Prefer the substream when the operator stored one: an app watching
    # gestures needs frame RATE, not pixels, and the sub costs a
    # fraction of the CPU to decode.
    use_sub = bool((cam.substream_url or "").strip())
    tap_name = substream_name(stream_name) if use_sub else stream_name

    token = None
    try:
        from services.mediamtx_jwt_service import MediaMtxJwtService

        token = MediaMtxJwtService.create_stream_token(
            user_id=0,
            username=f"app:{getattr(principal, 'app_id', 'platform')}",
            camera_id=None,
            # Exactly this path, and read only. Not "~.*".
            camera_path=tap_name,
            actions=["read"],
            expiry_minutes=STREAM_TOKEN_MINUTES,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("stream grant: could not mint MediaMTX JWT (%s)", exc)

    base = str(settings.mediamtx_rtsp_url).rstrip("/")
    url = f"{base}/{tap_name}"
    if token:
        url = f"{url}?jwt={urlquote(token, safe='.')}"
    return {
        "camera_id": cam.id,
        "path": tap_name,
        "url": url,
        "substream": use_sub,
        "expires_in": STREAM_TOKEN_MINUTES * 60,
        # Told, not guessed: the SDK renews on this rather than waiting
        # for a 401 mid-screening.
        "renew_after": int(STREAM_TOKEN_MINUTES * 60 * 0.8),
    }


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

    # Scoped to THIS path, playback only. The roster check above decides
    # which camera the app may ask about, but the URL it was handed used
    # to carry no credential at all — so an app could take the answer,
    # edit `path=` to a camera it was never assigned, and the playback
    # server, which was excluded from auth entirely, would serve it. The
    # check and the capability now agree.
    token = None
    try:
        from services.mediamtx_jwt_service import MediaMtxJwtService

        token = MediaMtxJwtService.create_stream_token(
            user_id=0,
            username=f"app:{getattr(principal, 'app_id', 'platform')}",
            camera_id=None,
            camera_path=path,
            actions=["playback"],
            expiry_minutes=STREAM_TOKEN_MINUTES,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("playback grant: could not mint MediaMTX JWT (%s)", exc)

    params = {"path": path, "start": start, "duration": str(duration)}
    if token:
        params["jwt"] = token
    return {"camera_id": cam.id, "path": path, "start": start,
            "duration": duration,
            "expires_in": STREAM_TOKEN_MINUTES * 60,
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


@router.get("/plates/inside")
async def app_plates_inside(
    in_cameras: str = Query("", description="Entry gates: comma-separated ids/handles"),
    out_cameras: str = Query("", description="Exit gates: comma-separated ids/handles"),
    hours: int = Query(24, ge=1, le=24 * 7),
    principal=Depends(_require_internal_key),
    db: Session = Depends(get_db),
):
    """Which vehicles are inside right now, and since when.

    The operator's Vehicles page has had this since gate pairing
    landed; an app could not reach it. So license-plate-recognition
    kept its own ledger of who had driven in and not out — in memory,
    losing every open visit on restart, which meant a vehicle that
    entered before a redeploy could never trigger an overstay alert
    however long it stayed. The same question, asked twice, answered
    from two places, one of which forgets.

    Stateless, like ``plates/sessions``: which cameras are entry gates
    and which are exits lives in the calling app's config, because the
    platform has no opinion about a site's traffic direction. The
    window is what makes a missed exit age out instead of leaving a
    vehicle inside forever.
    """
    from services.timeline_service import gate_occupancy

    return gate_occupancy(
        db,
        in_cameras=_roster_ids(db, principal, _parse_ids(in_cameras)),
        out_cameras=_roster_ids(db, principal, _parse_ids(out_cameras)),
        hours=hours,
        scope=_app_roster(db, principal),
    )


# ── Subject binding (RFC-0003) ──────────────────────────────────────


@router.get("/visits/at")
async def app_visit_at(
    camera: str = Query(..., description="Camera id or handle."),
    at: datetime = Query(..., description="The instant the frame was taken."),
    label: str | None = Query(None, description="Narrow to one object class."),
    tolerance_s: float = Query(5.0, ge=0.0, le=60.0),
    principal=Depends(_require_internal_key),
    db: Session = Depends(get_db),
):
    """Which visit was happening on this camera at this instant.

    The whole reason frame-polling apps could not write to the store.
    A ``FrameApp`` has a camera, some bytes and a moment; it has no
    ``event_id``, so whatever it learns from the frame has nowhere to
    go. smart-doorbell kept a parallel visit log for exactly this
    reason, and the reason was good: attaching a name by matching
    timestamps would make a guessed identity indistinguishable,
    afterwards, from a measured one.

    Indistinguishable is a property of the record, so the answer is to
    say which kind of match happened. ``binding`` is ``window`` when a
    visit's own span contained the instant — a lookup made by the
    component that owns the span, not a guess — and ``nearest`` when
    nothing contained it and the closest within ``tolerance_s`` was
    used, which IS a guess and is named one.

    Two visits covering the instant returns neither. A doorbell frame
    taken while two people are at the door does not identify whose face
    it is, and choosing would be inventing a fact.
    """
    from services.timeline_service import resolve_visit

    cam_ids = _roster_ids(db, principal, _parse_ids(camera))
    if not cam_ids:
        # Not an error: an app asking about a camera it does not hold
        # gets the same "nothing to bind to" it would get for a quiet
        # camera, rather than a 403 that tells it the camera exists.
        return {"event_id": None, "binding": None, "reason": "no visit"}
    return resolve_visit(
        db, camera_id=cam_ids[0], at=at, label=label,
        tolerance_s=tolerance_s, scope=_app_roster(db, principal))


class AppClaimIn(BaseModel):
    kind: str = Field(..., max_length=40)
    value: str = Field(..., max_length=120)
    confidence: float | None = None
    source_task: str | None = Field(None, max_length=40)
    source_adapter: str | None = Field(None, max_length=60)
    model_fingerprint: str | None = Field(None, max_length=120)
    correlation_id: str | None = Field(None, max_length=64)


class AppClaimsIn(BaseModel):
    event_id: int
    binding: str = Field("direct", max_length=16)
    claims: list[AppClaimIn] = []
    ran_tasks: list[str] = []


@router.post("/visits/claims")
async def app_write_claims(
    payload: AppClaimsIn,
    principal=Depends(_require_internal_key),
    db: Session = Depends(get_db),
):
    """Attach what this app worked out to a visit.

    Roster-scoped, unlike the camera-agent's descriptor endpoint this
    is modelled on: an app may only write to a camera it was given.
    Without that an app key could attach a claim — a NAME — to any
    visit on the site, which is a worse hole than any read.

    ``binding`` must be one the caller can justify. An app that
    resolved the subject through ``/visits/at`` passes back what that
    call returned; an app that already held the ``event_id`` says
    ``direct``. It is recorded per claim, so a reader can exclude
    anything bound by a timestamp in one filter.
    """
    from models import TimelineEvent
    from services.descriptor_store import BINDINGS, apply_descriptors

    binding = (payload.binding or "direct").strip().lower()
    if binding not in BINDINGS:
        raise HTTPException(
            status_code=422,
            detail=f"binding must be one of {sorted(BINDINGS)}")

    row = db.get(TimelineEvent, int(payload.event_id))
    if row is None:
        raise HTTPException(status_code=404, detail="unknown event")

    roster = _app_roster(db, principal)
    if roster is not None and row.camera_id not in roster:
        # 404, not 403: whether a visit exists on a camera this app was
        # not given is not this app's business either.
        raise HTTPException(status_code=404, detail="unknown event")

    written = apply_descriptors(db, row, payload.claims, payload.ran_tasks,
                                binding=binding)
    db.commit()
    return {"ok": True, "written": written, "binding": binding}


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


# ── Site mode: is the site armed? ───────────────────────────────────


@router.get("/site-mode")
async def app_site_mode(
    principal=Depends(_require_internal_key),
    db: Session = Depends(get_db),
):
    """The site's arming state (HA-118), read-only, same body as the
    operator's ``GET /site-mode``.

    Deployment-wide rather than roster-scoped: "is anyone home" is a
    property of the site, not of a camera. An app reads it so a doorbell
    can stay quiet while the family is in, or a package watcher only
    escalates when nobody is — instead of each app growing its own
    schedule knob that drifts from the alarm panel. Arming stays an
    operator verb (``PUT /site-mode``, ``settings.manage``); no app
    credential can change it.
    """
    from services import site_mode

    return {**site_mode.get(db), "modes": list(site_mode.MODES)}


# ── Durable per-app state ───────────────────────────────────────────


def _state_out(row: AppState) -> dict[str, Any]:
    return {"key": row.key, "value": row.value,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


@router.post("/evidence")
async def app_evidence_upload(
    request: Request,
    principal=Depends(_require_internal_key),
    db: Session = Depends(get_db),
):
    """Store one JPEG and return its path, for an app to cite in an alert.

    An app that wants a photo on its alert cannot put the photo IN the
    alert: alerts travel over NATS, whose default payload ceiling is
    1 MB, and a couple of base64 crops exceed it — the broker drops the
    publish and the alert is simply never seen. So the picture comes
    here first and only ``{"path": ...}`` rides along.

    Content-addressed by the store, so re-uploading the same bytes is
    free and returns the same path.
    """
    from services.evidence_store import MAX_EVIDENCE_BYTES, save_evidence_jpeg

    # Refuse on the declared length before reading, so an app cannot
    # make core hold an arbitrary body in memory.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_EVIDENCE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"evidence must be at most {MAX_EVIDENCE_BYTES} bytes")
    body = await request.body()
    try:
        rel = save_evidence_jpeg(body)
    except ValueError as exc:
        # Not a JPEG, empty, or over the cap: the app's bug, not ours.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=str(exc)) from exc
    return {"path": rel, "bytes": len(body)}


#: The shape ``save_evidence_jpeg`` produces: ``<first two hex>/<sha256>.jpg``.
#: This pattern is the whole security boundary of the read below — see it.
_CONTENT_ADDRESSED = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{64}\.jpg$")


@router.get("/evidence/{rel_path:path}")
async def app_evidence_read(
    rel_path: str,
    principal=Depends(_require_internal_key),
):
    """Read back a JPEG an app stored here. 404 for anything else.

    Apps could write a photo and never read one, so a picture an app
    saved was write-only to it: the doorbell that uploads a face crop
    cannot show that crop again after a restart, and an alert relay
    cannot attach the photo its alert cites.

    Why this is not a read primitive over the whole evidence store
    ---------------------------------------------------------------
    The store is CONTENT-ADDRESSED: ``save_evidence_jpeg`` names a file
    ``<sha256(bytes)>.jpg``. The path is therefore a capability — you
    can only name a file whose exact bytes you already had, or whose
    hash somebody handed you. Guessing one is guessing a SHA-256.

    That property holds ONLY for the content-addressed names, and the
    same root also holds Tier-0's visit evidence under structured,
    guessable paths (``cam1/2026/09/22/…``). So this route serves the
    content-addressed shape and nothing else. Without that check an app
    could walk the site's camera evidence by construction, which is a
    different feature entirely and not one anybody asked for.

    Traversal is refused twice over: the pattern admits no ``.`` or
    ``/`` beyond the one separator, and ``resolve_evidence`` re-checks
    containment under the root anyway. A path that is well-formed but
    absent is a 404 like any other, because retention sweeps these and
    an app must be able to tell "aged out" from "never existed" only in
    the sense that neither is available — no other information is owed.
    """
    from services.evidence_store import resolve_evidence

    if not _CONTENT_ADDRESSED.match(rel_path or ""):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="unknown evidence path",
        )
    path = resolve_evidence(rel_path)
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="unknown evidence path")
    return FileResponse(path, media_type="image/jpeg")


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


# ── Search ───────────────────────────────────────────────────────────
#
# The canonical store's search, for apps. Until this existed there was
# no way for an app to ask the platform "which visits match these
# words?" — the operator route (GET /api/v1/search) authenticates a
# USER and scopes by what that user can see, which an app holding an
# internal key can neither satisfy nor should.
#
# That absence is why footage-search shipped its own SQLite index, and
# why the camera-agent's attempt to stop using that index quietly did
# nothing: it called a method the SDK never had, against a route that
# never existed, and fell back to the private index on every query
# because AttributeError and "core unreachable" reach the same
# except-branch.
#
# It deliberately reuses search_events / count_search_events /
# summarise_hits rather than growing a second query path. One store,
# one predicate, one ranking: an app and the operator asking the same
# question have to get the same answer, or the app is a second source of
# truth again by a different route.


@router.get("/search")
async def app_search(
    text: str = Query("", description="Words to match in captions and attributes."),
    label: list[str] | None = Query(None, description="Object class; repeatable (OR)."),
    camera_id: list[int] | None = Query(None, description="Camera; repeatable (OR)."),
    plate: str | None = Query(None),
    attr: list[str] | None = Query(
        None, description="kind:value claim filter; repeatable (AND)."),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    limit: int = Query(25, ge=1, le=200),
    skip: int = Query(0, ge=0),
    principal=Depends(_require_internal_key),
    db: Session = Depends(get_db),
):
    """Visits matching these words, scoped to the app's own cameras.

    No sentence parsing here on purpose. The operator route parses
    English because a person typed it; an app has already decided what
    it is looking for, and two parsers disagreeing about one query is
    the failure mode that made the agent pass ``parse=False`` to a
    method that did not exist.
    """
    from services.camera_scope import scope_query
    from services.search_service import (count_search_events, search_events,
                                         summarise_hits)

    roster = _app_roster(db, principal)
    # A shortcut, not a guard: it saves an app with no cameras two round
    # trips to be told there is nothing. The scoping itself belongs to
    # `scope_query`, which turns an empty scope into a predicate that
    # matches nothing. The comment that used to sit here said this line
    # was what stopped a site-wide leak; deleting it failed no test, and
    # deleting `scope_query`'s branch too failed no test either, because
    # an empty `IN` is already false. Behaviour pinned in
    # test_scope_query_empty.py, with the reasoning.
    if roster is not None and not roster:
        return {"results": [], "count": 0, "total": 0, "answer": {}}

    cams = [c for c in (camera_id or []) if c]
    labels = [s for s in (label or []) if s]

    attrs: list[tuple[str, str]] = []
    for raw in attr or []:
        kind, sep, value = str(raw).partition(":")
        if sep and kind.strip() and value.strip():
            attrs.append((kind.strip().lower(), value.strip().lower()))

    filters = dict(from_=from_, to=to, plate=plate or None, scope=roster)
    hits = search_events(
        db, labels=labels, camera_ids=cams, text=text or "", attrs=attrs,
        limit=limit, skip=skip, **filters)
    total = count_search_events(
        db, labels=labels, camera_ids=cams, text=text or "", attrs=attrs,
        **filters)

    # Names for the rows being returned, not for the site: a scoped
    # route has no business reading the whole camera table, and a
    # dictionary built from it is one careless `.values()` away from
    # being the leak the scoping exists to prevent.
    names = {
        c.id: c.name
        for c in scope_query(db.query(Camera), Camera.id, roster).all()
    }
    return {
        "results": [
            {
                "id": h.event.id,
                "camera_id": h.event.camera_id,
                "camera_name": names.get(h.event.camera_id),
                "label": h.event.label,
                "score": round(h.score, 4),
                "started_at": (h.event.started_at.isoformat()
                               if h.event.started_at else None),
                "ended_at": (h.event.ended_at.isoformat()
                             if h.event.ended_at else None),
                "plate_text": h.event.plate_text,
                "caption": h.caption,
                "attributes": h.attributes,
                "claims": h.claims,
                # The app reads its own evidence through /evidence/{path};
                # this says whether there IS a photo, so a caller can tell
                # "none kept" from "not fetched yet".
                "has_evidence": bool(h.event.evidence_path),
            }
            for h in hits
        ],
        "count": len(hits),
        "total": total,
        "answer": summarise_hits(hits, total=total, camera_names=names),
    }
