# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Login vs. MFA enrollment state.

An account can be flagged mfa_enabled without ever enrolling a TOTP secret
(admin-created users before the default changed, provisioned bootstrap admins).
No valid code can exist for such an account, so demanding one at login locks it
out forever. These tests pin the intended flow: password login succeeds, the
flag is normalized to False so the client shows the MFA-setup wall, and only
genuinely enrolled users are asked for a code.
"""

from __future__ import annotations

import os
import secrets
import sys
import types as _types
from pathlib import Path

import pyotp
import pytest
from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

os.environ.setdefault("DATABASE_URL", "sqlite:///./_mfa_wall_test.db")
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

import core.auth as core_auth  # noqa: E402
from core.auth import get_password_hash  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import Role, User  # noqa: E402
from routers import auth as auth_router  # noqa: E402
from schemas import UserCreate  # noqa: E402
from services.user_service import UserService  # noqa: E402

PASSWORD = "Str0ng!passw0rd"


@pytest.fixture
def env(monkeypatch):
    # In full-suite runs, whichever sibling stubbed core.logging_config first
    # wins the sys.modules race, and some stubs lack log_action. The login
    # endpoints log liberally, so give the modules under test a logger that
    # accepts anything.
    monkeypatch.setattr(auth_router, "auth_logger", _L(), raising=False)
    monkeypatch.setattr(core_auth, "auth_logger", _L(), raising=False)
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    session_factory = sessionmaker(bind=eng)

    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api/v1")

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

    hashed = get_password_hash(PASSWORD)
    # Flagged for MFA but never enrolled — the broken state under test.
    unenrolled = User(
        username="unenrolled",
        email="unenrolled@example.com",
        hashed_password=hashed,
        is_active=True,
        password_set=True,
        mfa_enabled=True,
        role_id=role.id,
    )
    # Genuinely enrolled: has a TOTP secret.
    totp_secret = pyotp.random_base32()
    enrolled = User(
        username="enrolled",
        email="enrolled@example.com",
        hashed_password=hashed,
        is_active=True,
        password_set=True,
        mfa_enabled=True,
        role_id=role.id,
    )
    enrolled.mfa_secret = totp_secret
    db.add_all([unenrolled, enrolled])
    db.commit()

    client = TestClient(app)
    try:
        yield _types.SimpleNamespace(
            client=client,
            db=db,
            role=role,
            totp_secret=totp_secret,
        )
    finally:
        db.close()


def _login_json(client, username, password, code=None):
    body = {"username": username, "password": password}
    if code is not None:
        body["code"] = code
    return client.post("/api/v1/auth/login-json", json=body)


def test_unenrolled_user_logs_in_with_password_only(env):
    resp = _login_json(env.client, "unenrolled", PASSWORD)
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"]


def test_unenrolled_login_normalizes_flag_for_mfa_wall(env):
    _login_json(env.client, "unenrolled", PASSWORD)
    env.db.expire_all()
    user = env.db.query(User).filter(User.username == "unenrolled").first()
    # False → the SPA routes the session to the MFA-setup wall.
    assert user.mfa_enabled is False


def test_unenrolled_login_does_not_count_failed_attempts(env):
    _login_json(env.client, "unenrolled", PASSWORD)
    env.db.expire_all()
    user = env.db.query(User).filter(User.username == "unenrolled").first()
    assert user.failed_login_attempts == 0
    assert user.locked_until is None


def test_unenrolled_user_logs_in_via_form_endpoint(env):
    resp = env.client.post(
        "/api/v1/auth/login",
        data={"username": "unenrolled", "password": PASSWORD},
    )
    assert resp.status_code == 200, resp.text


def test_enrolled_user_still_needs_code(env):
    resp = _login_json(env.client, "enrolled", PASSWORD)
    assert resp.status_code == 401
    assert "mfa" in str(resp.json()["detail"]).lower()


def test_enrolled_user_logs_in_with_valid_code(env):
    code = pyotp.TOTP(env.totp_secret).now()
    resp = _login_json(env.client, "enrolled", PASSWORD, code=code)
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"]


def test_enrolled_flag_survives_successful_login(env):
    code = pyotp.TOTP(env.totp_secret).now()
    _login_json(env.client, "enrolled", PASSWORD, code=code)
    env.db.expire_all()
    user = env.db.query(User).filter(User.username == "enrolled").first()
    assert user.mfa_enabled is True


def test_admin_created_user_starts_unenrolled(env):
    user = UserService.create_user(
        env.db,
        UserCreate(
            username="fresh",
            email="fresh@example.com",
            password=PASSWORD,
            role_id=env.role.id,
        ),
    )
    assert user.mfa_enabled is False
    assert user.mfa_secret is None
