"""`q` on GET /cameras/ — the Cameras page search box.

The UI always sent ``q``; the endpoint never declared it, so FastAPI dropped it
silently and the box filtered nothing while looking alive. The contract pinned
here:

* no ``q`` (or a blank one) returns exactly the unfiltered list;
* ``q`` matches a camera's name or IP address, case-insensitively, as a
  substring;
* ``total`` counts the matches, not the whole fleet, so paging stays right;
* LIKE wildcards in ``q`` are literal: "cam_1" does not match "camX1";
* it never widens what a non-superuser can see.

And the Location filter beside it: ``location`` picks one location (trimmed,
any case), ``no_location`` the cameras without one, ``q`` matches a
location too, and GET /cameras/locations lists the locations the caller can
see, with counts, for the filter's menu.

Run with:

    cd server && pytest tests/test_cameras_search.py -v
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

os.environ.setdefault("DATABASE_URL", "sqlite:///./_cameras_search_test.db")
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

import core.auth as core_auth  # noqa: E402
import services.camera_status_service as css  # noqa: E402
from core.auth import create_access_token, get_password_hash  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import Camera, Role, User  # noqa: E402
from routers import cameras as cameras_router  # noqa: E402
from services.camera_status_service import CameraStatusService  # noqa: E402

PASSWORD = "Str0ng!passw0rd"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(core_auth, "auth_logger", _L(), raising=False)
    monkeypatch.setattr(cameras_router, "camera_logger", _L(), raising=False)
    monkeypatch.setattr(css, "_service", CameraStatusService())

    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    session_factory = sessionmaker(bind=eng)

    app = FastAPI()
    app.include_router(cameras_router.router, prefix="/api/v1")

    def _get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db

    db = session_factory()
    role = Role(name="admin", description="test role")
    db.add(role)
    db.flush()

    def make_user(username: str, superuser: bool) -> User:
        u = User(mfa_enabled=True, 
            username=username,
            email=f"{username}@example.com",
            hashed_password=get_password_hash(PASSWORD),
            is_active=True,
            is_superuser=superuser,
            password_set=True,
            role_id=role.id,
        )
        db.add(u)
        db.commit()
        db.refresh(u)
        return u

    owner = make_user("owner", False)
    make_user("admin", True)
    stranger = make_user("stranger", False)

    def make_camera(name: str, ip: str, owner_id: int = owner.id, location: str | None = None,
                    active: bool = True) -> Camera:
        cam = Camera(
            name=name, ip_address=ip, port=554, owner_id=owner_id,
            is_active=active, status="provisioned", location=location,
        )
        db.add(cam)
        db.commit()
        db.refresh(cam)
        return cam

    make_camera("Front Gate", "192.168.1.10", location="North Zone")
    make_camera("Back Door", "192.168.1.20", location=" north zone ")
    make_camera("cam_1", "10.0.0.5", location="South")
    make_camera("camX1", "10.0.0.6", location="")
    make_camera("Stranger Garage", "172.16.0.9", owner_id=stranger.id, location="West")
    make_camera("Old Shed", "10.9.9.9", location="South", active=False)

    client = TestClient(app)
    try:
        yield _types.SimpleNamespace(client=client)
    finally:
        db.close()


def _list(env, user: str, **params) -> dict:
    headers = {"Authorization": f"Bearer {create_access_token({'sub': user})}"}
    resp = env.client.get("/api/v1/cameras/", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _names(body: dict) -> set[str]:
    return {c["name"] for c in body["cameras"]}


def test_no_query_returns_the_unfiltered_list(env):
    body = _list(env, "admin")
    assert body["total"] == 5
    assert len(body["cameras"]) == 5


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_query_is_the_same_as_none(env, blank):
    assert _names(_list(env, "admin", q=blank)) == _names(_list(env, "admin"))


def test_matches_name_case_insensitively(env):
    body = _list(env, "admin", q="front")
    assert _names(body) == {"Front Gate"}
    assert body["total"] == 1


def test_matches_ip_address(env):
    assert _names(_list(env, "admin", q="192.168.1.")) == {"Front Gate", "Back Door"}


def test_like_wildcards_are_literal(env):
    assert _names(_list(env, "admin", q="cam_1")) == {"cam_1"}
    assert _names(_list(env, "admin", q="%")) == set()


def test_total_counts_matches_not_the_fleet(env):
    body = _list(env, "admin", q="10.0.0", limit=1)
    assert len(body["cameras"]) == 1
    assert body["total"] == 2


def test_search_never_widens_a_users_view(env):
    # The owner cannot see the stranger's camera, with or without a query
    # that would match it.
    assert "Stranger Garage" not in _names(_list(env, "owner"))
    assert _names(_list(env, "owner", q="garage")) == set()
    assert _names(_list(env, "owner", q="door")) == {"Back Door"}


# ── Location ──────────────────────────────────────────────────────────


def test_location_picks_one_location_trimmed_any_case(env):
    body = _list(env, "admin", location="NORTH ZONE")
    assert _names(body) == {"Front Gate", "Back Door"}
    assert body["total"] == 2
    assert _names(_list(env, "admin", location="south")) == {"cam_1"}
    assert _names(_list(env, "admin", location="south", active_only=False)) == {"cam_1", "Old Shed"}


def test_no_location_is_the_cameras_without_one(env):
    assert _names(_list(env, "admin", no_location=True)) == {"camX1"}


def test_a_blank_location_is_no_filter(env):
    assert _names(_list(env, "admin", location="  ")) == _names(_list(env, "admin"))


def test_search_matches_a_location_too(env):
    assert _names(_list(env, "admin", q="zone")) == {"Front Gate", "Back Door"}
    assert _names(_list(env, "admin", q="zone", location="south")) == set()


def test_location_never_widens_a_users_view(env):
    assert _names(_list(env, "owner", location="west")) == set()


def _locations(env, user: str, **params) -> dict:
    headers = {"Authorization": f"Bearer {create_access_token({'sub': user})}"}
    resp = env.client.get("/api/v1/cameras/locations", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_locations_lists_what_the_caller_sees_with_counts(env):
    body = _locations(env, "admin")
    # "North Zone" and " north zone " are one place, as the filter has it.
    assert [(r["location"], r["count"]) for r in body["locations"]] == [
        ("North Zone", 2), ("South", 1), ("West", 1)]
    assert body["none"] == 1
    assert _locations(env, "admin", active_only=False)["locations"][1] == {"location": "South", "count": 2}
    assert [r["location"] for r in _locations(env, "owner")["locations"]] == ["North Zone", "South"]

