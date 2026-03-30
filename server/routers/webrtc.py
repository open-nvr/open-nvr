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

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from core.auth import get_current_superuser
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

    # Validate via schema
    settings_obj = WebRTCSettingsSchema(**merged)
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
    return settings_obj.model_dump()


@router.get("/rtc-config", response_model=WebRTCClientConfig)
async def get_client_rtc_config(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Return a sanitized config suitable for RTCPeerConnection init and negotiation hints.

    Note: Currently restricted to superuser to avoid leaking TURN secrets broadly. Adjust as needed
    if clients need this anonymously via reverse proxy.
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
    return WebRTCClientConfig(
        iceServers=ice_servers,
        iceTransportPolicy=settings_obj.ice.transport_policy,
        codecPreferences={
            "video": settings_obj.codecs.video_preferred,
            "audio": settings_obj.codecs.audio_preferred,
        },
        bandwidth={
            "video_max_bitrate_kbps": settings_obj.bandwidth.video_max_bitrate_kbps,
            "audio_max_bitrate_kbps": settings_obj.bandwidth.audio_max_bitrate_kbps,
            "max_fps": settings_obj.bandwidth.max_fps,
            "resolution_cap": settings_obj.bandwidth.resolution_cap.model_dump(),
        },
    )
