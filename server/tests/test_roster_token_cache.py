# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The internal roster endpoint mints the platform's shared MediaMTX token
once and reuses it, instead of signing one per request (240 an hour on the
Ventuno Q, 2026-11-09). Run with:

    cd server && pytest tests/test_roster_token_cache.py -v
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

from core.config import settings  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import Camera, CameraMask, Role, User  # noqa: E402
from routers import internal_camera_agent as internal_router  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "inference_use_mediamtx_tap", True)
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[Camera.__table__, CameraMask.__table__,
                                             User.__table__, Role.__table__])
    factory = sessionmaker(bind=engine)
    s = factory()
    s.add(Role(id=1, name="admin"))
    s.add(User(id=1, username="op", email="op@x", hashed_password="x", role_id=1))
    s.add(Camera(id=1, name="Plain", ip_address="10.0.0.3", owner_id=1, is_active=True,
                 rtsp_url="rtsp://u:p@10.0.0.3:554/opaque/onvif/path"))
    s.commit(); s.close()

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


def _frame_url(client) -> str:
    return client.get("/internal/camera-agent/cameras").json()["cameras"][0]["frame_url"]


def test_platform_callers_share_one_token_across_requests(client, monkeypatch):
    minted = []

    def _mint(camera_path="~.*", username="camera-agent-internal"):
        minted.append(camera_path)
        return f"tok-{len(minted)}"

    monkeypatch.setattr(internal_router, "_mint_mediamtx_jwt", _mint)
    urls = [_frame_url(client) for _ in range(5)]
    assert minted == ["~.*"], "five roster reads, one signature"
    assert all(u.endswith("?jwt=tok-1") for u in urls)


def test_the_token_is_minted_again_once_its_reuse_window_passes(client, monkeypatch):
    minted = []
    monkeypatch.setattr(internal_router, "_mint_mediamtx_jwt",
                        lambda camera_path="~.*", username="x": (minted.append(1), f"tok-{len(minted)}")[1])
    clock = {"t": 1000.0}
    import time as _time
    monkeypatch.setattr(_time, "monotonic", lambda: clock["t"])
    assert _frame_url(client).endswith("tok-1")
    clock["t"] += internal_router._PLATFORM_TOKEN_REUSE_S - 1
    assert _frame_url(client).endswith("tok-1")
    clock["t"] += 2
    assert _frame_url(client).endswith("tok-2")
    assert len(minted) == 2


def test_a_failed_mint_is_not_cached_so_the_fallback_heals(client, monkeypatch):
    calls = []

    def _mint(camera_path="~.*", username="x"):
        calls.append(1)
        return None if len(calls) == 1 else "tok-ok"

    monkeypatch.setattr(internal_router, "_mint_mediamtx_jwt", _mint)
    assert "jwt=" not in _frame_url(client)        # bare URL fallback
    assert _frame_url(client).endswith("?jwt=tok-ok")
    assert len(calls) == 2


def test_default_recording_path_is_announced_once(monkeypatch, tmp_path, caplog):
    import logging

    import services.storage_service as ss

    monkeypatch.setattr(ss.settings, "recordings_base_path", str(tmp_path / "rec"))
    monkeypatch.setattr(ss, "_announced_default_paths", set())
    real = logging.getLogger("test.storage")
    monkeypatch.setattr(ss, "storage_logger", real)
    class _NoRow:                      # no stored storage setting: the default wins
        def filter(self, *a, **k):
            return self

        def first(self):
            return None

    class _NoDb:
        def query(self, *a, **k):
            return _NoRow()

    with caplog.at_level(logging.DEBUG, logger="test.storage"):
        for _ in range(4):
            ss.get_effective_recordings_base_path(_NoDb())
    infos = [r for r in caplog.records if r.levelno == logging.INFO and "Using default recording path" in r.getMessage()]
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG and "Using default recording path" in r.getMessage()]
    assert len(infos) == 1 and len(debugs) == 3
