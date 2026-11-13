# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""A camera whose picture has stopped changing: repeated frames raise a
per-camera alarm after N in a row, the first new frame clears it, the
status read says so, and a live camera never trips it."""

from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite:///./_frame_health_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest  # noqa: E402

from services import frame_health as fh  # noqa: E402


@pytest.fixture
def db(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from core.config import settings
    from core.database import Base
    from models import Camera, Role, User

    fh.reset_for_tests()
    monkeypatch.setattr(settings, fh.SETTING, 3, raising=False)
    engine = create_engine("sqlite:///:memory:", future=True,
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, future=True)()
    s.add(Role(id=1, name="admin", description="t")); s.commit()
    s.add(User(id=1, username="o", email="o@x.test", hashed_password="x", is_active=True, role_id=1)); s.commit()
    s.add(Camera(id=23, name="gate", ip_address="10.0.0.1", rtsp_url="rtsp://x/1", owner_id=1, is_active=True))
    s.add(Camera(id=24, name="yard", ip_address="10.0.0.2", rtsp_url="rtsp://x/2", owner_id=1, is_active=True))
    s.commit()
    yield s
    s.close(); engine.dispose()


def _visit(db, eid, camera_id, rel):
    from models import TimelineEvent

    now = datetime.now(UTC)
    row = TimelineEvent(id=eid, camera_id=camera_id, source="tier0", event_type="track", label="car",
                        started_at=now - timedelta(seconds=5), ended_at=now, evidence_path=rel)
    db.add(row); db.commit()
    return row


def test_repeats_raise_once_and_a_new_frame_clears(db):
    from models import CameraEvent

    first = _visit(db, 1, 23, "aa/aaa.jpg")
    assert fh.note(db, first, None) is None
    assert fh.note(db, _visit(db, 2, 23, "aa/aaa.jpg"), first) is None
    assert fh.note(db, _visit(db, 3, 23, "aa/aaa.jpg"), first) is None
    t = fh.note(db, _visit(db, 4, 23, "aa/aaa.jpg"), first)
    assert t and t["event_state"] == "active" and t["camera_name"] == "gate"
    assert "3 visits in a row" in t["description"]
    # more repeats: no second row
    assert fh.note(db, _visit(db, 5, 23, "aa/aaa.jpg"), first) is None
    assert fh.frozen_cameras(db, [23, 24]) == {23}
    # a new frame clears it
    t = fh.note(db, _visit(db, 6, 23, "bb/bbb.jpg"), None)
    assert t and t["event_state"] == "inactive"
    assert fh.frozen_cameras(db, [23, 24]) == set()
    rows = db.query(CameraEvent).filter(CameraEvent.event_type == fh.EVENT_TYPE).all()
    assert [r.event_state for r in rows] == ["active", "inactive"]


def test_a_live_camera_never_trips_and_streaks_are_per_camera(db):
    first = _visit(db, 1, 23, "aa/aaa.jpg")
    _visit(db, 2, 24, "cc/ccc.jpg")
    for i, rel in enumerate(("ab/1.jpg", "ac/2.jpg", "ad/3.jpg", "ae/4.jpg"), start=10):
        assert fh.note(db, _visit(db, i, 24, rel), None) is None
    assert fh.note(db, _visit(db, 20, 23, "aa/aaa.jpg"), first) is None
    assert fh.note(db, _visit(db, 21, 23, "aa/aaa.jpg"), first) is None
    assert fh.note(db, _visit(db, 22, 24, "af/5.jpg"), None) is None, "yard's fresh frames do not reset gate's streak"
    assert fh.note(db, _visit(db, 23, 23, "aa/aaa.jpg"), first)["event_state"] == "active"
    assert fh.frozen_cameras(db, [23, 24]) == {23}


def test_zero_disables(db, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, fh.SETTING, 0, raising=False)
    first = _visit(db, 1, 23, "aa/aaa.jpg")
    for i in range(2, 12):
        assert fh.note(db, _visit(db, i, 23, "aa/aaa.jpg"), first) is None
    assert fh.frozen_cameras(db, [23]) == set()


def test_the_caption_task_notes_it_and_publishes_the_transition(monkeypatch, db):
    """Through the real caption task: four visits of one frame, the
    fourth raises the alarm and the transition reaches the live bus."""
    import asyncio
    import pathlib
    import tempfile

    import core.database
    from core.config import settings
    from models import Camera, EventText
    from services import caption_enrichment as ce
    from services import evidence_store

    monkeypatch.setattr(settings, fh.SETTING, 3, raising=False)
    cam = db.get(Camera, 23)
    cam.assignments = [{"skill": "image_captioning"}]
    db.commit()

    class _Shared:
        def __init__(self, s): self._s = s
        def __getattr__(self, n): return getattr(self._s, n)
        def close(self): pass

    monkeypatch.setattr(core.database, "SessionLocal", lambda: _Shared(db))
    monkeypatch.setattr(settings, "events_caption_enrichment", True, raising=False)
    tmp = pathlib.Path(tempfile.mkdtemp()) / "e.jpg"
    tmp.write_bytes(b"\xff\xd8jpeg")
    monkeypatch.setattr(evidence_store, "resolve_evidence", lambda _p: tmp)

    async def _adapter():
        return "litertlm"

    async def _cap(jpeg, adapter, handle, event_id=None, *, priority="live"):
        return "A white van."

    monkeypatch.setattr(ce, "_resolve_caption_adapter", _adapter)
    monkeypatch.setattr(ce, "_caption_jpeg", _cap)
    published: list[dict] = []

    async def _pub(transition):
        published.append(transition)

    monkeypatch.setattr(fh, "publish", _pub)
    for eid in (1, 2, 3, 4):
        _visit(db, eid, 23, "aa/aaa.jpg")
    loop = asyncio.new_event_loop()
    try:
        for eid in (1, 2, 3, 4):
            loop.run_until_complete(ce.enrich_event_caption(eid))
    finally:
        loop.close()
    db.expire_all()
    assert db.get(EventText, 4).caption == "A white van."
    assert [t["event_state"] for t in published] == ["active"]
    assert fh.frozen_cameras(db, [23]) == {23}


def test_the_status_read_carries_the_picture_field(db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.database import get_db
    from models import CameraEvent
    from routers import internal_camera_agent as internal_router
    from services import camera_status_service, storage_service
    from services.retention_service import RetentionService

    db.add(CameraEvent(camera_id=23, event_type=fh.EVENT_TYPE, event_state="active",
                       description="frozen", occurred_at=datetime.now(UTC)))
    db.commit()
    svc = camera_status_service.get_camera_status_service()
    monkeypatch.setattr(svc, "snapshot", lambda ids: {23: True, 24: True})
    monkeypatch.setattr(svc, "online_since", lambda ids: {})
    monkeypatch.setattr(storage_service, "get_effective_recordings_base_path", lambda db=None: "/nonexistent")
    monkeypatch.setattr(RetentionService, "_get_retention_settings",
                        staticmethod(lambda db: {"min_free_space_gb": 10, "retention_days": 30}))
    app = FastAPI()
    app.include_router(internal_router.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[internal_router._require_internal_key] = lambda: None
    with TestClient(app) as c:
        r = c.get("/internal/camera-agent/status")
    assert r.status_code == 200
    rows = {row["open_nvr_camera_id"]: row for row in r.json()["cameras"]}
    assert rows["23"]["picture"] == "frozen" and rows["24"]["picture"] == "live"
