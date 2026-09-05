# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Camera-scope RBAC on the app registry routes.

An app is site-wide, but what it KNOWS is per camera: the occupancy of
each zone, the last plate on each gate, the polygon drawn on each
view. A user assigned two of the site's ten cameras must get exactly
those two cameras' slice of an app — its live state, its per-camera
config — and must not be able to change the app's site-wide settings,
turn it off for everyone, or drive an action at a camera that is not
theirs. Superusers keep the whole site.

Run with:
    cd server && pytest tests/test_apps_camera_scope.py -v
"""
from __future__ import annotations

import datetime as _dt

if not hasattr(_dt, "UTC"):
    _dt.UTC = _dt.timezone.utc  # noqa: UP017

import os
import secrets
import sys
import types as _types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
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

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import core.auth as auth_mod  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import Camera, CameraPermission, InstalledApp, Role, User  # noqa: E402
from routers import apps as apps_router  # noqa: E402
from routers.apps import get_read_principal  # noqa: E402

MANIFEST = {
    "id": "occupancy-counting",
    "name": "Occupancy Counting",
    "version": "1.0.0",
    "category": "analytics",
    "summary": "Counts people per zone.",
    "requires_tasks": ["object_detection"],
    "subscribes": "opennvr.inference.>",
    "params": [
        {"name": "max_occupancy", "required": False, "type": "int",
         "default": 25, "per_camera": False, "description": ""},
        {"name": "zones", "required": False, "type": "geometry.polygon",
         "default": None, "per_camera": True, "description": ""},
    ],
    "emits": [{"name": "over", "severity": "high", "description": ""}],
    "actions": [
        {"name": "reset", "label": "Reset a camera's count",
         "params": [{"name": "camera_id", "required": True, "type": "str",
                     "default": None, "per_camera": False, "description": ""}],
         "description": ""},
        {"name": "export", "label": "Export", "params": [], "description": ""},
    ],
}


@pytest.fixture()
def env():
    """One app, two cameras owned by the admin, a guard granted can_view
    on the gate and can_manage on the yard. The current caller is
    switchable so one client speaks as either user."""
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    s = SessionLocal()
    role = Role(name="admin")
    s.add(role)
    s.flush()
    admin = User(username="admin", email="a@x", hashed_password="x",
                 role_id=role.id, is_superuser=True)
    guard = User(username="guard", email="g@x", hashed_password="x",
                 role_id=role.id)
    s.add_all([admin, guard])
    s.flush()
    gate = Camera(name="Gate", ip_address="10.0.0.1", owner_id=admin.id)
    yard = Camera(name="Yard", ip_address="10.0.0.2", owner_id=admin.id)
    lobby = Camera(name="Lobby", ip_address="10.0.0.3", owner_id=admin.id)
    s.add_all([gate, yard, lobby])
    s.flush()
    s.add_all([
        CameraPermission(user_id=guard.id, camera_id=gate.id,
                         can_view=True, can_manage=False),
        CameraPermission(user_id=guard.id, camera_id=yard.id,
                         can_view=True, can_manage=True),
    ])
    s.add(InstalledApp(
        id="occupancy-counting", name="Occupancy Counting", version="1.0.0",
        url="http://occupancy:9200", manifest_json=MANIFEST, enabled=True,
        config_json={
            "max_occupancy": 25,
            "zones": {f"cam{gate.id}": [[0, 0], [1, 0], [1, 1]],
                      f"cam{yard.id}": [[0, 0], [1, 0], [1, 1]],
                      f"cam{lobby.id}": [[0, 0], [1, 0], [1, 1]]},
        },
    ))
    s.commit()
    ids = {"gate": gate.id, "yard": yard.id, "lobby": lobby.id}
    s.close()

    app = FastAPI()
    app.include_router(apps_router.router)

    def _db():
        sess = SessionLocal()
        try:
            yield sess
        finally:
            sess.close()

    current = {"user": guard}

    def _as_superuser():
        if not current["user"].is_superuser:
            raise HTTPException(status_code=403, detail="Not enough permissions")
        return current["user"]

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[auth_mod.get_current_active_user] = lambda: current["user"]
    app.dependency_overrides[auth_mod.get_current_superuser] = _as_superuser
    app.dependency_overrides[get_read_principal] = lambda: current["user"]
    with TestClient(app) as tc:
        yield tc, current, admin, guard, ids
    engine.dispose()


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _fake_app(monkeypatch, state):
    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            if url.endswith("/health"):
                return _Resp({"status": "ok", "ready": True})
            return _Resp(state)

    monkeypatch.setattr(apps_router.httpx, "AsyncClient", _Client)


def test_status_state_is_trimmed_to_the_users_cameras(env, monkeypatch):
    tc, current, admin, guard, ids = env
    g, y, l = (f"cam{ids[k]}" for k in ("gate", "yard", "lobby"))
    _fake_app(monkeypatch, {
        "total_people": 9,
        "cameras": {g: {"last_count": 2}, y: {"last_count": 3},
                    l: {"last_count": 4}},
        "gate_in_cameras": [g, l],
        "recent": [{"camera_id": l, "plate": "AAA"},
                   {"camera_id": g, "plate": "BBB"}],
    })
    state = tc.get("/apps/occupancy-counting/status").json()["state"]
    assert set(state["cameras"]) == {g, y}
    assert state["gate_in_cameras"] == [g]
    assert state["recent"] == [{"camera_id": g, "plate": "BBB"}]
    assert state["total_people"] == 9          # a roll-up, left alone

    current["user"] = admin
    state = tc.get("/apps/occupancy-counting/status").json()["state"]
    assert set(state["cameras"]) == {g, y, l}


def test_get_config_hides_other_cameras_per_camera_entries(env):
    tc, current, admin, guard, ids = env
    cfg = tc.get("/apps/occupancy-counting/config").json()["config"]
    assert cfg["max_occupancy"] == 25               # site-wide: readable
    assert set(cfg["zones"]) == {f"cam{ids['gate']}", f"cam{ids['yard']}"}
    current["user"] = admin
    cfg = tc.get("/apps/occupancy-counting/config").json()["config"]
    assert len(cfg["zones"]) == 3


def test_put_config_non_superuser_may_only_edit_manageable_cameras(env):
    tc, current, admin, guard, ids = env
    g, y, l = (f"cam{ids[k]}" for k in ("gate", "yard", "lobby"))
    stored = tc.get("/apps/occupancy-counting/config").json()["config"]
    # The guard reads a TRIMMED config (no lobby). The contract is
    # "send back what you read, plus your edits on your cameras": a
    # camera missing from the payload is one they never saw, never a
    # deletion of its zone.
    tri = [[0, 0], [2, 0], [2, 2]]

    # Redrawing the YARD zone (can_manage) is fine.
    ok = dict(stored, zones=dict(stored["zones"], **{y: tri}))
    r = tc.put("/apps/occupancy-counting/config", json=ok)
    assert r.status_code == 200, r.text
    assert r.json()["config"]["zones"][y] == tri
    # …and the lobby's zone (never visible to the guard) survived intact.
    current["user"] = admin
    full = tc.get("/apps/occupancy-counting/config").json()["config"]
    assert l in full["zones"]
    current["user"] = guard

    # Redrawing the GATE zone (view-only) is refused.
    bad = dict(ok, zones=dict(ok["zones"], **{g: tri}))
    r = tc.put("/apps/occupancy-counting/config", json=bad)
    assert r.status_code == 403 and g in r.json()["detail"]

    # Changing a site-wide setting is refused.
    bad = dict(ok, max_occupancy=99)
    r = tc.put("/apps/occupancy-counting/config", json=bad)
    assert r.status_code == 403 and "max_occupancy" in r.json()["detail"]

    # Drawing on a camera the guard cannot even see is refused.
    bad = dict(ok, zones=dict(ok["zones"], **{l: tri}))
    assert tc.put("/apps/occupancy-counting/config", json=bad).status_code == 403

    # A payload that leaves the per-camera key out entirely (the form
    # field was blank) touches nothing — never "erase all my zones".
    r = tc.put("/apps/occupancy-counting/config", json={"max_occupancy": 25})
    assert r.status_code == 200 and r.json()["config"]["zones"][y] == tri

    # The superuser changes anything.
    current["user"] = admin
    r = tc.put("/apps/occupancy-counting/config",
               json=dict(full, max_occupancy=99))
    assert r.status_code == 200 and r.json()["config"]["max_occupancy"] == 99


def test_enable_disable_is_superuser_only(env):
    tc, current, admin, guard, ids = env
    assert tc.post("/apps/occupancy-counting/disable").status_code == 403
    assert tc.post("/apps/occupancy-counting/enable").status_code == 403
    current["user"] = admin
    assert tc.post("/apps/occupancy-counting/disable").json()["enabled"] is False
    assert tc.post("/apps/occupancy-counting/enable").json()["enabled"] is True


def test_camera_targeted_action_needs_manage_on_that_camera(env, monkeypatch):
    tc, current, admin, guard, ids = env
    calls: list[str] = []

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            calls.append(json.get("camera_id", "-"))
            return _Resp({"ok": True})

    monkeypatch.setattr(apps_router.httpx, "AsyncClient", _Client)
    g, y = f"cam{ids['gate']}", f"cam{ids['yard']}"
    # yard: can_manage → allowed; gate: view-only → refused; unknown → refused.
    assert tc.post("/apps/occupancy-counting/actions/reset",
                   json={"camera_id": y}).status_code == 200
    assert tc.post("/apps/occupancy-counting/actions/reset",
                   json={"camera_id": g}).status_code == 403
    assert tc.post("/apps/occupancy-counting/actions/reset",
                   json={"camera_id": "cam999"}).status_code == 403
    # An action with no camera target stays open to any user.
    assert tc.post("/apps/occupancy-counting/actions/export",
                   json={}).status_code == 200
    assert calls == [y, "-"]
