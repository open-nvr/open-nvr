# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Internal camera-agent integration endpoints.

These endpoints are for trusted in-stack services, not browsers. They let the
camera-agent reuse cameras already configured in OpenNVR without knowing camera
passwords or requiring an operator login token.
"""

from __future__ import annotations

import binascii
import logging
import secrets
from datetime import datetime
from urllib.parse import quote as urlquote

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.config import settings
from core.database import get_db
from models import Camera, SecuritySetting
from services.stream_service import _build_stream_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/camera-agent", tags=["internal-camera-agent"])


def _require_internal_key(
    x_internal_api_key: str | None = Header(default=None, alias="X-Internal-Api-Key"),
    x_internal_api_key_alt: str | None = Header(default=None, alias="X-Internal-API-Key"),
) -> None:
    supplied = x_internal_api_key or x_internal_api_key_alt
    expected = settings.internal_api_key
    # Constant-time compare to avoid leaking the key via response timing.
    if not expected or not supplied or not secrets.compare_digest(str(supplied), str(expected)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid internal api key",
        )


class TrackEventIn(BaseModel):
    """One finished object visit, posted by the detect-pipeline at track end."""

    camera_id: int
    label: str
    score: float | None = None
    track_id: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    stationary: bool | None = None
    # Best-frame crop (JPEG, base64). Optional: a visit with no retained crop
    # is still history worth keeping.
    evidence_jpeg_b64: str | None = None


@router.post("/events", status_code=201)
async def ingest_track_event(
    payload: TrackEventIn,
    background: BackgroundTasks,
    _: None = Depends(_require_internal_key),
    db: Session = Depends(get_db),
):
    """Canonical-store ingest (RFC-0001 C1): persist a visit + its evidence.

    Called by the detect-pipeline when a track ends — the moment its best
    frame is final. Idempotent enough for retries: identical evidence
    content-addresses to the same file; duplicate rows are tolerated and
    cheap to de-dup at query time via (camera_id, track_id, started_at).
    """
    camera = db.query(Camera).filter(Camera.id == payload.camera_id).first()
    if camera is None:
        raise HTTPException(status_code=404, detail="unknown camera_id")

    evidence_rel = None
    if payload.evidence_jpeg_b64:
        import base64

        from services.evidence_store import MAX_EVIDENCE_BYTES, save_evidence_jpeg

        # Reject oversized payloads BEFORE decoding: base64 is 4/3 the raw
        # size, so anything beyond that bound can't be a valid crop and
        # shouldn't cost us the decode allocation.
        if len(payload.evidence_jpeg_b64) > (MAX_EVIDENCE_BYTES * 4) // 3 + 8:
            raise HTTPException(status_code=422, detail="evidence too large")
        try:
            evidence_rel = save_evidence_jpeg(
                base64.b64decode(payload.evidence_jpeg_b64, validate=True)
            )
        except (ValueError, binascii.Error) as e:
            raise HTTPException(status_code=422, detail=f"bad evidence: {e}")

    from sqlalchemy.exc import IntegrityError

    from services.timeline_service import record_track_visit

    try:
        row = record_track_visit(
            db,
            camera_id=payload.camera_id,
            label=payload.label,
            started_at=payload.started_at,
            ended_at=payload.ended_at,
            score=payload.score,
            track_id=payload.track_id,
            stationary=payload.stationary,
            evidence_path=evidence_rel,
        )
    except IntegrityError:
        # Retry raced an earlier success — the visit already exists
        # (uq_events_visit). Idempotent: report the existing row.
        db.rollback()
        from models import TimelineEvent

        existing = (
            db.query(TimelineEvent)
            .filter(
                TimelineEvent.camera_id == payload.camera_id,
                TimelineEvent.track_id == (payload.track_id or "")[:40],
                TimelineEvent.started_at == payload.started_at,
            )
            .first()
        )
        return {"id": existing.id if existing else None, "duplicate": True}
    # PR-C: vehicle visit with evidence -> queue ONE OCR pass over the best
    # frame (background — never on the ingest path). Best-effort: no adapter,
    # no plate, no problem.
    from services.plate_enrichment import enrich_event_plate, wants_plate

    if wants_plate(row.label, evidence_rel, settings.events_plate_enrichment):
        background.add_task(enrich_event_plate, row.id)
    return {"id": row.id, "evidence_path": evidence_rel}


@router.get("/events")
async def internal_list_events(
    camera_id: int | None = None,
    label: str | None = None,
    plate: str | None = None,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    limit: int = 100,
    _: None = Depends(_require_internal_key),
    db: Session = Depends(get_db),
):
    """Event-store read for trusted internal components (the camera agents).

    Same OVERLAP query the user API serves, fleet-wide: the agent is a
    platform component like tier0, not a per-user browser session — its
    answers are already scoped by which cameras it is configured to see.
    """
    from services.timeline_service import query_events

    rows = query_events(db, camera_id=camera_id, label=label, plate=plate,
                        from_=from_, to=to, limit=limit)
    return {
        "events": [
            {
                "id": e.id,
                "camera_id": e.camera_id,
                "label": e.label,
                "score": e.score,
                "started_at": e.started_at.isoformat() if e.started_at else None,
                "ended_at": e.ended_at.isoformat() if e.ended_at else None,
                "stationary": (e.payload or {}).get("stationary"),
                "plate_text": e.plate_text,
                "has_evidence": bool(e.evidence_path),
            }
            for e in rows
        ]
    }


@router.get("/events/{event_id}/evidence")
async def internal_event_evidence(
    event_id: int,
    _: None = Depends(_require_internal_key),
    db: Session = Depends(get_db),
):
    """The visit's best-frame JPEG, for agent-side face match / VLM looks."""
    from fastapi.responses import FileResponse

    from models import TimelineEvent
    from services.evidence_store import resolve_evidence

    e = db.query(TimelineEvent).filter(TimelineEvent.id == event_id).first()
    if e is None or not e.evidence_path:
        raise HTTPException(status_code=404, detail="no evidence")
    path = resolve_evidence(e.evidence_path)
    if path is None:
        raise HTTPException(status_code=404, detail="evidence missing")
    return FileResponse(path, media_type="image/jpeg")


GATE_MODE_KEY = "detect_gate_mode"
SHADOW_SINCE_KEY = "detect_shadow_since"
_VALID_GATE_MODES = ("off", "shadow", "enforce")


@router.get("/detect-config")
async def get_detect_config(
    _: None = Depends(_require_internal_key),
    db: Session = Depends(get_db),
):
    """Effective Tier-0 gate override for the detect-pipeline.

    The pipeline polls this on its reconcile tick (guided promotion: an admin
    flips shadow->enforce in the UI; the pipeline applies it live, no
    redeploy). ``gate_mode: null`` means "no override — follow your env".
    """
    row = (
        db.query(SecuritySetting)
        .filter(SecuritySetting.key == GATE_MODE_KEY)
        .first()
    )
    mode = (row.json_value or "").strip().lower() if row else ""
    return {"gate_mode": mode if mode in _VALID_GATE_MODES else None}


def _mint_mediamtx_jwt() -> str | None:
    """Mint a short-lived MediaMTX JWT with wildcard read scope.

    The camera-agent reads frames directly from MediaMTX's internal RTSP
    loopback (``rtsp://mediamtx:8554/cam-N``). MediaMTX requires a signed
    JWT on every RTSP connection — without it the connection is rejected with
    401 Unauthorized.

    This mirrors ``KaiCService._get_inference_mediamtx_jwt()``: wildcard read
    scope (``~.*``), 60-minute lifetime. Returns ``None`` on any error so the
    caller can fall back to the bare URL gracefully.
    """
    try:
        # Late import — MediaMtxJwtService loads RSA keys on first call.
        from services.mediamtx_jwt_service import MediaMtxJwtService

        return MediaMtxJwtService.create_stream_token(
            user_id=0,
            username="camera-agent-internal",
            camera_id=None,
            camera_path="~.*",
            actions=["read"],
            expiry_minutes=60,
        )
    except Exception as exc:
        logger.warning(
            "camera-agent endpoint: failed to mint MediaMTX JWT (%s) — "
            "returning bare RTSP URLs (camera-agent will get 401 from MediaMTX)",
            exc,
        )
        return None


@router.get("/cameras", dependencies=[Depends(_require_internal_key)])
def list_camera_agent_sources(
    db: Session = Depends(get_db),
    x_detect_hwaccel: str | None = Header(default=None, alias="X-Detect-Hwaccel"),
) -> dict[str, object]:
    """Return active cameras as frame sources for camera-agent.

    ``X-Detect-Hwaccel`` is the caller's **resolved** decode backend — what it
    can actually do, not what it was configured to want. When present it wins
    over ``settings.detect_hwaccel`` for the auto tap decision below.

    Why: handing out the full-resolution main stream is a bet that the reader
    has a GPU to absorb it. That bet used to be placed on a setting BOTH sides
    read independently, so a deployment with ``DETECT_HWACCEL=vaapi`` but no
    ``/dev/dri`` passed through got the main stream *and* software decode —
    several times the intended cost, silently. The reader is the only party
    that knows whether the render node is really there, so it now says so and
    we believe it. A caller that sends no header keeps the old behaviour.

    Prefer the MediaMTX internal RTSP tap so OpenNVR remains the owner of the
    camera connection. If the deployment disables that tap, fall back to the
    stored camera RTSP URL.

    The returned ``frame_url`` for MediaMTX tap paths includes a signed JWT
    (``?jwt=<token>``) so the camera-agent can authenticate with MediaMTX
    without needing the ``MEDIAMTX_SECRET`` key in its own config.
    """
    # Mint once for the whole response — all cameras share the same wildcard
    # token, so minting per-camera would waste RSA operations.
    mediamtx_jwt: str | None = (
        _mint_mediamtx_jwt() if settings.inference_use_mediamtx_tap else None
    )

    cameras = (
        db.query(Camera)
        .filter(Camera.is_active == True)  # noqa: E712 - SQLAlchemy expression
        .order_by(Camera.id.asc())
        .all()
    )
    out: list[dict[str, object]] = []
    for cam in cameras:
        stream_name = _build_stream_name(
            settings.mediamtx_stream_prefix,
            int(cam.id),
            str(cam.ip_address or ""),
        )
        if settings.inference_use_mediamtx_tap:
            base = (settings.mediamtx_rtsp_url or "rtsp://mediamtx:8554").rstrip("/")
            # Serve the LOW-RES SUBSTREAM tap when this camera has a sub
            # source (stored substream_url or a derivable vendor path —
            # mirroring exactly the condition under which the provisioner
            # creates the {name}-sub MediaMTX path) AND the tap policy says
            # so. Policy (settings.inference_tap_stream): 'auto' picks the
            # substream on CPU-only decode and the MAIN stream when
            # hardware decode is configured (a GPU box can afford full-res
            # decode and gets full-res evidence crops; detection accuracy
            # is equal either way — the model input is a fixed square);
            # 'sub'/'main' force it. Cameras with no sub source keep the
            # main tap regardless. This is what makes "configure the
            # camera's substream" actually reach Tier-0: decoding the sub
            # instead of the full main stream is the ~5x CPU difference
            # the detect-pipeline README documents.
            from services.stream_service import substream_name

            # STORED substream_url ONLY — deliberately narrower than the
            # provisioner (which also derives vendor-convention URLs for
            # the agent's on-demand live view, where a wrong guess merely
            # falls back to stills). Tier-0 is ALWAYS-ON: handing it a
            # derived URL that happens to be wrong — a Hikvision-shaped
            # path on a camera whose substream is disabled — would kill
            # detection for that camera outright. The operator's stored
            # URL is their explicit, presumably verified word; a guess is
            # not, so a guess never steers the detector.
            has_sub = bool((cam.substream_url or "").strip())
            mode = (settings.inference_tap_stream or "auto").strip().lower()
            if mode == "main":
                use_sub = False
            elif mode == "sub":
                use_sub = has_sub
            else:  # auto (and any unknown value degrades to auto)
                # The caller's REPORTED capability wins; the setting is only a
                # declaration of intent. No header = pre-handshake caller, so
                # keep reading the setting for it.
                hw = (
                    x_detect_hwaccel
                    if x_detect_hwaccel is not None
                    else (settings.detect_hwaccel or "cpu")
                ).strip().lower()
                use_sub = has_sub and hw in ("", "cpu")
            tap_name = substream_name(stream_name) if use_sub else stream_name
            frame_url = f"{base}/{tap_name}"
            # Append JWT so the camera-agent can authenticate with MediaMTX.
            # Fall back to bare URL when minting failed (keys not configured,
            # test environment, etc.) — agent will still start, just unable
            # to fetch frames for the tap path.
            if mediamtx_jwt:
                frame_url = f"{frame_url}?jwt={urlquote(mediamtx_jwt, safe='.')}"
            source = "mediamtx-sub" if use_sub else "mediamtx"
        elif cam.rtsp_url:
            frame_url = str(cam.rtsp_url)
            source = "camera"
        else:
            continue

        name = str(cam.name or f"Camera {cam.id}")
        role_bits = [name]
        if cam.location:
            role_bits.append(f"location: {cam.location}")
        if cam.description:
            role_bits.append(str(cam.description))
        role = "; ".join(role_bits)

        out.append(
            {
                "camera_id": f"cam{cam.id}",
                "open_nvr_camera_id": str(cam.id),
                "name": name,
                "frame_url": frame_url,
                "role": role,
                "source": source,
                # Per-camera capability assignment (slice 1 of
                # docs/design/per-camera-assignment.md). Additive: existing
                # consumers ignore it. [] = nothing assigned — consumers must
                # read that as "no restriction declared", never "do nothing".
                "assignments": list(cam.assignments or []),
            }
        )

    return {"cameras": out}



@router.get("/recordings/frame")
async def internal_recording_frame(
    camera_id: int = Query(..., description="Camera ID"),
    at: str = Query(..., description="Wall-clock instant (ISO 8601)"),
    _: None = Depends(_require_internal_key),
    db: Session = Depends(get_db),
):
    """One JPEG from recorded footage at a past instant, for the agent's
    describe_window. Reuses the recordings clip-resolution + ffmpeg extract;
    internal-key authed like the other camera-agent endpoints."""
    import asyncio
    from datetime import UTC, datetime

    from fastapi.responses import Response

    from routers.recordings import _extract_recording_frame, _resolve_frame_job

    try:
        at_dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail="bad 'at' timestamp")
    if at_dt.tzinfo is None:
        at_dt = at_dt.replace(tzinfo=UTC)

    job = _resolve_frame_job(db, camera_id, at_dt)
    if job is None:
        raise HTTPException(status_code=404, detail="no recording at that time")
    jpeg = await asyncio.to_thread(_extract_recording_frame, job[0], job[1])
    if not jpeg:
        raise HTTPException(status_code=502, detail="could not extract frame")
    return Response(
        content=jpeg, media_type="image/jpeg",
        headers={"Cache-Control": "max-age=86400"},
    )
