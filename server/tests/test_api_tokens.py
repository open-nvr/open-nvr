# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""API tokens (HA-101): a token can never do more than its owner.

Driven over HTTP through the real routers and the real JWT / token
authentication. Pinned here:

* deny by default: routes outside TOKEN_ROUTES answer 403 to a token;
* two-sided permission: the token's scope AND the owner's permission;
  taking a permission away from the owner takes it from the token at once;
* camera gate: a camera outside the allow-list is refused in the path and
  the query, and lists are narrowed to it (for an admin-owned token too);
* never a superuser: superuser-only routes refuse tokens, and the owner's
  row is never modified (TokenPrincipal is read-only);
* revoked / expired / wrong-secret tokens get 401, and an address outside
  allowed_cidrs gets 403;
* a token cannot mint tokens, and a user cannot grant more than they hold;
* every TOKEN_ROUTES entry names a real route (the table cannot rot);
* token requests are audited with actor ``token:<name>``.
"""

from __future__ import annotations

import importlib
import json
import os
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

_LOGGERS = ("main_logger", "auth_logger", "camera_logger", "recording_logger",
            "rtsp_logger", "api_logger", "mediamtx_logger", "config_logger",
            "storage_logger", "stream_logger", "ai_logger", "system_logger",
            "security_logger")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **kw: None


@pytest.fixture(autouse=True)
def _complete_logging_stub():
    lc = sys.modules.get("core.logging_config")
    if lc is not None:
        for name in _LOGGERS:
            if getattr(lc, name, None) is None:
                try:
                    setattr(lc, name, _L())
                except (AttributeError, TypeError):
                    pass
    yield


# The core modules these fixtures share are imported at COLLECTION time on
# purpose: conftest purges core.* modules first imported by a test module
# when that module ends, and a later module reusing this fixture would then
# get fresh copies while routers and middleware kept the old ones (a second
# request-context ContextVar, a second get_db).
import core.auth  # noqa: E402,F401
import core.database  # noqa: E402,F401
import core.permissions  # noqa: E402,F401
import core.request_context  # noqa: E402,F401

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402


@pytest.fixture()
def env(monkeypatch):
    import core.database as cdb
    import models
    from core.auth import create_access_token
    from services import api_tokens

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    monkeypatch.setattr(cdb, "SessionLocal", Session)
    api_tokens._last_used_written.clear()
    api_tokens.invalidate_caches()

    s = Session()
    perms = {n: models.Permission(name=n, description=n) for n in (
        "full_access", "cameras.view", "cameras.manage", "live.view",
        "recordings.view", "settings.view", "ptz.control", "api_tokens.manage")}
    s.add_all(perms.values())
    s.commit()
    admin_role = models.Role(name="admin")
    viewer_role = models.Role(name="viewer")
    s.add_all([admin_role, viewer_role])
    s.commit()
    s.add(models.RolePermission(role_id=admin_role.id, permission_id=perms["full_access"].id))
    for n in ("cameras.view", "live.view", "recordings.view", "settings.view",
              "api_tokens.manage"):
        s.add(models.RolePermission(role_id=viewer_role.id, permission_id=perms[n].id))
    admin = models.User(username="admin", email="a@x", hashed_password="h",
                        is_active=True, is_superuser=True, role_id=admin_role.id)
    viewer = models.User(username="vera", email="v@x", hashed_password="h",
                         is_active=True, is_superuser=False, role_id=viewer_role.id)
    s.add_all([admin, viewer])
    s.commit()
    for cid in (1, 2, 3):
        s.add(models.Camera(id=cid, name=f"c{cid}", ip_address=f"192.0.2.{cid}",
                            rtsp_url=f"rtsp://192.0.2.{cid}/s", owner_id=admin.id,
                            is_active=True))
    s.commit()
    s.add(models.CameraPermission(user_id=viewer.id, camera_id=3, can_view=True))
    s.commit()
    ids = {"admin": admin.id, "viewer": viewer.id, "viewer_role": viewer_role.id,
           "live": perms["live.view"].id}
    s.close()

    from core.database import get_db
    from middleware.request_logging import RequestLoggingMiddleware

    def _db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)
    routers = [importlib.import_module(m) for m in (
        "routers.system", "routers.cameras", "routers.api_tokens",
        "routers.recordings", "routers.audit_logs", "routers.events",
        "routers.timeline_events", "routers.zones", "routers.live_state", "routers.media",
        "routers.alerts_inbox", "routers.site_mode", "routers.entities",
        "routers.search")]
    for mod in routers:
        app.include_router(mod.router, prefix="/api/v1")
    # Override EVERY get_db these routers depend on, not just the one in
    # sys.modules now: conftest purges core.* modules first imported by a
    # test module, so a router imported by an earlier module can still hold
    # (through core.auth / core.permissions) the get_db of a previous
    # core.database object, and would otherwise reach the real database.
    for g in {get_db, *_reachable_get_dbs(routers)}:
        app.dependency_overrides[g] = _db

    async def _fake_stats(db, cam):
        return {"camera_id": cam.id}

    import services.camera_stats as cs
    monkeypatch.setattr(cs, "get_camera_stats", _fake_stats)

    def jwt_for(username):
        return {"Authorization": f"Bearer {create_access_token({'sub': username})}"}

    client = TestClient(app, client=("192.168.1.20", 50000))
    return type("Env", (), {"client": client, "Session": Session, "ids": ids,
                            "jwt": staticmethod(jwt_for), "models": models})


def _reachable_get_dbs(modules):
    found = set()
    for mod in modules:
        for value in vars(mod).values():
            for fn in (value, getattr(value, "__init__", None), getattr(value, "__call__", None)):
                g = getattr(fn, "__globals__", None)
                if isinstance(g, dict) and callable(g.get("get_db")):
                    found.add(g["get_db"])
    return found


def _mint(env, who="admin", **body):
    body.setdefault("name", "ha-main")
    body.setdefault("scopes", ["settings.view", "cameras.view", "live.view",
                               "recordings.view"])
    r = env.client.post("/api/v1/api-tokens", json=body, headers=env.jwt(who))
    assert r.status_code == 201, r.text
    return r.json()


def _as(token):
    return {"Authorization": f"Bearer {token}"}


def test_create_returns_the_secret_once_and_stores_only_a_hash(env):
    out = _mint(env)
    assert out["token"].startswith("onvr_")
    listed = env.client.get("/api/v1/api-tokens", headers=env.jwt("admin")).json()["tokens"]
    assert listed[0]["prefix"] == out["prefix"] and "token" not in listed[0]
    s = env.Session()
    row = s.query(env.models.ApiToken).one()
    assert row.token_hash != out["token"] and out["token"] not in json.dumps(
        {k: str(v) for k, v in row.__dict__.items()})
    s.close()


def test_listed_route_with_scope_works(env):
    tok = _mint(env)["token"]
    r = env.client.get("/api/v1/system/info", headers=_as(tok))
    assert r.status_code == 200 and r.json()["site_id"]


def test_route_outside_the_table_is_refused(env):
    tok = _mint(env)["token"]
    # superuser-only and simply-unlisted routes alike
    assert env.client.get("/api/v1/audit-logs/", headers=_as(tok)).status_code == 403
    r = env.client.get("/api/v1/api-tokens", headers=_as(tok))
    assert r.status_code == 403 and "not available to API tokens" in r.text


def test_a_token_cannot_mint_tokens(env):
    tok = _mint(env)["token"]
    r = env.client.post("/api/v1/api-tokens", headers=_as(tok),
                        json={"name": "x", "scopes": ["cameras.view"]})
    assert r.status_code == 403


def test_scope_is_required(env):
    tok = _mint(env, scopes=["cameras.view"])["token"]
    r = env.client.get("/api/v1/system/info", headers=_as(tok))
    assert r.status_code == 403 and "settings.view" in r.text


def test_owner_losing_a_permission_takes_it_from_the_token(env):
    tok = _mint(env, who="vera", scopes=["settings.view"])["token"]
    assert env.client.get("/api/v1/system/info", headers=_as(tok)).status_code == 200
    s = env.Session()
    settings_perm = s.query(env.models.Permission).filter_by(name="settings.view").one()
    s.query(env.models.RolePermission).filter_by(
        role_id=env.ids["viewer_role"], permission_id=settings_perm.id).delete()
    s.commit()
    s.close()
    assert env.client.get("/api/v1/system/info", headers=_as(tok)).status_code == 403


def test_system_info_describes_the_calling_token(env):
    tok = _mint(env, who="vera", scopes=["settings.view", "cameras.view"], camera_ids=[3],
                expires_in_days=30)["token"]
    s = env.Session()
    cams = s.query(env.models.Permission).filter_by(name="cameras.view").one()
    s.query(env.models.RolePermission).filter_by(
        role_id=env.ids["viewer_role"], permission_id=cams.id).delete()
    s.commit()
    s.close()
    caller = env.client.get("/api/v1/system/info", headers=_as(tok)).json()["caller"]
    # Only what the owner still holds; never the secret.
    assert caller["kind"] == "token" and caller["name"] == "ha-main"
    assert caller["scopes"] == ["settings.view"] and caller["camera_ids"] == [3]
    left = datetime.fromisoformat(caller["expires_at"]) - datetime.now(UTC)
    assert timedelta(days=29) < left <= timedelta(days=30)
    assert tok not in json.dumps(caller)
    user = env.client.get("/api/v1/system/info", headers=env.jwt("admin")).json()["caller"]
    assert user == {"kind": "user", "username": "admin"}


def test_system_info_reports_time_and_network(env, monkeypatch):
    from core.config import settings

    info = env.client.get("/api/v1/system/info", headers=env.jwt("admin")).json()
    skew = datetime.fromisoformat(info["server_time"]) - datetime.now(UTC)
    assert abs(skew.total_seconds()) < 5
    assert info["network"]["rtsps_exposed"] is False          # default: loopback only
    assert isinstance(info["network"]["webrtc_ice_hosts"], bool)
    monkeypatch.setattr(settings, "mediamtx_external_rtsps_url", "rtsps://192.168.1.20:8322")
    info = env.client.get("/api/v1/system/info", headers=env.jwt("admin")).json()
    assert info["network"]["rtsps_exposed"] is True
    monkeypatch.setattr(settings, "mediamtx_external_rtsps_url", "rtsps://127.0.0.1:8322")
    info = env.client.get("/api/v1/system/info", headers=env.jwt("admin")).json()
    assert info["network"]["rtsps_exposed"] is False


def test_camera_gate_in_path_and_query(env):
    tok = _mint(env, camera_ids=[1])["token"]
    assert env.client.get("/api/v1/cameras/1/stats", headers=_as(tok)).status_code == 200
    r = env.client.get("/api/v1/cameras/2/stats", headers=_as(tok))
    assert r.status_code == 403 and "camera" in r.text
    start = datetime.now(UTC).isoformat()
    r = env.client.post("/api/v1/recordings/export/ticket",
                        params={"camera_id": 2, "start": start, "duration": 5},
                        headers=_as(tok))
    assert r.status_code == 403


def test_camera_list_is_narrowed_for_an_admin_owned_token(env):
    tok = _mint(env, camera_ids=[1, 3])["token"]
    cams = env.client.get("/api/v1/cameras/", headers=_as(tok)).json()["cameras"]
    assert sorted(c["id"] for c in cams) == [1, 3]
    # an admin-owned token WITHOUT an allow-list sees everything the admin sees
    tok_all = _mint(env, name="all")["token"]
    cams = env.client.get("/api/v1/cameras/", headers=_as(tok_all)).json()["cameras"]
    assert sorted(c["id"] for c in cams) == [1, 2, 3]


def test_viewer_token_sees_only_what_the_viewer_sees(env):
    tok = _mint(env, who="vera", scopes=["cameras.view"])["token"]
    cams = env.client.get("/api/v1/cameras/", headers=_as(tok)).json()["cameras"]
    assert [c["id"] for c in cams] == [3]


def test_revoked_expired_and_forged_tokens_are_401(env):
    out = _mint(env)
    tok = out["token"]
    forged = tok[:-4] + ("AAAA" if not tok.endswith("AAAA") else "BBBB")
    assert env.client.get("/api/v1/system/info", headers=_as(forged)).status_code == 401

    s = env.Session()
    row = s.query(env.models.ApiToken).one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    s.commit()
    s.close()
    assert env.client.get("/api/v1/system/info", headers=_as(tok)).status_code == 401

    tok2 = _mint(env, name="second")["token"]
    new_id = [t for t in env.client.get("/api/v1/api-tokens", headers=env.jwt("admin"))
              .json()["tokens"] if t["name"] == "second"][0]["id"]
    assert env.client.delete(f"/api/v1/api-tokens/{new_id}",
                             headers=env.jwt("admin")).status_code == 200
    assert env.client.get("/api/v1/system/info", headers=_as(tok2)).status_code == 401


def test_allowed_cidrs(env):
    inside = _mint(env, name="lan", allowed_cidrs=["192.168.1.0/24"])["token"]
    outside = _mint(env, name="far", allowed_cidrs=["10.0.0.0/8"])["token"]
    assert env.client.get("/api/v1/system/info", headers=_as(inside)).status_code == 200
    r = env.client.get("/api/v1/system/info", headers=_as(outside))
    # Named, so a client can tell "fix the token's addresses" from a missing scope.
    assert r.status_code == 403 and r.headers["X-OpenNVR-Error"] == "token_address"
    r = env.client.get("/api/v1/system/info", headers=_as(_mint(env, name="x",
                                                                 scopes=["cameras.view"])["token"]))
    assert r.status_code == 403 and "X-OpenNVR-Error" not in r.headers


def test_cannot_grant_more_than_you_hold(env):
    h = env.jwt("vera")
    r = env.client.post("/api/v1/api-tokens", headers=h,
                        json={"name": "x", "scopes": ["cameras.manage"]})
    assert r.status_code == 403
    r = env.client.post("/api/v1/api-tokens", headers=h,
                        json={"name": "x", "scopes": ["cameras.view"], "camera_ids": [1]})
    assert r.status_code == 403
    r = env.client.post("/api/v1/api-tokens", headers=env.jwt("admin"),
                        json={"name": "x", "scopes": ["full_access"]})
    assert r.status_code == 422


def test_the_owner_row_is_never_modified(env):
    from services.api_tokens import TokenPrincipal

    tok = _mint(env)["token"]
    for path in ("/api/v1/system/info", "/api/v1/cameras/", "/api/v1/audit-logs/"):
        env.client.get(path, headers=_as(tok))
    s = env.Session()
    admin = s.get(env.models.User, env.ids["admin"])
    assert admin.is_superuser is True
    p = TokenPrincipal(admin, 1, "t", frozenset(), None)
    assert p.is_superuser is False and p.username == "admin"
    with pytest.raises(AttributeError):
        p.is_superuser = False
    with pytest.raises(AttributeError):
        p.username = "x"
    s.close()


def test_token_requests_are_audited_with_the_token_actor(env):
    tok = _mint(env, name="ha-main", camera_ids=[1])["token"]
    start = datetime.now(UTC).isoformat()
    r = env.client.post("/api/v1/recordings/export/ticket",
                        params={"camera_id": 1, "start": start, "duration": 5},
                        headers={**_as(tok), "X-Correlation-Id": "ha-ctx-7"})
    assert r.status_code == 200, r.text
    s = env.Session()
    row = s.query(env.models.AuditLog).filter_by(action="recording.export").one()
    assert json.loads(row.details)["actor"] == "token:ha-main"
    assert row.correlation_id == "ha-ctx-7" and row.user_id == env.ids["admin"]
    s.close()


def test_last_used_is_recorded_but_throttled(env):
    out = _mint(env)
    for _ in range(3):
        env.client.get("/api/v1/system/info", headers=_as(out["token"]))
    s = env.Session()
    row = s.query(env.models.ApiToken).one()
    assert row.last_used_at is not None and row.last_used_ip == "192.168.1.20"
    s.close()
    from services import api_tokens
    assert len(api_tokens._last_used_written) == 1


def test_firewall_validity_cache(env):
    from services import api_tokens

    tok = _mint(env)["token"]
    assert api_tokens.is_valid_token_cached(tok) is True
    assert api_tokens.is_valid_token_cached("onvr_deadbeef_" + "x" * 40) is False
    assert api_tokens.is_valid_token_cached("eyJhbGciOi.jwt") is False


def test_random_bearers_never_cost_a_query(env, monkeypatch):
    """M1 review: each unknown ``onvr_`` bearer used to be a DB round trip on
    the event loop. Now one table load per TTL, whatever the traffic; a
    revoke or a mint reloads at once."""
    from services import api_tokens

    out = _mint(env)
    loads = []
    real = api_tokens._reload_live
    monkeypatch.setattr(api_tokens, "_reload_live", lambda: loads.append(1) or real())
    api_tokens.invalidate_caches()
    for i in range(50):
        assert api_tokens.is_valid_token_cached(f"onvr_{i:08d}_" + "x" * 40) is False
    assert api_tokens.is_valid_token_cached(out["token"]) is True
    assert len(loads) == 1
    env.client.delete(f"/api/v1/api-tokens/{out['id']}", headers=env.jwt("admin"))
    assert api_tokens.is_valid_token_cached(out["token"]) is False
    assert len(loads) == 2


def test_every_token_route_is_a_real_route():
    """The table must track reality: a renamed route would otherwise leave a
    dead entry (harmless) or, worse, a wrong one."""
    from services.api_tokens import TOKEN_ROUTES

    app = FastAPI()
    for mod in ("routers.system", "routers.cameras", "routers.recordings",
                "routers.streams", "routers.timeline_events", "routers.alerts_inbox",
                "routers.events", "routers.zones", "routers.live_state", "routers.media",
                "routers.site_mode", "routers.entities", "routers.search",
                "routers.api_tokens"):
        app.include_router(importlib.import_module(mod).router, prefix="/api/v1")
    # OpenAPI paths are full templates on every FastAPI version; app.routes
    # nests included routers from 0.140 on.
    real = {(m.upper(), path) for path, ops in app.openapi()["paths"].items() for m in ops}
    missing = [k for k in TOKEN_ROUTES if k not in real]
    assert missing == [], missing


@pytest.mark.parametrize("path_params, query, allowed", [
    ({"camera_id": "1"}, "", True),
    ({"camera_id": "2"}, "", False),
    ({}, "camera_id=2", False),
    ({}, "cam_id=2", False),
    ({}, "camera=cam2", False),
    ({}, "camera=cam1", True),
    ({}, "camera_ids=1,2", False),
    ({}, "camera_ids=1", True),
    ({}, "path=cam-2", False),
    ({}, "path=cam-1", True),
    ({}, "camera_id=abc", False),     # unparseable: fail closed
    ({}, "unrelated=2", True),
])
def test_central_camera_gate_on_its_own(env, path_params, query, allowed):
    """The gate itself, independent of any route's own camera check (several
    routes double-check; routes that do not rely on this alone)."""
    from types import SimpleNamespace

    from fastapi import HTTPException
    from starlette.datastructures import QueryParams

    from services import api_tokens

    tok = _mint(env, camera_ids=[1])["token"]
    cam = path_params.get("camera_id", "1")
    req = SimpleNamespace(
        method="GET",
        scope={"route": SimpleNamespace(path="/api/v1/cameras/{camera_id}/stats"),
               "path": f"/api/v1/cameras/{cam}/stats",
               "path_params": {"camera_id": cam}},
        path_params=path_params,
        query_params=QueryParams(query),
        client=SimpleNamespace(host="192.168.1.20"),
        headers={},
    )
    s = env.Session()
    try:
        if allowed:
            assert api_tokens.authorize_request(req, s, tok).camera_ids == frozenset({1})
        else:
            with pytest.raises(HTTPException) as exc:
                api_tokens.authorize_request(req, s, tok)
            assert exc.value.status_code == 403
    finally:
        s.close()


@pytest.mark.parametrize("route_path, path, params, expected", [
    # FastAPI < 0.140: the matched route carries the full template.
    ("/api/v1/cameras/{camera_id}/stats", "/api/v1/cameras/7/stats",
     {"camera_id": 7}, "/api/v1/cameras/{camera_id}/stats"),
    # FastAPI >= 0.140: only the part below the include prefix.
    ("/cameras/{camera_id}/stats", "/api/v1/cameras/7/stats",
     {"camera_id": 7}, "/api/v1/cameras/{camera_id}/stats"),
    ("/system/info", "/api/v1/system/info", {}, "/api/v1/system/info"),
    # Converter syntax in the template.
    ("/recordings/{p:path}", "/api/v1/recordings/a/b.mp4",
     {"p": "a/b.mp4"}, "/api/v1/recordings/{p:path}"),
    # Rendered template doesn't line up with the real path: deny.
    ("/cameras/{camera_id}/stats", "/api/v1/cameras/007/stats", {"camera_id": 7}, None),
])
def test_route_key_rebuilds_the_full_template(route_path, path, params, expected):
    from types import SimpleNamespace

    from starlette.routing import compile_path

    from services.api_tokens import _route_key

    _, _, convertors = compile_path(route_path)
    req = SimpleNamespace(method="get", scope={
        "route": SimpleNamespace(path=route_path, param_convertors=convertors),
        "path": path, "path_params": params, "root_path": ""})
    assert _route_key(req) == (("GET", expected) if expected else None)


def test_route_key_on_the_installed_fastapi():
    """End to end through the real router nesting of whatever FastAPI is
    installed, so a version change can't silently turn every token into a 403."""
    from fastapi import APIRouter

    from services.api_tokens import _route_key

    seen = {}
    r = APIRouter(prefix="/cameras")

    @r.get("/{camera_id}/stats")
    def stats(camera_id: int, request: Request):
        seen["key"] = _route_key(request)
        return {}

    app = FastAPI()
    app.include_router(r, prefix="/api/v1")
    assert TestClient(app).get("/api/v1/cameras/3/stats").status_code == 200
    assert seen["key"] == ("GET", "/api/v1/cameras/{camera_id}/stats")


# ── events WebSocket (HA-103) ─────────────────────────────────────────────


def _ws_ticket(env, token):
    return env.client.post("/api/v1/events/ws-ticket", headers=_as(token))


def _open(client, ticket, **params):
    q = "&".join([f"ticket={ticket}"] + [f"{k}={v}" for k, v in params.items()])
    return client.websocket_connect(f"/api/v1/events/ws?{q}")


def test_a_token_socket_keeps_the_tokens_cameras_and_scopes(env):
    tok = _mint(env, camera_ids=[1])["token"]  # no alerts.view
    r = _ws_ticket(env, tok)
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "token"
    with _open(env.client, r.json()["ticket"]) as ws:
        hello = ws.receive_json()
    assert hello["event_type"] == "subscribed"
    assert hello["filters"]["event_types"] == sorted(
        ["camera_status", "camera_event", "tracks", "inference_result", "inference_error",
         "live_state", "media_ready", "site_mode", "entity_state", "descriptors_changed"])

    # A camera outside the allow-list is refused, not silently empty.
    from starlette.websockets import WebSocketDisconnect

    t2 = _ws_ticket(env, tok).json()["ticket"]
    with pytest.raises(WebSocketDisconnect) as exc:
        with _open(env.client, t2, camera_id=2) as ws:
            ws.receive_json()
    assert exc.value.reason == "forbidden"


def test_a_token_socket_is_scoped_to_what_the_owner_sees(env):
    """vera sees only camera 3; her token with no allow-list gets exactly that."""
    from starlette.websockets import WebSocketDisconnect

    tok = _mint(env, who="vera", scopes=["cameras.view"])["token"]
    t = _ws_ticket(env, tok).json()["ticket"]
    with pytest.raises(WebSocketDisconnect):
        with _open(env.client, t, camera_id=1) as ws:
            ws.receive_json()
    t = _ws_ticket(env, tok).json()["ticket"]
    with _open(env.client, t, camera_id=3) as ws:
        assert ws.receive_json()["filters"]["event_types"] == [
            "camera_status", "descriptors_changed", "entity_state", "live_state"]


def test_ws_ticket_needs_cameras_view(env):
    tok = _mint(env, scopes=["settings.view"])["token"]
    assert _ws_ticket(env, tok).status_code == 403


def test_the_token_is_rechecked_when_the_socket_opens(env):
    """The ticket lives 30 s: a revoke inside that window must still win."""
    from starlette.websockets import WebSocketDisconnect

    out = _mint(env)
    t = _ws_ticket(env, out["token"]).json()["ticket"]
    env.client.delete(f"/api/v1/api-tokens/{out['id']}", headers=env.jwt("admin"))
    with pytest.raises(WebSocketDisconnect) as exc:
        with _open(env.client, t) as ws:
            ws.receive_json()
    assert exc.value.reason == "unauthorized"


def test_allowed_networks_apply_to_the_socket_address(env):
    from starlette.websockets import WebSocketDisconnect

    tok = _mint(env, allowed_cidrs=["192.168.1.0/24"])["token"]
    t = _ws_ticket(env, tok).json()["ticket"]  # minted from 192.168.1.20
    elsewhere = TestClient(env.client.app, client=("10.9.9.9", 50000))
    with pytest.raises(WebSocketDisconnect):
        with _open(elsewhere, t) as ws:
            ws.receive_json()


def test_the_ws_ticket_route_and_the_handshake_need_the_same_scope():
    from services import api_tokens

    assert api_tokens.TOKEN_ROUTES[("POST", "/api/v1/events/ws-ticket")] ==         api_tokens.WS_TICKET_SCOPE


def test_bus_event_type_entitlement():
    from services.event_bus_service import _Subscriber

    sub = _Subscriber(10, None, None, frozenset({1}), frozenset({"tracks"}))
    assert sub.matches({"event_type": "tracks", "camera_id": 1})
    assert not sub.matches({"event_type": "app_alert", "camera_id": 1})
    assert not sub.matches({"event_type": "tracks", "camera_id": 2})
    assert not sub.matches({"event_type": "brand_new_type", "camera_id": 1})
    anyone = _Subscriber(10, None, None, None, None)
    assert anyone.matches({"event_type": "brand_new_type", "camera_id": 9})


# ── camera detection flag (HA-106) ────────────────────────────────────────


def test_detection_flag_round_trips_and_is_audited_with_its_reason(env):
    r = env.client.put("/api/v1/cameras/1", headers=env.jwt("admin"),
                       json={"detection_enabled": False, "reason": "away mode"})
    assert r.status_code == 200, r.text
    assert r.json()["detection_enabled"] is False
    s = env.Session()
    try:
        cam = s.get(env.models.Camera, 1)
        assert cam.detection_enabled is False
        assert not hasattr(cam, "reason")
        row = (s.query(env.models.AuditLog).filter_by(action="camera.update")
               .order_by(env.models.AuditLog.id.desc()).first())
        details = json.loads(row.details)
        assert details["reason"] == "away mode"
        assert details["detection_enabled"] == {"from": True, "to": False}
        assert "reason" not in details["updated_fields"]
    finally:
        s.close()
    # Cameras from before the column (NULL) read as on.
    assert env.client.get("/api/v1/cameras/2", headers=env.jwt("admin")).json()[
        "detection_enabled"] is True


def test_a_token_may_toggle_detection_and_nothing_else(env):
    tok = _mint(env, scopes=["cameras.view", "cameras.manage"], camera_ids=[1])["token"]
    r = env.client.put("/api/v1/cameras/1", headers=_as(tok),
                       json={"detection_enabled": False, "reason": "ha: away"})
    assert r.status_code == 200, r.text
    for body in ({"name": "renamed"}, {"rtsp_url": "rtsp://evil/s"},
                 {"is_active": False}, {"password": "x"}):
        r = env.client.put("/api/v1/cameras/1", headers=_as(tok), json=body)
        assert r.status_code == 403, (body, r.text)
    # Not on a camera outside its allow-list.
    r = env.client.put("/api/v1/cameras/2", headers=_as(tok), json={"detection_enabled": False})
    assert r.status_code == 403
    s = env.Session()
    try:
        assert s.get(env.models.Camera, 1).name == "c1"
        row = (s.query(env.models.AuditLog).filter_by(action="camera.update")
               .order_by(env.models.AuditLog.id.desc()).first())
        assert json.loads(row.details)["actor"].startswith("token:")
    finally:
        s.close()


def test_toggling_detection_needs_the_manage_scope(env):
    tok = _mint(env, scopes=["cameras.view"])["token"]
    r = env.client.put("/api/v1/cameras/1", headers=_as(tok), json={"detection_enabled": False})
    assert r.status_code == 403


def test_a_token_is_judged_by_its_owners_camera_rights(env):
    """An admin's token reaches a camera another user owns (the admin can);
    a viewer's token cannot manage cameras the viewer only sees."""
    s = env.Session()
    try:
        s.get(env.models.Camera, 2).owner_id = env.ids["viewer"]
        s.commit()
    finally:
        s.close()
    tok = _mint(env, scopes=["cameras.view", "cameras.manage"])["token"]
    assert env.client.put("/api/v1/cameras/2", headers=_as(tok),
                          json={"detection_enabled": False}).status_code == 200


@pytest.mark.parametrize("version", ["1", "2"])
def test_revoking_a_token_closes_its_open_sockets(env, monkeypatch, version):  # noqa: F811
    """M1 review: sockets used to live on after a revoke until the client
    reconnected (days, for Home Assistant). Now re-checked while open."""
    ev = importlib.import_module("routers.events")
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setattr(ev, "WS_RECHECK_S", 0.2)
    monkeypatch.setattr(ev, "V2_HEARTBEAT_S", 0.2)   # the v2 loop wakes on it
    out = _mint(env)
    t = _ws_ticket(env, out["token"]).json()["ticket"]
    with _open(env.client, t, v=version) as ws:
        ws.receive_json()
        if version == "2":
            ws.receive_json()          # the snapshot
        env.client.delete(f"/api/v1/api-tokens/{out['id']}", headers=env.jwt("admin"))
        with pytest.raises(WebSocketDisconnect) as exc:
            for _ in range(20):
                ws.receive_json()
    assert exc.value.code == 4401


def test_a_scope_change_closes_the_socket_too(env, monkeypatch):  # noqa: F811
    ev = importlib.import_module("routers.events")
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setattr(ev, "WS_RECHECK_S", 0.2)
    monkeypatch.setattr(ev, "V2_HEARTBEAT_S", 0.2)   # the v2 loop wakes on it
    tok = _mint(env, who="vera", scopes=["cameras.view", "live.view"])["token"]
    t = _ws_ticket(env, tok).json()["ticket"]
    with _open(env.client, t, v="2") as ws:
        ws.receive_json()
        ws.receive_json()
        # vera's role loses live.view: the token's event types shrink.
        s = env.Session()
        s.query(env.models.RolePermission).filter_by(
            role_id=env.ids["viewer_role"], permission_id=env.ids["live"]).delete()
        s.commit()
        s.close()
        with pytest.raises(WebSocketDisconnect) as exc:
            for _ in range(20):
                ws.receive_json()
    assert exc.value.code == 4401



# ── card session tokens (HA-304) ───────────────────────────────────────


def _session(env, parent_token, **body):
    return env.client.post("/api/v1/api-tokens/session", json=body, headers=_as(parent_token))


def test_a_token_opens_a_read_only_session(env):
    parent = _mint(env, scopes=["settings.view", "cameras.view", "cameras.manage",
                                "live.view"], camera_ids=[1, 3])["token"]
    r = _session(env, parent, ttl_s=300)
    assert r.status_code == 201, r.text
    out = r.json()
    # Reading only, the parent's cameras, at most the asked lifetime.
    assert out["scopes"] == ["cameras.view", "live.view", "settings.view"]
    assert out["camera_ids"] == [1, 3]
    left = datetime.fromisoformat(out["expires_at"]) - datetime.now(UTC)
    assert timedelta(seconds=290) < left <= timedelta(seconds=300)
    child = out["token"]
    assert env.client.get("/api/v1/cameras/1/stats", headers=_as(child)).status_code == 200
    assert env.client.get("/api/v1/cameras/2/stats", headers=_as(child)).status_code == 403
    r = env.client.put("/api/v1/cameras/1", json={"detection_enabled": False},
                       headers=_as(child))
    assert r.status_code == 403                          # the parent could; the card can't
    # Not listed with the user's tokens.
    listed = env.client.get("/api/v1/api-tokens", headers=env.jwt("admin")).json()["tokens"]
    assert [x["name"] for x in listed] == ["ha-main"]


def test_session_limits(env):
    parent = _mint(env, camera_ids=[1], expires_in_days=1)["token"]
    assert _session(env, parent, camera_ids=[2]).status_code == 403   # not its camera
    assert _session(env, parent, ttl_s=3600).status_code == 422       # 10 min at most
    child = _session(env, parent).json()["token"]
    assert _session(env, child).status_code == 403                    # no nesting
    r = env.client.post("/api/v1/api-tokens/session", json={}, headers=env.jwt("admin"))
    assert r.status_code == 403                                       # tokens only
    # Never past the parent's own expiry.
    s = env.Session()
    row = s.query(env.models.ApiToken).filter_by(name="ha-main").one()
    row.expires_at = datetime.now(UTC) + timedelta(seconds=90)
    s.commit()
    s.close()
    out = _session(env, parent, ttl_s=600).json()
    left = datetime.fromisoformat(out["expires_at"]) - datetime.now(UTC)
    assert left <= timedelta(seconds=90)


def test_revoking_the_parent_ends_its_sessions(env):
    minted = _mint(env)
    child = _session(env, minted["token"]).json()["token"]
    assert env.client.get("/api/v1/cameras/", headers=_as(child)).status_code == 200
    env.client.delete(f"/api/v1/api-tokens/{minted['id']}", headers=env.jwt("admin"))
    assert env.client.get("/api/v1/cameras/", headers=_as(child)).status_code == 401


def test_system_info_publishes_the_passthrough_allowlist(env):
    info = env.client.get("/api/v1/system/info", headers=env.jwt("admin")).json()
    assert "/api/v1/cameras/" in info["passthrough_allowlist"]
    assert not any("api-tokens" in p for p in info["passthrough_allowlist"])
