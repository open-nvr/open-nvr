# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
Camera connectivity tracking (MediaMTX runOnReady/runOnNotReady hooks +
reconciler). Run with:

    cd server && pytest tests/test_camera_status_hooks.py -v

Coverage:
* Hook endpoints require X-MTX-Secret; __startup__ and *-sub paths ignored.
* Offline transitions are debounced, published once on the event bus, and
  persisted as CameraEvent(event_type="connection") rows.
* A ready signal inside the debounce window cancels the pending offline.
* Reconciler: first pass seeds silently; later passes publish real flips;
  an unreachable MediaMTX admin API never mass-flaps cameras offline.
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

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import core.database as core_db  # noqa: E402
import services.camera_status_service as css  # noqa: E402
import services.event_bus_service as ebs  # noqa: E402
from core.config import settings  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import Camera, CameraEvent, Role, User  # noqa: E402
from routers import mediamtx_hooks as hooks_router  # noqa: E402
from services.camera_status_service import CameraStatusService  # noqa: E402


@pytest.fixture
def db_env(monkeypatch):
    """Shared in-memory DB: the same connection backs both the request-scoped
    ``get_db`` session and the service's ``SessionLocal`` (used by the
    best-effort CameraEvent persistence)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(core_db, "SessionLocal", Session)

    s = Session()
    role = Role(name="admin")
    s.add(role)
    s.commit()
    owner = User(username="t", email="t@t.io", hashed_password="x", role_id=role.id)
    s.add(owner)
    s.commit()
    cam = Camera(name="Gate", ip_address="10.0.0.9", port=80, owner_id=owner.id)
    s.add(cam)
    s.commit()
    s.refresh(cam)

    yield _types.SimpleNamespace(Session=Session, session=s, camera=cam)
    s.close()


@pytest.fixture
def published(monkeypatch):
    """Record camera_status publications instead of hitting the real bus."""
    events: list[dict] = []

    async def _record(*, camera_id, status, payload):
        events.append({"camera_id": camera_id, "status": status, **payload})

    monkeypatch.setattr(ebs, "publish_camera_status", _record)
    return events


@pytest.fixture
def service(monkeypatch, published):
    """Fresh (non-singleton) service with a zero debounce by default."""
    monkeypatch.setattr(css, "OFFLINE_DEBOUNCE_SECONDS", 0.0)
    svc = CameraStatusService()
    monkeypatch.setattr(css, "_service", svc)
    return svc


@pytest.fixture
def client(db_env, service):
    app = FastAPI()
    app.include_router(hooks_router.router)

    def _get_db():
        s = db_env.Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db
    return TestClient(app)


HOOK_HEADERS = {"X-MTX-Secret": settings.mediamtx_secret}


async def _drain(svc: CameraStatusService) -> None:
    """Let pending debounce tasks commit."""
    pending = list(svc._pending_offline.values())
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Hook endpoints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", ["path-ready", "path-not-ready"])
def test_hooks_require_secret(client, endpoint):
    resp = client.get(f"/mediamtx/hooks/{endpoint}", params={"path": "cam-1"})
    assert resp.status_code == 401


@pytest.mark.parametrize("path", ["__startup__", "cam-1-sub"])
@pytest.mark.parametrize("endpoint", ["path-ready", "path-not-ready"])
def test_hooks_ignore_non_camera_paths(client, service, endpoint, path):
    resp = client.get(
        f"/mediamtx/hooks/{endpoint}", params={"path": path}, headers=HOOK_HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert service._status == {}


def test_hook_unknown_path_accepted(client):
    resp = client.get(
        "/mediamtx/hooks/path-ready",
        params={"path": "cam-99999"},
        headers=HOOK_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "unknown_path"


def test_hook_ready_marks_camera_online(client, db_env, service, published):
    cam = db_env.camera
    resp = client.get(
        "/mediamtx/hooks/path-ready",
        params={"path": f"cam-{cam.id}"},
        headers=HOOK_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "path": f"cam-{cam.id}"}
    assert service.is_online(cam.id) is True
    # First-ever sighting seeds silently — no "back online" alert.
    assert published == []


# ---------------------------------------------------------------------------
# Transition semantics (service level — full async control)
# ---------------------------------------------------------------------------


def _signal(svc, cam, online, reason="hook"):
    return svc.handle_signal(
        camera_id=cam.id,
        camera_name=cam.name,
        path=f"cam-{cam.id}",
        online=online,
        reason=reason,
    )


def test_offline_published_once_and_persisted(db_env, service, published):
    cam = db_env.camera

    async def run():
        await _signal(service, cam, True)  # seed online (silent)
        await _signal(service, cam, False)
        await _drain(service)
        await _signal(service, cam, False)  # duplicate — must be a no-op
        await _drain(service)

    asyncio.run(run())

    assert [e["status"] for e in published] == ["offline"]
    assert published[0]["camera_id"] == cam.id
    assert published[0]["camera_name"] == "Gate"

    rows = db_env.session.query(CameraEvent).all()
    assert len(rows) == 1
    assert (rows[0].event_type, rows[0].event_state) == ("connection", "active")


def test_recovery_publishes_online_and_bumps_history(db_env, service, published):
    cam = db_env.camera

    async def run():
        await _signal(service, cam, True)
        await _signal(service, cam, False)
        await _drain(service)
        await _signal(service, cam, True)

    asyncio.run(run())

    assert [e["status"] for e in published] == ["offline", "online"]
    states = [r.event_state for r in db_env.session.query(CameraEvent).all()]
    assert states == ["active", "inactive"]


def test_ready_within_debounce_cancels_offline(db_env, service, published, monkeypatch):
    monkeypatch.setattr(css, "OFFLINE_DEBOUNCE_SECONDS", 0.2)
    cam = db_env.camera

    async def run():
        await _signal(service, cam, True)
        await _signal(service, cam, False)  # schedules debounced commit
        await _signal(service, cam, True)  # flap resolves inside the window
        await asyncio.sleep(0.3)

    asyncio.run(run())

    assert published == []  # the flap never surfaced
    assert service.is_online(cam.id) is True
    assert db_env.session.query(CameraEvent).count() == 0


# ---------------------------------------------------------------------------
# Reconciler
# ---------------------------------------------------------------------------


def _paths_result(ready_paths: dict[str, bool]):
    return {
        "status": "ok",
        "details": {
            "items": [
                {"name": name, "ready": ready, "bytesReceived": 100 if ready else 0}
                for name, ready in ready_paths.items()
            ]
        },
    }


def test_reconciler_seeds_silently_then_reports_flips(
    db_env, service, published, monkeypatch
):
    from services.mediamtx_admin_service import MediaMtxAdminService
    from services.stream_service import _build_stream_name

    cam = db_env.camera
    path = _build_stream_name(settings.mediamtx_stream_prefix, cam.id, cam.ip_address)

    results = [_paths_result({path: True}), _paths_result({path: False})]

    async def fake_list():
        return results.pop(0)

    monkeypatch.setattr(MediaMtxAdminService, "list_active_paths", fake_list)

    async def run():
        await service.reconcile_once()  # seed pass — silent
        assert published == []
        assert service.is_online(cam.id) is True

        await service.reconcile_once()  # camera dropped since the seed
        await _drain(service)

    asyncio.run(run())

    assert [e["status"] for e in published] == ["offline"]
    assert published[0]["reason"] == "reconciler"


def test_reconciler_skips_when_mediamtx_unreachable(
    db_env, service, published, monkeypatch
):
    from services.mediamtx_admin_service import MediaMtxAdminService
    from services.stream_service import _build_stream_name

    cam = db_env.camera
    path = _build_stream_name(settings.mediamtx_stream_prefix, cam.id, cam.ip_address)

    results = [
        _paths_result({path: True}),
        {"status": "error", "message": "connect timeout"},
    ]

    async def fake_list():
        return results.pop(0)

    monkeypatch.setattr(MediaMtxAdminService, "list_active_paths", fake_list)

    async def run():
        await service.reconcile_once()  # seed: online
        await service.reconcile_once()  # MediaMTX down — must not flap offline
        await _drain(service)

    asyncio.run(run())

    assert published == []
    assert service.is_online(cam.id) is True


# ---------------------------------------------------------------------------
# snapshot(): the batch read GET /cameras/ uses to attach live_online.
# ---------------------------------------------------------------------------


def test_snapshot_reports_unknown_for_unseen_cameras(service):
    """Never-seen ids must come back None, not False.

    This is the guard on the restart window: _status is empty until the first
    reconcile pass, and reporting False there would paint the whole fleet
    "Disconnected" for RECONCILE_INITIAL_DELAY_SECONDS after every restart.
    """
    assert service.snapshot([1, 2, 3]) == {1: None, 2: None, 3: None}
    assert service.snapshot([]) == {}


def test_snapshot_reflects_committed_transitions(db_env, service, published):
    cam = db_env.camera

    async def run():
        await service.handle_signal(
            camera_id=cam.id, camera_name=cam.name, path="cam-1",
            online=True, reason="hook",
        )
        assert service.snapshot([cam.id]) == {cam.id: True}

        await service.handle_signal(
            camera_id=cam.id, camera_name=cam.name, path="cam-1",
            online=False, reason="hook",
        )
        await _drain(service)
        assert service.snapshot([cam.id]) == {cam.id: False}

    asyncio.run(run())


def test_snapshot_is_unknown_during_offline_debounce(db_env, service, monkeypatch):
    """A camera mid-debounce still reads as its last committed state.

    The badge must not flip before the transition commits, or it would
    disagree with the toast the operator sees.
    """
    monkeypatch.setattr(css, "OFFLINE_DEBOUNCE_SECONDS", 30.0)
    cam = db_env.camera

    async def run():
        await service.handle_signal(
            camera_id=cam.id, camera_name=cam.name, path="cam-1",
            online=True, reason="hook",
        )
        await service.handle_signal(
            camera_id=cam.id, camera_name=cam.name, path="cam-1",
            online=False, reason="hook",
        )
        # Pending, not committed — still online.
        assert service.snapshot([cam.id]) == {cam.id: True}
        for task in service._pending_offline.values():
            task.cancel()

    asyncio.run(run())
