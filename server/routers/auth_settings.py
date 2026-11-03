# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Superuser settings for how people sign in: the identity provider
(OpenID Connect) and the mail server the password reset uses. Secrets
are write-only here: the responses say whether one is stored, never
what it is."""
from __future__ import annotations

import contextlib
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from core.auth import get_current_superuser
from core.database import get_db
from models import Role
from services import mailer, oidc
from services.audit_service import write_audit_log

router = APIRouter(prefix="/auth-settings", tags=["auth-settings"])


def _audit(db, request, user, what: str, details: dict[str, Any]) -> None:
    with contextlib.suppress(Exception):
        write_audit_log(db, action="settings.update", user_id=user.id, entity_type="auth_settings",
                        entity_id=what, details=details,
                        ip=request.client.host if request and request.client else None,
                        user_agent=request.headers.get("user-agent") if request else None)


# ── mail ────────────────────────────────────────────────────────────

class MailSettingsUpdate(BaseModel):
    host: str | None = Field(None, max_length=255)
    port: int | None = Field(None, ge=1, le=65535)
    security: str | None = Field(None, pattern="^(none|starttls|tls)$")
    username: str | None = Field(None, max_length=255)
    password: str | None = Field(None, max_length=255)      # "" keeps, null clears
    from_address: str | None = Field(None, max_length=255)
    from_name: str | None = Field(None, max_length=100)


@router.get("/mail")
async def get_mail(db: Session = Depends(get_db), _: Any = Depends(get_current_superuser)):
    return mailer.public(db)


@router.put("/mail")
async def put_mail(payload: MailSettingsUpdate, request: Request, db: Session = Depends(get_db),
                   user: Any = Depends(get_current_superuser)):
    out = mailer.save(db, payload.model_dump(exclude_unset=True))
    _audit(db, request, user, "mail", {k: ("***" if k == "password" else v)
                                        for k, v in payload.model_dump(exclude_unset=True).items()})
    return out


class MailTest(BaseModel):
    to: EmailStr


@router.post("/mail/test")
async def test_mail(body: MailTest, db: Session = Depends(get_db), user: Any = Depends(get_current_superuser)):
    try:
        await mailer.send(db, str(body.to), "OpenNVR test message",
                          f"This is a test message from OpenNVR, sent by {user.username}. Mail is configured.\n")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Sending failed: {exc}") from exc
    return {"ok": True}


# ── OpenID Connect ──────────────────────────────────────────────────

class OidcSettingsUpdate(BaseModel):
    enabled: bool | None = None
    issuer: str | None = Field(None, max_length=512)
    client_id: str | None = Field(None, max_length=255)
    client_secret: str | None = Field(None, max_length=1024)   # "" keeps, null clears
    scopes: str | None = Field(None, max_length=255)
    button_label: str | None = Field(None, max_length=60)
    auto_create: bool | None = None
    default_role_id: int | None = None
    role_claim: str | None = Field(None, max_length=100)
    role_map: dict[str, str] | None = None
    superuser_values: list[str] | None = None
    allow_local_login: bool | None = None


def _oidc_view(db) -> dict[str, Any]:
    """The settings and the roles to offer new accounts. GET and PUT both
    answer with it: the page redraws from what a save returns, and without
    the roles it crashed after every successful save."""
    out = oidc.public(db)
    out["roles"] = [{"id": r.id, "name": r.name} for r in db.query(Role).order_by(Role.id).all()]
    return out


@router.get("/oidc")
async def get_oidc(db: Session = Depends(get_db), _: Any = Depends(get_current_superuser)):
    return _oidc_view(db)


@router.put("/oidc")
async def put_oidc(payload: OidcSettingsUpdate, request: Request, db: Session = Depends(get_db),
                   user: Any = Depends(get_current_superuser)):
    patch = payload.model_dump(exclude_unset=True)
    if patch.get("default_role_id") is not None and \
            db.query(Role).filter(Role.id == patch["default_role_id"]).first() is None:
        raise HTTPException(status_code=422, detail="default_role_id is not a role")
    if patch.get("enabled"):
        cfg = {**oidc.load(db), **{k: v for k, v in patch.items() if v is not None}}
        if not (cfg["issuer"] and cfg["client_id"]):
            raise HTTPException(status_code=422, detail="issuer and client_id are needed before enabling")
    oidc.save(db, patch)
    _audit(db, request, user, "oidc", {k: ("***" if k == "client_secret" else v) for k, v in patch.items()})
    return _oidc_view(db)


class OidcTest(BaseModel):
    issuer: str = Field(..., max_length=512)


@router.post("/oidc/test")
async def test_oidc(body: OidcTest, _: Any = Depends(get_current_superuser)):
    """Fetch the provider's discovery document and key set, and say what
    it supports — before anyone is locked out by a typo."""
    try:
        doc = await oidc.discover(body.issuer.strip(), fresh=True)
        keys = await oidc.jwks(doc, fresh=True)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Discovery failed: {exc}") from exc
    return {"ok": True, "issuer": doc.get("issuer"), "authorization_endpoint": doc.get("authorization_endpoint"),
            "token_endpoint": doc.get("token_endpoint"), "userinfo_endpoint": doc.get("userinfo_endpoint"),
            "scopes_supported": doc.get("scopes_supported"), "claims_supported": doc.get("claims_supported"),
            "pkce": "S256" in (doc.get("code_challenge_methods_supported") or []),
            "keys": len(keys.get("keys") or [])}
