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
The addresses MediaMTX advertises as WebRTC ICE host candidates (issue #925).

MediaMTX runs on the Docker bridge, so left to itself it can only gather the
container's own addresses (127.0.0.1 and 172.28.0.x), which no browser on
another machine can reach. Something has to tell it the addresses viewers
can use, on the ICE port. That list is built here from three sources, in
this order:

1. The admin's public address (Settings → WebRTC): an IP or a DNS name.
   MediaMTX resolves names itself, so a DDNS address keeps working.
2. The public IP found with STUN. Core asks the configured STUN servers from
   inside its container, which works: the answer is the router's address.
   Only the IP is kept; the port STUN reports is a random NAT mapping, not
   the forwarded ICE port, which is also why MediaMTX's own STUN gathering
   cannot do this job (its reflexive candidate lands on a random socket).
3. The LAN address ``start.sh`` detected at ``up`` (MEDIAMTX_WEBRTC_HOSTS).
   Core cannot see the host's interfaces, so it cannot refresh this one;
   the browser covers a moved LAN address by adding the address it opened
   the page on (app/src/lib/liveTransport.ts).

The list is pushed to MediaMTX from the ``runOnInit`` startup hook on every
MediaMTX start (so a recreated container keeps it), when the settings are
saved, and whenever the STUN answer changes.

An earlier version "learned" addresses from nginx's ``X-Server-Addr``. In
the stock layout nginx sits on the bridge too, so it never learned anything;
``migrate_legacy`` moves whatever an operator put in that row by hand into
the admin's public address, once.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import socket
import struct
import time
from dataclasses import dataclass
from typing import Any

from core.client_ip import _in_nets, _internal_nets
from core.config import settings
from models import SecuritySetting

logger = logging.getLogger(__name__)

#: The settings row the WebRTC page writes (routers/webrtc.py).
WEBRTC_ROW_KEY = "webrtc"
#: The learned-hosts row of the earlier design; read once for migration.
SETTING_KEY = "webrtc_ice_hosts"

#: Every extra host is another candidate in every SDP answer, and ICE checks
#: them all, so the list stays short.
MAX_HOSTS = 8

STUN_DEFAULT_PORT = 3478
STUN_TIMEOUT_S = 2.0
#: How often the public IP is asked for again. An ISP change is picked up
#: within this; a viewer whose WebRTC fails meanwhile gets HLS, not an error.
STUN_REFRESH_S = 600
#: A DNS name in the admin's address is resolved for the browser's own
#: candidates (rtc-config) at most this often.
RESOLVE_CACHE_S = 300

_STUN_MAGIC = 0x2112A442
_STUN_BINDING_REQUEST = 0x0001
_STUN_BINDING_SUCCESS = 0x0101
_ATTR_MAPPED = 0x0001
_ATTR_XOR_MAPPED = 0x0020


@dataclass
class PublicIP:
    ip: str | None = None
    at: float = 0.0
    server: str | None = None


class WebRTCIceHostService:
    """Owns ``webrtcAdditionalHosts``: what MediaMTX advertises to browsers."""

    _public = PublicIP()
    _refresher: asyncio.Task | None = None
    #: The one-off round a settings save asks for (refresh_soon); kept so the
    #: task is not garbage-collected mid-flight.
    _pending_refresh: asyncio.Task | None = None
    _resolved: dict[str, tuple[float, list[str]]] = {}

    # ---- validation -------------------------------------------------

    @staticmethod
    def is_advertisable(addr: str) -> bool:
        """Whether ``addr`` (an IP) is worth advertising as an ICE host
        candidate: not loopback, link-local, multicast, unspecified, and not
        a Docker bridge address, which MediaMTX already offers and the
        browser already cannot reach."""
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if (
            ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_unspecified
            or ip.is_reserved
        ):
            return False
        return not _in_nets(addr, _internal_nets())

    @staticmethod
    def is_hostname(value: str) -> bool:
        """A DNS name (not an IP literal) MediaMTX can resolve."""
        if not value or len(value) > 253 or value.lower() == "localhost":
            return False
        try:
            ipaddress.ip_address(value)
            return False
        except ValueError:
            pass
        labels = value.rstrip(".").split(".")
        return all(
            0 < len(lb) <= 63 and lb.replace("-", "").isalnum() and not lb.startswith("-") and not lb.endswith("-")
            for lb in labels
        )

    # ---- the WebRTC settings row ------------------------------------

    @staticmethod
    def _webrtc_row_value(db) -> dict[str, Any]:
        row = db.query(SecuritySetting).filter(SecuritySetting.key == WEBRTC_ROW_KEY).first()
        if not row:
            return {}
        try:
            val = json.loads(row.json_value or "{}")
        except Exception:
            return {}
        return val if isinstance(val, dict) else {}

    @classmethod
    def admin_address(cls, db) -> str | None:
        val = (cls._webrtc_row_value(db).get("public_address") or "").strip()
        if not val:
            return None
        if cls.is_hostname(val) or cls.is_advertisable(val):
            return val
        return None

    @classmethod
    def public_ice_port(cls, db) -> int:
        port = cls._webrtc_row_value(db).get("public_ice_port")
        if isinstance(port, int) and 0 < port < 65536:
            return port
        return int(getattr(settings, "webrtc_ice_port", 8189) or 8189)

    @classmethod
    def stun_servers(cls, db) -> list[str]:
        from schemas import WebRTCSettings as WebRTCSettingsSchema

        val = cls._webrtc_row_value(db)
        raw = val.get("stun_servers") if "stun_servers" in val else WebRTCSettingsSchema().stun_servers
        return [s.strip() for s in (raw or []) if isinstance(s, str) and s.strip()]

    @classmethod
    def set_admin_address(cls, db, address: str | None) -> None:
        row = db.query(SecuritySetting).filter(SecuritySetting.key == WEBRTC_ROW_KEY).first()
        val = cls._webrtc_row_value(db)
        val["public_address"] = address
        payload = json.dumps(val)
        if row:
            row.json_value = payload
        else:
            db.add(SecuritySetting(key=WEBRTC_ROW_KEY, json_value=payload))
        db.commit()

    # ---- STUN --------------------------------------------------------

    @staticmethod
    def parse_stun_url(url: str) -> tuple[str, int] | None:
        """``stun:host[:port]`` → (host, port). TURN entries are not STUN
        servers for this purpose and give None."""
        u = url.strip()
        if u.startswith("stun:"):
            u = u[5:]
        elif u.startswith("stuns:"):
            return None  # STUN over TLS is not a UDP binding
        else:
            return None
        u = u.split("?", 1)[0].strip()
        if not u:
            return None
        if u.startswith("["):
            host, _, rest = u[1:].partition("]")
            port = rest[1:] if rest.startswith(":") else ""
        else:
            host, _, port = u.rpartition(":") if u.count(":") == 1 else (u, "", "")
        try:
            return host, int(port) if port else STUN_DEFAULT_PORT
        except ValueError:
            return None

    @staticmethod
    def parse_stun_response(data: bytes, txid: bytes) -> str | None:
        """The mapped address out of a Binding Success response (RFC 5389),
        XOR-MAPPED-ADDRESS first, MAPPED-ADDRESS as the fallback."""
        if len(data) < 20:
            return None
        mtype, mlen, cookie = struct.unpack("!HHI", data[:8])
        if mtype != _STUN_BINDING_SUCCESS or cookie != _STUN_MAGIC or data[8:20] != txid:
            return None
        mapped: str | None = None
        i = 20
        end = min(len(data), 20 + mlen)
        while i + 4 <= end:
            atype, alen = struct.unpack("!HH", data[i : i + 4])
            value = data[i + 4 : i + 4 + alen]
            i += 4 + alen + ((4 - alen % 4) % 4)
            if atype not in (_ATTR_MAPPED, _ATTR_XOR_MAPPED) or len(value) < 8:
                continue
            family = value[1]
            raw = value[4:8] if family == 0x01 else value[4:20]
            if atype == _ATTR_XOR_MAPPED:
                key = struct.pack("!I", _STUN_MAGIC) + (txid if family != 0x01 else b"")
                raw = bytes(a ^ b for a, b in zip(raw, key))
            try:
                addr = str(ipaddress.ip_address(raw))
            except ValueError:
                continue
            if atype == _ATTR_XOR_MAPPED:
                return addr
            mapped = mapped or addr
        return mapped

    @classmethod
    async def _stun_query(cls, host: str, port: int, timeout: float) -> str | None:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
        if not infos:
            return None
        family, _, _, _, sockaddr = infos[0]
        txid = os.urandom(12)
        request = struct.pack("!HHI", _STUN_BINDING_REQUEST, 0, _STUN_MAGIC) + txid
        reply: asyncio.Future[bytes] = loop.create_future()

        class _Proto(asyncio.DatagramProtocol):
            def connection_made(self, transport):  # noqa: D401
                transport.sendto(request)

            def datagram_received(self, data, _addr):
                if not reply.done():
                    reply.set_result(data)

            def error_received(self, exc):
                if not reply.done():
                    reply.set_exception(exc)

        transport, _ = await loop.create_datagram_endpoint(
            _Proto, remote_addr=sockaddr[:2], family=family
        )
        try:
            data = await asyncio.wait_for(reply, timeout)
        finally:
            transport.close()
        return cls.parse_stun_response(data, txid)

    @classmethod
    async def discover_public_ip(
        cls, servers: list[str], timeout: float = STUN_TIMEOUT_S
    ) -> tuple[str, str] | None:
        """Ask each STUN server in turn; the first usable answer wins."""
        for url in servers:
            parsed = cls.parse_stun_url(url)
            if not parsed:
                continue
            try:
                ip = await cls._stun_query(parsed[0], parsed[1], timeout)
            except Exception as e:  # noqa: BLE001 - one server down is not an error
                logger.debug("STUN %s failed: %s", url, e)
                continue
            if ip and cls.is_advertisable(ip):
                return ip, url
        return None

    @classmethod
    async def refresh_public_ip(cls, db, force: bool = False) -> str | None:
        """The STUN-discovered public IP, asked for again when stale.

        A round that gets no answer keeps the last address: one lost UDP
        exchange or a STUN server having a bad minute must not take the
        public candidate away from every remote viewer for the next
        refresh interval. The address goes only when the admin removes the
        STUN servers, or when a server answers with a different one.
        """
        now = time.monotonic()
        if not force and cls._public.at and now - cls._public.at < STUN_REFRESH_S:
            return cls._public.ip
        servers = cls.stun_servers(db)
        previous = cls._public
        if not servers:
            cls._public = PublicIP(None, now, None)
        else:
            found = await cls.discover_public_ip(servers)
            if found:
                cls._public = PublicIP(found[0], now, found[1])
            else:
                cls._public = PublicIP(previous.ip, now, previous.server)
                if previous.ip:
                    logger.info(
                        "STUN gave no answer; keeping the public IP %s for WebRTC", previous.ip
                    )
        if cls._public.ip != previous.ip:
            logger.info("Public IP for WebRTC (STUN): %s", cls._public.ip or "none")
        return cls._public.ip

    @classmethod
    def public_ip(cls) -> PublicIP:
        return cls._public

    # ---- resolution -------------------------------------------------

    @classmethod
    def resolve_entries(cls, db) -> list[dict[str, str]]:
        """What MediaMTX advertises, with where each entry came from:
        the admin's address, then STUN, then the launcher's LAN seed."""
        out: list[dict[str, str]] = []
        seen: set[str] = set()

        def add(host: str | None, source: str) -> None:
            if host and host not in seen:
                seen.add(host)
                out.append({"host": host, "source": source})

        add(cls.admin_address(db), "admin")
        add(cls._public.ip, "stun")
        seed = (getattr(settings, "mediamtx_webrtc_hosts", "") or "").strip()
        for part in seed.split(","):
            part = part.strip()
            if part and cls.is_advertisable(part):
                add(part, "env")
        return out[:MAX_HOSTS]

    @classmethod
    def resolve(cls, db) -> list[str]:
        return [e["host"] for e in cls.resolve_entries(db)]

    @classmethod
    async def resolved_ips(cls, db) -> list[str]:
        """The advertised list as IP literals, for the browser's own
        candidates: a DNS name is resolved here (cached), never by the
        browser, which does not do it reliably."""
        out: list[str] = []
        for host in cls.resolve(db):
            if cls.is_hostname(host):
                out.extend(await cls._lookup(host))
            else:
                out.append(host)
        return list(dict.fromkeys(out))

    @classmethod
    async def _lookup(cls, host: str) -> list[str]:
        now = time.monotonic()
        cached = cls._resolved.get(host)
        if cached and now - cached[0] < RESOLVE_CACHE_S:
            return cached[1]
        ips: list[str] = []
        try:
            loop = asyncio.get_running_loop()
            infos = await asyncio.wait_for(loop.getaddrinfo(host, None), 2.0)
            ips = [i[4][0] for i in infos if cls.is_advertisable(i[4][0])]
        except Exception as e:  # noqa: BLE001 - an unresolvable name just adds nothing
            logger.debug("Could not resolve %s: %s", host, e)
        ips = list(dict.fromkeys(ips))
        cls._resolved[host] = (now, ips)
        return ips

    # ---- migration ---------------------------------------------------

    @classmethod
    def migrate_legacy(cls, db) -> str | None:
        """Move a hand-made ``webrtc_ice_hosts`` row into the admin's public
        address (first entry that is not the LAN seed), then drop the row.
        Returns the address moved, if any."""
        row = db.query(SecuritySetting).filter(SecuritySetting.key == SETTING_KEY).first()
        if not row:
            return None
        try:
            legacy = json.loads(row.json_value or "[]")
        except Exception:
            legacy = []
        seed = {p.strip() for p in (getattr(settings, "mediamtx_webrtc_hosts", "") or "").split(",")}
        moved: str | None = None
        if not cls.admin_address(db):
            for h in legacy if isinstance(legacy, list) else []:
                if isinstance(h, str) and h not in seed and (cls.is_advertisable(h) or cls.is_hostname(h)):
                    moved = h
                    break
            if moved:
                cls.set_admin_address(db, moved)
                logger.info("Moved legacy WebRTC host %s into the public address setting", moved)
        db.delete(row)
        db.commit()
        return moved

    # ---- application ------------------------------------------------

    @classmethod
    async def apply_to_mediamtx(cls, db, stun: bool = True) -> bool:
        """Build the list and push it to MediaMTX. Returns True if applied.

        ``stun=False`` pushes what is known now without waiting on a STUN
        round (each unreachable server costs STUN_TIMEOUT_S): the settings
        save uses it and asks for the round in the background."""
        from services.mediamtx_admin_service import MediaMtxAdminService

        try:
            cls.migrate_legacy(db)
        except Exception as e:  # noqa: BLE001 - never block advertising on it
            logger.warning("Legacy WebRTC host migration failed: %s", e)
        if stun:
            try:
                await cls.refresh_public_ip(db)
            except Exception as e:  # noqa: BLE001
                logger.debug("STUN lookup failed: %s", e)
        cls.ensure_refresher()

        hosts = cls.resolve(db)
        if not hosts:
            # Made loud: with no advertisable host every WHEP session from
            # another machine dies on the ICE timeout, and the UI quietly
            # takes the HLS rung. The empty list is still pushed: an address
            # the admin just cleared must stop being advertised.
            logger.warning(
                "No WebRTC ICE host to advertise — live WebRTC from other machines "
                "will fall back to HLS. Set a public address under Settings → WebRTC, "
                "configure a STUN server there, or set MEDIAMTX_WEBRTC_HOSTS in .env "
                "to this server's LAN IP (start.sh does this for you)."
            )
        result = await MediaMtxAdminService.set_webrtc_additional_hosts(hosts)
        if result.get("status") == "ok":
            logger.info("Applied WebRTC ICE hosts to MediaMTX: %s", hosts)
            return True
        logger.warning("Could not apply WebRTC ICE hosts %s: %s", hosts, result)
        return False

    @classmethod
    def ensure_refresher(cls) -> None:
        """A background loop that re-asks STUN and re-applies the list when
        the public IP changes. Started once, from the first apply."""
        if cls._refresher and not cls._refresher.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        cls._refresher = loop.create_task(cls._refresh_loop())

    @classmethod
    async def refresh_once(cls) -> None:
        """One forced STUN round on a session of its own; the list is pushed
        again only when the public IP changed."""
        from core.database import SessionLocal
        from services.mediamtx_admin_service import MediaMtxAdminService

        before = cls._public.ip
        with SessionLocal() as db:
            await cls.refresh_public_ip(db, force=True)
            if cls._public.ip != before:
                hosts = cls.resolve(db)
                if hosts:
                    await MediaMtxAdminService.set_webrtc_additional_hosts(hosts)
                    logger.info("Re-applied WebRTC ICE hosts after public IP change: %s", hosts)

    @classmethod
    def refresh_soon(cls) -> None:
        """Run ``refresh_once`` in the background: the settings save answers
        at once and the STUN round, seconds long when a server is down,
        catches up behind it."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _run() -> None:
            try:
                await cls.refresh_once()
            except Exception as e:  # best effort
                logger.debug("WebRTC host refresh failed: %s", e)

        cls._pending_refresh = loop.create_task(_run())

    @classmethod
    async def _refresh_loop(cls) -> None:
        while True:
            await asyncio.sleep(STUN_REFRESH_S)
            try:
                await cls.refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - keep the loop alive
                logger.debug("WebRTC host refresh failed: %s", e)
