# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
Slice 4 of docs/design/per-camera-assignment.md: the live model polling
loop is retired. POST /{id}/start-inference answers 410 Gone with an
actionable migration pointer (Tier-0 + camera assignments); the
InferenceManager keeps ONLY the on-demand recording analysis.

Run with:

    cd server && pytest tests/test_retired_live_inference.py -v
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


for _name in (
    "main_logger", "auth_logger", "camera_logger",
    "recording_logger", "cloud_logger", "ai_logger",
):
    setattr(_lm, _name, _L())
_lm.setup_logging = lambda *a, **k: None
sys.modules.setdefault("core.logging_config", _lm)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.auth import get_current_active_user  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import AIModel, AuditLog, Role, User  # noqa: E402
from routers import ai_model_management as mgmt  # noqa: E402


class _StubUser:
    id = 1
    username = "tester"
    is_superuser = True


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[AIModel.__table__, AuditLog.__table__,
                Role.__table__, User.__table__],
    )
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    session.add(AIModel(
        id=1, name="Person watch", model_name="yolov8",
        task="person_detection", enabled=True,
        source_type="live", assigned_camera_id=1, inference_interval=2,
    ))
    session.commit()
    session.close()

    app = FastAPI()
    app.include_router(mgmt.router)

    def _override_db():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_active_user] = lambda: _StubUser()
    with TestClient(app) as c:
        yield c


def _start_path(client) -> str:
    # Router prefix may or may not include /api/v1 — derive it.
    prefix = mgmt.router.prefix
    return f"{prefix}/1/start-inference"


def test_live_start_is_gone_with_a_migration_pointer(client):
    r = client.post(_start_path(client))
    assert r.status_code == 410
    detail = r.json()["detail"]
    assert "Assignments" in detail and "Tier-0" in detail
    assert "Recording analysis is unaffected" in detail


def test_unknown_model_is_still_a_404_not_a_410(client):
    r = client.post(_start_path(client).replace("/1/", "/999/"))
    assert r.status_code == 404


def test_manager_keeps_recording_analysis_only():
    from services.inference_manager import InferenceManager

    mgr = InferenceManager()
    # The live loops are gone...
    assert not hasattr(mgr, "start_inference")
    assert not hasattr(mgr, "start_cloud_inference")
    # ...the forensic recording pass and the stop machinery remain.
    assert hasattr(mgr, "start_recording_inference")
    assert hasattr(mgr, "stop_inference") and hasattr(mgr, "stop_all")
