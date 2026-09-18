# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Create, list and revoke API tokens (HA-101).

Only a logged-in user holding ``api_tokens.manage`` may call these; an API
token cannot (these routes are not in services.api_tokens.TOKEN_ROUTES), so
a token can never mint another token.

A new token can never exceed its creator: every requested scope must be one
the creator holds, and every camera one the creator can see. The secret is
returned ONCE, from the create call; only its SHA-256 is stored.
"""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.database import get_db
from core.permissions import RequirePermission, user_has_permission
from models import ApiToken
from services import api_tokens
from services.audit_service import audit_request
from services.camera_scope import visible_camera_ids

router = APIRouter(prefix="/api-tokens", tags=["api-tokens"])

_manage = RequirePermission("api_tokens.manage")

#: Longest lifetime a token may be given (days). None (no expiry) is allowed.
MAX_EXPIRY_DAYS = 3650


class TokenCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    scopes: list[str] = Field(..., min_length=1)
    camera_ids: list[int] | None = None
    allowed_cidrs: list[str] | None = None
    expires_in_days: int | None = Field(None, ge=1, le=MAX_EXPIRY_DAYS)


def _view(row: ApiToken) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "prefix": row.prefix,
        "owner_user_id": row.owner_user_id,
        "scopes": row.scopes or [],
        "camera_ids": row.camera_ids,
        "allowed_cidrs": row.allowed_cidrs,
        "expires_at": row.expires_at,
        "created_at": row.created_at,
        "last_used_at": row.last_used_at,
        "last_used_ip": row.last_used_ip,
        "revoked_at": row.revoked_at,
    }


@router.get("")
async def list_tokens(db: Session = Depends(get_db), current_user=Depends(_manage)):
    """The caller's own tokens (a superuser sees every user's), newest first.
    Card session tokens (short-lived children of a token) are not listed."""
    q = db.query(ApiToken).filter(ApiToken.parent_id.is_(None))
    if not current_user.is_superuser:
        q = q.filter(ApiToken.owner_user_id == current_user.id)
    return {"tokens": [_view(r) for r in q.order_by(ApiToken.id.desc()).all()]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_token(
    payload: TokenCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(_manage),
):
    scopes = sorted(set(payload.scopes))
    unknown = [s for s in scopes if s not in api_tokens.ALLOWED_SCOPES]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Scopes not allowed for tokens: {unknown}")
    lacking = [s for s in scopes if not user_has_permission(current_user, s)]
    if lacking:
        raise HTTPException(status_code=403,
                            detail=f"You cannot grant permissions you do not hold: {lacking}")

    cameras = None
    if payload.camera_ids is not None:
        cameras = sorted(set(int(c) for c in payload.camera_ids))
        visible = visible_camera_ids(db, current_user)
        if visible is not None and not set(cameras) <= visible:
            raise HTTPException(status_code=403,
                                detail="You can only grant cameras you can see")

    cidrs = None
    if payload.allowed_cidrs is not None:
        try:
            cidrs = [str(ipaddress.ip_network(c.strip(), strict=False))
                     for c in payload.allowed_cidrs if c.strip()]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid CIDR: {exc}") from exc

    plain, prefix, digest = api_tokens.mint_token()
    row = ApiToken(
        name=payload.name.strip(),
        prefix=prefix,
        token_hash=digest,
        owner_user_id=current_user.id,
        scopes=scopes,
        camera_ids=cameras,
        allowed_cidrs=cidrs,
        expires_at=(datetime.now(UTC) + timedelta(days=payload.expires_in_days)
                    if payload.expires_in_days else None),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    api_tokens.invalidate_caches()  # the firewall sees the new token at once
    audit_request(
        db, request, action="api_token.create", user_id=current_user.id,
        entity_type="api_token", entity_id=row.id,
        details={"name": row.name, "prefix": prefix, "scopes": scopes,
                 "camera_ids": cameras, "allowed_cidrs": cidrs,
                 "expires_at": row.expires_at.isoformat() if row.expires_at else None},
    )
    # The ONLY time the secret is ever returned.
    return {**_view(row), "token": plain}


@router.delete("/{token_id}")
async def revoke_token(
    token_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(_manage),
):
    row = db.query(ApiToken).filter(ApiToken.id == token_id).first()
    if row is None or (row.owner_user_id != current_user.id
                       and not current_user.is_superuser):
        raise HTTPException(status_code=404, detail="Token not found")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        # Its card sessions go with it.
        db.query(ApiToken).filter(ApiToken.parent_id == row.id,
                                  ApiToken.revoked_at.is_(None)).update(
            {ApiToken.revoked_at: row.revoked_at}, synchronize_session=False)
        db.commit()
        api_tokens.invalidate_caches()
        audit_request(
            db, request, action="api_token.revoke", user_id=current_user.id,
            entity_type="api_token", entity_id=row.id,
            details={"name": row.name, "prefix": row.prefix},
        )
    return _view(row)


class SessionCreate(BaseModel):
    camera_ids: list[int] | None = None
    ttl_s: int = Field(api_tokens.SESSION_MAX_TTL_S, ge=60, le=api_tokens.SESSION_MAX_TTL_S)


@router.post("/session", status_code=status.HTTP_201_CREATED)
async def create_session_token(
    payload: SessionCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """A dashboard card's credential (design §7.8), minted by an API token
    (the Home Assistant integration) for a browser: its parent's scopes
    limited to reading, its parent's cameras (or fewer), at most ten
    minutes and never past the parent's expiry; revoked with the parent.
    A session token cannot mint another."""
    if not api_tokens.is_token_principal(current_user):
        raise HTTPException(status_code=403, detail="Only an API token can open a session")
    parent = db.query(ApiToken).filter(ApiToken.id == current_user.token_id).first()
    if parent is None or parent.parent_id is not None:
        raise HTTPException(status_code=403, detail="A session token cannot open a session")
    scopes = sorted(s for s in api_tokens.SESSION_SCOPES
                    if api_tokens.token_has_permission(current_user, s))
    if not scopes:
        raise HTTPException(status_code=403, detail="Nothing this token may read")
    allowed = current_user.camera_ids
    cameras = None if payload.camera_ids is None else sorted(set(payload.camera_ids))
    if cameras is not None and allowed is not None and not set(cameras) <= allowed:
        raise HTTPException(status_code=403,
                            detail="This API token is not allowed to use that camera")
    if cameras is None and allowed is not None:
        cameras = sorted(allowed)
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=payload.ttl_s)
    parent_expires = api_tokens._as_aware(parent.expires_at)
    if parent_expires is not None and parent_expires < expires:
        expires = parent_expires
    # Expired sessions are useless: keep the table from growing with them.
    db.query(ApiToken).filter(ApiToken.parent_id.isnot(None),
                              ApiToken.expires_at < now - timedelta(hours=1)).delete(
        synchronize_session=False)
    plain, prefix, digest = api_tokens.mint_token()
    row = ApiToken(name=f"session:{parent.name}"[:64], prefix=prefix, token_hash=digest,
                   owner_user_id=parent.owner_user_id, scopes=scopes, camera_ids=cameras,
                   allowed_cidrs=None, expires_at=expires, parent_id=parent.id)
    db.add(row)
    db.commit()
    api_tokens.invalidate_caches()
    audit_request(
        db, request, action="api_token.session", user_id=parent.owner_user_id,
        entity_type="api_token", entity_id=row.id,
        details={"parent": parent.prefix, "prefix": prefix, "scopes": scopes,
                 "camera_ids": cameras, "expires_at": expires.isoformat()},
    )
    return {"token": plain, "expires_at": expires.isoformat(), "scopes": scopes,
            "camera_ids": cameras}
