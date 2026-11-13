# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign-in without a password: OpenID Connect, and the e-mailed
password reset. Both sit beside the password routes under ``/auth``.

OIDC: ``GET /auth/oidc/start`` sends the browser to the provider with a
PKCE challenge and a signed state cookie; the provider returns to
``GET /auth/oidc/callback``, which verifies everything, finds or creates
the account, and hands the browser a one-time code in the login page's
URL; the page swaps it at ``POST /auth/oidc/exchange`` for the same
token pair a password login returns. Tokens never travel in a URL.

Reset: ``POST /auth/forgot`` always answers the same way, whether or not
the address exists, and sends a 30-minute single-use link only when the
operator configured mail. ``POST /auth/reset`` sets the password, ends
every other session of that account, and clears a lockout.
"""
from __future__ import annotations

import contextlib
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from core.config import settings
from core.database import get_db
from models import User
from schemas import Token
from services import mailer, oidc
from services.audit_service import write_audit_log
from services.user_service import UserService

logger = logging.getLogger("opennvr.sso")
router = APIRouter(prefix="/auth", tags=["auth-sso"])

RESET_TTL_MINUTES = 30
_forgot_calls: dict[str, list[float]] = {}
FORGOT_PER_HOUR = 5


def _public_origin(request: Request) -> str:
    """Where the browser reaches this site, for links in mail and the OIDC
    redirect URI: the proxy's forwarded headers, else the Origin header,
    else the request's own base."""
    proto = request.headers.get("x-forwarded-proto")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if host:
        return f"{proto or request.url.scheme}://{host}".rstrip("/")
    origin = request.headers.get("origin")
    if origin:
        return origin.rstrip("/")
    return str(request.base_url).rstrip("/")


def _token_pair(db: Session, request: Request, response: Response, user: User, method: str) -> dict[str, Any]:
    from core.auth import create_access_token, create_refresh_token
    from routers.auth import _enroll_device, _token_claims

    if user.failed_login_attempts or user.locked_until:
        user.failed_login_attempts = 0
        user.locked_until = None
        db.add(user)
        db.commit()
    access = create_access_token(data=_token_claims(user),
                                 expires_delta=timedelta(minutes=settings.access_token_expire_minutes))
    refresh = create_refresh_token(data=_token_claims(user))
    with contextlib.suppress(Exception):
        write_audit_log(db, action="login", user_id=user.id, entity_type="user", entity_id=user.id,
                        details={"username": user.username, "method": method},
                        ip=request.client.host if request and request.client else None,
                        user_agent=request.headers.get("user-agent") if request else None)
    device_token = _enroll_device(db, request, response, user.id)
    return {"access_token": access, "refresh_token": refresh, "token_type": "bearer", "device_token": device_token}


# ── what the login page may know ────────────────────────────────────

@router.get("/recovery-options")
async def recovery_options(db: Session = Depends(get_db)):
    """Which ways in exist besides a password: an e-mailed reset (when mail
    is configured) and single sign-on (when a provider is configured)."""
    return {"email_reset": mailer.is_configured(mailer.load(db)), "oidc": oidc.login_offer(db)}


# ── e-mailed password reset ─────────────────────────────────────────

class ForgotBody(BaseModel):
    email: EmailStr


class ResetBody(BaseModel):
    token: str = Field(..., min_length=20, max_length=4096)
    password: str = Field(..., min_length=8, max_length=128)


def _rate_limited(key: str) -> bool:
    now = time.monotonic()
    calls = [t for t in _forgot_calls.get(key, []) if now - t < 3600]
    _forgot_calls[key] = calls
    if len(calls) >= FORGOT_PER_HOUR:
        return True
    calls.append(now)
    return False


def reset_token_for(user: User) -> str:
    now = datetime.now(UTC)
    return jwt.encode({"sub": user.username, "ver": int(user.token_version or 0), "type": "reset",
                       "iat": now, "exp": now + timedelta(minutes=RESET_TTL_MINUTES)},
                      settings.secret_key, algorithm=settings.algorithm)


def _user_for_reset_token(db: Session, token: str) -> User | None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError:
        return None
    if payload.get("type") != "reset" or not payload.get("sub"):
        return None
    user = db.query(User).filter(User.username == payload["sub"]).first()
    if user is None or not user.is_active:
        return None
    # The version is the single-use latch: a reset bumps it, so the same
    # link (and any older one) is dead afterwards.
    if int(payload.get("ver", -1)) != int(user.token_version or 0):
        return None
    return user


@router.post("/forgot")
async def forgot_password(body: ForgotBody, request: Request, db: Session = Depends(get_db)):
    """Send a reset link if the address belongs to a local account. The
    answer is the same either way, so the route tells nobody which
    addresses exist; it only says whether mail is configured at all."""
    configured = mailer.is_configured(mailer.load(db))
    if not configured:
        return {"ok": True, "mail_configured": False}
    ip = request.client.host if request and request.client else "?"
    if _rate_limited(f"ip:{ip}") or _rate_limited(f"email:{body.email.lower()}"):
        return {"ok": True, "mail_configured": True}
    user = db.query(User).filter(User.email == str(body.email).lower()).first() \
        or db.query(User).filter(User.email == str(body.email)).first()
    if user is not None and user.is_active and (user.auth_provider or "local") == "local":
        link = f"{_public_origin(request)}/reset-password?token={reset_token_for(user)}"
        text = (f"Hello {user.first_name or user.username},\n\n"
                f"Someone asked to reset the OpenNVR password for this address. If that was you, "
                f"open this link within {RESET_TTL_MINUTES} minutes:\n\n{link}\n\n"
                f"If it was not you, ignore this message; nothing changes until the link is used.\n")
        try:
            await mailer.send(db, str(body.email), "Reset your OpenNVR password", text)
            with contextlib.suppress(Exception):
                write_audit_log(db, action="auth.reset_requested", user_id=user.id, entity_type="user",
                                entity_id=user.id, details={"email": str(body.email)}, ip=ip,
                                user_agent=request.headers.get("user-agent") if request else None)
        except Exception as exc:
            logger.warning("password reset mail to %s failed: %s", body.email, exc)
    return {"ok": True, "mail_configured": True}


@router.post("/reset")
async def reset_password(body: ResetBody, request: Request, db: Session = Depends(get_db)):
    user = _user_for_reset_token(db, body.token)
    if user is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="This reset link is invalid, expired or already used. Ask for a new one.")
    from core.auth import get_password_hash

    UserService._enforce_password_policy(db, user.username, user.email, body.password)
    user.hashed_password = get_password_hash(body.password)
    user.password_set = True
    user.token_version = int(user.token_version or 0) + 1
    user.failed_login_attempts = 0
    user.locked_until = None
    db.add(user)
    db.commit()
    with contextlib.suppress(Exception):
        write_audit_log(db, action="auth.password_reset", user_id=user.id, entity_type="user", entity_id=user.id,
                        details={"self_service": True},
                        ip=request.client.host if request and request.client else None,
                        user_agent=request.headers.get("user-agent") if request else None)
    return {"ok": True, "username": user.username}


# ── OpenID Connect ──────────────────────────────────────────────────

def _login_url(request: Request, **query: str) -> str:
    from urllib.parse import urlencode

    return f"{_public_origin(request)}/login" + (f"?{urlencode(query)}" if query else "")


@router.get("/oidc/start")
async def oidc_start(request: Request, next: str | None = None, db: Session = Depends(get_db)):
    """Send the browser to the identity provider."""
    redirect_uri = f"{_public_origin(request)}{settings.api_prefix}/auth/oidc/callback"
    try:
        url, cookie = await oidc.begin(db, redirect_uri, next if (next or "").startswith("/") else "/")
    except Exception as exc:
        logger.warning("oidc start failed: %s", exc)
        return RedirectResponse(_login_url(request, sso_error=str(exc)[:160]), status_code=302)
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(oidc.STATE_COOKIE, cookie, max_age=oidc.STATE_TTL_SECONDS, httponly=True,
                    samesite="lax", secure=request.url.scheme == "https", path=f"{settings.api_prefix}/auth/oidc")
    return resp


@router.get("/oidc/callback")
async def oidc_callback(request: Request, code: str | None = None, state: str | None = None,
                        error: str | None = None, error_description: str | None = None,
                        db: Session = Depends(get_db)):
    """Back from the provider: verify, resolve the account, hand the page a
    one-time code."""
    if error:
        return RedirectResponse(_login_url(request, sso_error=(error_description or error)[:160]), status_code=302)
    if not code or not state:
        return RedirectResponse(_login_url(request, sso_error="the identity provider sent no code"), status_code=302)
    try:
        claims = await oidc.complete(db, code=code, state=state, cookie=request.cookies.get(oidc.STATE_COOKIE))
        user = oidc.resolve_user(db, claims, oidc.load(db))
    except ValueError as exc:
        return RedirectResponse(_login_url(request, sso_error=str(exc)[:160]), status_code=302)
    except Exception as exc:
        logger.warning("oidc callback failed: %s", exc, exc_info=True)
        return RedirectResponse(_login_url(request, sso_error="single sign-on failed; try again"), status_code=302)
    handoff = oidc.issue_exchange_code(user.id, claims.get("_next") or "/")
    resp = RedirectResponse(_login_url(request, sso=handoff), status_code=302)
    resp.delete_cookie(oidc.STATE_COOKIE, path=f"{settings.api_prefix}/auth/oidc")
    return resp


@router.post("/oidc/exchange", response_model=Token)
async def oidc_exchange(request: Request, response: Response, code: str = Body(..., embed=True),
                        db: Session = Depends(get_db)):
    """The one-time code from the callback, for the token pair."""
    redeemed = oidc.redeem_exchange_code(code)
    if redeemed is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign-in code expired; try again")
    user_id, _next = redeemed
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()  # noqa: E712
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account unavailable")
    return _token_pair(db, request, response, user, "oidc")
