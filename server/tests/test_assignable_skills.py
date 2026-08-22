# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
GET /cameras/assignable-skills — suggestions + live availability for the
camera Assignments editor (per-camera assignment, consumer 3).

Run with:

    cd server && pytest tests/test_assignable_skills.py -v
"""
from __future__ import annotations

import datetime as _dt

if not hasattr(_dt, "UTC"):
    _dt.UTC = _dt.timezone.utc  # noqa: UP017 — only runs where UTC is absent

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
# Force-assign (not setdefault): an earlier-collected test may have installed
# a narrower stub without a __getattr__ fallback, which breaks
# `from core.logging_config import mediamtx_logger` at import time.
sys.modules["core.logging_config"] = _lm

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.auth import get_current_active_user  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import InstalledApp, Role, User  # noqa: E402
from routers import cameras as cameras_router  # noqa: E402


class _StubUser:
    id = 1
    username = "tester"
    is_superuser = True


class _FakeKaiC:
    def __init__(self, adapters=None, fail=False):
        self._adapters = adapters or {}
        self._fail = fail

    async def get_capabilities(self):
        if self._fail:
            raise RuntimeError("KAI-C unreachable")
        return {"adapters": self._adapters}


def _client(monkeypatch, *, kaic: _FakeKaiC, apps=()):
    import services.kai_c_service as kaic_mod

    monkeypatch.setattr(kaic_mod, "get_kai_c_service", lambda: kaic)
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine, tables=[InstalledApp.__table__, User.__table__, Role.__table__]
    )
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    for app_id, enabled in apps:
        session.add(InstalledApp(
            id=app_id, name=app_id.replace("-", " ").title(), version="1.0.0",
            url=f"http://{app_id}:9200", manifest_json={"id": app_id},
            config_json={}, enabled=enabled,
        ))
    session.commit()
    session.close()

    app = FastAPI()
    app.include_router(cameras_router.router)

    def _override_db():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_active_user] = lambda: _StubUser()
    return TestClient(app)


def _skills(client) -> dict[str, dict]:
    r = client.get("/cameras/assignable-skills")
    assert r.status_code == 200, r.text
    return {s["skill"]: s for s in r.json()["skills"]}


def test_tier0_always_provides_object_detection(monkeypatch):
    sk = _skills(_client(monkeypatch, kaic=_FakeKaiC()))
    assert sk["object_detection"]["available"] is True
    assert "Tier-0" in sk["object_detection"]["hint"]


def test_adapter_tasks_and_aliases_mark_availability(monkeypatch):
    kaic = _FakeKaiC(adapters={
        # canonical spelling on one adapter, an ALIAS on another
        "plates": {"capabilities": {"tasks_advertised": ["lpr"]}},
        "faces": {"capabilities": {"tasks_advertised": ["face_recognition"]}},
    })
    sk = _skills(_client(monkeypatch, kaic=kaic))
    assert sk["license_plate_recognition"]["available"] is True   # via alias
    assert sk["face_recognition"]["available"] is True
    assert sk["image_captioning"]["available"] is False
    assert "AI Adapters" in sk["image_captioning"]["hint"]


def test_kaic_unreachable_means_unknown_not_unavailable(monkeypatch):
    sk = _skills(_client(monkeypatch, kaic=_FakeKaiC(fail=True)))
    # Tri-state: null, never false — the UI must not grey on unknown.
    assert sk["license_plate_recognition"]["available"] is None
    assert sk["object_detection"]["available"] is True            # Tier-0 regardless


def test_installed_apps_are_skills_available_when_enabled(monkeypatch):
    client = _client(monkeypatch, kaic=_FakeKaiC(), apps=[
        ("occupancy-counting", True), ("loitering-detection", False),
    ])
    sk = _skills(client)
    assert sk["occupancy_counting"]["available"] is True
    assert sk["occupancy_counting"]["source"] == "app"
    assert sk["loitering_detection"]["available"] is False
    assert "disabled" in sk["loitering_detection"]["hint"]
