# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Self-deletion / self-deactivation guards and MFA-confirmed user deletion.

Issue #176: the default admin could delete their own account from the UI;
the soft delete flips is_active, so the next login says "Account is inactive"
and a single-admin install is bricked. These tests pin the guards: no user may
delete or deactivate themselves, and deleting anyone requires the caller's
current TOTP code in the X-MFA-Code header.
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

os.environ.setdefault("DATABASE_URL", "sqlite:///./_delete_guard_test.db")
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
from core.auth import create_access_token, get_password_hash  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import Role, User  # noqa: E402
from routers import users as users_router  # noqa: E402

PASSWORD = "Str0ng!passw0rd"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(core_auth, "auth_logger", _L(), raising=False)
    monkeypatch.setattr(users_router, "main_logger", _L(), raising=False)

    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    session_factory = sessionmaker(bind=eng)

    app = FastAPI()
    app.include_router(users_router.router, prefix="/api/v1")

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
    totp_secret = pyotp.random_base32()
    admin = User(
        username="admin",
        email="admin@example.com",
        hashed_password=hashed,
        is_active=True,
        is_superuser=True,
        password_set=True,
        mfa_enabled=True,
        role_id=role.id,
    )
    admin.mfa_secret = totp_secret
    # A superuser who never enrolled MFA — must not be able to delete anyone.
    admin_no_mfa = User(
        username="admin2",
        email="admin2@example.com",
        hashed_password=hashed,
        is_active=True,
        is_superuser=True,
        password_set=True,
        mfa_enabled=False,
        role_id=role.id,
    )
    victim = User(
        username="victim",
        email="victim@example.com",
        hashed_password=hashed,
        is_active=True,
        password_set=True,
        role_id=role.id,
    )
    db.add_all([admin, admin_no_mfa, victim])
    db.commit()

    client = TestClient(app)
    try:
        yield _types.SimpleNamespace(
            client=client,
            db=db,
            admin=admin,
            admin_no_mfa=admin_no_mfa,
            victim=victim,
            totp_secret=totp_secret,
        )
    finally:
        db.close()


def _auth(username: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token({'sub': username})}"}


def _code(env) -> str:
    return pyotp.TOTP(env.totp_secret).now()


def test_self_delete_is_blocked_even_with_valid_code(env):
    resp = env.client.delete(
        f"/api/v1/users/{env.admin.id}",
        headers={**_auth("admin"), "X-MFA-Code": _code(env)},
    )
    assert resp.status_code == 400
    assert "own account" in resp.json()["detail"]
    env.db.expire_all()
    assert env.db.get(User, env.admin.id).is_active is True


def test_delete_without_code_is_rejected(env):
    resp = env.client.delete(
        f"/api/v1/users/{env.victim.id}", headers=_auth("admin")
    )
    assert resp.status_code == 401
    env.db.expire_all()
    assert env.db.get(User, env.victim.id).is_active is True


def test_delete_with_wrong_code_is_rejected(env):
    resp = env.client.delete(
        f"/api/v1/users/{env.victim.id}",
        headers={**_auth("admin"), "X-MFA-Code": "000000"},
    )
    assert resp.status_code == 401
    env.db.expire_all()
    assert env.db.get(User, env.victim.id).is_active is True


def test_delete_with_valid_code_succeeds(env):
    resp = env.client.delete(
        f"/api/v1/users/{env.victim.id}",
        headers={**_auth("admin"), "X-MFA-Code": _code(env)},
    )
    assert resp.status_code == 200, resp.text
    env.db.expire_all()
    assert env.db.get(User, env.victim.id).is_active is False


def test_superuser_without_mfa_cannot_delete(env):
    resp = env.client.delete(
        f"/api/v1/users/{env.victim.id}",
        headers={**_auth("admin2"), "X-MFA-Code": "123456"},
    )
    assert resp.status_code == 400
    assert "MFA" in resp.json()["detail"]


def test_self_deactivation_via_update_is_blocked(env):
    resp = env.client.put(
        f"/api/v1/users/{env.admin.id}",
        json={"is_active": False},
        headers=_auth("admin"),
    )
    assert resp.status_code == 400
    assert "own account" in resp.json()["detail"]
    env.db.expire_all()
    assert env.db.get(User, env.admin.id).is_active is True


def test_deactivating_another_user_via_update_still_works(env):
    resp = env.client.put(
        f"/api/v1/users/{env.victim.id}",
        json={"is_active": False},
        headers=_auth("admin"),
    )
    assert resp.status_code == 200, resp.text
    env.db.expire_all()
    assert env.db.get(User, env.victim.id).is_active is False
