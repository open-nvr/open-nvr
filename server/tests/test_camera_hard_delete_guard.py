# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Guards on POST /cameras/{id}/hard-delete (issue #243).

Permanently deleting a camera purges its DB rows AND its recordings from
disk, so it is deliberately hard to trigger: superuser only, the camera must
already be in the bin, the caller must present a current TOTP code
(X-MFA-Code) and type the exact phrase
``hard delete <camera name> and it's recording``. A recordings directory
whose identity marker belongs to a different camera uuid is skipped, never
deleted.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import types as _types
from datetime import UTC, datetime
from pathlib import Path

import pyotp
import pytest
from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

os.environ.setdefault("DATABASE_URL", "sqlite:///./_camera_hard_delete_test.db")
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

import core.auth as core_auth  # noqa: E402
from core.auth import create_access_token, get_password_hash  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import Camera, Recording, Role, User  # noqa: E402
from routers import cameras as cameras_router  # noqa: E402
from services.mediamtx_admin_service import MediaMtxAdminService  # noqa: E402

PASSWORD = "Str0ng!passw0rd"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(core_auth, "auth_logger", _L(), raising=False)
    monkeypatch.setattr(cameras_router, "camera_logger", _L(), raising=False)

    async def _fake_unprovision(camera_id, camera_ip):
        return {"status": "success"}

    monkeypatch.setattr(
        MediaMtxAdminService, "unprovision_path", staticmethod(_fake_unprovision)
    )
    # Recordings root for the purge — an isolated temp dir, never D:'s data.
    monkeypatch.setattr(
        cameras_router,
        "get_effective_recordings_base_path",
        lambda db: str(tmp_path),
    )

    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
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
    totp_secret = pyotp.random_base32()
    admin = User(
        username="admin",
        email="admin@example.com",
        hashed_password=get_password_hash(PASSWORD),
        is_active=True,
        is_superuser=True,
        password_set=True,
        mfa_enabled=True,
        role_id=role.id,
    )
    admin.mfa_secret = totp_secret
    plain_user = User(
        username="pleb",
        email="pleb@example.com",
        hashed_password=get_password_hash(PASSWORD),
        is_active=True,
        is_superuser=False,
        password_set=True,
        role_id=role.id,
    )
    db.add_all([admin, plain_user])
    db.commit()

    def make_binned_camera(name="Old Cam", *, deleted=True, with_recordings=True):
        cam = Camera(
            name=name,
            ip_address="10.0.0.9",
            port=554,
            owner_id=admin.id,
            is_active=not deleted,
            deleted_at=datetime.now(UTC) if deleted else None,
            status="unknown",
        )
        db.add(cam)
        db.commit()
        db.refresh(cam)
        if with_recordings:
            db.add(
                Recording(
                    filename="seg.mp4",
                    file_path=f"cam-{cam.id}/2026-08-17/seg.mp4",
                    start_time=datetime.now(UTC),
                    camera_id=cam.id,
                )
            )
            db.commit()
            cam_dir = tmp_path / f"cam-{cam.id}" / "2026-08-17"
            cam_dir.mkdir(parents=True)
            (cam_dir / "seg.mp4").write_bytes(b"\x00")
        return cam

    client = TestClient(app)
    try:
        yield _types.SimpleNamespace(
            client=client,
            db=db,
            admin=admin,
            make_binned_camera=make_binned_camera,
            totp_secret=totp_secret,
            root=tmp_path,
        )
    finally:
        db.close()


def _auth(username: str = "admin") -> dict:
    return {"Authorization": f"Bearer {create_access_token({'sub': username})}"}


def _code(env) -> str:
    return pyotp.TOTP(env.totp_secret).now()


def _phrase(cam_name: str) -> str:
    return f"hard delete {cam_name} and it's recording"


def _post(env, cam, *, phrase=None, code=None, user="admin"):
    headers = _auth(user)
    if code is not None:
        headers["X-MFA-Code"] = code
    return env.client.post(
        f"/api/v1/cameras/{cam.id}/hard-delete",
        json={"confirmation_phrase": phrase or _phrase(cam.name)},
        headers=headers,
    )


def test_non_superuser_forbidden(env):
    cam = env.make_binned_camera(with_recordings=False)
    resp = _post(env, cam, code=_code(env), user="pleb")
    assert resp.status_code == 403
    assert env.db.get(Camera, cam.id) is not None


def test_not_binned_camera_conflicts(env):
    cam = env.make_binned_camera(deleted=False, with_recordings=False)
    resp = _post(env, cam, code=_code(env))
    assert resp.status_code == 409
    assert env.db.get(Camera, cam.id) is not None


def test_missing_or_wrong_mfa_rejected(env):
    cam = env.make_binned_camera(with_recordings=False)
    assert _post(env, cam).status_code == 401
    assert _post(env, cam, code="000000").status_code == 401
    assert env.db.get(Camera, cam.id) is not None


def test_wrong_phrase_rejected(env):
    cam = env.make_binned_camera(with_recordings=False)
    resp = _post(env, cam, phrase="hard delete something else", code=_code(env))
    assert resp.status_code == 400
    assert _phrase(cam.name) in resp.json()["detail"]
    assert env.db.get(Camera, cam.id) is not None


def test_hard_delete_purges_rows_and_files(env):
    cam = env.make_binned_camera()
    cam_dir = env.root / f"cam-{cam.id}"
    assert cam_dir.is_dir()

    resp = _post(env, cam, code=_code(env))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["files"] == "deleted"
    assert body["recording_rows_deleted"] == 1

    env.db.expunge_all()
    assert env.db.query(Camera).filter_by(id=cam.id).first() is None
    assert env.db.query(Recording).filter_by(camera_id=cam.id).count() == 0
    assert not cam_dir.exists()


def test_dir_owned_by_other_camera_is_skipped(env):
    cam = env.make_binned_camera()
    cam_dir = env.root / f"cam-{cam.id}"
    # Another camera's identity marker claims this directory.
    (cam_dir / ".camera-identity.json").write_text(
        json.dumps({"version": 1, "camera_uuid": "someone-elses-uuid"}),
        encoding="utf-8",
    )

    resp = _post(env, cam, code=_code(env))
    assert resp.status_code == 200, resp.text
    assert resp.json()["files"] == "skipped-identity-mismatch"
    # DB rows are gone, but the foreign archive stays on disk untouched.
    env.db.expunge_all()
    assert env.db.query(Camera).filter_by(id=cam.id).first() is None
    assert (cam_dir / "2026-08-17" / "seg.mp4").exists()
