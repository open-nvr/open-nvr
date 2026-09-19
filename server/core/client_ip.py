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


def _parse_hop(raw: str) -> str | None:
    """One X-Forwarded-For entry as a bare IP, or None if it is not one."""
    hop = raw.strip()
    if hop.startswith("[") and "]" in hop:          # [v6]:port
        hop = hop[1:hop.index("]")]
    elif hop.count(":") == 1:                       # v4:port
        hop = hop.rsplit(":", 1)[0]
    try:
        return str(ipaddress.ip_address(hop))
    except ValueError:
        return None


def get_client_ip(request: Request) -> str:
    """Best-effort real client IP, safe against a forged X-Forwarded-For.

    Only a request whose socket peer is a trusted proxy may speak through
    X-Forwarded-For at all. The header is then read from the RIGHT: each
    proxy APPENDS the address it saw (nginx: ``$proxy_add_x_forwarded_for``),
    so the right-most entries were written by our own proxies and the
    left-most by whoever sent the request. Walking right to left, trusted
    proxy hops are skipped and the first other address is the client.

    The old rule took the left-most entry, which the client chooses: sending
    ``X-Forwarded-For: 127.0.0.1`` through nginx arrived as ``"127.0.0.1,
    <real ip>"`` and resolved to loopback, which the device firewall exempts.

    If every hop is trusted (the client itself sits inside a trusted range),
    the right-most hop is returned: the address our own proxy actually saw.
    ``X-Real-IP`` (nginx overwrites it with ``$remote_addr``) is the fallback
    when X-Forwarded-For is absent or unusable.
    """
    peer = request.client.host if request.client else ""
    if not (peer and _in_nets(peer, _trusted_proxy_nets())):
        return peer
    hops = [_parse_hop(h) for h in request.headers.get("x-forwarded-for", "").split(",")
            if h.strip()]
    trusted = _trusted_proxy_nets()
    nearest_valid: str | None = None
    for hop in reversed(hops):
        if hop is None:
            # A malformed hop: nothing to its left can be trusted either.
            break
        nearest_valid = nearest_valid or hop
        if not _in_nets(hop, trusted):
            return hop
    if nearest_valid:
        return nearest_valid
    real = _parse_hop(request.headers.get("x-real-ip", ""))
    return real or peer


def is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def is_internal_service(ip: str) -> bool:
    """True for the reverse proxy and sibling containers (MediaMTX, KAI-C, …),
    which must never be firewalled."""
    return is_loopback(ip) or _in_nets(ip, _internal_nets())
