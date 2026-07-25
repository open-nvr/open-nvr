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
Device-firewall middleware — refuse API access from unapproved browsers.

Guards only ``/api/*`` — static assets and the SPA shell always load so a
blocked device can render a clear "pending approval" page rather than a blank
screen. Bootstrap paths (login, health, JWKS) stay open so the first browser can
authenticate and enroll, and MediaMTX can fetch signing keys.

A browser is identified by the device cookie the server issued at login, NOT by
its IP: behind NAT every client shares one address (Docker Desktop's port
forwarding makes all LAN clients appear as the bridge gateway), so IP-keyed
gating silently let any second device inherit the first one's approval.

Decision order — machine callers first, then browsers:
  1. open path                          -> allow (bootstrap/SPA)
  2. loopback                           -> allow (``docker exec`` recovery)
  3. valid internal API key             -> allow (sibling service, no cookie)
  4. device cookie present              -> allow iff that browser is approved
  5. user session but no device cookie  -> deny (user traffic must enroll; this
                                           is what stops a blocked browser from
                                           deleting its cookie to slip into the
                                           internal-network case below)
  6. no cookie, no session, internal net -> allow (sibling service on the
                                           compose network)
  7. otherwise                          -> deny

Fail-open by construction: if the master switch is off, or the caller is
loopback / an internal service, or a registry lookup errors, the request
proceeds. Losing access to your own NVR must require a deliberate approval
state, never an accident.
"""

from __future__ import annotations

import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from core.client_ip import get_client_ip, is_internal_service, is_loopback
from core.database import SessionLocal
from services import device_firewall_service as dfw

# API paths reachable by an unapproved device. Everything else under /api is
# gated. Kept deliberately small.
_OPEN_API_PREFIXES = (
    "/api/v1/auth/",  # login / setup / refresh — enrollment happens on login
    "/api/v1/device-firewall/status",  # lets a blocked device learn its state
    "/api/v1/health",
)


def _is_open(path: str) -> bool:
    # Non-API paths (SPA, static assets, /health, /.well-known/jwks.json) are
    # never gated, so only /api/* needs checking.
    if not path.startswith("/api/"):
        return True
    return any(path.startswith(p) for p in _OPEN_API_PREFIXES)


def _has_internal_key(request: Request) -> bool:
    """True for a sibling service presenting the shared internal API key."""
    sent = request.headers.get("x-internal-api-key") or ""
    if not sent:
        return False
    try:
        from core.config import settings

        expected = settings.internal_api_key or ""
    except Exception:
        return False
    return bool(expected) and secrets.compare_digest(sent, expected)


class DeviceFirewallMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if _is_open(request.url.path):
            return await call_next(request)

        ip = get_client_ip(request)
        if is_loopback(ip) or _has_internal_key(request):
            return await call_next(request)

        token = request.cookies.get(dfw.DEVICE_COOKIE_NAME)
        has_session = bool(request.headers.get("authorization"))

        db = SessionLocal()
        try:
            if not dfw.enforcement_active(db):
                return await call_next(request)
            if token:
                if dfw.is_allowed_browser(db, token):
                    return await call_next(request)
            elif not has_session and is_internal_service(ip):
                # Sibling container (MediaMTX/KAI-C/adapters) calling the API
                # without a browser cookie or a user session.
                return await call_next(request)
        finally:
            db.close()

        return JSONResponse(
            status_code=403,
            content={
                "detail": "This device is not approved to access OpenNVR.",
                "device_ip": ip,
                "code": "device_not_approved",
            },
        )
