# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Internal camera-agent integration endpoints.

These endpoints are for trusted in-stack services, not browsers. They let the
camera-agent reuse cameras already configured in OpenNVR without knowing camera
passwords or requiring an operator login token.
"""

from __future__ import annotations

import logging
import secrets
from urllib.parse import quote as urlquote

from fastapi import APIRouter, Depends, Header, HTTPException, status
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
def list_camera_agent_sources(db: Session = Depends(get_db)) -> dict[str, object]:
    """Return active cameras as frame sources for camera-agent.

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
    out: list[dict[str, str]] = []
    for cam in cameras:
        stream_name = _build_stream_name(
            settings.mediamtx_stream_prefix,
            int(cam.id),
            str(cam.ip_address or ""),
        )
        if settings.inference_use_mediamtx_tap:
            base = (settings.mediamtx_rtsp_url or "rtsp://mediamtx:8554").rstrip("/")
            frame_url = f"{base}/{stream_name}"
            # Append JWT so the camera-agent can authenticate with MediaMTX.
            # Fall back to bare URL when minting failed (keys not configured,
            # test environment, etc.) — agent will still start, just unable
            # to fetch frames for the tap path.
            if mediamtx_jwt:
                frame_url = f"{frame_url}?jwt={urlquote(mediamtx_jwt, safe='.')}"
            source = "mediamtx"
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
            }
        )

    return {"cameras": out}
