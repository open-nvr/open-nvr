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
import socket
from urllib.parse import urlparse

from kai_c.contract_types import CapabilitiesResponse, Permissions

logger = logging.getLogger(__name__)


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

VALID_SOVEREIGNTY_MODES = frozenset({"local_only", "federated", "cloud_allowed"})


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
        if not host_is_loopback(host):
            raise SovereigntyViolation(
                f"AI_SOVEREIGNTY=local_only refuses non-loopback adapter URL "
                f"{adapter_url!r} (host={host})."
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
