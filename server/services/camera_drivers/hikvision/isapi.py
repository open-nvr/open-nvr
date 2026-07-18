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
ISAPI HTTP primitive — Hikvision's REST-ish device API over HTTP Digest.

Mirrors ``onvif_digest_service._onvif_request``: a thin, dependency-light
``httpx`` call that returns ``(status_code, text)`` and never raises on HTTP
status (callers decide what a non-200 means). Used by HikvisionIsapiDriver for
the settings ONVIF can't reach.
"""

from __future__ import annotations

import httpx
from fastapi import HTTPException


async def isapi_request(
    ip: str,
    path: str,
    username: str | None = None,
    password: str | None = None,
    *,
    method: str = "GET",
    port: int = 80,
    body: str | None = None,
    timeout: float = 15.0,
) -> tuple[int, str]:
    """Make an ISAPI request. ``path`` starts with ``/ISAPI/...``.

    Returns ``(status_code, response_text)``. Raises HTTPException only on
    transport failure (timeout / connection refused), matching the ONVIF
    primitive's contract so drivers handle both the same way.
    """
    url = f"http://{ip}:{port}{path}"
    auth = httpx.DigestAuth(username, password) if username else None
    headers = {"Content-Type": "application/xml"} if body else {}
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.request(
                method, url, content=body, headers=headers, auth=auth
            )
            return resp.status_code, resp.text
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail=f"ISAPI timeout to {url}")
        except httpx.ConnectError as e:
            raise HTTPException(
                status_code=503, detail=f"Cannot connect to camera ISAPI: {e}"
            )
