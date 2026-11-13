# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The device firewall now covers the events WebSocket (HA-006).

DeviceFirewallMiddleware is HTTP-only, so the WebSocket handshake used to
skip it: a ticket minted by an approved browser could be opened from any
machine within its 30 s life. The mint request DOES pass the middleware,
so its facts are bound to the ticket and re-checked at the handshake.
Pinned here:

* enforcement off: unchanged behaviour, anyone with a valid ticket;
* enforcement on: the handshake must come from the minting client, and
  that client's device token must still be approved;
* a browser cannot send the device header on a WebSocket, so the token
  bound at mint time is what counts (a handshake cookie is the fallback);
* loopback and internal-key (service) tickets pass, and so does a sibling
  container with no token, exactly as the HTTP middleware allows;
* the binding lives and dies with its ticket (single use, pruned on expiry).
"""

import importlib
import os
import sys
import time
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("INTERNAL_API_KEY", "x" * 48)
os.environ.setdefault("SECRET_KEY", "s" * 64)
os.environ.setdefault("MEDIAMTX_SECRET", "m" * 48)

ev = importlib.import_module("routers.events")

APPROVED = "tok-approved"
PENDING = "tok-pending"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    ev._ws_tickets.clear()
    ev._ws_ticket_bindings.clear()
    state = {"enforcing": True}
    monkeypatch.setattr(ev.dfw, "enforcement_active_cached",
                        lambda: state["enforcing"])
    monkeypatch.setattr(ev.dfw, "is_allowed_browser_cached",
                        lambda tok: tok == APPROVED)
    # Nothing is behind a trusted proxy in these tests; peer == client.
    import core.client_ip as cip

    monkeypatch.setattr(cip, "_trusted_proxy_nets", lambda: ())
    monkeypatch.setattr(cip, "_internal_nets", lambda: (
        __import__("ipaddress").ip_network("172.28.0.0/16"),))
    yield state
    ev._ws_tickets.clear()
    ev._ws_ticket_bindings.clear()


def _conn(ip, headers=None, cookies=None):
    """A request or WebSocket as far as the firewall helpers look at it."""
    return SimpleNamespace(client=SimpleNamespace(host=ip),
                           headers=headers or {}, cookies=cookies or {})


def _bind(ip, token=None, internal=False):
    return ev.WsTicketBinding(client_ip=ip, device_token=token,
                              internal_key=internal)


def test_enforcement_off_changes_nothing(_fresh):
    _fresh["enforcing"] = False
    assert ev._ws_firewall_allows(_conn("203.0.113.9"), _bind("198.51.100.1"))
    assert ev._ws_firewall_allows(_conn("203.0.113.9"), None)


def test_approved_browser_from_the_minting_ip_passes():
    assert ev._ws_firewall_allows(_conn("192.168.1.20"),
                                  _bind("192.168.1.20", APPROVED))


def test_ticket_opened_from_another_machine_is_refused():
    # The ticket carries an approved browser's token; a copy used elsewhere
    # must not borrow that approval.
    assert not ev._ws_firewall_allows(_conn("192.168.1.99"),
                                      _bind("192.168.1.20", APPROVED))


def test_unapproved_device_is_refused():
    assert not ev._ws_firewall_allows(_conn("192.168.1.20"),
                                      _bind("192.168.1.20", PENDING))


def test_handshake_cookie_is_the_fallback_token():
    conn = _conn("192.168.1.20", cookies={"opennvr_device": APPROVED})
    assert ev._ws_firewall_allows(conn, _bind("192.168.1.20", None))


def test_browser_without_any_token_is_refused():
    assert not ev._ws_firewall_allows(_conn("192.168.1.20"),
                                      _bind("192.168.1.20", None))


def test_loopback_and_service_tickets_pass():
    assert ev._ws_firewall_allows(_conn("127.0.0.1"), None)
    assert ev._ws_firewall_allows(_conn("172.28.5.5"),
                                  _bind("172.28.5.5", None, internal=True))


def test_api_token_ticket_passes_from_the_minting_ip_only():
    """An API token is a bound credential (HA-103): like on HTTP it needs no
    approved browser, but the ticket still can't be opened elsewhere."""
    tok = ev.WsTicketBinding(client_ip="192.168.1.20", device_token=None,
                             api_token_id=7)
    assert ev._ws_firewall_allows(_conn("192.168.1.20"), tok)
    assert not ev._ws_firewall_allows(_conn("192.168.1.99"), tok)


def test_sibling_container_without_token_passes_like_http():
    assert ev._ws_firewall_allows(_conn("172.28.0.7"), _bind("172.28.0.7"))


def test_mint_binds_ip_and_device_token():
    async def run():
        req = _conn("192.168.1.20", headers={"x-device-token": APPROVED})
        user = SimpleNamespace(username="alice")
        return await ev.create_ws_ticket(request=req, principal=user)

    import asyncio

    out = asyncio.run(run())
    b = ev._ws_ticket_bindings[out["ticket"]]
    assert b == _bind("192.168.1.20", APPROVED, internal=False)


def test_service_mint_is_marked_internal():
    import asyncio

    out = asyncio.run(ev.create_ws_ticket(request=_conn("172.28.0.9"),
                                          principal=None))
    assert out["kind"] == "service"
    assert ev._ws_ticket_bindings[out["ticket"]].internal_key is True


def test_binding_is_consumed_with_its_ticket():
    t, _ = ev._mint_ws_ticket("alice", _bind("192.168.1.20", APPROVED))
    assert t in ev._ws_ticket_bindings
    ev._consume_ws_ticket(t)
    assert t not in ev._ws_ticket_bindings


def test_binding_is_pruned_with_an_expired_ticket():
    t, _ = ev._mint_ws_ticket("alice", _bind("192.168.1.20", APPROVED))
    ev._ws_tickets[t] = ("alice", time.time() - 1)
    ev._mint_ws_ticket("bob")  # minting prunes
    assert t not in ev._ws_tickets and t not in ev._ws_ticket_bindings


def test_service_ticket_is_honoured_only_from_inside_the_stack():
    service = _bind("172.28.5.5", None, internal=True)
    assert ev._ws_firewall_allows(_conn("172.28.5.5"), service)
    # A leaked service ticket (unscoped, in a query string) opened from an
    # outside machine while enforcing must not bypass the firewall.
    assert not ev._ws_firewall_allows(_conn("203.0.113.50"), service)


# ── Wiring: the real handler enforces the decision ──────────────────────


def test_handshake_is_refused_end_to_end(monkeypatch):
    """Drive events_stream itself: a ticket minted from one IP and opened
    from another, with enforcement on, closes with 1008 device_not_approved.
    Pins the call site, not just the helper."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from starlette.websockets import WebSocketDisconnect

    import models

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    s = Session()
    role = models.Role(name="viewer")
    s.add(role)
    s.commit()
    s.add(models.User(username="alice", email="a@x", hashed_password="h",
                      is_active=True, is_superuser=True, role_id=role.id))
    s.commit()
    s.close()

    def _db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr(ev, "get_db", _db)
    app = FastAPI()
    app.include_router(ev.router, prefix="/api/v1")
    client = TestClient(app)

    # TestClient connects as host "testclient"; bind the ticket elsewhere.
    ticket, _ = ev._mint_ws_ticket("alice", _bind("192.168.1.20", APPROVED))
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/api/v1/events/ws?ticket={ticket}") as ws:
            ws.receive_json()
    assert exc.value.code == 1008
    assert exc.value.reason == "device_not_approved"
