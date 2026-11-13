# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""A forged X-Forwarded-For must not choose the client IP (HA-008).

nginx APPENDS the address it saw ($proxy_add_x_forwarded_for), so a client
that sends ``X-Forwarded-For: 127.0.0.1`` reaches core as
``"127.0.0.1, <real ip>"``. The old rule took the left-most entry, the one
the client wrote, which made the device firewall (loopback is exempt), the
WebSocket IP binding and every audit row's IP attacker-controlled.
get_client_ip now walks the header from the right, skipping trusted hops.
"""

from __future__ import annotations

import importlib
import os
import sys
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("INTERNAL_API_KEY", "x" * 48)
os.environ.setdefault("SECRET_KEY", "s" * 64)
os.environ.setdefault("MEDIAMTX_SECRET", "m" * 48)

from core import client_ip  # noqa: E402
from core.config import settings  # noqa: E402

NGINX = "172.28.0.10"


@pytest.fixture(autouse=True)
def _trust_docker_net(monkeypatch):
    client_ip._trusted_proxy_nets.cache_clear()
    client_ip._internal_nets.cache_clear()
    monkeypatch.setattr(settings, "trusted_proxy_cidrs",
                        "127.0.0.1/32,::1/128,172.28.0.0/16")
    monkeypatch.setattr(settings, "internal_service_cidrs",
                        "127.0.0.1/32,172.28.0.0/16")
    yield
    client_ip._trusted_proxy_nets.cache_clear()
    client_ip._internal_nets.cache_clear()


def _req(xff=None, peer=NGINX, real_ip=None):
    headers = {}
    if xff is not None:
        headers["x-forwarded-for"] = xff
    if real_ip is not None:
        headers["x-real-ip"] = real_ip
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


@pytest.mark.parametrize("xff, expected", [
    # nginx saw 192.168.1.20 and appended it; the client's forged entry is ignored
    ("127.0.0.1, 192.168.1.20", "192.168.1.20"),
    ("10.0.0.1, 127.0.0.1, 192.168.1.20", "192.168.1.20"),
    ("172.28.0.2, 192.168.1.20", "192.168.1.20"),
    # an honest single hop
    ("192.168.1.20", "192.168.1.20"),
    # chained trusted proxies are skipped from the right
    ("192.168.1.20, 172.28.0.3", "192.168.1.20"),
    # client really inside a trusted range: the hop our proxy actually saw
    ("172.28.0.1", "172.28.0.1"),
    ("8.8.8.8, 172.28.0.1", "8.8.8.8"),
    # ports and IPv6
    ("192.168.1.20:51234", "192.168.1.20"),
    ("[2001:db8::7]:443", "2001:db8::7"),
])
def test_right_walk_resolution(xff, expected):
    assert client_ip.get_client_ip(_req(xff)) == expected


def test_a_malformed_hop_stops_the_walk():
    # Nothing left of an unparseable hop can be trusted; fall back to the
    # nearest valid hop to its right.
    assert client_ip.get_client_ip(_req("127.0.0.1, garbage, 192.168.1.20")) == "192.168.1.20"
    assert client_ip.get_client_ip(_req("127.0.0.1, garbage")) == NGINX


def test_x_real_ip_is_the_fallback():
    assert client_ip.get_client_ip(_req(None, real_ip="192.168.1.30")) == "192.168.1.30"
    assert client_ip.get_client_ip(_req(None, real_ip="nonsense")) == NGINX


def test_untrusted_peer_cannot_speak_through_the_header():
    assert client_ip.get_client_ip(_req("127.0.0.1", peer="203.0.113.9")) == "203.0.113.9"


def test_http_firewall_can_no_longer_be_bypassed_with_a_forged_loopback(monkeypatch):
    """The real bypass: enforcement on, no device token, forged loopback XFF
    through nginx. It used to pass as 127.0.0.1; now it is refused."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    mw = importlib.import_module("middleware.device_firewall")
    monkeypatch.setattr(mw.dfw, "enforcement_active_cached", lambda: True)
    monkeypatch.setattr(mw.dfw, "is_allowed_browser_cached", lambda tok: False)

    app = FastAPI()
    app.add_middleware(mw.DeviceFirewallMiddleware)

    @app.get("/api/v1/cameras/")
    def guarded():
        return {"ok": True}

    with TestClient(app, client=(NGINX, 50000)) as c:
        forged = c.get("/api/v1/cameras/",
                       headers={"x-forwarded-for": "127.0.0.1, 192.168.1.99"})
        honest_loopback = c.get("/api/v1/cameras/",
                                headers={"x-forwarded-for": "127.0.0.1"})
    assert forged.status_code == 403
    assert forged.json()["device_ip"] == "192.168.1.99"
    # nginx itself reporting a loopback client is still loopback.
    assert honest_loopback.status_code == 200


def test_ws_ticket_cannot_be_borrowed_with_a_forged_xff(monkeypatch):
    """HA-006's IP binding only works if the handshake IP is not client-chosen."""
    ev = importlib.import_module("routers.events")
    monkeypatch.setattr(ev.dfw, "enforcement_active_cached", lambda: True)
    monkeypatch.setattr(ev.dfw, "is_allowed_browser_cached", lambda tok: True)
    binding = ev.WsTicketBinding(client_ip="192.168.1.20",
                                 device_token="approved", internal_key=False)
    attacker = SimpleNamespace(
        client=SimpleNamespace(host=NGINX),
        headers={"x-forwarded-for": "192.168.1.20, 192.168.1.99"}, cookies={})
    victim = SimpleNamespace(
        client=SimpleNamespace(host=NGINX),
        headers={"x-forwarded-for": "192.168.1.20"}, cookies={})
    assert not ev._ws_firewall_allows(attacker, binding)
    assert ev._ws_firewall_allows(victim, binding)
