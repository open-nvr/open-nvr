"""More than 100 cameras (#671).

Two lists were read one page at a time and never past the first:

* ``GET /cameras/`` pages at 100 by default. A non-superuser's owned and
  granted cameras were each paged on their own and merged, so later pages
  skipped rows and ``total`` was the merged page's length, not the count.
* MediaMTX's ``/v3/paths/list`` pages at 100 by default, and every camera
  holds two paths (main and -sub). ``list_active_paths`` read page 0 only,
  so the status reconciler saw ~50 cameras and marked the rest offline.

Run with:

    cd server && pytest tests/test_camera_list_past_100.py -v
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
import types as _types
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

os.environ.setdefault("DATABASE_URL", "sqlite:///./_camera_list_past_100_test.db")
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
import services.mediamtx_admin_service as mas  # noqa: E402
from core.auth import create_access_token, get_password_hash  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import Camera, CameraPermission, Role, User  # noqa: E402
from routers import cameras as cameras_router  # noqa: E402
from services.camera_status_service import CameraStatusService  # noqa: E402
from services.mediamtx_admin_service import MediaMtxAdminService  # noqa: E402

FLEET = 130  # past the old 100-row page


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
            username=username, email=f"{username}@example.com",
            hashed_password=get_password_hash("Str0ng!passw0rd"),
            is_active=True, is_superuser=superuser, password_set=True,
            role_id=role.id,
        )
        db.add(u)
        db.commit()
        db.refresh(u)
        return u

    make_user("admin", True)
    viewer = make_user("viewer", False)
    other = make_user("other", False)

    # Interleave ownership so owned and granted ids alternate: the old
    # merge-two-pages code got exactly this case wrong.
    owned, granted = [], []
    for i in range(FLEET):
        mine = i % 2 == 0
        cam = Camera(
            name=f"cam {i:03d}", ip_address=f"10.0.{i // 250}.{i % 250 + 1}",
            port=554, owner_id=viewer.id if mine else other.id,
            is_active=True, status="provisioned",
        )
        db.add(cam)
        db.flush()
        if mine:
            owned.append(cam.id)
        elif i % 4 == 1:
            db.add(CameraPermission(user_id=viewer.id, camera_id=cam.id, can_view=True))
            granted.append(cam.id)
    db.commit()

    try:
        yield _types.SimpleNamespace(
            client=TestClient(app), visible=sorted(owned + granted),
        )
    finally:
        db.close()


def _get(env, user: str, **params) -> dict:
    headers = {"Authorization": f"Bearer {create_access_token({'sub': user})}"}
    resp = env.client.get("/api/v1/cameras/", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _all_pages(env, user: str, page: int) -> tuple[list[int], int]:
    ids: list[int] = []
    while True:
        body = _get(env, user, skip=len(ids), limit=page)
        ids += [c["id"] for c in body["cameras"]]
        if len(body["cameras"]) < page:
            return ids, body["total"]


def test_total_counts_the_whole_fleet_not_the_page(env):
    body = _get(env, "admin")
    assert len(body["cameras"]) == 100  # the default page size is unchanged
    assert body["total"] == FLEET


@pytest.mark.parametrize("page", [7, 50, 100])
def test_superuser_pages_cover_every_camera_once_in_order(env, page):
    ids, total = _all_pages(env, "admin", page)
    assert total == FLEET
    assert len(ids) == FLEET
    assert ids == sorted(set(ids))


@pytest.mark.parametrize("page", [7, 50, 100])
def test_user_pages_cover_own_and_granted_once_in_order(env, page):
    ids, total = _all_pages(env, "viewer", page)
    assert total == len(env.visible)
    assert ids == env.visible


def test_user_total_is_the_count_not_the_page_length(env):
    body = _get(env, "viewer", limit=10)
    assert len(body["cameras"]) == 10
    assert body["total"] == len(env.visible)


# ---------------------------------------------------------------------------
# MediaMTX /v3/paths/list paging
# ---------------------------------------------------------------------------


class _FakeMediaMtx:
    """Serves ``paths`` the way MediaMTX pages them: itemsPerPage (default
    100) and a zero-based page, with pageCount/itemCount in the body."""

    def __init__(self, paths: list[str], fail_page: int | None = None):
        self.paths = paths
        self.fail_page = fail_page
        self.requests: list[dict] = []

    def client(self, *a, **k):
        fake = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, headers=None, params=None):
                params = dict(params or {})
                fake.requests.append(params)
                per = int(params.get("itemsPerPage", 100))
                page = int(params.get("page", 0))
                if page == fake.fail_page:
                    return mas.httpx.Response(500, json={"error": "boom"})
                items = [{"name": p, "ready": True, "bytesReceived": 1}
                         for p in fake.paths[page * per:(page + 1) * per]]
                return mas.httpx.Response(200, json={
                    "itemCount": len(fake.paths),
                    "pageCount": max(1, -(-len(fake.paths) // per)),
                    "items": items,
                })

        return _Client()


@pytest.fixture
def mediamtx(monkeypatch):
    monkeypatch.setattr(mas.settings, "mediamtx_admin_api", "http://mtx:9997/v3")

    def install(fake: _FakeMediaMtx) -> _FakeMediaMtx:
        monkeypatch.setattr(mas.httpx, "AsyncClient", fake.client)
        return fake

    return install


def _paths(cameras: int) -> list[str]:
    return [p for i in range(1, cameras + 1) for p in (f"cam-{i}", f"cam-{i}-sub")]


def test_list_active_paths_returns_every_page(mediamtx, monkeypatch):
    monkeypatch.setattr(mas, "_PATHS_PAGE_SIZE", 100)  # MediaMTX's default
    fake = mediamtx(_FakeMediaMtx(_paths(FLEET)))

    result = asyncio.run(MediaMtxAdminService.list_active_paths())

    assert result["status"] == "ok"
    names = [i["name"] for i in result["details"]["items"]]
    assert names == _paths(FLEET)
    assert result["details"]["itemCount"] == 2 * FLEET
    assert len(fake.requests) == 3  # 260 paths, 100 a page


def test_list_active_paths_asks_for_large_pages(mediamtx):
    fake = mediamtx(_FakeMediaMtx(_paths(FLEET)))
    result = asyncio.run(MediaMtxAdminService.list_active_paths())
    assert len(result["details"]["items"]) == 2 * FLEET
    assert fake.requests == [{"itemsPerPage": mas._PATHS_PAGE_SIZE, "page": 0}]


def test_a_failed_later_page_fails_the_whole_list(mediamtx, monkeypatch):
    # A partial list would read as half the fleet going offline.
    monkeypatch.setattr(mas, "_PATHS_PAGE_SIZE", 100)
    mediamtx(_FakeMediaMtx(_paths(FLEET), fail_page=1))
    result = asyncio.run(MediaMtxAdminService.list_active_paths())
    assert result["status"] == "error"


def test_endless_paging_is_an_error_not_a_truncated_list(mediamtx, monkeypatch):
    monkeypatch.setattr(mas, "_PATHS_PAGE_SIZE", 100)
    monkeypatch.setattr(mas, "_PATHS_MAX_PAGES", 2)
    mediamtx(_FakeMediaMtx(_paths(FLEET)))
    result = asyncio.run(MediaMtxAdminService.list_active_paths())
    assert result["status"] == "error"
