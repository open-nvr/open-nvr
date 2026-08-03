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
Secureye HTTP primitive — this vendor's device API over HTTP Digest.

Deliberately self-contained: every vendor package owns its own transport so a
change here can never affect another brand's driver, even though the wire
mechanics resemble Hikvision's. See ``docs/adding-a-camera-vendor.md``.

Secureye devices expose **two** XML namespaces over the same transport:

* ``/CGI/...``   — the vendor's native API (imaging, OSD, snapshot config).
  Paths are channel- and template-scoped, e.g.
  ``/CGI/Image/channels/1/color/template/0``.
* ``/ISAPI/...`` — a *partial* ISAPI-style subset (motion, time, ipFilter,
  storage/hdd). It is NOT Hikvision's ISAPI: the XML carries no Hikvision
  namespace and most Hikvision paths return HTTP 400.

One client serves both because the difference is only the URL prefix.
"""

from __future__ import annotations

import httpx
from fastapi import HTTPException


async def cgi_request(
    ip: str,
    path: str,
    username: str | None = None,
    password: str | None = None,
    *,
    method: str = "GET",
    port: int = 80,
    scheme: str = "http",
    body: str | None = None,
    timeout: float = 15.0,
) -> tuple[int, str]:
    """Make a Secureye device request. ``path`` starts with ``/CGI/`` or ``/ISAPI/``.

    Returns ``(status_code, response_text)``. Raises HTTPException only on
    transport failure (timeout / connection refused) — a non-200 is data the
    caller interprets, since this firmware answers 400 for endpoints it simply
    does not implement. ``verify=False`` for self-signed camera certs.
    """
    url = f"{scheme}://{ip}:{port}{path}"
    auth = httpx.DigestAuth(username, password) if username else None
    headers = {"Content-Type": "application/xml"} if body else {}
    async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
        try:
            resp = await client.request(
                method, url, content=body, headers=headers, auth=auth
            )
            return resp.status_code, resp.text
        except httpx.TimeoutException:
            raise HTTPException(
                status_code=504, detail=f"Secureye CGI timeout to {url}"
            ) from None
        except httpx.ConnectError as e:
            raise HTTPException(
                status_code=503, detail=f"Cannot connect to camera: {e}"
            ) from e
