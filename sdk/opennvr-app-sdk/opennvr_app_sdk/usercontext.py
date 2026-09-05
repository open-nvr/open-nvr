# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Who is asking — the operator behind a ``/ui`` view or an action.

Core authenticates the person, then forwards their identity to the app
in ``X-OpenNVR-User``: a short-lived HS256 JWT signed with the SHA-256
of this app's own key (see ``credentials.py``) — a secret both sides
hold and never exchange. Inside ``ui_html()`` and ``on_action()`` an
app reads it with :func:`current_user`::

    def on_action(self, name, params):
        user = current_user()
        if user is None or not user.can_see(params["camera_id"]):
            raise ValueError("not your camera")

``None`` means core sent no context (an older core, no app key issued
yet, or a caller that is not the OpenNVR proxy). Verification is
stdlib-only; a token that fails any check is treated as absent, never
trusted partially.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field

logger = logging.getLogger("opennvr.app.user")

USER_CONTEXT_HEADER = "X-OpenNVR-User"
_LEEWAY_SECONDS = 30


@dataclass(frozen=True)
class UserContext:
    user_id: int
    username: str
    is_superuser: bool = False
    #: Camera ids (server-side ints) the user may VIEW; ``None`` = all.
    cameras: frozenset[int] | None = None
    #: Camera ids the user may MANAGE (control / configure); ``None`` = all.
    manage: frozenset[int] | None = None
    #: ``"ui"`` for a dashboard view, ``"action"`` for an invoked action.
    purpose: str = ""
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    def can_see(self, camera) -> bool:
        """``camera`` as an int id or a ``camN`` handle."""
        return self.cameras is None or _camera_id(camera) in self.cameras

    def can_manage(self, camera) -> bool:
        return self.manage is None or _camera_id(camera) in self.manage

    def visible(self, camera_ids) -> list:
        """Filter a list of ids/handles down to what this user may see."""
        return [c for c in camera_ids if self.can_see(c)]


def _camera_id(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    if text.startswith("cam-"):
        text = text[4:]
    elif text.startswith("cam"):
        text = text[3:]
    return int(text) if text.isdigit() else None


_CURRENT: ContextVar[UserContext | None] = ContextVar("opennvr_user", default=None)


def current_user() -> UserContext | None:
    """The operator behind the request being served, or ``None``."""
    return _CURRENT.get()


def bind_user(user: UserContext | None):
    return _CURRENT.set(user)


def unbind_user(token) -> None:
    _CURRENT.reset(token)


def signing_secret(app_key: str | None) -> str | None:
    """What core signs with: the SHA-256 hex of the app's key."""
    if not app_key:
        return None
    return hashlib.sha256(app_key.encode("utf-8")).hexdigest()


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def verify_user_context(token: str | None, secret: str | None, *,
                        audience: str | None = None,
                        now: float | None = None) -> UserContext | None:
    """Verify an ``X-OpenNVR-User`` value. Any failure → ``None``."""
    if not token or not secret:
        return None
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
        header = json.loads(_b64url_decode(header_b64))
        if header.get("alg") != "HS256":
            return None
        expected = hmac.new(secret.encode("utf-8"),
                            f"{header_b64}.{payload_b64}".encode("ascii"),
                            hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(sig_b64)):
            return None
        claims = json.loads(_b64url_decode(payload_b64))
    except Exception:  # noqa: BLE001 — malformed = absent
        return None
    if claims.get("iss") != "opennvr":
        return None
    if audience is not None and claims.get("aud") != audience:
        return None
    clock = time.time() if now is None else now
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or clock > float(exp) + _LEEWAY_SECONDS:
        return None
    try:
        user_id = int(claims["sub"])
    except (KeyError, TypeError, ValueError):
        return None
    cams = claims.get("cameras")
    manage = claims.get("manage")
    return UserContext(
        user_id=user_id,
        username=str(claims.get("username") or ""),
        is_superuser=bool(claims.get("is_superuser")),
        cameras=None if cams is None else frozenset(int(c) for c in cams),
        manage=None if manage is None else frozenset(int(c) for c in manage),
        purpose=str(claims.get("purpose") or ""),
        raw=claims,
    )
