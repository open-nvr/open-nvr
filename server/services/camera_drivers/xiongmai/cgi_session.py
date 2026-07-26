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
Xiongmai AppWeb2.0 HTTPS web-CGI session — the ``/CGI/`` API behind the newer XM
web UI, used as a fallback when the Sofia binary port (34567) is disabled.

Auth is a session handshake (NOT HTTP Digest), verified live against a Sparsh
SC-INA50B (an XM rebadge):

    GET  /CGI/Security/sessionLogin      -> { salt, sessionID }
         Password = sofia_hash( sofia_hash(pw) + salt )     # XM folded MD5, twice
    POST /CGI/Security/SelfExt/userCheck -> { sessionID }   # the live token
         then cookie  webSessionID=<sessionID>  on every /CGI/ call.

The web app is HTTPS-only with a self-signed cert (``verify=False``). Sessions
are cached; logins are never retried in a loop (XM lockout).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from .sofia import sofia_hash


class CgiError(Exception):
    """Any XM web-CGI transport or auth failure."""


class CgiSession:
    """A single logged-in web-CGI session (holds the ``webSessionID`` cookie)."""

    def __init__(self, ip: str, username: str, password: str, scheme: str = "https"):
        self.ip = ip
        self.username = username or ""
        self.password = password or ""
        self.scheme = scheme
        self.session_id: str | None = None

    @property
    def _base(self) -> str:
        return f"{self.scheme}://{self.ip}"

    async def login(self, timeout: float = 10.0) -> None:
        headers = {"If-Modified-Since": "0"}
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            r1 = await client.get(
                f"{self._base}/CGI/Security/sessionLogin?timestamp:1", headers=headers
            )
            s1 = _loads(r1.text)
            salt = s1.get("salt")
            pre_sid = s1.get("sessionID")
            if not salt:
                raise CgiError("no salt in sessionLogin response")
            password = sofia_hash(sofia_hash(self.password) + salt)
            r2 = await client.post(
                f"{self._base}/CGI/Security/SelfExt/userCheck",
                headers={**headers, "Content-Type": "application/json"},
                cookies={"webSessionID": pre_sid} if pre_sid else None,
                content=json.dumps(
                    {
                        "Username": self.username,
                        "Password": password,
                        "Sessionid": pre_sid,
                    }
                ).encode(),
            )
            a = _loads(r2.text)
            if str(a.get("statusValue")) != "200" or not a.get("sessionID"):
                raise CgiError(f"userCheck failed: statusValue={a.get('statusValue')}")
            self.session_id = a["sessionID"]

    async def get(self, path: str, timeout: float = 10.0) -> tuple[int, str]:
        if not self.session_id:
            raise CgiError("not logged in")
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            r = await client.get(
                f"{self._base}{path}",
                headers={"If-Modified-Since": "0"},
                cookies={"webSessionID": self.session_id},
            )
            return r.status_code, r.text

    async def post_json(
        self, path: str, body: dict[str, Any], timeout: float = 10.0
    ) -> tuple[int, str]:
        if not self.session_id:
            raise CgiError("not logged in")
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            r = await client.post(
                f"{self._base}{path}",
                headers={"If-Modified-Since": "0", "Content-Type": "application/json"},
                cookies={"webSessionID": self.session_id},
                content=json.dumps(body).encode(),
            )
            return r.status_code, r.text


def _loads(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}


# --- session cache ---------------------------------------------------------

_SESSIONS: dict[tuple[str, str], CgiSession] = {}
_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


def _lock(key: tuple[str, str]) -> asyncio.Lock:
    lk = _LOCKS.get(key)
    if lk is None:
        lk = _LOCKS[key] = asyncio.Lock()
    return lk


async def get_session(
    ip: str, username: str, password: str, scheme: str = "https"
) -> CgiSession:
    """Return a logged-in, cached CgiSession (one login under a lock)."""
    key = (ip, username or "")
    async with _lock(key):
        sess = _SESSIONS.get(key)
        if sess is not None and sess.session_id:
            return sess
        sess = CgiSession(ip, username, password, scheme)
        await sess.login()
        _SESSIONS[key] = sess
        return sess


def drop_session(ip: str, username: str) -> None:
    _SESSIONS.pop((ip, username or ""), None)


async def probe(
    ip: str, scheme: str = "https", timeout: float = 5.0
) -> bool:
    """Fingerprint XM AppWeb2.0 WITHOUT credentials (no lockout risk): the
    ``sessionLogin`` endpoint returns a salt unauthenticated. Returns False on
    any failure."""
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            r = await client.get(
                f"{scheme}://{ip}/CGI/Security/sessionLogin?timestamp:1",
                headers={"If-Modified-Since": "0"},
            )
        j = _loads(r.text)
        return bool(j.get("salt")) and str(j.get("statusValue")) == "200"
    except Exception:
        return False
