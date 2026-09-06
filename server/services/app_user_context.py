# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Who is asking? — user identity forwarded to an app.

Core authenticates the operator on ``GET /apps/{id}/ui`` and
``POST /apps/{id}/actions/{name}`` and then calls the app, which until
now saw only the deployment key: no user, no camera scope. An app could
not draw a per-user page or refuse an action on a camera the caller may
not touch, and the camera-agent example re-implemented login just to
find out who it was talking to.

The proxies now attach ``X-OpenNVR-User``: a short-lived (60 s) HS256
JWT describing the caller — id, username, superuser flag, the camera
ids they may VIEW (``null`` = every camera) and MANAGE, and the purpose
(``ui`` / ``action``). It is signed with the app's ``api_key_hash``:
the SHA-256 of the app's own key, which the app can compute from the
key it holds and core already stores — a per-app shared secret with
nothing new to provision. No key issued → no context is forwarded
(the app behaves as before).

The SDK verifies it in ``opennvr_app_sdk.usercontext`` and exposes
``current_user()`` inside ``ui_html()`` / ``on_action()``.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from jose import jwt
from sqlalchemy.orm import Session

USER_CONTEXT_HEADER = "X-OpenNVR-User"
USER_CONTEXT_TTL_SECONDS = 60
ISSUER = "opennvr"


def mint_user_context(db: Session, row, user, *, purpose: str) -> str | None:
    """The signed ``X-OpenNVR-User`` value for ``user`` calling ``row``
    (an InstalledApp), or ``None`` when the app holds no key yet."""
    secret = getattr(row, "api_key_hash", None)
    if not secret:
        return None
    from services.camera_scope import manageable_camera_ids, visible_camera_ids

    view = visible_camera_ids(db, user)
    manage = manageable_camera_ids(db, user)
    now = datetime.now(UTC)
    claims = {
        "iss": ISSUER,
        "aud": row.id,
        "sub": str(user.id),
        "username": user.username,
        "is_superuser": bool(getattr(user, "is_superuser", False)),
        "cameras": None if view is None else sorted(view),
        "manage": None if manage is None else sorted(manage),
        "purpose": purpose,
        "iat": now,
        "exp": now + timedelta(seconds=USER_CONTEXT_TTL_SECONDS),
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def user_context_headers(db: Session, row, user, *, purpose: str) -> dict[str, str]:
    token = mint_user_context(db, row, user, purpose=purpose)
    return {USER_CONTEXT_HEADER: token} if token else {}


# ── Call tokens: core → app, without the site key ─────────────────────
#
# The app's write surfaces (``/actions/{name}``, ``/entitlement/verify``)
# used to be gated on the deployment's INTERNAL_API_KEY, which meant core
# had to hand every app the site key on every call. Since SDK 0.6 the
# gate is a short-lived token signed with the SAME per-app secret as
# ``X-OpenNVR-User`` (the sha256 of the app's own key): the app can
# verify it came from its core, and never sees a credential that opens
# anything but itself. Older SDKs still gate on the site key; core keeps
# sending it to them until they upgrade (``needs_legacy_site_key``).

CALL_TOKEN_HEADER = "X-OpenNVR-Call"
CALL_TOKEN_TTL_SECONDS = 60
#: SDKs from this version verify X-OpenNVR-Call and no longer need the
#: site key on the wire.
CALL_TOKEN_MIN_SDK = (0, 6, 0)


def _vt(text: str | None) -> tuple[int, ...] | None:
    if not text:
        return None
    try:
        return tuple(int(p) for p in str(text).split(".")[:3])
    except ValueError:
        return None


def needs_legacy_site_key(row) -> bool:
    """Whether this app must still receive ``X-Internal-Api-Key`` on
    action / entitlement calls: no per-app secret to sign with, or an
    SDK too old to verify the call token."""
    if not getattr(row, "api_key_hash", None):
        return True
    ver = _vt(getattr(row, "sdk_version", None))
    return ver is None or ver < CALL_TOKEN_MIN_SDK


def mint_call_token(row, *, purpose: str) -> str | None:
    """A signed, single-purpose, 60-second token proving the request
    comes from this app's core; ``None`` when the app holds no key."""
    secret = getattr(row, "api_key_hash", None)
    if not secret:
        return None
    now = datetime.now(UTC)
    claims = {
        "iss": ISSUER,
        "aud": row.id,
        "purpose": purpose,
        "jti": secrets.token_hex(8),
        "iat": now,
        "exp": now + timedelta(seconds=CALL_TOKEN_TTL_SECONDS),
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def call_headers(row, *, purpose: str) -> dict[str, str]:
    """Transport auth for a core → app call: the call token, plus the
    site key only for apps that cannot verify one yet."""
    headers: dict[str, str] = {}
    token = mint_call_token(row, purpose=purpose)
    if token:
        headers[CALL_TOKEN_HEADER] = token
    if needs_legacy_site_key(row):
        from core.config import settings

        if settings.internal_api_key:
            headers["X-Internal-Api-Key"] = settings.internal_api_key
    return headers
