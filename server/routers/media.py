# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Signed media URLs (HA-112): ``POST /media/sign`` and ``GET /media/s/{token}``.

Signing needs a logged-in user or an API token, the kind's permission and
the camera. Fetching needs only the URL, which is why the device firewall
lets ``/api/v1/media/s/`` through: the signature is the credential, it names
one resource, it expires, and the signer's access is re-checked on every
fetch (services/media_signing.py).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.auth import get_current_active_user, get_current_superuser
from core.database import get_db, release
from core.permissions import user_has_permission
from models import AppAlert, Camera, TimelineEvent, User
from services import api_tokens, media_signing
from services.audit_service import audit_request, write_audit_log
from services.camera_scope import can_view_camera

router = APIRouter(prefix="/media", tags=["media"])


class SignIn(BaseModel):
    kind: str = Field(..., pattern="^(event|alert_image|clip)$")
    #: event id / alert id.
    id: int | None = None
    #: event: evidence | scene | plate | plate_frame; alert_image: the image key.
    name: str | None = Field(None, max_length=40)
    #: clip only.
    camera_id: int | None = None
    start: datetime | None = None
    duration_s: float | None = Field(None, gt=0, le=media_signing.MAX_CLIP_S)
    ttl_s: int = Field(media_signing.DEFAULT_TTL_S, ge=media_signing.MIN_TTL_S,
                       le=media_signing.MAX_TTL_S)


def _alert_camera_id(alert: AppAlert) -> int | None:
    from services.alerts_inbox import _camera_num

    return _camera_num(alert.camera_id)


def _resolve(db: Session, claims: dict) -> tuple[int | None, str]:
    """``(camera_id, what)`` for the resource the claims name; 404 if gone.
    ``what`` is a file path (images) or ``clip``."""
    kind = claims.get("k")
    if kind == "event":
        col = media_signing.EVENT_IMAGES.get(claims.get("n") or "evidence")
        e = db.query(TimelineEvent).filter(TimelineEvent.id == claims.get("i")).first()
        rel = getattr(e, col, None) if (e is not None and col) else None
        if not rel:
            raise HTTPException(status_code=404, detail="Not found")
        return e.camera_id, rel
    if kind == "alert_image":
        a = db.query(AppAlert).filter(AppAlert.id == claims.get("i")).first()
        try:
            rel = (json.loads(a.images) or {}).get(claims.get("n")) if a and a.images else None
        except ValueError:
            rel = None
        if not isinstance(rel, str) or not rel:
            raise HTTPException(status_code=404, detail="Not found")
        return _alert_camera_id(a), rel
    if kind == "clip":
        cam = db.query(Camera).filter(Camera.id == claims.get("c")).first()
        if cam is None:
            raise HTTPException(status_code=404, detail="Not found")
        return cam.id, "clip"
    raise HTTPException(status_code=404, detail="Not found")


def _may_see(db: Session, principal, camera_id: int | None) -> bool:
    if camera_id is None:
        # An alert about no camera: visible to whoever may see the fleet.
        from services.camera_scope import visible_camera_ids

        return visible_camera_ids(db, principal) is None
    return can_view_camera(db, principal, camera_id)


@router.post("/sign")
async def sign_media(
    payload: SignIn,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """A URL for one piece of media that works without a login until it
    expires (default 24 h), e.g. for a phone notification."""
    needed = media_signing.KIND_PERMISSION[payload.kind]
    if not user_has_permission(current_user, needed):
        raise HTTPException(status_code=403, detail=f"Needs the {needed} permission")
    claims: dict = {"k": payload.kind, "u": current_user.id,
                    "t": current_user.token_id if api_tokens.is_token_principal(current_user)
                    else None}
    if payload.kind == "clip":
        if payload.camera_id is None or payload.start is None or payload.duration_s is None:
            raise HTTPException(status_code=422, detail="clip needs camera_id, start, duration_s")
        start = payload.start if payload.start.tzinfo else payload.start.replace(tzinfo=UTC)
        claims.update(c=payload.camera_id,
                      s=start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                      d=round(float(payload.duration_s), 3))
    else:
        if payload.id is None:
            raise HTTPException(status_code=422, detail=f"{payload.kind} needs id")
        if payload.kind == "event" and (payload.name or "evidence") not in media_signing.EVENT_IMAGES:
            raise HTTPException(status_code=422, detail="unknown event image")
        claims.update(i=payload.id, n=payload.name or ("evidence" if payload.kind == "event" else None))
    # The camera gate for a body-carried camera (clips) and the access check
    # for every kind, now, at signing.
    camera_id, _what = _resolve(db, claims)
    if payload.kind == "clip":
        api_tokens.check_token_camera(current_user, camera_id)
    if not _may_see(db, current_user, camera_id):
        raise HTTPException(status_code=404, detail="Not found")
    token, exp = media_signing.sign(db, claims, payload.ttl_s)
    audit_request(db, request, action="media.sign", user_id=current_user.id,
                  entity_type=payload.kind, entity_id=payload.id or camera_id,
                  details={"camera_id": camera_id, "name": claims.get("n"),
                           "start": claims.get("s"), "duration_s": claims.get("d"),
                           "expires_at": datetime.fromtimestamp(exp, UTC).isoformat()})
    return {"url": f"/api/v1/media/s/{token}", "token": token,
            "expires_at": datetime.fromtimestamp(exp, UTC).isoformat()}


@router.get("/s/{token}")
async def fetch_signed_media(token: str, db: Session = Depends(get_db)):
    """The media a signed URL names. No login: the URL is the credential."""
    try:
        claims = media_signing.verify(db, token)
    except media_signing.BadToken:
        raise HTTPException(status_code=403, detail="Invalid or expired link") from None
    principal = media_signing.signer_principal(db, claims)
    if principal is None:
        raise HTTPException(status_code=403, detail="Invalid or expired link")
    camera_id, what = _resolve(db, claims)
    needed = media_signing.KIND_PERMISSION.get(claims.get("k"))
    if (needed is None or not user_has_permission(principal, needed)
            or not _may_see(db, principal, camera_id)):
        raise HTTPException(status_code=403, detail="Invalid or expired link")
    if media_signing.should_audit(token):
        write_audit_log(db, action="media.fetch", user_id=claims.get("u"),
                        entity_type=claims.get("k"), entity_id=claims.get("i") or camera_id,
                        details={"camera_id": camera_id, "name": claims.get("n"),
                                 "actor": (f"token:{principal.token_name}"
                                           if api_tokens.is_token_principal(principal)
                                           else f"user:{principal.username}")})
    if what == "clip":
        from routers.recordings import _build_stream_name, settings, stream_playback_clip

        cam = db.query(Camera).filter(Camera.id == camera_id).first()
        path = _build_stream_name(settings.mediamtx_stream_prefix, cam.id, cam.ip_address)
        release(db)
        return await stream_playback_clip(path, claims["s"], claims["d"],
                                          f"camera{camera_id}.mp4", inline=True)
    from services.evidence_store import resolve_evidence

    path = resolve_evidence(what)
    if path is None:
        raise HTTPException(status_code=404, detail="Not found")
    # Don't hold a pooled connection for a client-paced transfer.
    release(db)
    remaining = max(0, claims["x"] - int(datetime.now(UTC).timestamp()))
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": f"private, max-age={min(remaining, 86400)}"})


@router.post("/keys/rotate")
async def rotate_media_keys(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
):
    """New signing key. URLs signed with the previous key keep working;
    rotating twice invalidates every signed URL."""
    kid = media_signing.rotate_keys(db)
    audit_request(db, request, action="media.rotate_keys", user_id=current_user.id,
                  entity_type="setting", entity_id=None, details={"kid": kid})
    return {"current": kid}

