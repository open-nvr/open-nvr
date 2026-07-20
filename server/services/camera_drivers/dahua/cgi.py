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
Dahua CGI HTTP primitive — Dahua's ``/cgi-bin/*.cgi`` device API over HTTP
Digest.

Self-contained per vendor (see docs/adding-a-camera-vendor.md): a thin ``httpx`` call returning
``(status_code, text)`` that raises only on transport failure (callers decide
what a non-200 means). Dahua replies are ``key=value`` text (one per line), not
XML — ``parse_kv`` turns a response body into a flat dict keyed by the full
dotted path (e.g. ``table.MotionDetect[0].Enable``).
"""

from __future__ import annotations

import httpx
from fastapi import HTTPException


async def dahua_request(
    ip: str,
    path: str,
    username: str | None = None,
    password: str | None = None,
    *,
    method: str = "GET",
    port: int = 80,
    params: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> tuple[int, str]:
    """Make a Dahua CGI request. ``path`` starts with ``/cgi-bin/...``.

    ``params`` (used for ``setConfig``) is passed through httpx so its bracketed
    keys (``VideoWidget[0].TimeTitle.EncodeBlend``) are encoded correctly.
    Returns ``(status_code, response_text)``; raises HTTPException only on
    transport failure (timeout / connection refused), matching the ISAPI
    primitive's contract so drivers handle both the same way.
    """
    url = f"http://{ip}:{port}{path}"
    auth = httpx.DigestAuth(username, password) if username else None
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.request(method, url, params=params, auth=auth)
            return resp.status_code, resp.text
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail=f"Dahua CGI timeout to {url}")
        except httpx.ConnectError as e:
            raise HTTPException(
                status_code=503, detail=f"Cannot connect to camera CGI: {e}"
            )


def parse_kv(text: str) -> dict[str, str]:
    """Parse a Dahua ``key=value`` response body into a flat dict.

    Keys keep their full dotted/bracketed path exactly as the camera sends them
    (``table.Network.eth0.IPAddress``); blank lines and lines without ``=`` are
    skipped. Values are returned verbatim (stripped).
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out
