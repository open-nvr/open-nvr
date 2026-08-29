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
The addresses MediaMTX advertises as WebRTC ICE host candidates.

Why this exists: MediaMTX runs on the Docker bridge, so ``webrtcIPsFromInterfaces``
can only ever gather the container's own addresses (``127.0.0.1`` and
``172.28.0.x``). A LAN browser cannot route to either, so unless something tells
MediaMTX the host's real address, the SDP answer carries no reachable candidate,
ICE never completes, and the UI silently falls back to HLS.

``MEDIAMTX_WEBRTC_HOSTS`` was that "something", but it is baked into the
container at *create* time, so any ``docker compose up -d`` run without
``start.sh``'s exports wiped it and broke live WebRTC with no error anywhere.

So the value lives in the database instead and is pushed to MediaMTX at
runtime, from the ``runOnInit`` startup hook that fires on every MediaMTX
start. Two consequences:

* Recreating the container no longer matters — the hook re-applies within
  seconds regardless of what the container's environment says.
* The set can be *learned*. ``learn()`` records the address a browser actually
  reached this server on, so a DHCP move repairs itself on the next page load.

Storage is its own ``SecuritySetting`` row rather than a field inside the
``webrtc`` row: ``PUT /webrtc/settings`` rewrites that row from a Pydantic
model, which would silently drop any key the schema does not declare and throw
away everything learned here.
"""

from __future__ import annotations

import ipaddress
import json
import logging

from core.client_ip import _in_nets, _internal_nets, _parse_cidrs
from core.config import settings
from models import SecuritySetting

logger = logging.getLogger(__name__)

SETTING_KEY = "webrtc_ice_hosts"

# Bounded so a host that legitimately changes address over months cannot grow
# the advertised candidate list without limit — every extra host is another
# candidate in every SDP answer, and ICE checks them all.
MAX_HOSTS = 8


class WebRTCIceHostService:
    """Owns ``webrtcAdditionalHosts``: what MediaMTX advertises to browsers."""

    # ---- validation -------------------------------------------------

    @staticmethod
    def is_advertisable(addr: str) -> bool:
        """Whether ``addr`` is worth advertising as an ICE host candidate.

        Rejects anything a browser on another machine could not use, and
        anything that would merely re-advertise what MediaMTX already gathers
        for itself: loopback, link-local, multicast, unspecified, and the
        Docker bridge networks (``internal_service_cidrs``, which pins the
        compose subnets).
        """
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
        # The bridge addresses are exactly what MediaMTX already offers and the
        # browser already cannot reach — re-adding them is pure noise.
        return not _in_nets(addr, _internal_nets())

    @staticmethod
    def is_trusted_proxy(peer: str) -> bool:
        """Whether ``peer`` (the socket peer) may set ``X-Server-Addr``.

        Same trust model as ``core.client_ip``: a header describing the
        network is believed only from the reverse proxy, never from an
        arbitrary client.
        """
        return bool(peer) and _in_nets(peer, _parse_cidrs(settings.trusted_proxy_cidrs))

    # ---- storage ----------------------------------------------------

    @staticmethod
    def load(db) -> list[str]:
        row = (
            db.query(SecuritySetting).filter(SecuritySetting.key == SETTING_KEY).first()
        )
        if not row:
            return []
        try:
            val = json.loads(row.json_value or "[]")
        except Exception:
            return []
        return [h for h in val if isinstance(h, str)] if isinstance(val, list) else []

    @staticmethod
    def store(db, hosts: list[str]) -> None:
        row = (
            db.query(SecuritySetting).filter(SecuritySetting.key == SETTING_KEY).first()
        )
        payload = json.dumps(hosts)
        if row:
            row.json_value = payload
        else:
            db.add(SecuritySetting(key=SETTING_KEY, json_value=payload))
        db.commit()

    # ---- resolution -------------------------------------------------

    @classmethod
    def resolve(cls, db) -> list[str]:
        """The hosts to advertise: everything learned, then the env seed.

        The environment value is a *seed*, not an override — it is what
        ``start.sh`` detected, which is right on a fresh install and stale
        after the host moves. Learned addresses come first so the most
        recently observed one is preferred.
        """
        hosts = cls.load(db)
        seed = (getattr(settings, "mediamtx_webrtc_hosts", "") or "").strip()
        for part in seed.split(","):
            part = part.strip()
            if part and part not in hosts and cls.is_advertisable(part):
                hosts.append(part)
        return hosts[:MAX_HOSTS]

    # ---- application ------------------------------------------------

    @classmethod
    async def apply_to_mediamtx(cls, db) -> bool:
        """Push the resolved host list to MediaMTX. Returns True if applied."""
        from services.mediamtx_admin_service import MediaMtxAdminService

        hosts = cls.resolve(db)
        if not hosts:
            # The failure this whole module exists to prevent, made loud: with
            # no advertisable host every WHEP session dies on a 10s ICE
            # timeout and the UI quietly degrades to HLS.
            logger.warning(
                "No WebRTC ICE host to advertise — MediaMTX will offer only "
                "container-local candidates and live WebRTC will fail (HLS "
                "still works). Set MEDIAMTX_WEBRTC_HOSTS in .env, or open Live "
                "View once so the address can be learned."
            )
            return False
        result = await MediaMtxAdminService.set_webrtc_additional_hosts(hosts)
        if result.get("status") == "ok":
            logger.info("Applied WebRTC ICE hosts to MediaMTX: %s", hosts)
            return True
        logger.warning("Could not apply WebRTC ICE hosts %s: %s", hosts, result)
        return False

    @classmethod
    async def learn(cls, db, addr: str) -> bool:
        """Record an address a browser genuinely reached this server on.

        No-op unless ``addr`` is advertisable and new. On a new address the
        list is persisted and pushed to MediaMTX immediately, so the repair
        takes effect for the WHEP request that follows this one.
        """
        if not cls.is_advertisable(addr):
            return False
        hosts = cls.load(db)
        if addr in hosts:
            return False
        # Most recent first, oldest evicted — a host that moves repeatedly
        # keeps working without the list growing forever.
        hosts = [addr] + [h for h in hosts if h != addr]
        cls.store(db, hosts[:MAX_HOSTS])
        logger.info("Learned new WebRTC ICE host %s", addr)
        await cls.apply_to_mediamtx(db)
        return True
