# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
Per-camera capability assignment — slice 1 of
docs/design/per-camera-assignment.md.

Run with:

    cd server && pytest tests/test_camera_assignments.py -v

Coverage:

* ``CameraAssignment`` / ``CameraUpdate`` validation — the write-path
  rules: skill vocabulary shape, label normalization + bounds, the
  8-assignments cap, one entry per skill, ``[]`` clears.
* The internal camera-agent endpoint serves ``assignments`` additively:
  a camera with assignments returns them verbatim; one without returns
  ``[]``; every pre-existing key is still present (back-compat).
"""
from __future__ import annotations

# Python 3.10 sandbox polyfill — pyproject requires 3.11+ where
# datetime.UTC exists. No-op on 3.11+.
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
from pydantic import ValidationError  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.database import Base, get_db  # noqa: E402
from models import Camera, Role, User  # noqa: E402
from routers import internal_camera_agent as internal_router  # noqa: E402
from schemas import CameraAssignment, CameraUpdate  # noqa: E402


# ─── Write-path validation (the schema IS the rulebook) ─────────────────


def test_assignment_accepts_skill_and_optional_labels():
    a = CameraAssignment(skill="license_plate_recognition")
    assert a.skill == "license_plate_recognition" and a.labels is None
    b = CameraAssignment(skill="object_detection", labels=["Person", "TRUCK", "person"])
    # normalized: lowercased, deduped, order kept
    assert b.labels == ["person", "truck"]


def test_assignment_rejects_bad_skill_shapes():
    for bad in ("", "X", "Object Detection", "lpr!", "9skill", "a"):
        with pytest.raises(ValidationError):
            CameraAssignment(skill=bad)


def test_assignment_label_bounds():
    with pytest.raises(ValidationError):
        CameraAssignment(skill="object_detection", labels=[""])
    with pytest.raises(ValidationError):
        CameraAssignment(skill="object_detection", labels=["x" * 65])
    with pytest.raises(ValidationError):
        CameraAssignment(
            skill="object_detection", labels=[f"label{i}" for i in range(33)]
        )


def test_update_caps_assignments_and_forbids_duplicate_skills():
    ok = CameraUpdate(assignments=[
        {"skill": "object_detection", "labels": ["truck"]},
        {"skill": "license_plate_recognition"},
    ])
    dumped = ok.model_dump(exclude_unset=True)["assignments"]
    # model_dump produces plain JSON-ready dicts — what setattr stores.
    assert dumped[0] == {"skill": "object_detection", "labels": ["truck"]}
    with pytest.raises(ValidationError):
        CameraUpdate(assignments=[{"skill": f"skill_{i}"} for i in range(9)])
    with pytest.raises(ValidationError):
        CameraUpdate(assignments=[
            {"skill": "object_detection"}, {"skill": "object_detection"},
        ])


def test_update_empty_list_clears_and_none_means_untouched():
    cleared = CameraUpdate(assignments=[])
    assert cleared.model_dump(exclude_unset=True) == {"assignments": []}
    untouched = CameraUpdate(name="Gate")
    assert "assignments" not in untouched.model_dump(exclude_unset=True)


# ─── Read path: the internal camera-agent endpoint ──────────────────────


def _make_app():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine, tables=[Camera.__table__, User.__table__, Role.__table__]
    )
    session_factory = sessionmaker(bind=engine)

    app = FastAPI()
    app.include_router(internal_router.router)

    def _override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[internal_router._require_internal_key] = lambda: None
    return app, session_factory


@pytest.fixture
def seeded_client(monkeypatch):
    """Two cameras — one assigned, one not — behind the internal router.

    MediaMTX tap is disabled so ``frame_url`` falls back to the stored
    rtsp_url and the test needs no JWT keys.
    """
    from core.config import settings

    monkeypatch.setattr(settings, "inference_use_mediamtx_tap", False)
    app, session_factory = _make_app()
    session = session_factory()
    session.add(Role(id=1, name="admin"))
    session.add(User(id=1, username="op", email="op@x",
                     hashed_password="x", role_id=1))
    session.add(Camera(
        id=1, name="Gate", ip_address="10.0.0.1", owner_id=1, is_active=True,
        rtsp_url="rtsp://cam1/stream",
        assignments=[{"skill": "license_plate_recognition"},
                     {"skill": "object_detection", "labels": ["person", "truck"]}],
    ))
    session.add(Camera(
        id=2, name="Yard", ip_address="10.0.0.2", owner_id=1, is_active=True,
        rtsp_url="rtsp://cam2/stream",
    ))
    session.commit()
    session.close()
    with TestClient(app) as client:
        yield client


def test_internal_endpoint_serves_assignments_additively(seeded_client):
    body = seeded_client.get("/internal/camera-agent/cameras").json()
    cams = {c["camera_id"]: c for c in body["cameras"]}
    assert cams["cam1"]["assignments"] == [
        {"skill": "license_plate_recognition"},
        {"skill": "object_detection", "labels": ["person", "truck"]},
    ]
    # NULL in the DB serves as [] — "nothing assigned", never absent.
    assert cams["cam2"]["assignments"] == []
    # Back-compat: every pre-existing key is still there for old consumers.
    for key in ("camera_id", "open_nvr_camera_id", "name",
                "frame_url", "role", "source"):
        assert key in cams["cam1"], key


def test_update_dump_round_trips_through_the_model(seeded_client):
    """What CameraUpdate dumps is exactly what the JSON column stores and
    the internal endpoint serves — no serialization drift between the
    write path's setattr loop and the read path."""
    upd = CameraUpdate(assignments=[{"skill": "occupancy_counting"}])
    dumped = upd.model_dump(exclude_unset=True)["assignments"]
    assert dumped == [{"skill": "occupancy_counting"}]
