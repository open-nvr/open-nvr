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
Real client IP resolution — the single source of truth.

Behind nginx (and in Docker bridge mode), the socket peer is the proxy, not the
client, and the real address arrives in ``X-Forwarded-For``. Trusting that
header blindly is the classic IP-allowlist bypass — a client simply sends
``X-Forwarded-For: <allowed-ip>``. So the header is honored ONLY when the
immediate peer is a configured trusted proxy; otherwise the socket peer is used.

Every part of OpenNVR that needs a client IP (device firewall, audit log) must
call ``get_client_ip`` so the trust decision lives in exactly one place.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache

from starlette.requests import Request

from core.config import settings


@lru_cache(maxsize=1)
def _trusted_proxy_nets() -> tuple[ipaddress._BaseNetwork, ...]:
    return _parse_cidrs(settings.trusted_proxy_cidrs)


@lru_cache(maxsize=1)
def _internal_nets() -> tuple[ipaddress._BaseNetwork, ...]:
    return _parse_cidrs(settings.internal_service_cidrs)


def _parse_cidrs(raw: str) -> tuple[ipaddress._BaseNetwork, ...]:
    nets: list[ipaddress._BaseNetwork] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    return tuple(nets)


def _in_nets(ip: str, nets: tuple[ipaddress._BaseNetwork, ...]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in nets)


def get_client_ip(request: Request) -> str:
    """Best-effort real client IP.

    If the socket peer is a trusted proxy, take the left-most (original client)
    entry of ``X-Forwarded-For``; otherwise use the socket peer directly. Falls
    back to the peer if the header is malformed.
    """
    peer = request.client.host if request.client else ""
    if peer and _in_nets(peer, _trusted_proxy_nets()):
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            # left-most is the original client; strip any port suffix
            first = xff.split(",")[0].strip()
            first = first.rsplit(":", 1)[0] if first.count(":") == 1 else first
            try:
                ipaddress.ip_address(first)
                return first
            except ValueError:
                pass
        real = request.headers.get("x-real-ip", "").strip()
        if real:
            try:
                ipaddress.ip_address(real)
                return real
            except ValueError:
                pass
    return peer


def is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def is_internal_service(ip: str) -> bool:
    """True for the reverse proxy and sibling containers (MediaMTX, KAI-C, …),
    which must never be firewalled."""
    return is_loopback(ip) or _in_nets(ip, _internal_nets())
