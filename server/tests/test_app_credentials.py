# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Per-app credentials (services/app_keys.py).

Every SDK app used to boot with the deployment's INTERNAL_API_KEY and
could therefore read every camera, every app's config and live state.
Now ``POST /apps/register`` mints the app its own ``oak_…`` key, returned
once; with it the app reads only its own registry rows and only the
cameras the operator assigned to it, and a superuser can rotate or
revoke it without touching the site key.

Run with:
    cd server && pytest tests/test_app_credentials.py -v
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

SITE_KEY = secrets.token_urlsafe(48)
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ["INTERNAL_API_KEY"] = SITE_KEY
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

import core.auth as auth_mod  # noqa: E402
from core.config import settings  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import Camera, InstalledApp, Role, TimelineEvent, User  # noqa: E402
from routers import apps as apps_router  # noqa: E402
from routers import internal_camera_agent as internal_router  # noqa: E402
from services import app_keys  # noqa: E402


def _manifest(app_id="loitering-detection", provides=("loitering",)):
    return {"id": app_id, "name": app_id.title(), "version": "1.0.0",
            "category": "perimeter", "summary": "", "requires_tasks": [],
            "subscribes": "opennvr.inference.>", "params": [], "emits": [],
            "provides": list(provides)}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_key", SITE_KEY, raising=False)
    monkeypatch.setattr(settings, "inference_use_mediamtx_tap", False, raising=False)
    monkeypatch.setattr(auth_mod, "auth_logger", _L(), raising=False)

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
                 role_id=role.id, is_superuser=True, is_active=True)
    s.add(admin)
    s.flush()
    gate = Camera(name="Gate", ip_address="10.0.0.1", owner_id=admin.id, is_active=True,
                  rtsp_url="rtsp://10.0.0.1/s", assignments=[{"skill": "loitering"}])
    yard = Camera(name="Yard", ip_address="10.0.0.2", owner_id=admin.id, is_active=True,
                  rtsp_url="rtsp://10.0.0.2/s",
                  assignments=[{"skill": "license_plate_recognition"}])
    lobby = Camera(name="Lobby", ip_address="10.0.0.3", owner_id=admin.id, is_active=True,
                   rtsp_url="rtsp://10.0.0.3/s")
    s.add_all([gate, yard, lobby])
    s.flush()
    from datetime import datetime, timezone
    for cam in (gate, yard):
        s.add(TimelineEvent(camera_id=cam.id, source="tier0", event_type="track",
                            label="person", started_at=datetime.now(timezone.utc)))
    s.commit()
    ids = {"gate": gate.id, "yard": yard.id, "lobby": lobby.id}
    s.close()

    app = FastAPI()
    app.include_router(apps_router.router)
    app.include_router(internal_router.router)

    def _db():
        sess = SessionLocal()
        try:
            yield sess
        finally:
            sess.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[auth_mod.get_current_superuser] = lambda: admin
    with TestClient(app) as tc:
        yield tc, ids, SessionLocal


def _site():
    return {"X-Internal-Api-Key": SITE_KEY}


def _app(key):
    return {"X-Internal-Api-Key": key}


def _register(tc, headers, **kw):
    body = {"url": "http://loitering:9200", "manifest": _manifest(), **kw}
    return tc.post("/apps/register", json=body, headers=headers)


# ── issuing ─────────────────────────────────────────────────────────────


def test_first_registration_mints_a_key_once(env):
    tc, _, SessionLocal = env
    r = _register(tc, _site(), sdk_version="0.2.0")
    assert r.status_code == 200, r.text
    body = r.json()
    key = body["api_key"]
    assert key.startswith("oak_loitering-detection_") and body["has_api_key"] is True
    assert body["registry"]["api_version"] and body["registry"]["min_sdk_version"]
    # Stored hashed, never in the clear.
    s = SessionLocal()
    row = s.get(InstalledApp, "loitering-detection")
    assert row.api_key_hash == app_keys.hash_key(key) and key not in str(row.__dict__)
    s.close()
    # Re-registering with the SITE key and no wants_key: the key stands,
    # and is NOT returned again.
    r = _register(tc, _site())
    assert r.status_code == 200 and "api_key" not in r.json()
    # Re-registering with its OWN key: accepted, nothing minted.
    r = _register(tc, _app(key))
    assert r.status_code == 200 and "api_key" not in r.json()
    assert r.json()["has_api_key"] is True
    s = SessionLocal()
    assert s.get(InstalledApp, "loitering-detection").api_key_hash == app_keys.hash_key(key)
    s.close()


def test_wants_key_reissues_and_invalidates_the_old_one(env):
    tc, *_ = env
    old = _register(tc, _site()).json()["api_key"]
    new = _register(tc, _site(), wants_key=True).json()["api_key"]
    assert new != old
    assert tc.get("/apps/loitering-detection/config", headers=_app(new)).status_code == 200
    assert tc.get("/apps/loitering-detection/config", headers=_app(old)).status_code == 401


def test_an_app_key_cannot_register_or_read_another_app(env):
    tc, *_ = env
    key = _register(tc, _site()).json()["api_key"]
    other = {"url": "http://lpr:9200", "manifest": _manifest("license-plate-recognition")}
    assert tc.post("/apps/register", json=other, headers=_site()).status_code == 200
    r = tc.post("/apps/register", json=other, headers=_app(key))
    assert r.status_code == 403
    assert tc.get("/apps/license-plate-recognition/config", headers=_app(key)).status_code == 403
    assert tc.get("/apps/license-plate-recognition/status", headers=_app(key)).status_code == 403
    # Its own: fine (status probes the app URL; unreachable degrades, not 4xx).
    assert tc.get("/apps/loitering-detection/config", headers=_app(key)).status_code == 200
    assert tc.get("/apps/loitering-detection/status", headers=_app(key)).status_code == 200


def test_garbage_and_revoked_keys_are_401(env):
    tc, *_ = env
    key = _register(tc, _site()).json()["api_key"]
    assert tc.get("/apps/loitering-detection/config",
                  headers=_app("oak_loitering-detection_" + "0" * 32)).status_code == 401
    assert tc.get("/apps/loitering-detection/config",
                  headers=_app("oak_nonsense")).status_code == 401
    assert tc.delete("/apps/loitering-detection/key").json()["has_api_key"] is False
    assert tc.get("/apps/loitering-detection/config", headers=_app(key)).status_code == 401
    # The site key still works for the platform.
    assert tc.get("/apps/loitering-detection/config", headers=_site()).status_code == 200


def test_rotate_returns_a_fresh_key(env):
    tc, *_ = env
    old = _register(tc, _site()).json()["api_key"]
    new = tc.post("/apps/loitering-detection/key/rotate").json()["api_key"]
    assert new != old
    assert tc.get("/apps/loitering-detection/config", headers=_app(old)).status_code == 401
    assert tc.get("/apps/loitering-detection/config", headers=_app(new)).status_code == 200


# ── the internal door, scoped to the app's roster ──────────────────────


def test_internal_cameras_and_events_follow_the_apps_assignments(env):
    tc, ids, _ = env
    key = _register(tc, _site()).json()["api_key"]
    # Gate is assigned "loitering" (this app's `provides`); yard and lobby are not.
    cams = tc.get("/internal/camera-agent/cameras", headers=_app(key)).json()["cameras"]
    assert [int(c["open_nvr_camera_id"]) for c in cams] == [ids["gate"]]
    # The site key sees the fleet.
    cams = tc.get("/internal/camera-agent/cameras", headers=_site()).json()["cameras"]
    assert sorted(int(c["open_nvr_camera_id"]) for c in cams) == sorted(ids.values())
    # Events likewise.
    ev = tc.get("/internal/camera-agent/events", headers=_app(key)).json()["events"]
    assert {e["camera_id"] for e in ev} == {ids["gate"]}
    ev = tc.get("/internal/camera-agent/events", headers=_site()).json()["events"]
    assert {e["camera_id"] for e in ev} == {ids["gate"], ids["yard"]}


def test_unassigned_app_sees_the_fleet_additive_rule(env):
    """No camera names this app → no restriction declared → everything
    (docs/CAMERA_ASSIGNMENTS.md, same as the SDK's cameras_for_skill)."""
    tc, ids, _ = env
    body = {"url": "http://occ:9200", "manifest": _manifest("occupancy-counting", ("occupancy",))}
    key = tc.post("/apps/register", json=body, headers=_site()).json()["api_key"]
    cams = tc.get("/internal/camera-agent/cameras", headers=_app(key)).json()["cameras"]
    assert sorted(int(c["open_nvr_camera_id"]) for c in cams) == sorted(ids.values())


def test_pipeline_write_routes_refuse_app_keys(env):
    tc, ids, _ = env
    key = _register(tc, _site()).json()["api_key"]
    r = tc.post("/internal/camera-agent/events", headers=_app(key), json={
        "camera_id": ids["gate"], "label": "person",
        "started_at": "2026-09-05T10:00:00+00:00"})
    assert r.status_code == 403
    assert tc.get("/internal/camera-agent/detect-config", headers=_app(key)).status_code == 403


def test_app_roster_resolution_unit():
    """The skill names an app answers to: `provides` + its id both ways."""
    row = _types.SimpleNamespace(id="license-plate-recognition",
                                 manifest_json={"provides": ["license_plate_recognition"]})
    assert app_keys.app_skills(row) == {"license_plate_recognition",
                                        "license-plate-recognition"}
    plain, digest = app_keys.mint_key("x-app")
    assert plain.startswith("oak_x-app_") and digest == app_keys.hash_key(plain)
    assert app_keys.looks_like_app_key(plain) and not app_keys.looks_like_app_key(SITE_KEY)
