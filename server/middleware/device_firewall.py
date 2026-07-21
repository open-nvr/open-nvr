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
Device-firewall middleware — refuse API access from unapproved devices.

Guards only ``/api/*`` — static assets and the SPA shell always load so a
blocked device can render a clear "pending approval" page rather than a blank
screen. Bootstrap paths (login, health, JWKS) stay open so the first device can
authenticate and enroll, and MediaMTX can fetch signing keys.

Fail-open by construction: if the master switch is off, or the caller is
loopback / an internal service, or a registry lookup errors, the request
proceeds. Losing access to your own NVR must require a deliberate approval
state, never an accident.
"""

from __future__ import annotations

import contextlib

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from core.client_ip import get_client_ip, is_internal_service
from core.database import SessionLocal
from services import device_firewall_service as dfw

# API paths reachable by an unapproved device. Everything else under /api is
# gated. Kept deliberately small.
_OPEN_API_PREFIXES = (
    "/api/v1/auth/",  # login / setup / refresh — enrollment happens on login
    "/api/v1/device-firewall/status",  # lets a blocked device learn its state
    "/api/v1/health",
)
# Non-API public paths (health probe, JWKS for MediaMTX).
_OPEN_EXACT = ("/health", "/.well-known/jwks.json")


def _is_open(path: str) -> bool:
    if not path.startswith("/api/"):
        return True  # SPA + static assets always load
    if path in _OPEN_EXACT:
        return True
    return any(path.startswith(p) for p in _OPEN_API_PREFIXES)


class DeviceFirewallMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if _is_open(request.url.path):
            return await call_next(request)

        ip = get_client_ip(request)
        if is_internal_service(ip):
            return await call_next(request)

        db = SessionLocal()
        try:
            if dfw.is_allowed(db, ip):
                return await call_next(request)
            # Record the unapproved attempt so an admin can see and approve it.
            with contextlib.suppress(Exception):
                dfw.touch(db, ip, request.headers.get("user-agent"))
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
