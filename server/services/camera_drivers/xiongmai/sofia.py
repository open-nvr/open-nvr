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
Xiongmai "Sofia" / DVRIP transport — the vendor's native binary control protocol
on TCP 34567, spoken by virtually every Xiongmai OEM board (and its countless
rebadges: Sparsh, and many generic brands).

Wire format (20-byte little-endian header + null-terminated JSON payload):

    0xff 0x00 00 00 | session(u32) | sequence(u32) | 00 00 | msgid(u16) | len(u32)

Login proves the password with Xiongmai's custom hash (``sofia_hash``): a full
MD5 folded into 8 ``[0-9A-Za-z]`` chars — NOT a standard hex digest. This module
owns the transport only; the driver maps it to OpenNVR's capability model.

Lockout note: Xiongmai locks the admin account after a few *failed* logins, so
sessions are cached and reused, logins are never retried in a tight loop, and the
device fingerprint (``probe``) sends NO credentials at all.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import struct
from typing import Any

# DVRIP message ids (request codes; the device replies with req+1).
LOGIN_REQ = 1000
KEEPALIVE_REQ = 1006
SYSINFO_REQ = 1020
CONFIG_SET_REQ = 1040
CONFIG_GET_REQ = 1042
ABILITY_GET_REQ = 1360
SYSMANAGER_REQ = 1450

_HEADER = struct.Struct("<BB2xII2xHI")  # 0xff,0x00,-,session,seq,-,msgid,datalen


def sofia_hash(password: str) -> str:
    """Xiongmai's password hash: MD5 → fold 16 bytes to 8 ``[0-9A-Za-z]`` chars.

    ``c_i = (md5[2i] + md5[2i+1]) % 62`` mapped 0-9→'0'-'9', 10-35→'A'-'Z',
    36-61→'a'-'z'. This is the exact algorithm the device's own ``MD5.js`` uses,
    and it is NOT a standard hex digest.
    """
    digest = hashlib.md5(password.encode("utf-8")).digest()
    out = []
    for i in range(8):
        v = (digest[2 * i] + digest[2 * i + 1]) % 62
        if v <= 9:
            out.append(chr(v + 48))       # '0'-'9'
        elif v <= 35:
            out.append(chr(v + 55))       # 'A'-'Z'
        else:
            out.append(chr(v + 61))       # 'a'-'z'
    return "".join(out)


def _pack(session: int, seq: int, msgid: int, payload: bytes) -> bytes:
    return _HEADER.pack(0xFF, 0x00, session, seq, msgid, len(payload)) + payload


def _payload(obj: dict[str, Any]) -> bytes:
    # DVRIP payloads are JSON terminated with newline + NUL.
    return json.dumps(obj).encode("utf-8") + b"\x0a\x00"


class SofiaError(Exception):
    """Any Sofia/DVRIP transport or protocol failure."""


class SofiaSession:
    """A single authenticated DVRIP connection. Not thread-safe; callers use the
    module-level ``get_session`` cache + lock."""

    def __init__(self, ip: str, username: str, password: str, port: int = 34567):
        self.ip = ip
        self.username = username or ""
        self.password = password or ""
        self.port = port
        self.session = 0
        self.seq = 0
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def _open(self, timeout: float) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.ip, self.port), timeout
        )

    async def _send(self, msgid: int, obj: dict[str, Any]) -> None:
        assert self._writer is not None
        self._writer.write(_pack(self.session, self.seq, msgid, _payload(obj)))
        await self._writer.drain()
        self.seq += 1

    async def _recv(self, timeout: float) -> dict[str, Any]:
        assert self._reader is not None
        head = await asyncio.wait_for(self._reader.readexactly(20), timeout)
        if head[0] != 0xFF:
            raise SofiaError("not a DVRIP frame")
        _, _, session, _seq, _msgid, dlen = _HEADER.unpack(head)
        self.session = session or self.session
        body = await asyncio.wait_for(self._reader.readexactly(dlen), timeout) if dlen else b""
        body = body.rstrip(b"\x00\x0a")
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8", "replace"))
        except json.JSONDecodeError as e:
            raise SofiaError(f"bad JSON: {e}") from e

    async def login(self, timeout: float = 6.0) -> None:
        await self._open(timeout)
        await self._send(
            LOGIN_REQ,
            {
                "EncryptType": "MD5",
                "LoginType": "DVRIP-Web",
                "UserName": self.username,
                "PassWord": sofia_hash(self.password),
            },
        )
        resp = await self._recv(timeout)
        # Ret 100 = OK. Session id comes back as a hex string ("0x0000000c").
        if resp.get("Ret") not in (100, 515) or "SessionID" not in resp:
            raise SofiaError(f"login rejected: Ret={resp.get('Ret')}")
        try:
            self.session = int(str(resp["SessionID"]), 16)
        except (TypeError, ValueError):
            self.session = 0

    async def command(
        self, msgid: int, obj: dict[str, Any], timeout: float = 6.0
    ) -> dict[str, Any]:
        obj = {**obj, "SessionID": f"0x{self.session:08X}"}
        await self._send(msgid, obj)
        return await self._recv(timeout)

    async def get_config(self, name: str, timeout: float = 6.0) -> dict[str, Any]:
        return await self.command(CONFIG_GET_REQ, {"Name": name}, timeout)

    async def set_config(
        self, name: str, value: Any, timeout: float = 6.0
    ) -> dict[str, Any]:
        return await self.command(CONFIG_SET_REQ, {"Name": name, name: value}, timeout)

    async def system_info(self, timeout: float = 6.0) -> dict[str, Any]:
        return await self.command(SYSINFO_REQ, {"Name": "SystemInfo"}, timeout)

    async def reboot(self, timeout: float = 6.0) -> dict[str, Any]:
        return await self.command(
            SYSMANAGER_REQ,
            {"Name": "OPMachine", "OPMachine": {"Action": "Reboot"}},
            timeout,
        )

    async def close(self) -> None:
        w = self._writer
        self._reader = self._writer = None
        if w is not None:
            try:
                w.close()
                await asyncio.wait_for(w.wait_closed(), 2.0)
            except Exception:
                pass


# --- session cache (avoid re-login hammering / lockout) --------------------

# key: (ip, port, username) -> SofiaSession
_SESSIONS: dict[tuple[str, int, str], SofiaSession] = {}
_LOCKS: dict[tuple[str, int, str], asyncio.Lock] = {}


def _lock(key: tuple[str, int, str]) -> asyncio.Lock:
    lk = _LOCKS.get(key)
    if lk is None:
        lk = _LOCKS[key] = asyncio.Lock()
    return lk


async def get_session(
    ip: str, username: str, password: str, port: int = 34567
) -> SofiaSession:
    """Return a logged-in, cached SofiaSession, logging in once under a lock.

    On failure the cache entry is dropped and the error propagates — callers
    treat a Sofia failure as "native layer unavailable" and fall back to ONVIF.
    """
    key = (ip, port, username or "")
    async with _lock(key):
        sess = _SESSIONS.get(key)
        if sess is not None:
            return sess
        sess = SofiaSession(ip, username, password, port)
        await sess.login()
        _SESSIONS[key] = sess
        return sess


async def drop_session(ip: str, username: str, port: int = 34567) -> None:
    key = (ip, port, username or "")
    sess = _SESSIONS.pop(key, None)
    if sess is not None:
        await sess.close()


async def probe(ip: str, port: int = 34567, timeout: float = 2.0) -> bool:
    """Fingerprint a Xiongmai board WITHOUT sending credentials (no lockout risk).

    Opens TCP 34567 and sends a session-0 ABILITY_GET; a Sofia server answers
    with a DVRIP-framed response (first byte 0xff) even when not logged in, which
    a non-Xiongmai host never does. Returns False on any failure.
    """
    reader = writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout
        )
        writer.write(_pack(0, 0, ABILITY_GET_REQ, _payload({"Name": "General"})))
        await asyncio.wait_for(writer.drain(), timeout)
        head = await asyncio.wait_for(reader.readexactly(1), timeout)
        return head == b"\xff"
    except Exception:
        return False
    finally:
        if writer is not None:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), 1.0)
            except Exception:
                pass
