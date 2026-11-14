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
WebRTC settings router. Stores settings in SecuritySetting row with key 'webrtc'.
Superuser-only for mutations. Provides typed validation and a client config view.
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from core.auth import get_current_active_user, get_current_superuser
from core.config import settings
from core.database import get_db
from models import SecuritySetting
from schemas import (
    WebRTCClientConfig,
    WebRTCSettings as WebRTCSettingsSchema,
    WebRTCSettingsUpdate,
)
from services.audit_service import write_audit_log

router = APIRouter(prefix="/webrtc", tags=["webrtc"])


DEFAULTS = WebRTCSettingsSchema().model_dump()


def _get_webrtc_row(db: Session) -> SecuritySetting:
    row = db.query(SecuritySetting).filter(SecuritySetting.key == "webrtc").first()
    if not row:
        row = SecuritySetting(key="webrtc", json_value=json.dumps(DEFAULTS))
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


@router.get("/settings")
async def get_webrtc_settings(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    row = _get_webrtc_row(db)
    try:
        val = json.loads(row.json_value or "{}")
    except Exception:
        val = {}
    # Validate/shape with schema
    try:
        settings_obj = WebRTCSettingsSchema(**{**DEFAULTS, **val})
    except Exception:
        # If stored value invalid, reset to defaults
        settings_obj = WebRTCSettingsSchema(**DEFAULTS)
    return settings_obj.model_dump()


@router.put("/settings")
async def update_webrtc_settings(
    payload: WebRTCSettingsUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    row = _get_webrtc_row(db)
    # Merge payload into current (or defaults), then validate
    try:
        current_val = json.loads(row.json_value or "{}")
    except Exception:
        current_val = {}

    base = {**DEFAULTS, **current_val}
    update_dict = payload.model_dump(exclude_unset=True)

    def deep_merge(a, b):
        if isinstance(a, dict) and isinstance(b, dict):
            out = dict(a)
            for k, v in b.items():
                out[k] = deep_merge(out.get(k), v)
            return out
        return b if b is not None else a

    merged = deep_merge(base, update_dict)
    # For these two, None in the payload means "clear it": the Settings page
    # sends null for an emptied field, and deep_merge's "None keeps the old
    # value" would make the public address impossible to remove.
    for key in ("public_address", "public_ice_port"):
        if key in update_dict:
            merged[key] = update_dict[key]

    # Validate via schema. The payload model already refused a bad address;
    # this guards the merge of the payload with what was stored.
    try:
        settings_obj = WebRTCSettingsSchema(**merged)
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="; ".join(err.get("msg", "") for err in e.errors()) or "Invalid WebRTC settings",
        ) from e
    row.json_value = json.dumps(settings_obj.model_dump())
    db.commit()
    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="webrtc_settings",
            entity_id="webrtc",
            details=payload.model_dump(exclude_unset=True),
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass

    # Push the new ICE servers to the running MediaMTX so the media server uses
    # the same STUN/TURN as the browser. Best-effort: MediaMTX may not be up or
    # configured, and that must not fail the save.
    await _apply_ice_to_mediamtx(settings_obj)
    # The public address or the STUN list may have changed: re-advertise what
    # is known now, and let a forced STUN round catch up in the background
    # (an unreachable STUN server costs seconds; the save must not wait on it).
    try:
        from services.webrtc_ice_host_service import WebRTCIceHostService

        await WebRTCIceHostService.apply_to_mediamtx(db, stun=False)
        WebRTCIceHostService.refresh_soon()
    except Exception:
        pass

    return settings_obj.model_dump()


def _mediamtx_ice_servers(settings_obj: WebRTCSettingsSchema) -> list[dict]:
    """Convert stored STUN/TURN into MediaMTX's ``webrtcICEServers2`` shape.

    Each entry is ``{url, username, password, clientOnly}``. TURN is shared
    (``clientOnly: false``): the media server relays through it too. STUN is
    for the browser only: MediaMTX's single-port ICE listener cannot carry a
    STUN-discovered candidate (pion gathers it on a separate random socket
    that nothing forwards), so having MediaMTX ask would cost a STUN round
    trip per session for nothing. Core asks STUN itself for the public IP and
    advertises that on the ICE port (services/webrtc_ice_host_service.py).
    """
    out: list[dict] = []
    for s in settings_obj.stun_servers or []:
        if s:
            out.append(
                {"url": s, "username": "", "password": "", "clientOnly": True}
            )
    for t in settings_obj.turn_servers or []:
        if not t.url:
            continue
        out.append(
            {
                "url": t.url,
                "username": t.username or "",
                "password": t.credential or "",
                "clientOnly": False,
            }
        )
    return out


async def _apply_ice_to_mediamtx(settings_obj: WebRTCSettingsSchema) -> None:
    try:
        from services.mediamtx_admin_service import MediaMtxAdminService

        if not MediaMtxAdminService.is_configured():
            return
        await MediaMtxAdminService.set_webrtc_ice_servers(
            _mediamtx_ice_servers(settings_obj)
        )
    except Exception:
        # Media server unreachable / not provisioned — the browser side still
        # has the settings; MediaMTX picks them up next reload.
        pass


async def _apply_stored_ice_to_mediamtx() -> None:
    """Load the stored WebRTC settings and push them to MediaMTX. Used by the
    MediaMTX startup hook so a restart re-applies saved STUN/TURN."""
    from core.database import SessionLocal

    with SessionLocal() as db:
        row = _get_webrtc_row(db)
        try:
            val = json.loads(row.json_value or "{}")
        except Exception:
            val = {}
        settings_obj = WebRTCSettingsSchema(**{**DEFAULTS, **val})
    await _apply_ice_to_mediamtx(settings_obj)


@router.get("/rtc-config", response_model=WebRTCClientConfig)
async def get_client_rtc_config(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """ICE configuration for RTCPeerConnection, used by the live player.

    Any authenticated user needs this to watch a stream, so it is not
    superuser-gated. TURN credentials are inherently client-side — the browser
    cannot relay through TURN without them — so restricting this endpoint would
    break playback rather than protect the secret. Keep TURN credentials
    short-lived if that matters for a deployment.
    """
    row = _get_webrtc_row(db)
    try:
        val = json.loads(row.json_value or "{}")
    except Exception:
        val = {}
    settings_obj = WebRTCSettingsSchema(**{**DEFAULTS, **val})
    stun = settings_obj.stun_servers
    turn = settings_obj.turn_servers
    ice_servers = []
    if stun:
        for s in stun:
            ice_servers.append({"urls": s})
    if turn:
        for t in turn:
            entry = {"urls": t.url}
            if t.username:
                entry["username"] = t.username
            if t.credential:
                entry["credential"] = t.credential
            ice_servers.append(entry)
    # What the player needs to add its own candidates (the address it opened
    # the page on, on these ports) — app/src/lib/liveTransport.ts.
    from services.webrtc_ice_host_service import WebRTCIceHostService

    ice_port = int(settings.webrtc_ice_port or 8189)
    # The server list is only consumed in single-port mode (candidates on the
    # page's own port); outside it, do not resolve a DNS name on every tile.
    server_hosts: list[str] = []
    if settings.webrtc_single_port:
        try:
            server_hosts = await WebRTCIceHostService.resolved_ips(db)
        except Exception:
            server_hosts = []
    return WebRTCClientConfig(
        iceServers=ice_servers,
        iceTransportPolicy=settings_obj.transport_policy,
        icePort=ice_port,
        publicIcePort=settings_obj.public_ice_port or ice_port,
        singlePortWebrtc=bool(settings.webrtc_single_port),
        serverHosts=server_hosts,
    )


@router.get("/advertised")
async def get_advertised_hosts(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """The addresses MediaMTX advertises to viewers and where each came from
    (the Settings → WebRTC page), with the ICE ports to forward."""
    from services.webrtc_ice_host_service import WebRTCIceHostService

    ice_port = int(settings.webrtc_ice_port or 8189)
    public_ip = WebRTCIceHostService.public_ip()
    return {
        "hosts": WebRTCIceHostService.resolve_entries(db),
        "ice_port": ice_port,
        "public_ice_port": WebRTCIceHostService.public_ice_port(db),
        "stun_public_ip": public_ip.ip,
        "stun_server": public_ip.server,
    }
