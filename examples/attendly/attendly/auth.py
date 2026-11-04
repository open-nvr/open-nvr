"""Login is OpenNVR's. The app keeps no user table: ``/api/auth/login-json``
and ``/refresh`` are proxied to core, and every API call's bearer is
validated against core's ``/api/v1/auth/me`` (cached per token for a
minute, and a token core validated recently rides out a short core
outage). The same contract the Camera Agent uses."""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

TIER_RANK = {"viewer": 0, "operator": 1, "admin": 2}


class AuthUnavailable(RuntimeError):
    """Core could not be asked about a token (not "the token is bad")."""


def user_tier(user: Dict[str, Any]) -> str:
    if user.get("is_superuser"):
        return "admin"
    role = str(user.get("role_name") or "").lower()
    if role == "admin":
        return "admin"
    if role == "operator":
        return "operator"
    return "viewer"


def public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    """Core's ``UserResponse`` → the shape attendly's UI already reads."""
    tier = user_tier(user)
    created = user.get("created_at")
    return {
        "id": str(user.get("id", "")),
        "username": str(user.get("username", "")),
        "email": str(user.get("email") or ""),
        "mfa_enabled": bool(user.get("mfa_enabled", False)),
        "is_admin": tier == "admin",
        "role_name": user.get("role_name"),
        "tier": tier,
        "created_at": str(created or ""),
    }


def _detail_text(data: Any) -> str:
    if isinstance(data, dict):
        d = data.get("detail", data)
        if isinstance(d, dict):
            return str(d.get("message") or d.get("error") or d)
        return str(d)
    return str(data)


class OpenNVRAuth:
    def __init__(self, *, base_url: str, ttl_seconds: float = 60.0, grace_seconds: float = 120.0,
                 timeout_seconds: float = 5.0, http_client: httpx.Client | None = None) -> None:
        self._base = f"{base_url.rstrip('/')}/api/v1/auth"
        self._ttl = ttl_seconds
        self._grace = grace_seconds
        self._http = http_client or httpx.Client(timeout=timeout_seconds, trust_env=False)
        # token -> (checked_at, user | None)
        self._cache: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}
        self._cache_max = 256

    # ── Proxies ─────────────────────────────────────────────────────

    def login(self, username: str, password: str, code: Optional[str] = None, *,
              device_token: Optional[str] = None, user_agent: Optional[str] = None) -> Tuple[int, Any]:
        body: Dict[str, Any] = {"username": username, "password": password}
        if code:
            body["code"] = code
        headers: Dict[str, str] = {}
        if device_token:
            headers["x-device-token"] = device_token
        if user_agent:
            headers["user-agent"] = user_agent
        try:
            resp = self._http.post(f"{self._base}/login-json", json=body, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("auth: login proxy unreachable: %s", exc)
            return 503, {"detail": "OpenNVR server unreachable"}
        try:
            data = resp.json()
        except Exception:
            data = {"detail": resp.text[:200]}
        # Core answers a missing TOTP with 401 "Invalid or missing MFA code";
        # the login page expects 202 to ask for the code, as before.
        if resp.status_code == 401 and not code and "mfa" in _detail_text(data).lower():
            return 202, {"detail": "MFA required. Enter your authenticator code."}
        return resp.status_code, data

    def refresh(self, refresh_token: str) -> Tuple[int, Any]:
        try:
            resp = self._http.post(f"{self._base}/refresh", json={"refresh_token": refresh_token})
        except httpx.HTTPError as exc:
            logger.warning("auth: refresh proxy unreachable: %s", exc)
            return 503, {"detail": "OpenNVR server unreachable"}
        try:
            return resp.status_code, resp.json()
        except Exception:
            return resp.status_code, {"detail": resp.text[:200]}

    # ── Validation ──────────────────────────────────────────────────

    def me(self, token: str) -> Optional[Dict[str, Any]]:
        """Core's user payload for a bearer, ``None`` when core rejected it.
        Raises :class:`AuthUnavailable` when core could not be asked and
        the token has no recent good verdict to ride on."""
        now = time.monotonic()
        hit = self._cache.get(token)
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1]
        try:
            resp = self._http.get(f"{self._base}/me", headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            return self._ride_or_raise(now, hit, str(exc))
        if resp.status_code >= 500:
            return self._ride_or_raise(now, hit, f"HTTP {resp.status_code}")
        user: Optional[Dict[str, Any]] = None
        if resp.status_code == 200:
            try:
                data = resp.json()
                user = data if isinstance(data, dict) else None
            except Exception:
                user = None
        self._remember(token, now, user)
        return user

    def _ride_or_raise(self, now: float, hit, why: str) -> Optional[Dict[str, Any]]:
        if hit is not None and hit[1] is not None and now - hit[0] < self._ttl + self._grace:
            logger.warning("auth: core unreachable (%s); serving cached verdict", why)
            return hit[1]
        raise AuthUnavailable(why)

    def _remember(self, token: str, now: float, user: Optional[Dict[str, Any]]) -> None:
        if len(self._cache) >= self._cache_max:
            oldest = min(self._cache.items(), key=lambda kv: kv[1][0])[0]
            self._cache.pop(oldest, None)
        self._cache[token] = (now, user)

    def forget(self, token: str) -> None:
        self._cache.pop(token, None)
