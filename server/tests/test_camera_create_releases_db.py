# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Camera creation must release the DB session before network probes (#988).

FastAPI's auth dependency opens a read transaction on ``db`` before
``create_camera_checked`` runs. If ONVIF/RTSP probes against an unreachable
camera take over Postgres's 60s ``idle_in_transaction_session_timeout``, the
server drops the connection and the subsequent duplicate check fails with
``OperationalError`` (500).
"""
from __future__ import annotations

import asyncio
import os
import secrets
import sys
from pathlib import Path

from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

os.environ.setdefault("DATABASE_URL", "sqlite:///./_cam_release_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.database import Base  # noqa: E402
from models import Camera, Role, User  # noqa: E402
from routers import cameras as cameras_router  # noqa: E402
from schemas import CameraCreate  # noqa: E402
from services import camera_source_resolver as resolver  # noqa: E402
from services.camera_service import CameraService  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_session_and_user():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = session_factory()
    role = Role(name="operator", description="test")
    db.add(role)
    db.flush()
    user = User(
        username="op",
        email="op@example.test",
        hashed_password="x",
        is_active=True,
        password_set=True,
        role_id=role.id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    # Simulate FastAPI auth dependency holding an open read transaction on `db`.
    _ = db.get(User, user.id)
    assert db.in_transaction()
    return db, user


def test_create_camera_checked_releases_db_before_probes(monkeypatch):
    db, user = _make_session_and_user()
    events: list[str] = []

    orig_release = cameras_router.release

    def tracking_release(session):
        assert session is db
        events.append(f"release:in_tx={session.in_transaction()}")
        orig_release(session)
        assert not session.in_transaction()

    async def fake_fetch_identity(ip, username, password):
        events.append(f"fetch_identity:in_tx={db.in_transaction()}")
        return None

    async def fake_sync_time(ip, username, password, port, scheme):
        events.append(f"sync_time:in_tx={db.in_transaction()}")
        return False

    async def fake_substream(cam):
        events.append(f"substream:in_tx={db.in_transaction()}")
        return cam

    async def fake_create_camera(*, db, camera_create, owner_id):
        events.append(f"create_camera:owner={owner_id}")
        cam = Camera(
            name=camera_create.name,
            ip_address=camera_create.ip_address,
            port=camera_create.port,
            rtsp_url=camera_create.rtsp_url,
            owner_id=owner_id,
            status="unknown",
        )
        db.add(cam)
        db.commit()
        db.refresh(cam)
        return cam

    monkeypatch.setattr(cameras_router, "release", tracking_release)
    monkeypatch.setattr(resolver, "fetch_identity", fake_fetch_identity)
    monkeypatch.setattr(resolver, "sync_camera_time", fake_sync_time)
    monkeypatch.setattr(cameras_router, "_discover_substream_for_create", fake_substream)
    monkeypatch.setattr(CameraService, "create_camera", staticmethod(fake_create_camera))
    monkeypatch.setattr(cameras_router, "_record_audit_log", lambda *a, **k: None)
    monkeypatch.setattr(cameras_router, "_check_duplicate_ips", lambda *a, **k: None)

    payload = CameraCreate(
        name="Gate Cam",
        ip_address="192.168.1.50",
        username="admin",
        password="secret",
        rtsp_url="rtsp://192.168.1.50:554/stream1",
    )

    resp = _run(
        cameras_router.create_camera_checked(
            payload,
            force=False,
            db=db,
            current_user=user,
            request=None,
            probe=True,
        )
    )

    assert resp.name == "Gate Cam"
    assert events == [
        "release:in_tx=True",
        "fetch_identity:in_tx=False",
        "sync_time:in_tx=False",
        "substream:in_tx=False",
        f"create_camera:owner={resp.owner_id}",
    ]


def test_create_camera_checked_skips_release_when_no_probe(monkeypatch):
    db, user = _make_session_and_user()
    release_calls = []

    monkeypatch.setattr(cameras_router, "release", lambda s: release_calls.append(s))

    async def fake_create_camera(*, db, camera_create, owner_id):
        cam = Camera(
            name=camera_create.name,
            ip_address=camera_create.ip_address,
            port=camera_create.port,
            rtsp_url=camera_create.rtsp_url,
            owner_id=owner_id,
            status="unknown",
        )
        db.add(cam)
        db.commit()
        db.refresh(cam)
        return cam

    monkeypatch.setattr(CameraService, "create_camera", staticmethod(fake_create_camera))
    monkeypatch.setattr(cameras_router, "_record_audit_log", lambda *a, **k: None)
    monkeypatch.setattr(cameras_router, "_check_duplicate_ips", lambda *a, **k: None)

    payload = CameraCreate(
        name="Imported Cam",
        ip_address="192.168.1.51",
        username="admin",
        password="secret",
        rtsp_url="rtsp://admin:secret@192.168.1.51:554/stream1",
    )

    resp = _run(
        cameras_router.create_camera_checked(
            payload,
            force=True,
            db=db,
            current_user=user,
            request=None,
            probe=False,
            duplicate_check=False,
        )
    )

    assert resp.name == "Imported Cam"
    assert release_calls == []
