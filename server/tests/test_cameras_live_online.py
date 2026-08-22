# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""`live_online` on GET /cameras/ — the field the UI renders its stream badge from.

Connectivity is tracked in memory by CameraStatusService (MediaMTX
runOnReady/runOnNotReady hooks + a reconciler). Before this field existed the
web UI had to probe /cameras/{id}/mediamtx-status once per camera to find out
whether a stream was live, which cost three MediaMTX round trips per row.

The contract pinned here:

* live_online mirrors the tracker's committed state for active cameras;
* it is None (UNKNOWN) — never False — when the tracker has not seen a camera,
  because False renders as "Disconnected" and would paint the whole fleet red
  for the first 30s after every restart;
* paused cameras are always None: the reconciler never walks them;
* it never widens what a non-superuser can see;
* attaching it costs no MediaMTX call, so the list endpoint keeps working when
  the media server is unreachable.

Run with:

    cd server && pytest tests/test_cameras_live_online.py -v
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

os.environ.setdefault("DATABASE_URL", "sqlite:///./_cameras_live_online_test.db")
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
# Force-assign (not setdefault): an earlier-collected test may have installed a
# narrower stub without a __getattr__ fallback.
sys.modules["core.logging_config"] = _lm

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
from services.mediamtx_admin_service import MediaMtxAdminService  # noqa: E402

PASSWORD = "Str0ng!passw0rd"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(core_auth, "auth_logger", _L(), raising=False)
    monkeypatch.setattr(cameras_router, "camera_logger", _L(), raising=False)

    # Fresh, non-singleton tracker. cameras.py imports the *accessor*, which
    # reads this module global, so swapping it here swaps what the endpoint sees.
    tracker = CameraStatusService()
    monkeypatch.setattr(css, "_service", tracker)

    # The list path must never reach MediaMTX. Blow up loudly if it tries.
    async def _explode(*a, **k):
        raise AssertionError("GET /cameras/ must not call MediaMTX")

    monkeypatch.setattr(
        MediaMtxAdminService, "list_active_paths", staticmethod(_explode)
    )
    monkeypatch.setattr(
        MediaMtxAdminService, "get_active_path", staticmethod(_explode)
    )
    monkeypatch.setattr(MediaMtxAdminService, "path_status", staticmethod(_explode))

    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
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
        u = User(
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
    admin = make_user("admin", True)
    stranger = make_user("stranger", False)

    counter = {"n": 0}

    def make_camera(**kw) -> Camera:
        counter["n"] += 1
        n = counter["n"]
        defaults = dict(
            name=f"Cam {n}",
            ip_address=f"10.0.0.{n}",
            port=554,
            owner_id=owner.id,
            is_active=True,
            status="provisioned",
        )
        defaults.update(kw)
        cam = Camera(**defaults)
        db.add(cam)
        db.commit()
        db.refresh(cam)
        return cam

    client = TestClient(app)
    try:
        yield _types.SimpleNamespace(
            client=client,
            db=db,
            tracker=tracker,
            owner=owner,
            admin=admin,
            stranger=stranger,
            make_camera=make_camera,
        )
    finally:
        db.close()


def _auth(username: str = "owner") -> dict:
    return {"Authorization": f"Bearer {create_access_token({'sub': username})}"}


def _rows(env, user: str = "owner", **params) -> dict:
    resp = env.client.get("/api/v1/cameras/", params=params, headers=_auth(user))
    assert resp.status_code == 200, resp.text
    return {c["id"]: c for c in resp.json()["cameras"]}


def test_live_online_mirrors_the_tracker(env):
    up = env.make_camera()
    down = env.make_camera()
    env.tracker._status[up.id] = True
    env.tracker._status[down.id] = False

    rows = _rows(env, "admin")
    assert rows[up.id]["live_online"] is True
    assert rows[down.id]["live_online"] is False


def test_unseeded_tracker_reports_unknown_not_offline(env):
    """The restart-window guard.

    _status is empty for the first RECONCILE_INITIAL_DELAY_SECONDS. If that
    surfaced as False the UI would show every camera "Disconnected" after each
    restart, which is exactly the false alarm this field must not create.
    """
    cams = [env.make_camera() for _ in range(3)]

    rows = _rows(env, "admin")
    for cam in cams:
        assert rows[cam.id]["live_online"] is None


def test_paused_camera_is_unknown_even_if_tracker_says_online(env):
    """A paused camera has no MediaMTX path, so a stale True must not leak out.

    The reconciler only walks is_active cameras, so an entry left over from
    before the pause would otherwise render the camera as live.
    """
    paused = env.make_camera(is_active=False)
    env.tracker._status[paused.id] = True

    rows = _rows(env, "admin", active_only=False)
    assert rows[paused.id]["live_online"] is None


def test_live_online_does_not_widen_visibility(env):
    """A non-superuser still sees only their own rows, live_online or not."""
    mine = env.make_camera()
    theirs = env.make_camera(owner_id=env.stranger.id)
    env.tracker._status[mine.id] = True
    env.tracker._status[theirs.id] = True

    rows = _rows(env, "owner")
    assert mine.id in rows
    assert theirs.id not in rows
    assert rows[mine.id]["live_online"] is True


def test_list_serves_without_mediamtx(env):
    """The whole point: no MediaMTX round trip on the list path.

    Every MediaMtxAdminService entry point is monkeypatched to raise, so this
    passing proves the endpoint reads the in-memory tracker instead.
    """
    cam = env.make_camera()
    env.tracker._status[cam.id] = True

    rows = _rows(env, "admin")
    assert rows[cam.id]["live_online"] is True
