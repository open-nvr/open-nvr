# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Sovereignty enforcement for AI adapters (V-022).

Beyond loopback, checks the adapter's declared ``permissions.network_egress``:

* ``local_only``    — URL must be on this machine AND every declared
                       network_egress host must ALSO be on this machine
                       (loopback, the Docker bridge subnet, or the container
                       runtime's host alias ``host.docker.internal``). An
                       adapter that proxies to a host-local runtime — the
                       ollamavlm adapter calling the operator's own Ollama —
                       keeps the AI on this box and passes; anything pointing
                       off-machine is a cloud/LAN proxy → refused.
* ``federated``     — network_egress may be non-empty but must enumerate every
                       host explicitly; wildcards are refused.
* ``cloud_allowed`` — no checks.

Runs at registration and on every /capabilities poll, so an adapter that adds
egress at runtime is de-registered.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
from urllib.parse import urlparse

from kai_c.contract_types import CapabilitiesResponse, Permissions

logger = logging.getLogger(__name__)


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

VALID_SOVEREIGNTY_MODES = frozenset({"local_only", "federated", "cloud_allowed"})

# "On this machine" = loopback + the operator's own Docker bridge subnet
# (bridge traffic stays in-kernel). Operator-configurable for non-standard
# bridge ranges. See V-022 and DESIGN_NOTES: KAI-C sovereignty & the Docker bridge.
_DOCKER_BRIDGE_SUBNET = os.getenv("OPENNVR_DOCKER_SUBNET", "172.28.0.0/16")

# The container runtime's aliases for the DOCKER HOST itself. By definition
# these point at the machine running Docker — the same box KAI-C runs on — so
# for sovereignty purposes they are "this machine" even though they resolve
# OUTSIDE the bridge subnet (Docker Desktop hands out e.g. 192.168.65.x).
# This is what lets an adapter proxy to a host-local runtime (the operator's
# own Ollama on host.docker.internal) under local_only.
_HOST_ALIAS_NAMES = frozenset({"host.docker.internal", "gateway.docker.internal"})


class SovereigntyViolation(Exception):
    """Raised when an adapter (or its declared egress) violates the
    active sovereignty policy. The string form is operator-facing."""


def host_is_loopback(host: str | None) -> bool:
    """Same logic as the legacy main.py helper, kept here so callers
    don't need to reach into main.py to use it."""
    if not host:
        return False
    h = host.strip("[]").lower()
    if h in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        pass
    saved = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(2.0)
        try:
            infos = socket.getaddrinfo(h, None)
        except (socket.gaierror, socket.timeout, OSError):
            return False
    finally:
        socket.setdefaulttimeout(saved)
    return bool(infos) and all(
        ipaddress.ip_address(info[4][0]).is_loopback for info in infos
    )


def _docker_bridge_net() -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    try:
        return ipaddress.ip_network(_DOCKER_BRIDGE_SUBNET)
    except (ValueError, TypeError):
        logger.warning(
            "OPENNVR_DOCKER_SUBNET=%r is not a valid network; ignoring it "
            "for sovereignty checks.",
            _DOCKER_BRIDGE_SUBNET,
        )
        return None


def host_is_on_this_machine(host: str | None) -> bool:
    """V-022 sovereignty-local host check.

    Returns True when ``host`` is on the same physical machine as KAI-C,
    which for sovereignty purposes means either:

      * a loopback host/IP (``localhost``, ``127.0.0.1``, ``::1``, or
        anything resolving to ``is_loopback``); or
      * a host/IP inside the operator's own Docker bridge subnet
        (``OPENNVR_DOCKER_SUBNET``, default ``172.28.0.0/16``) — traffic
        between bridge-network containers stays inside this host's kernel
        networking stack, so it never leaves the box; or
      * the container runtime's host aliases (``host.docker.internal`` /
        ``gateway.docker.internal``) — by definition the machine running
        Docker, even though they resolve outside the bridge subnet.

    Everything else is rejected, including non-bridge RFC1918 / ULA / LAN
    addresses (those are peer hosts on the same LAN, which V-022
    specifically excludes from the AI plane).

    Mirrors ``_host_is_on_this_machine`` in main.py so the registration- and
    poll-time checks agree with the import-time startup guard.
    """
    if host_is_loopback(host):
        return True
    if not host:
        return False
    if host.strip("[]").lower() in _HOST_ALIAS_NAMES:
        # The container runtime defines these names as "the machine running
        # Docker" — this host, by construction, wherever they resolve.
        return True
    bridge_net = _docker_bridge_net()
    if bridge_net is None:
        return False
    h = host.strip("[]").lower()
    # Direct IP literal.
    try:
        return ipaddress.ip_address(h) in bridge_net
    except ValueError:
        pass
    # Hostname (e.g. a Docker service name) — resolve and require EVERY
    # returned address to be loopback or inside the bridge subnet.
    saved = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(2.0)
        try:
            infos = socket.getaddrinfo(h, None)
        except (socket.gaierror, socket.timeout, OSError):
            return False
    finally:
        socket.setdefaulttimeout(saved)
    if not infos:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_loopback:
            continue
        if ip in bridge_net:
            continue
        return False
    return True


def _url_host(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.hostname
    if host == "0.0.0.0":
        # The wildcard bind isn't a routable destination; treat as
        # non-loopback so we refuse it explicitly.
        return host
    return host


def _has_wildcard(egress: list[str]) -> bool:
    return any("*" in entry for entry in egress)


def check_adapter(
    *,
    sovereignty_mode: str,
    adapter_url: str,
    capabilities: CapabilitiesResponse | None,
) -> None:
    """Raise :class:`SovereigntyViolation` if the adapter doesn't fit
    the active sovereignty mode.

    ``capabilities`` may be None for early-stage checks (URL-only,
    before we've polled the adapter). When provided, we also inspect
    ``capabilities.permissions.network_egress``.
    """
    mode = sovereignty_mode.lower()
    if mode not in VALID_SOVEREIGNTY_MODES:
        raise SovereigntyViolation(
            f"AI_SOVEREIGNTY={sovereignty_mode!r} is invalid; expected one of "
            f"{sorted(VALID_SOVEREIGNTY_MODES)}."
        )

    if mode == "cloud_allowed":
        return

    host = _url_host(adapter_url)

    if mode == "local_only":
        if host == "0.0.0.0":
            raise SovereigntyViolation(
                f"adapter URL {adapter_url!r}: host 0.0.0.0 is the wildcard "
                f"bind, not a loopback address."
            )
        if not host_is_on_this_machine(host):
            raise SovereigntyViolation(
                f"AI_SOVEREIGNTY=local_only refuses adapter URL "
                f"{adapter_url!r} (host={host}): it is not on this machine. "
                f"Accepted hosts are loopback (localhost/127.0.0.1/::1) or "
                f"any host inside the Docker bridge subnet "
                f"{_DOCKER_BRIDGE_SUBNET} (set OPENNVR_DOCKER_SUBNET if your "
                f"bridge uses a different range)."
            )
        if capabilities is not None:
            egress = capabilities.permissions.network_egress
            # Judge each declared egress DESTINATION, not the existence of
            # egress: an adapter proxying to a runtime on this same machine
            # (the ollamavlm adapter → the operator's host Ollama) keeps the
            # AI plane local and is exactly what local_only protects. Only a
            # destination off this machine makes it a cloud/LAN proxy.
            if _has_wildcard(egress):
                raise SovereigntyViolation(
                    f"AI_SOVEREIGNTY=local_only refuses adapter "
                    f"{capabilities.adapter.name!r}: "
                    f"permissions.network_egress contains wildcard entries "
                    f"({egress!r})."
                )
            for entry in egress:
                entry_host = _url_host(entry) if "://" in entry else entry.split(":", 1)[0]
                if not host_is_on_this_machine(entry_host):
                    raise SovereigntyViolation(
                        f"AI_SOVEREIGNTY=local_only refuses adapter "
                        f"{capabilities.adapter.name!r}: declared "
                        f"network_egress entry {entry!r} (host={entry_host}) "
                        f"is not on this "
                        f"machine. Loopback, the Docker bridge subnet "
                        f"({_DOCKER_BRIDGE_SUBNET}), and the runtime's host "
                        f"aliases ({', '.join(sorted(_HOST_ALIAS_NAMES))}) "
                        f"stay local; anything else needs "
                        f"AI_SOVEREIGNTY=federated (enumerated hosts) or "
                        f"cloud_allowed."
                    )
        return

    # mode == "federated"
    if capabilities is not None:
        egress = capabilities.permissions.network_egress
        if _has_wildcard(egress):
            raise SovereigntyViolation(
                f"AI_SOVEREIGNTY=federated refuses adapter "
                f"{capabilities.adapter.name!r}: "
                f"permissions.network_egress contains wildcard entries "
                f"({egress!r}); enumerate every host explicitly."
            )


def adapter_summary_for_audit(capabilities: CapabilitiesResponse | None) -> dict:
    """Subset of the capabilities dict that lands in the audit log on
    sovereignty refusals. Keeps the log compact while preserving
    enough context for an incident reviewer."""
    if capabilities is None:
        return {}
    return {
        "adapter_name": capabilities.adapter.name,
        "adapter_version": capabilities.adapter.version,
        "permissions": capabilities.permissions.model_dump(mode="json"),
    }
