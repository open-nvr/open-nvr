# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The camera agent's system-status read on the internal door: each
camera's live connectivity and observed recording state, and the
recording disk against its purge floor. Run with:

    cd server && pytest tests/test_internal_status.py -v
"""
from __future__ import annotations

import os
import secrets
import sys
import types as _types
from datetime import UTC, datetime, timedelta
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

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.database import Base, get_db  # noqa: E402
from models import Camera, CameraConfig, CameraEvent, CameraMask, Recording, Role, User  # noqa: E402
from routers import internal_camera_agent as internal_router  # noqa: E402
from services import camera_status_service, storage_service  # noqa: E402
from services.retention_service import RetentionService  # noqa: E402

NOW = datetime.now(UTC)


@pytest.fixture
def client(monkeypatch, tmp_path):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[Camera.__table__, CameraMask.__table__,
                                             User.__table__, Role.__table__,
                                             Recording.__table__, CameraConfig.__table__,
                                             CameraEvent.__table__])
    factory = sessionmaker(bind=engine)
    s = factory()
    s.add(Role(id=1, name="admin"))
    s.add(User(mfa_enabled=True, id=1, username="op", email="op@x", hashed_password="x", role_id=1))
    s.add(Camera(id=1, name="Gate", ip_address="10.0.0.3", owner_id=1, is_active=True, created_at=NOW - timedelta(days=2),
                 rtsp_url="rtsp://u:p@10.0.0.3:554/a"))
    s.add(Camera(id=2, name="Yard", ip_address="10.0.0.4", owner_id=1, is_active=True, created_at=NOW - timedelta(days=2),
                 rtsp_url="rtsp://u:p@10.0.0.4:554/b"))
    s.add(Camera(id=3, name="Paused", ip_address="10.0.0.5", owner_id=1, is_active=False,
                 rtsp_url="rtsp://u:p@10.0.0.5:554/c"))
    s.add(CameraConfig(camera_id=1, recording_enabled=True))
    s.add(CameraConfig(camera_id=2, recording_enabled=True))
    # The gate recorded a minute ago; the yard's newest segment is three hours old.
    s.add(Recording(camera_id=1, filename="a.mp4", file_path="/r/a.mp4",
                    start_time=NOW - timedelta(minutes=1)))
    s.add(Recording(camera_id=2, filename="b.mp4", file_path="/r/b.mp4",
                    start_time=NOW - timedelta(hours=3)))
    s.commit(); s.close()

    svc = camera_status_service.get_camera_status_service()
    monkeypatch.setattr(svc, "snapshot", lambda ids: {1: True, 2: True})
    monkeypatch.setattr(svc, "online_since", lambda ids: {1: NOW - timedelta(days=1), 2: NOW - timedelta(days=1)})
    monkeypatch.setattr(storage_service, "get_effective_recordings_base_path", lambda db=None: str(tmp_path))
    monkeypatch.setattr(RetentionService, "_get_retention_settings",
                        staticmethod(lambda db: {"min_free_space_gb": 10, "retention_days": 30}))

    app = FastAPI()
    app.include_router(internal_router.router)

    def _db():
        d = factory()
        try:
            yield d
        finally:
            d.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[internal_router._require_internal_key] = lambda: None
    with TestClient(app) as c:
        yield c


def test_each_active_camera_reports_connectivity_and_recording_state(client):
    body = client.get("/internal/camera-agent/status").json()
    cams = {c["camera_id"]: c for c in body["cameras"]}
    assert set(cams) == {"cam1", "cam2"}, "a paused camera is not in the fleet"
    assert cams["cam1"]["online"] is True
    assert cams["cam1"]["recording_state"] == "recording"
    assert cams["cam1"]["name"] == "Gate"
    assert cams["cam1"]["open_nvr_camera_id"] == "1"
    assert cams["cam2"]["recording_state"] == "stalled", "a live stream with a 3 h old newest segment"
    assert cams["cam2"]["last_recording_at"] is not None


def test_the_recording_disk_is_read_against_its_floor(client):
    st = client.get("/internal/camera-agent/status").json()["storage"]
    assert st["min_free_gb"] == 10.0
    assert st["free_gb"] is not None and st["total_gb"] is not None
    assert st["free_gb"] <= st["total_gb"]


def test_a_storage_failure_leaves_the_camera_rows_intact(client, monkeypatch):
    def _boom(db=None):
        raise RuntimeError("no disk")
    monkeypatch.setattr(storage_service, "get_effective_recordings_base_path", _boom)
    body = client.get("/internal/camera-agent/status").json()
    assert len(body["cameras"]) == 2
    assert body["storage"]["free_gb"] is None


def test_a_camera_with_an_active_degraded_alarm_reads_degraded(client):
    """Segments keep landing (so the age rule says recording) but the
    segment-health check has an active recording_degraded alarm: the
    agent's status read says degraded, like the list's badge."""
    from routers import internal_camera_agent as mod

    # Reach the test DB through the same dependency the app uses.
    gen = client.app.dependency_overrides[mod.get_db]()
    db = next(gen)
    db.add(CameraEvent(camera_id=1, event_type="recording_degraded", event_state="active",
                       description="frozen", occurred_at=NOW))
    db.commit()
    body = client.get("/internal/camera-agent/status").json()
    cams = {c["camera_id"]: c for c in body["cameras"]}
    assert cams["cam1"]["recording_state"] == "degraded"
    assert cams["cam2"]["recording_state"] == "stalled", "untouched by the other camera's alarm"


def test_the_agents_interactive_beacon_holds_the_backfill(client, monkeypatch):
    from services import enrichment_governor as gov

    gov._reset_for_tests()
    r = client.post("/internal/camera-agent/interactive", json={"source": "camera-agent"})
    assert r.status_code == 204
    assert gov.interactive_recently()
    assert gov._last_interactive_source == "camera-agent"
