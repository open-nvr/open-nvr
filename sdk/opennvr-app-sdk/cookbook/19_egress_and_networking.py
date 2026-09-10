# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""Reaching the outside world — `proxy_address`, `connect_via_proxy`.

Demonstrates: `proxy_address`, `connect_via_proxy`, and the egress rules
an app lives under (docs/APP_NETWORK.md).

Apps run on an internal network with no direct route out. Everything an
app sends outward goes through the deployment's egress proxy, against a
host allow-list the operator can see and audit — which is what lets an
operator install a third-party app in a hospital or a ministry and know
what it can talk to.

HTTP clients need nothing: `httpx` and `requests` honour the standard
proxy environment the installer sets. These helpers exist for the
PLAIN-TCP cases — an MQTT broker, a syslog collector, a SIP registrar —
where there is no proxy-aware library to lean on.
"""
import socket

from opennvr_app_sdk import connect_via_proxy, proxy_address


def http_needs_nothing() -> None:
    """The common case. Do NOT pass `trust_env=False` on outbound calls
    to third parties: that is what disables the proxy the deployment
    configured, and the call will simply fail."""
    import httpx
    httpx.post("https://hooks.example.com/notify", json={"ok": True}, timeout=5.0)


def plain_tcp_through_the_proxy(host: str, port: int) -> socket.socket:
    """`connect_via_proxy` returns the (host, port) to dial for a
    destination — the proxy when one is configured and the destination
    is not exempt, the destination itself otherwise. Dial what it
    returns, not what you were given."""
    target = connect_via_proxy(host, port) or (host, port)
    return socket.create_connection(target, timeout=10.0)


def is_there_a_proxy_at_all() -> tuple[str, int] | None:
    """None means this deployment has no egress proxy configured — a
    bare dev run. Behave the same either way rather than branching on
    it: `connect_via_proxy` already does."""
    return proxy_address("https")


def declare_your_hosts() -> list[str]:
    """The allow-list is not something the SDK enforces — the operator
    sets it from what your app DECLARES. Name every host you dial in
    your README and your catalog listing; an undeclared host is a
    surprise in someone's audit log, which is how an app gets removed
    from a site."""
    return ["hooks.example.com", "api.example.com"]
