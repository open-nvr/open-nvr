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
