# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""The stack's egress proxy, for libraries that do not read the proxy
environment themselves.

An OpenNVR deployment runs apps on an internal network: the only way
to the LAN or the internet is the egress proxy the platform hands
every app as ``HTTP_PROXY`` / ``HTTPS_PROXY`` (with ``NO_PROXY`` for
the stack's own services). ``httpx``, ``requests``, ``urllib`` and
``aiohttp(trust_env=True)`` honour those on their own. Anything that
speaks plain TCP — an MQTT client, a database driver — needs to be
pointed at the proxy explicitly, as an HTTP CONNECT tunnel; these
helpers say where it is and which hosts must go direct.

The proxy allows what the app's catalog listing declared plus what
the operator allowed for this install; anything else is refused with
a 403 and shows up in the operator's inbox naming the host — see
docs/APP_NETWORK.md.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

__all__ = ["proxy_url", "proxy_address", "bypasses", "connect_via_proxy"]


def proxy_url(scheme: str = "https") -> str | None:
    """The proxy URL the platform set for ``scheme`` (``https`` by
    default, falling back to the HTTP one), or ``None`` when the app
    runs without one."""
    names = ([f"{scheme.upper()}_PROXY", f"{scheme.lower()}_proxy"]
             + (["HTTP_PROXY", "http_proxy"] if scheme.lower() != "http" else []))
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def proxy_address(scheme: str = "https") -> tuple[str, int] | None:
    """``(host, port)`` of the proxy, or ``None``."""
    url = proxy_url(scheme)
    if not url:
        return None
    parts = urlsplit(url if "://" in url else f"http://{url}")
    if not parts.hostname:
        return None
    return parts.hostname, parts.port or 3128


def bypasses(host: str) -> bool:
    """Whether ``host`` is on ``NO_PROXY`` (exact, or a domain suffix)
    and must be dialled directly — core, the buses, the proxy itself."""
    raw = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return False
    for entry in raw.split(","):
        e = entry.strip().lower()
        if not e:
            continue
        if e == "*":
            return True
        e = e.split(":", 1)[0]
        if e.startswith("."):
            e = e[1:]
        if h == e or h.endswith("." + e):
            return True
    return False


def connect_via_proxy(host: str, port: int) -> tuple[str, int] | None:
    """Where a plain-TCP client should tunnel to reach ``host:port``:
    the proxy address, or ``None`` to connect directly (no proxy set,
    or the host is on ``NO_PROXY``)."""
    if bypasses(host):
        return None
    return proxy_address("https")
