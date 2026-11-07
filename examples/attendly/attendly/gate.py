"""The auth gate: one HTTP middleware instead of per-route dependencies.

``auth_mode: opennvr`` — every API request carries an OpenNVR bearer, which
is validated against core (``attendly.auth.OpenNVRAuth``). Tiers by role
name: viewer < operator < admin. The UI shell and its assets stay open (the
page renders the login card); the contract surface core probes
(``/health``, ``/manifest``, ``/state``) stays open as well.

``auth_mode: none`` — everything is open; a dev convenience for loopback.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse

from .auth import TIER_RANK, AuthUnavailable, OpenNVRAuth, public_user, user_tier
from .config import get_settings

logger = logging.getLogger(__name__)

OPEN_PATHS = {"/health", "/manifest", "/state", "/api/auth/login", "/api/auth/login-json", "/api/auth/refresh"}
API_PREFIXES = (
    "/api/", "/students", "/recognition", "/attendance", "/streams", "/classrooms", "/courses", "/schedules",
    "/video_registration", "/registry", "/planner", "/classes", "/cameras", "/docs", "/openapi.json", "/redoc",
    "/staff", "/departments", "/staff-attendance", "/gates", "/meals",
)

DEV_USER = {"id": 0, "username": "dev", "email": "", "role_name": "admin", "is_superuser": True, "mfa_enabled": False,
            "created_at": ""}


def is_api_path(path: str) -> bool:
    return any(path == p.rstrip("/") or path.startswith(p) for p in API_PREFIXES)


def required_tier(method: str, path: str) -> str:
    if method in ("GET", "HEAD", "OPTIONS"):
        return "viewer"
    if method == "DELETE" and path.startswith(("/students", "/staff/", "/gates", "/departments")):
        return "admin"
    if path.startswith("/registry/student/") and (path.endswith("/soft_delete") or (method == "DELETE" and path.endswith("/faces"))):
        return "admin"
    if path.startswith("/attendance/refinalize"):
        return "admin"
    return "operator"


class AuthGate:
    def __init__(self, *, auth_mode: str, auth: Optional[OpenNVRAuth]) -> None:
        self.auth_mode = auth_mode
        self.auth = auth

    def user_for_token(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        """Core's user for a bearer, None when rejected; raises
        :class:`AuthUnavailable` when core cannot be asked."""
        if self.auth_mode == "none":
            return dict(DEV_USER)
        if not token or self.auth is None:
            return None
        return self.auth.me(token)

    def forget(self, token: str) -> None:
        if self.auth is not None:
            self.auth.forget(token)


_gate: Optional[AuthGate] = None
_lock = threading.Lock()


def get_gate() -> AuthGate:
    global _gate
    with _lock:
        if _gate is None:
            s = get_settings()
            mode = (s.auth_mode or "opennvr").strip().lower()
            auth = OpenNVRAuth(base_url=s.opennvr_url, timeout_seconds=s.platform_timeout_seconds) if mode == "opennvr" else None
            _gate = AuthGate(auth_mode=mode, auth=auth)
        return _gate


def set_gate(gate: Optional[AuthGate]) -> None:
    global _gate
    with _lock:
        _gate = gate


def _bearer(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


async def current_user(request: Request) -> Dict[str, Any]:
    user = getattr(request.state, "user", None)
    if user is None:
        try:
            user = get_gate().user_for_token(_bearer(request))
        except AuthUnavailable:
            raise HTTPException(status_code=503, detail="OpenNVR server unreachable")
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def ws_user_allowed(token: Optional[str], tier: str = "operator") -> bool:
    try:
        user = get_gate().user_for_token(token)
    except AuthUnavailable:
        return False
    return user is not None and TIER_RANK[user_tier(user)] >= TIER_RANK[tier]


async def auth_middleware(request: Request, call_next):
    path = request.url.path
    gate = get_gate()
    if gate.auth_mode == "none" or path in OPEN_PATHS or not is_api_path(path):
        if gate.auth_mode == "none":
            request.state.user = dict(DEV_USER)
        return await call_next(request)
    try:
        user = gate.user_for_token(_bearer(request))
    except AuthUnavailable:
        return JSONResponse(status_code=503, content={"detail": "OpenNVR server unreachable; try again shortly"})
    if user is None:
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"},
                            headers={"WWW-Authenticate": "Bearer"})
    need = required_tier(request.method, path)
    have = user_tier(user)
    if TIER_RANK[have] < TIER_RANK[need]:
        return JSONResponse(status_code=403, content={"detail": f"This action needs the {need} role (you are {have})"})
    request.state.user = user
    request.state.public_user = public_user(user)
    return await call_next(request)
