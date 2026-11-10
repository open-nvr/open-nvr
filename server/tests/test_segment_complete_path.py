# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The segment-complete hook stats the file where THIS process can open it.

MediaMTX spells MTX_SEGMENT_PATH as the shipped recordPath does, relative to
its own working directory (/). Resolved from core's /app that became
/app/app/recordings/…, so every row was indexed with no file_size and no
codec, and the segment-health check (which needs the size at the moment the
segment closes) was blind — Ventuno Q, 2026-11-09: 9,400 rows, all NULL at
insert, back-filled by the reconciler 15 minutes later. Run with:

    cd server && pytest tests/test_segment_complete_path.py -v
"""
from __future__ import annotations

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

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.database import Base, get_db  # noqa: E402
from models import Camera, CameraEvent, Recording, Role, User  # noqa: E402
from routers import mediamtx_hooks as hooks  # noqa: E402

REL = "cam-1/2026-11-09/17/28-40-035995.mp4"


@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[Camera.__table__, Recording.__table__,
                                             CameraEvent.__table__, User.__table__, Role.__table__])
    factory = sessionmaker(bind=engine)
    s = factory()
    s.add(Role(id=1, name="admin"))
    s.add(User(id=1, username="op", email="op@x", hashed_password="x", role_id=1))
    cam = Camera(id=1, name="Gate", ip_address="10.0.0.3", owner_id=1, is_active=True,
                 rtsp_url="rtsp://u:p@10.0.0.3:554/a")
    s.add(cam)
    s.commit()

    # The file on disk, under a root that stands in for /app/recordings.
    root = tmp_path / "app" / "recordings"
    f = root / REL
    f.parent.mkdir(parents=True)
    f.write_bytes(b"\x00" * 7_654_321)

    # MediaMTX's spelling resolves from / in production; from tmp_path here.
    monkeypatch.setattr(hooks, "absolute_segment_path",
                        lambda p: str((tmp_path / p.lstrip("./")).resolve()))
    monkeypatch.setattr(hooks, "_verify_hook_token", lambda request: True)
    monkeypatch.setattr(hooks, "_camera_from_path", lambda db, path: db.query(Camera).first())
    import services.camera_identity as ident
    import services.hevc_remux_service as hevc
    import services.storage_service as storage
    monkeypatch.setattr(storage, "get_effective_recordings_base_path", lambda db=None: str(root))
    monkeypatch.setattr(ident, "verify_segment_identity", lambda cam, path, root: True)
    monkeypatch.setattr(hevc, "probe_video_codec", lambda path: "h264")

    app = FastAPI()
    app.include_router(hooks.router)

    def _db():
        d = factory()
        try:
            yield d
        finally:
            d.close()

    app.dependency_overrides[get_db] = _db
    with TestClient(app) as c:
        yield c, factory, f


def test_a_relative_segment_path_is_stated_and_sized_at_insert(env):
    client, factory, f = env
    r = client.get("/mediamtx/hooks/segment-complete",
                   params={"path": "cam-1", "segment_path": "./app/recordings/" + REL,
                           "segment_duration": "60.2"})
    assert r.status_code == 200, r.text
    db = factory()
    row = db.query(Recording).first()
    assert row is not None
    assert row.file_path == REL
    assert row.file_size == f.stat().st_size, "the size the segment-health check judges by"
    assert row.codec == "h264", "read once here, so playback never has to probe"
    assert row.duration == pytest.approx(60.2)
