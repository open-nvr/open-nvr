# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Device-firewall middleware decision order.

The gate itself is the security boundary, so these drive it through a real ASGI
app: an approved browser passes, an unapproved one is refused, and — the point
of moving identity off the IP — deleting the device cookie must NOT let a user
session slip through on the strength of its network address (which, behind
Docker Desktop's NAT, is the trusted bridge gateway for every LAN client).
"""

from __future__ import annotations

import os
import secrets
import sys
import types as _types
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

os.environ.setdefault("DATABASE_URL", "sqlite:///./_fwmw_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

_lm = _types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules.setdefault("core.logging_config", _lm)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.database import Base  # noqa: E402
from services import device_firewall_service as dfw  # noqa: E402

GUARDED = "/api/v1/cameras"
OPEN = "/api/v1/auth/login"

_MW_PATH = REPO_ROOT / "server" / "middleware" / "device_firewall.py"


def _load_middleware():
    """Load the middleware module from its file, bypassing ``middleware/__init__``.

    The package __init__ pulls in request_logging, which needs the REAL
    core.logging_config — and a sibling suite may have stubbed that module in
    sys.modules. Loading by path keeps this test independent of suite order.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("_dfw_mw_under_test", _MW_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def env(monkeypatch):
    """A one-route app behind the real middleware, on an in-memory DB."""
    from core import client_ip
    from core.config import settings

    mw = _load_middleware()

    # StaticPool + one connection: the firewall opens its OWN session, and a
    # plain :memory: engine would hand it a separate, empty database. The
    # middleware no longer touches the DB itself — it delegates to the
    # service's TTL-cached helpers — so patch the service's session factory
    # and reset its decision cache between tests.
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    session_factory = sessionmaker(bind=eng)
    monkeypatch.setattr(dfw, "_new_session", lambda: session_factory())
    dfw.invalidate_decision_cache()

    # Trust the proxy so X-Forwarded-For is honored, mirroring the nginx setup.
    client_ip._trusted_proxy_nets.cache_clear()
    client_ip._internal_nets.cache_clear()
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32,172.28.0.0/16")
    monkeypatch.setattr(settings, "internal_service_cidrs", "127.0.0.1/32,172.28.0.0/16")

    app = FastAPI()
    app.add_middleware(mw.DeviceFirewallMiddleware)

    @app.get(GUARDED)
    def guarded():
        return {"ok": True}

    @app.post(OPEN)
    def login():
        return {"ok": True}

    db = session_factory()
    dfw.set_enforcement(db, True)
    try:
        yield _types.SimpleNamespace(app=app, db=db, settings=settings)
    finally:
        db.close()
        dfw.invalidate_decision_cache()
        client_ip._trusted_proxy_nets.cache_clear()
        client_ip._internal_nets.cache_clear()


def _client(env, cookies=None, peer="172.28.0.7"):
    """A client whose socket peer is nginx, as in the real deployment (the
    default TestClient peer is "testclient", which no CIDR would trust)."""
    return TestClient(env.app, cookies=cookies, client=(peer, 51234))


def _get(env, path=GUARDED, cookies=None, headers=None, xff="192.168.31.50"):
    """Request as if proxied by nginx (peer = the proxy, XFF = the client)."""
    hdrs = {}
    if xff:
        hdrs["x-forwarded-for"] = xff
    hdrs.update(headers or {})
    with _client(env, cookies) as c:
        return c.get(path, headers=hdrs)


def test_open_path_never_gated(env):
    with _client(env) as c:
        assert c.post(OPEN).status_code == 200


def test_approved_browser_passes_via_header(env):
    """The SPA sends the device token as a HEADER — its fetch client omits
    credentials, so a cookie would never be stored or returned."""
    _dev, token = dfw.register_authenticated_browser(
        env.db, None, "192.168.31.50", "UA", 1
    )  # first browser -> approved
    assert _get(env, headers={dfw.DEVICE_HEADER_NAME: token}).status_code == 200


def test_approved_browser_passes_via_cookie(env):
    """Cookie remains supported for non-SPA clients."""
    _dev, token = dfw.register_authenticated_browser(
        env.db, None, "192.168.31.50", "UA", 1
    )
    assert _get(env, cookies={dfw.DEVICE_COOKIE_NAME: token}).status_code == 200


def test_pending_browser_is_refused(env):
    dfw.register_authenticated_browser(env.db, None, "192.168.31.50", "first", 1)
    _dev, token = dfw.register_authenticated_browser(
        env.db, None, "192.168.31.50", "second", 1
    )
    r = _get(env, headers={dfw.DEVICE_HEADER_NAME: token})
    assert r.status_code == 403
    assert r.json()["code"] == "device_not_approved"


def test_forged_token_is_refused(env):
    dfw.register_authenticated_browser(env.db, None, "192.168.31.50", "first", 1)
    assert _get(env, headers={dfw.DEVICE_HEADER_NAME: "forged"}).status_code == 403
    assert _get(env, cookies={dfw.DEVICE_COOKIE_NAME: "forged"}).status_code == 403


def test_client_without_device_token_is_not_locked_out(env):
    """Regression for a real lockout: an earlier revision denied "has a session
    but no device token", which refused EVERY user the moment enforcement was
    switched on — the SPA had no way to hold a token yet, so no browser had one,
    including the admin's own. A tokenless caller whose address is internal
    (which NAT makes every client look like) must still pass; it enrolls on its
    next login."""
    dfw.register_authenticated_browser(env.db, None, "172.28.0.1", "first", 1)
    r = _get(env, xff="172.28.0.1", headers={"authorization": "Bearer some.jwt"})
    assert r.status_code == 200


def test_tokenless_external_client_is_refused(env):
    """Where real client addresses survive (native Linux), an un-enrolled
    browser is still refused — it must log in to enroll."""
    dfw.register_authenticated_browser(env.db, None, "192.168.31.50", "first", 1)
    r = _get(env, xff="203.0.113.9", headers={"authorization": "Bearer some.jwt"})
    assert r.status_code == 403


def test_sibling_service_without_token_passes(env):
    """MediaMTX/KAI-C call the API from the compose network with no device
    token; they must never be firewalled."""
    dfw.register_authenticated_browser(env.db, None, "192.168.31.50", "first", 1)
    assert _get(env, xff="172.28.0.5").status_code == 200


def test_internal_api_key_passes(env):
    dfw.register_authenticated_browser(env.db, None, "192.168.31.50", "first", 1)
    r = _get(env, headers={"x-internal-api-key": env.settings.internal_api_key})
    assert r.status_code == 200


def test_wrong_internal_api_key_is_refused(env):
    dfw.register_authenticated_browser(env.db, None, "192.168.31.50", "first", 1)
    assert _get(env, headers={"x-internal-api-key": "nope"}).status_code == 403


def test_loopback_always_allowed(env):
    """``docker exec`` recovery path — must work even with nothing approved."""
    assert _get(env, xff="127.0.0.1").status_code == 200
    # …and directly from the loopback peer, with no proxy in front at all.
    with _client(env, peer="127.0.0.1") as c:
        assert c.get(GUARDED).status_code == 200


def test_enforcement_off_allows_unknown_browser(env):
    dfw.set_enforcement(env.db, False)
    assert _get(env).status_code == 200
