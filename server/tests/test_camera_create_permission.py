# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Closing the self-service doors into the camera scope.

Per-camera RBAC (PR #399) makes "which cameras can this user see" the
security boundary — and two routes let a user widen it themselves:
``POST /cameras/`` (any active user became the OWNER of the camera it
added) and ``POST /auth/register`` (anyone reaching the box could mint
a viewer account). Adding a camera now requires the seeded
``cameras.manage`` permission, self-registration is off unless the
operator opts in, and the assignment API gains the read it lacked:
``GET /cameras/{id}/permissions``.

Run with:
    cd server && pytest tests/test_camera_create_permission.py -v
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
sys.modules["core.logging_config"] = _lm

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import core.auth as core_auth  # noqa: E402
from core.config import settings  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import Camera, CameraPermission, Permission, Role, RolePermission, User  # noqa: E402
from routers import auth as auth_router  # noqa: E402
from routers import cameras as cameras_router  # noqa: E402


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(core_auth, "auth_logger", _L(), raising=False)
    monkeypatch.setattr(cameras_router, "camera_logger", _L(), raising=False)
    monkeypatch.setattr(auth_router, "auth_logger", _L(), raising=False)

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    SessionLocal = sessionmaker(bind=eng, expire_on_commit=False)

    db = SessionLocal()
    manage = Permission(name="cameras.manage", description="")
    viewer_role = Role(name="viewer", description="")
    operator_role = Role(name="operator", description="")
    db.add_all([manage, viewer_role, operator_role])
    db.flush()
    db.add(RolePermission(role_id=operator_role.id, permission_id=manage.id))

    def user(name, role, superuser=False):
        u = User(username=name, email=f"{name}@x", hashed_password="x",
                 is_active=True, is_superuser=superuser, password_set=True,
                 role_id=role.id)
        db.add(u)
        db.flush()
        return u

    viewer = user("viewer", viewer_role)
    operator = user("operator", operator_role)
    admin = user("admin", viewer_role, superuser=True)
    db.commit()
    # Role/permissions are lazy relationships; load them while attached.
    for u in (viewer, operator, admin):
        db.refresh(u)
        _ = [p.name for p in (u.role.permissions or [])]
        db.expunge(u)
    db.close()

    app = FastAPI()
    app.include_router(cameras_router.router, prefix="/api/v1")
    app.include_router(auth_router.router, prefix="/api/v1")

    def _db():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    current = {"user": viewer}
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[core_auth.get_current_active_user] = lambda: current["user"]
    with TestClient(app) as tc:
        yield tc, current, {"viewer": viewer, "operator": operator, "admin": admin}, SessionLocal


CAMERA = {"name": "Gate", "ip_address": "10.0.0.7", "port": 554,
          "rtsp_url": "rtsp://10.0.0.7/stream"}


def test_adding_a_camera_needs_cameras_manage(env, monkeypatch):
    tc, current, users, _ = env
    # The create path provisions the stream — keep it out of the test.
    import services.camera_service as cs

    async def _create(db, camera_create, owner_id):
        cam = Camera(name=camera_create.name, ip_address=camera_create.ip_address,
                     port=camera_create.port, rtsp_url=camera_create.rtsp_url,
                     owner_id=owner_id, is_active=True)
        db.add(cam)
        db.commit()
        db.refresh(cam)
        return cam

    monkeypatch.setattr(cs.CameraService, "create_camera", staticmethod(_create))

    r = tc.post("/api/v1/cameras/", json=CAMERA)
    assert r.status_code == 403 and "cameras.manage" in r.json()["detail"]

    current["user"] = users["operator"]
    r = tc.post("/api/v1/cameras/", json=CAMERA)
    assert r.status_code == 200, r.text
    assert r.json()["owner_id"] == users["operator"].id

    current["user"] = users["admin"]          # superusers hold it implicitly
    assert tc.post("/api/v1/cameras/", json=dict(CAMERA, ip_address="10.0.0.8"),
                   params={"force": "true"}).status_code == 200


def test_route_is_gated_structurally():
    """The gate must be the dependency itself, not a check that a later
    refactor could route around."""
    route = next(r for r in cameras_router.router.routes
                 if r.path == "/cameras/" and "POST" in r.methods)
    names = {d.call.__name__ for d in route.dependant.dependencies}
    assert "require_permission[cameras.manage]" in names


def test_list_camera_permissions(env):
    tc, current, users, SessionLocal = env
    s = SessionLocal()
    cam = Camera(name="Yard", ip_address="10.0.0.9", port=554,
                 owner_id=users["operator"].id, is_active=True)
    s.add(cam)
    s.flush()
    s.add(CameraPermission(user_id=users["viewer"].id, camera_id=cam.id,
                           can_view=True, can_manage=False))
    s.commit()
    cam_id = cam.id
    s.close()

    # A stranger to the camera cannot enumerate its grants (403 from the
    # same owner-or-superuser gate that assigning uses).
    current["user"] = users["viewer"]
    assert tc.get(f"/api/v1/cameras/{cam_id}/permissions").status_code == 403

    current["user"] = users["operator"]
    rows = tc.get(f"/api/v1/cameras/{cam_id}/permissions").json()
    assert rows[0] == {"user_id": users["operator"].id, "username": "operator",
                       "can_view": True, "can_manage": True, "is_owner": True}
    assert rows[1] == {"user_id": users["viewer"].id, "username": "viewer",
                       "can_view": True, "can_manage": False, "is_owner": False}

    current["user"] = users["admin"]
    assert len(tc.get(f"/api/v1/cameras/{cam_id}/permissions").json()) == 2


def test_self_registration_is_opt_in(env, monkeypatch):
    tc, *_ = env
    body = {"username": "walkin", "email": "guest@example.com", "password": "Correct-Horse-Battery-9"}
    monkeypatch.setattr(settings, "public_registration_enabled", False)
    r = tc.post("/api/v1/auth/register", json=body)
    assert r.status_code == 403 and "administrator" in r.json()["detail"]
    assert tc.post("/api/v1/auth/check-setup").json()["registration_open"] is False

    monkeypatch.setattr(settings, "public_registration_enabled", True)
    assert tc.post("/api/v1/auth/check-setup").json()["registration_open"] is True
    r = tc.post("/api/v1/auth/register", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["username"] == "walkin"
