# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
V-022 sovereignty enforcement (v2).

The original M1a implementation in main.py only checked that adapter
URLs were loopback. Per §11.1 of the contract design, the V-022
tightening also inspects the adapter's declared
``permissions.network_egress``:

* ``local_only``    — adapter URL MUST be loopback AND
                       ``permissions.network_egress`` MUST be empty.
                       Any non-empty egress list means the adapter is
                       a cloud-proxy and is refused.
* ``federated``     — adapter MAY have a non-empty
                       ``permissions.network_egress`` list but it MUST
                       enumerate every host explicitly. Wildcards
                       (``*``, ``*.example.com``) are refused.
* ``cloud_allowed`` — no checks; suitable for hosted deployments.

These checks run at registration time (so an adapter that violates
policy never enters the registry) and on every ``/capabilities`` poll
(so an adapter that ADDS egress at runtime gets de-registered).
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

# ISSUE-70 / ISSUE-28: the V-022 sovereignty claim is "all AI inference
# happens on THIS physical machine", not "loopback URLs only". In a
# normal single-box Docker deployment the adapter runs in its own
# container and is reached by its service name (e.g.
# ``http://yolov8-adapter:9002``), which resolves to a Docker-bridge IP
# inside ``OPENNVR_DOCKER_SUBNET``. Packets between bridge-network
# containers never leave the host's kernel networking stack, so those
# adapters are equally "on this machine" for sovereignty purposes.
#
# main.py's ``_host_is_on_this_machine`` already encoded this for the
# import-time startup guard, but the registration- and poll-time check
# below (the one the registry actually calls) stayed loopback-only, so
# the on-box adapter was wrongly refused and never registered. The
# subnet is operator-configurable so non-standard bridge ranges keep
# working without losing the sovereignty guarantee.
_DOCKER_BRIDGE_SUBNET = os.getenv("OPENNVR_DOCKER_SUBNET", "172.28.0.0/16")


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
        networking stack, so it never leaves the box.

    Everything else is rejected, including non-bridge RFC1918 / ULA / LAN
    addresses (those are peer hosts on the same LAN, which V-022
    specifically excludes from the AI plane).

    Mirrors ``_host_is_on_this_machine`` in main.py so the registration-
    and poll-time checks agree with the import-time startup guard
    (ISSUE-70).
    """
    if host_is_loopback(host):
        return True
    if not host:
        return False
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
            if egress:
                raise SovereigntyViolation(
                    f"AI_SOVEREIGNTY=local_only refuses adapter "
                    f"{capabilities.adapter.name!r}: declared "
                    f"permissions.network_egress={egress!r} is non-empty "
                    f"(cloud-proxy adapter)."
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
