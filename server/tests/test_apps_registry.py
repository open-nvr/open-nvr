# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
Tests for the app registry (App SDK spec §05): the ``installed_apps``
table, the six ``/api/v1/apps`` routes, and the pure
``validate_app_config`` function.

Run with:

    cd server && pytest tests/test_apps_registry.py -v

Coverage:

* ``POST /apps/register`` validates the manifest (id/name/version) and
  upserts by manifest id — re-registration on app restart refreshes
  url/version/manifest but preserves the operator-owned ``enabled``
  flag and ``config_json``. Off-box URLs (public IPs, link-local cloud
  metadata, dotted FQDNs) are refused (SSRF guard, ``validate_app_url``).
* ``POST /apps/{id}/enable`` / ``/disable`` flip the flag; unknown id
  is 404.
* ``PUT /apps/{id}/config`` rejects unknown keys, missing required
  params, and type mismatches; accepts per_camera dicts and
  ``geometry.polygon`` lists; stores valid config with omitted manifest
  defaults materialized in.
* ``GET /apps/{id}/status`` degrades gracefully when the app is down
  and short-circuits (never fetches) a blocked stored URL.
* ``validate_app_config`` unit tests — the pure function the future
  SDK conformance kit reuses.
"""

from __future__ import annotations

# Python 3.10 sandbox polyfill — pyproject requires 3.11+ where
# datetime.UTC exists. No-op on 3.11+.
import datetime as _dt

if not hasattr(_dt, "UTC"):
    _dt.UTC = _dt.timezone.utc  # noqa: UP017 — only runs where UTC is absent

import os
import secrets
import sys
import types as _types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

# Settings need to be valid for the modules under test to import.
from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault(
    "CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode()
)

# Stub ``core.logging_config`` — same pattern as
# test_cloud_status_configured.py. The real module wants a writable
# ``logs/`` directory; the router under test only needs no-op loggers.
_lm = _types.ModuleType("core.logging_config")


class _L:
    def info(self, *a, **kw):
        pass

    def warning(self, *a, **kw):
        pass

    def error(self, *a, **kw):
        pass

    def debug(self, *a, **kw):
        pass

    def critical(self, *a, **kw):
        pass

    def log_action(self, *a, **kw):
        pass


for _name in (
    "main_logger",
    "auth_logger",
    "camera_logger",
    "recording_logger",
    "cloud_logger",
    "ai_logger",
):
    setattr(_lm, _name, _L())
sys.modules["core.logging_config"] = _lm


# ─── App under test: the real router on an in-memory SQLite DB ──────────
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.auth import (  # noqa: E402
    create_access_token, get_current_active_user, get_current_superuser,
)
from core.database import Base, get_db  # noqa: E402
from models import AuditLog, InstalledApp, Role, User  # noqa: E402
from routers import apps as apps_router  # noqa: E402
from routers.apps import (  # noqa: E402
    get_read_principal,
    get_register_principal,
    validate_app_config,
)


class _StubUser:
    """Just enough of a User for the router + audit log. A superuser:
    camera scoping has its own module (test_apps_camera_scope)."""

    id = 1
    username = "tester"
    is_superuser = True


def _make_app():
    """The real apps router over a shared in-memory DB.

    ``StaticPool`` pins every session to one connection so all requests
    see the same ``:memory:`` database. Returns the FastAPI app plus
    the session factory (for fixtures that need to seed rows).
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            InstalledApp.__table__,
            AuditLog.__table__,
            Role.__table__,
            User.__table__,
        ],
    )
    session_factory = sessionmaker(bind=engine)

    app = FastAPI()
    app.include_router(apps_router.router)

    def _override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_db
    return app, session_factory, engine


@pytest.fixture
def client():
    """A TestClient with auth overridden — RBAC is exercised by the
    shared auth tests (and ``test_register_auth_*`` below), not
    re-proven per route."""
    app, _session_factory, engine = _make_app()
    app.dependency_overrides[get_current_active_user] = lambda: _StubUser()
    app.dependency_overrides[get_current_superuser] = lambda: _StubUser()
    app.dependency_overrides[get_register_principal] = lambda: _StubUser()
    app.dependency_overrides[get_read_principal] = lambda: _StubUser()

    with TestClient(app) as test_client:
        yield test_client
    engine.dispose()


class _AnyLogger:
    """A no-op logger accepting ANY method — whichever test module's
    logging stub won the ``sys.modules`` race, ``core.auth`` must not
    blow up on the methods it calls (``log_action`` et al.)."""

    def __getattr__(self, name):
        return lambda *a, **kw: None


@pytest.fixture
def auth_client(monkeypatch):
    """A TestClient with REAL registration auth (no
    ``get_register_principal`` override) — exercises the user-JWT and
    ``X-Internal-Api-Key`` service paths. A live user is seeded so the
    JWT path can resolve ``sub`` to a row."""
    # In full-suite runs another module's core.logging_config stub may
    # have been imported first; core.auth's bound auth_logger then
    # lacks log_action. The real logger isn't under test — replace it.
    import core.auth as core_auth

    monkeypatch.setattr(core_auth, "auth_logger", _AnyLogger())

    app, session_factory, engine = _make_app()
    # Operator routes stay overridden — only registration auth is real.
    app.dependency_overrides[get_current_active_user] = lambda: _StubUser()
    app.dependency_overrides[get_current_superuser] = lambda: _StubUser()

    session = session_factory()
    role = Role(name="admin")
    session.add(role)
    session.flush()
    session.add(
        User(
            username="operator",
            email="operator@example.com",
            hashed_password="x",  # never verified — the JWT is the credential
            is_active=True,
            role_id=role.id,
        )
    )
    session.commit()
    session.close()

    with TestClient(app) as test_client:
        test_client.session_factory = session_factory
        yield test_client
    engine.dispose()


def _manifest(**overrides) -> dict:
    """A wire-shaped AppManifest.to_dict() (loitering, per spec §04)."""
    manifest = {
        "id": "loitering-detection",
        "name": "Loitering Detection",
        "version": "1.0.0",
        "category": "perimeter",
        "summary": "Alerts when a watched object dwells in a zone.",
        "requires_tasks": ["object_detection"],
        "subscribes": "opennvr.inference.>",
        "params": [
            {
                "name": "watch_labels",
                "required": False,
                "type": "list",
                "default": ["person"],
                "per_camera": False,
                "description": "",
            },
            {
                "name": "threshold_s",
                "required": False,
                "type": "float",
                "default": 30.0,
                "per_camera": False,
                "description": "",
            },
            {
                "name": "zones",
                "required": False,
                "type": "geometry.polygon",
                "default": None,
                "per_camera": True,
                "description": "",
            },
        ],
        "emits": [
            {"name": "loitering", "severity": "high", "description": ""}
        ],
    }
    manifest.update(overrides)
    return manifest


def _register(client, url="http://loitering:9200", **overrides):
    return client.post(
        "/apps/register", json={"url": url, "manifest": _manifest(**overrides)}
    )


# ─── POST /apps/register ────────────────────────────────────────────────


def test_register_creates_record(client):
    """Happy path: the SDK's boot-time register lands a catalog row."""
    resp = _register(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "loitering-detection"
    assert body["name"] == "Loitering Detection"
    assert body["category"] == "perimeter"
    assert body["version"] == "1.0.0"
    assert body["url"] == "http://loitering:9200"
    assert body["enabled"] is False           # operator opt-in, not auto-on
    assert body["status"] == "registered"
    assert body["last_seen"] is not None
    assert body["manifest"]["params"][0]["name"] == "watch_labels"
    assert body["config"] == {}


@pytest.mark.parametrize("missing_field", ["id", "name", "version"])
def test_register_rejects_incomplete_manifest(client, missing_field):
    """A manifest without id/name/version is a 400 naming the field."""
    manifest = _manifest()
    del manifest[missing_field]
    resp = client.post(
        "/apps/register", json={"url": "http://x:1", "manifest": manifest}
    )
    assert resp.status_code == 400
    assert missing_field in resp.json()["detail"]


def test_reregister_preserves_enabled_and_config(client):
    """App restarts must not lose operator state.

    Re-registration refreshes url/version/manifest (the app's side of
    the row) but ``enabled`` and ``config`` belong to the operator and
    survive — the whole point of upserting by manifest id.
    """
    _register(client)
    assert client.post("/apps/loitering-detection/enable").status_code == 200
    assert (
        client.put(
            "/apps/loitering-detection/config", json={"threshold_s": 12.5}
        ).status_code
        == 200
    )

    resp = _register(client, url="http://loitering:9999", version="1.1.0")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True                    # preserved
    # Preserved — including the materialized manifest default for the
    # omitted watch_labels param (zones defaults to None, so no entry).
    assert body["config"] == {
        "threshold_s": 12.5,
        "watch_labels": ["person"],
    }
    assert body["version"] == "1.1.0"                 # refreshed
    assert body["url"] == "http://loitering:9999"     # refreshed
    assert body["status"] == "registered"

    # And the catalog still shows exactly one row for the app.
    listing = client.get("/apps").json()
    assert [row["id"] for row in listing] == ["loitering-detection"]


@pytest.mark.parametrize(
    "url",
    [
        "http://8.8.8.8:9200",              # public IP
        "http://169.254.169.254/latest",    # link-local cloud metadata
        "http://apps.example.com:9200",     # dotted FQDN
    ],
)
def test_register_rejects_offbox_urls(url, client):
    """SSRF guard: /status server-side fetches the stored URL, so only
    on-box / private destinations may register (see validate_app_url,
    mirroring kai-c/kai_c/sovereignty.py)."""
    resp = _register(client, url=url)
    assert resp.status_code == 400
    assert "url" in resp.json()["detail"]
    # Nothing landed in the catalog.
    assert client.get("/apps").json() == []


@pytest.mark.parametrize(
    "url",
    [
        "http://loitering:9200",     # single-label Docker service name
        "http://localhost:9200",     # loopback by name
        "http://172.28.0.5:9200",    # RFC1918 (Docker bridge) IP
    ],
)
def test_register_accepts_local_urls(url, client):
    resp = _register(client, url=url)
    assert resp.status_code == 200
    assert resp.json()["url"] == url


# ─── POST /apps/register auth (service key vs user JWT) ─────────────────
#
# Registration is service-to-service: SDK apps boot with only the
# deployment's INTERNAL_API_KEY, so the route accepts EITHER a user JWT
# OR a matching ``X-Internal-Api-Key`` header (get_register_principal).
# These run against ``auth_client`` — the fixture WITHOUT the register
# auth override — so the real dependency is exercised.


def _effective_internal_key() -> str:
    from core.config import settings

    return settings.internal_api_key


def test_register_with_internal_api_key_succeeds(auth_client):
    """The SDK's boot-time register authenticates with the shared
    internal key alone — no user JWT involved."""
    resp = auth_client.post(
        "/apps/register",
        json={"url": "http://loitering:9200", "manifest": _manifest()},
        headers={"X-Internal-Api-Key": _effective_internal_key()},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == "loitering-detection"


def test_register_with_wrong_internal_api_key_is_401(auth_client):
    resp = auth_client.post(
        "/apps/register",
        json={"url": "http://loitering:9200", "manifest": _manifest()},
        headers={"X-Internal-Api-Key": "not-the-key"},
    )
    assert resp.status_code == 401
    # Nothing landed in the catalog (read it back with the valid key).
    assert auth_client.get(
        "/apps", headers={"X-Internal-Api-Key": _effective_internal_key()}
    ).json() == []


def test_register_without_any_credential_is_401(auth_client):
    resp = auth_client.post(
        "/apps/register",
        json={"url": "http://loitering:9200", "manifest": _manifest()},
    )
    assert resp.status_code == 401
    assert auth_client.get(
        "/apps", headers={"X-Internal-Api-Key": _effective_internal_key()}
    ).json() == []


def test_register_with_user_jwt_still_works(auth_client):
    """The pre-existing operator path — a real bearer token minted for
    a live user row — keeps working alongside the service key."""
    token = create_access_token({"sub": "operator"})
    resp = auth_client.post(
        "/apps/register",
        json={"url": "http://loitering:9200", "manifest": _manifest()},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == "loitering-detection"


def test_register_jwt_wins_when_key_header_does_not_match(auth_client):
    """The SDK sends its one token as BOTH headers (Authorization +
    X-Internal-Api-Key). When that token is a user JWT, the key check
    misses but the bearer path must still authenticate the call."""
    token = create_access_token({"sub": "operator"})
    resp = auth_client.post(
        "/apps/register",
        json={"url": "http://loitering:9200", "manifest": _manifest()},
        headers={
            "Authorization": f"Bearer {token}",
            "X-Internal-Api-Key": token,  # not the internal key
        },
    )
    assert resp.status_code == 200


def test_register_with_garbage_bearer_is_401(auth_client):
    resp = auth_client.post(
        "/apps/register",
        json={"url": "http://loitering:9200", "manifest": _manifest()},
        headers={"Authorization": "Bearer not-a-jwt"},
    )
    assert resp.status_code == 401


def test_write_routes_do_not_accept_internal_api_key(auth_client):
    """enable/disable/config are operator actions — NEITHER service-key
    dependency may open them. They stay strictly ``get_current_active_user``
    so the service key can never toggle or reconfigure an app. Verified on
    the real router's dependency graph (the fixture bypasses the user
    dependency only for register)."""
    from routers.apps import router as apps_router_obj

    # register accepts the key via get_register_principal; the read
    # routes accept it via get_read_principal — both are intended. The
    # check is per (path, METHOD): /apps/{app_id}/config carries a GET
    # (read — the app's live-config poll, service key OK) AND a PUT
    # (write — operator only), so path-only matching would be wrong in
    # both directions.
    service_principals = {"get_register_principal", "get_read_principal"}
    write_routes = {("/apps/{app_id}/enable", "POST"),
                    ("/apps/{app_id}/disable", "POST"),
                    ("/apps/{app_id}/config", "PUT")}
    checked = set()
    for route in apps_router_obj.routes:
        for method in route.methods or ():
            if (route.path, method) not in write_routes:
                continue
            checked.add((route.path, method))
            dependency_names = {
                dep.call.__name__ for dep in route.dependant.dependencies
            }
            assert not (service_principals & dependency_names), (
                route.path, method,
            )
    assert checked == write_routes         # all three write routes exist


def test_read_routes_are_read_only(auth_client):
    """list + status accept the service key (get_read_principal) but expose
    no write dependency — they can only READ. No mutation path is reachable
    through the read principal."""
    from routers.apps import router as apps_router_obj

    read_routes = {
        ("/apps", "GET"),
        ("/apps/{app_id}/status", "GET"),
        # Live config delivery: the running app polls its own config
        # with the internal key it already holds for registration.
        ("/apps/{app_id}/config", "GET"),
    }
    checked: set[tuple[str, str]] = set()
    for route in apps_router_obj.routes:
        if (route.path, "GET") not in read_routes or "GET" not in route.methods:
            continue
        checked.add((route.path, "GET"))
        dependency_names = {
            dep.call.__name__ for dep in route.dependant.dependencies
        }
        assert "get_read_principal" in dependency_names, route.path
        # The register/write service identity is never wired here.
        assert "get_register_principal" not in dependency_names, route.path
    # Guard against vacuous passes: if the read routes are ever renamed or
    # removed, the loop body would simply never run — fail instead.
    assert checked == read_routes, f"read routes not found: {read_routes - checked}"


# ─── Read routes accept the internal key (service reads for the agent) ──


def test_list_apps_with_internal_api_key_succeeds(auth_client):
    """The OpenNVR Agent lists the catalog with the shared internal key
    alone — no user JWT — so it can surface installed apps as skills."""
    # Seed a row via the service key, then read it back with the key.
    reg = auth_client.post(
        "/apps/register",
        json={"url": "http://loitering:9200", "manifest": _manifest()},
        headers={"X-Internal-Api-Key": _effective_internal_key()},
    )
    assert reg.status_code == 200
    resp = auth_client.get(
        "/apps", headers={"X-Internal-Api-Key": _effective_internal_key()}
    )
    assert resp.status_code == 200
    assert [row["id"] for row in resp.json()] == ["loitering-detection"]


def test_status_with_internal_api_key_succeeds(auth_client, monkeypatch):
    """The agent probes an app's live state with the internal key alone."""

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class _LiveClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            if url.endswith("/health"):
                return _Resp({"ready": True})
            return _Resp({"active_tracks": 1})

    auth_client.post(
        "/apps/register",
        json={"url": "http://loitering:9200", "manifest": _manifest()},
        headers={"X-Internal-Api-Key": _effective_internal_key()},
    )
    monkeypatch.setattr(apps_router.httpx, "AsyncClient", _LiveClient)

    resp = auth_client.get(
        "/apps/loitering-detection/status",
        headers={"X-Internal-Api-Key": _effective_internal_key()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["health"]["ready"] is True
    assert body["state"] == {"active_tracks": 1}


def test_read_with_wrong_internal_api_key_and_no_user_is_401(auth_client):
    """A bad key with no bearer to fall back to is refused on both read
    routes — the service key is the only credential and it doesn't match."""
    for path in ("/apps", "/apps/anything/status"):
        resp = auth_client.get(path, headers={"X-Internal-Api-Key": "not-the-key"})
        assert resp.status_code == 401, path


def test_read_without_any_credential_is_401(auth_client):
    """No bearer, no key → 401 on the read routes (get_read_principal)."""
    for path in ("/apps", "/apps/anything/status"):
        assert auth_client.get(path).status_code == 401, path


def test_list_apps_with_user_jwt_still_works(auth_client):
    """The pre-existing operator path — a real bearer token for a live
    user row — keeps reading the catalog alongside the service key."""
    token = create_access_token({"sub": "operator"})
    auth_client.post(
        "/apps/register",
        json={"url": "http://loitering:9200", "manifest": _manifest()},
        headers={"X-Internal-Api-Key": _effective_internal_key()},
    )
    resp = auth_client.get("/apps", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert [row["id"] for row in resp.json()] == ["loitering-detection"]


# ─── enable / disable ───────────────────────────────────────────────────


def test_enable_then_disable_flips_flag(client):
    _register(client)
    assert client.post("/apps/loitering-detection/enable").json()["enabled"] is True
    assert (
        client.post("/apps/loitering-detection/disable").json()["enabled"] is False
    )


def test_enable_unknown_app_is_404(client):
    assert client.post("/apps/ghost/enable").status_code == 404
    assert client.post("/apps/ghost/disable").status_code == 404


# ─── PUT /apps/{id}/config ──────────────────────────────────────────────


def test_config_rejects_unknown_key(client):
    _register(client)
    resp = client.put(
        "/apps/loitering-detection/config", json={"not_a_param": 1}
    )
    assert resp.status_code == 400
    assert "not_a_param" in resp.json()["detail"]


def test_config_rejects_wrong_type(client):
    _register(client)
    resp = client.put(
        "/apps/loitering-detection/config", json={"threshold_s": "thirty"}
    )
    assert resp.status_code == 400
    assert "threshold_s" in resp.json()["detail"]


def test_config_missing_required_param(client):
    """required=True + no default + absent from config ⇒ 400."""
    params = [
        {
            "name": "api_key",
            "required": True,
            "type": "str",
            "default": None,
            "per_camera": False,
            "description": "",
        }
    ]
    _register(client, id="keyed-app", params=params)
    resp = client.put("/apps/keyed-app/config", json={})
    assert resp.status_code == 400
    assert "api_key" in resp.json()["detail"]
    # Providing it satisfies the requirement.
    assert (
        client.put("/apps/keyed-app/config", json={"api_key": "s3cr3t"}).status_code
        == 200
    )


def test_config_valid_payload_is_stored(client):
    """A fully valid config — per_camera geometry included — persists."""
    _register(client)
    config = {
        "watch_labels": ["person", "car"],
        "threshold_s": 45,                       # int accepted for float
        "zones": {"cam-1": [[0, 0], [1, 0], [1, 1]]},  # per-camera polygon
    }
    resp = client.put("/apps/loitering-detection/config", json=config)
    assert resp.status_code == 200
    assert resp.json()["config"] == config
    # Round-trips through the catalog listing.
    assert client.get("/apps").json()[0]["config"] == config


def test_config_materializes_manifest_defaults(client):
    """Omitted optional params with a non-None manifest default land in
    the stored config; provided values are never overwritten — so
    config_json IS the effective config (spec §05)."""
    _register(client)
    resp = client.put(
        "/apps/loitering-detection/config", json={"threshold_s": 12.5}
    )
    assert resp.status_code == 200
    config = resp.json()["config"]
    assert config["threshold_s"] == 12.5              # provided value wins
    assert config["watch_labels"] == ["person"]       # default filled in
    assert "zones" not in config                      # default None ⇒ absent
    # Round-trips through the catalog listing.
    assert client.get("/apps").json()[0]["config"] == config


def test_config_per_camera_param_must_be_dict(client):
    """zones is per_camera — a bare polygon (not keyed by camera) is 400."""
    _register(client)
    resp = client.put(
        "/apps/loitering-detection/config", json={"zones": [[0, 0], [1, 1]]}
    )
    assert resp.status_code == 400
    assert "per_camera" in resp.json()["detail"]


# ─── GET /apps/{id}/status ──────────────────────────────────────────────


def test_status_unreachable_app_degrades_gracefully(client, monkeypatch):
    """A dead app yields a 200 with an unreachable health stanza —
    never a 5xx — and the stored row status flips for the catalog dot."""
    import httpx

    class _DeadClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            raise httpx.ConnectError("connection refused")

    _register(client)
    monkeypatch.setattr(apps_router.httpx, "AsyncClient", _DeadClient)

    resp = client.get("/apps/loitering-detection/status")
    assert resp.status_code == 200
    assert resp.json() == {"health": {"status": "unreachable"}, "state": None}
    assert client.get("/apps").json()[0]["status"] == "unreachable"


def test_status_healthy_app_proxies_health_and_state(client, monkeypatch):
    """A live app's /health + /state are proxied through verbatim and
    the row flips to ok with a fresh last_seen."""

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class _LiveClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            if url.endswith("/health"):
                return _Resp({"ready": True, "uptime_s": 12, "events_seen": 3})
            return _Resp({"active_tracks": 2})

    _register(client)
    monkeypatch.setattr(apps_router.httpx, "AsyncClient", _LiveClient)

    resp = client.get("/apps/loitering-detection/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["health"]["ready"] is True
    assert body["state"] == {"active_tracks": 2}

    row = client.get("/apps").json()[0]
    assert row["status"] == "ok"
    assert row["last_seen"] is not None


def test_status_unknown_app_is_404(client):
    assert client.get("/apps/ghost/status").status_code == 404


def test_status_short_circuits_blocked_stored_url(client, monkeypatch):
    """Defense in depth: a row whose URL predates the register-time SSRF
    guard is refused at fetch time — no outbound request is made."""
    # Sneak a bad URL past registration, as a pre-guard row would have.
    with monkeypatch.context() as m:
        m.setattr(apps_router, "validate_app_url", lambda url: None)
        assert _register(client, url="http://169.254.169.254").status_code == 200

    class _MustNotFetch:
        def __init__(self, *args, **kwargs):
            raise AssertionError("blocked URL must never be fetched")

    monkeypatch.setattr(apps_router.httpx, "AsyncClient", _MustNotFetch)

    resp = client.get("/apps/loitering-detection/status")
    assert resp.status_code == 200
    assert resp.json() == {"health": {"status": "blocked_url"}, "state": None}
    assert client.get("/apps").json()[0]["status"] == "blocked_url"


# ─── validate_app_config (pure function, conformance-kit surface) ───────


def _param(name, type_name, *, required=False, default=None, per_camera=False):
    return {
        "name": name,
        "required": required,
        "type": type_name,
        "default": default,
        "per_camera": per_camera,
        "description": "",
    }


def test_validate_empty_config_against_optional_params_is_clean():
    manifest = {"params": [_param("threshold_s", "float", default=30.0)]}
    assert validate_app_config(manifest, {}) == []


def test_validate_lists_all_unknown_keys():
    manifest = {"params": [_param("a", "int")]}
    errors = validate_app_config(manifest, {"b": 1, "c": 2, "a": 3})
    assert len(errors) == 1
    assert "b" in errors[0] and "c" in errors[0]


def test_validate_missing_required_without_default():
    manifest = {"params": [_param("api_key", "str", required=True)]}
    errors = validate_app_config(manifest, {})
    assert errors and "api_key" in errors[0]


def test_validate_required_with_default_is_satisfied():
    """required + a usable default ⇒ absence is fine (the default rules)."""
    manifest = {
        "params": [_param("labels", "list", required=True, default=["person"])]
    }
    assert validate_app_config(manifest, {}) == []


def test_validate_type_checks_primitives():
    manifest = {
        "params": [
            _param("s", "str"),
            _param("i", "int"),
            _param("f", "float"),
            _param("b", "bool"),
            _param("l", "list"),
        ]
    }
    good = {"s": "x", "i": 3, "f": 0.5, "b": True, "l": [1]}
    assert validate_app_config(manifest, good) == []
    # int is acceptable where float is declared (JSON numbers).
    assert validate_app_config(manifest, {"f": 2}) == []
    # bool must not satisfy int/float despite bool ⊂ int in Python.
    assert validate_app_config(manifest, {"i": True}) != []
    assert validate_app_config(manifest, {"f": False}) != []
    assert validate_app_config(manifest, {"s": 1}) != []
    assert validate_app_config(manifest, {"l": "not-a-list"}) != []


def test_validate_geometry_passthrough():
    """Dotted types are list-shaped on the wire; no deep validation."""
    manifest = {"params": [_param("zone", "geometry.polygon")]}
    assert validate_app_config(manifest, {"zone": [[0, 0], [1, 1], [1, 0]]}) == []
    assert validate_app_config(manifest, {"zone": "not-a-list"}) != []


def test_validate_per_camera_requires_dict_of_typed_values():
    manifest = {
        "params": [_param("dwell", "float", per_camera=True)]
    }
    # Must be a dict keyed by camera id …
    assert validate_app_config(manifest, {"dwell": 3.0}) != []
    # … whose values pass the type check.
    assert validate_app_config(manifest, {"dwell": {"cam-1": 3.0, "cam-2": 5}}) == []
    errors = validate_app_config(manifest, {"dwell": {"cam-1": "slow"}})
    assert errors and "cam-1" in errors[0]


def test_validate_unknown_type_name_is_not_blocking():
    """A manifest with a type name we don't know shouldn't brick config."""
    manifest = {"params": [_param("x", "quaternion")]}
    assert validate_app_config(manifest, {"x": object()}) == []


def test_get_config_with_internal_api_key_succeeds(auth_client):
    """Live config delivery: the RUNNING APP polls its own config with
    the deployment's internal key (the same credential it registers
    with) — no user JWT in a headless container."""
    reg = auth_client.post(
        "/apps/register",
        json={"url": "http://loitering:9200", "manifest": _manifest()},
        headers={"X-Internal-Api-Key": _effective_internal_key()},
    )
    assert reg.status_code == 200

    resp = auth_client.get(
        "/apps/loitering-detection/config",
        headers={"X-Internal-Api-Key": _effective_internal_key()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "loitering-detection"
    assert isinstance(body["config"], dict)

    # And it reflects a PUT (user-JWT path) — the registry is the
    # single source of truth the app converges to.
    put = auth_client.put(
        "/apps/loitering-detection/config",
        json={"watch_labels": ["person", "car"]},
    )
    assert put.status_code == 200
    resp2 = auth_client.get(
        "/apps/loitering-detection/config",
        headers={"X-Internal-Api-Key": _effective_internal_key()},
    )
    assert resp2.json()["config"]["watch_labels"] == ["person", "car"]


def test_get_config_requires_a_credential(auth_client):
    """No JWT, no internal key → 401; wrong key → 401."""
    import httpx as _httpx  # noqa: F401 — parity with sibling tests

    reg = auth_client.post(
        "/apps/register",
        json={"url": "http://loitering:9200", "manifest": _manifest()},
        headers={"X-Internal-Api-Key": _effective_internal_key()},
    )
    assert reg.status_code == 200
    bare = auth_client.get(
        "/apps/loitering-detection/config", headers={"Authorization": ""}
    )
    assert bare.status_code in (401, 403)
    wrong = auth_client.get(
        "/apps/loitering-detection/config",
        headers={"X-Internal-Api-Key": "not-the-key"},
    )
    assert wrong.status_code in (401, 403)


# ── Manifest-declared actions (user-JWT-only proxy) ─────────────────


def _manifest_with_action(**overrides) -> dict:
    m = _manifest(**overrides)
    m["actions"] = [
        {
            "name": "search",
            "label": "Search footage",
            "params": [
                {"name": "query", "type": "str", "required": True,
                 "default": None, "per_camera": False, "description": ""},
                {"name": "limit", "type": "int", "required": False,
                 "default": 10, "per_camera": False, "description": ""},
            ],
            "description": "", "confirm": False,
        }
    ]
    return m


def _register_action_app(auth_client, enabled=True):
    reg = auth_client.post(
        "/apps/register",
        json={"url": "http://loitering:9200", "manifest": _manifest_with_action()},
        headers={"X-Internal-Api-Key": _effective_internal_key()},
    )
    assert reg.status_code == 200
    if enabled:
        en = auth_client.post("/apps/loitering-detection/enable")
        assert en.status_code == 200
    return reg


class _ActionClient:
    """Fake httpx.AsyncClient recording the proxied POST."""

    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).calls.append((url, json, dict(headers or {})))

        class _R:
            status_code = 200

            def json(self):
                return {"results": [{"camera": "cam-1", "caption": "red truck"}]}

        return _R()


def test_action_invoke_happy_path(auth_client, monkeypatch):
    _register_action_app(auth_client)
    _ActionClient.calls = []
    monkeypatch.setattr(apps_router.httpx, "AsyncClient", _ActionClient)

    resp = auth_client.post(
        "/apps/loitering-detection/actions/search",
        json={"query": "red truck", "limit": 5},
    )
    assert resp.status_code == 200
    assert resp.json()["results"][0]["caption"] == "red truck"
    # Proxied to the app's contract surface with the validated params
    # AND the deployment key (the app's action POST is key-gated).
    assert len(_ActionClient.calls) == 1
    url, payload, headers = _ActionClient.calls[0]
    assert url == "http://loitering:9200/actions/search"
    assert payload == {"query": "red truck", "limit": 5}
    assert headers.get("X-Internal-Api-Key") == _effective_internal_key()
    # Audited with param KEYS only — never operator search terms.
    s = auth_client.session_factory()
    try:
        audits = [
            a for a in s.query(AuditLog).all()
            if a.action == "app.action.invoke"
        ]
    finally:
        s.close()
    assert len(audits) == 1
    details = audits[0].details
    if isinstance(details, str):
        import json as _json
        details = _json.loads(details)
    assert details["param_keys"] == ["limit", "query"]
    assert "red truck" not in str(audits[0].details)


def test_action_internal_key_cannot_invoke(monkeypatch):
    """GOVERNANCE: actions are operator verbs — the service key (which
    the OpenNVR Agent holds) must NEVER invoke one. A prompt-injected
    agent must not be able to act on an app. Uses a client with REAL
    auth (no get_current_active_user override): the key alone bounces
    at the door, before any registry lookup or proxying."""
    _ActionClient.calls = []
    monkeypatch.setattr(apps_router.httpx, "AsyncClient", _ActionClient)
    app, _sf, engine = _make_app()
    with TestClient(app) as tc:
        resp = tc.post(
            "/apps/loitering-detection/actions/search",
            json={"query": "x"},
            headers={"X-Internal-Api-Key": _effective_internal_key()},
        )
    engine.dispose()
    assert resp.status_code in (401, 403)
    assert _ActionClient.calls == []  # never reached the app


def test_action_undeclared_is_404(auth_client, monkeypatch):
    _register_action_app(auth_client)
    monkeypatch.setattr(apps_router.httpx, "AsyncClient", _ActionClient)
    resp = auth_client.post(
        "/apps/loitering-detection/actions/enroll-face", json={}
    )
    assert resp.status_code == 404
    assert "declares no action" in resp.json()["detail"]


def test_action_bad_params_is_400(auth_client, monkeypatch):
    _register_action_app(auth_client)
    _ActionClient.calls = []
    monkeypatch.setattr(apps_router.httpx, "AsyncClient", _ActionClient)
    resp = auth_client.post(
        "/apps/loitering-detection/actions/search",
        json={"query": "x", "limit": "ten"},
    )
    assert resp.status_code == 400
    assert _ActionClient.calls == []


def test_action_disabled_app_is_409(auth_client, monkeypatch):
    _register_action_app(auth_client, enabled=False)
    monkeypatch.setattr(apps_router.httpx, "AsyncClient", _ActionClient)
    resp = auth_client.post(
        "/apps/loitering-detection/actions/search", json={"query": "x"}
    )
    assert resp.status_code == 409
