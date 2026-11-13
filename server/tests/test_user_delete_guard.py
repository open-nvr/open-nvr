# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Self-deletion guards, reversible deactivation, and MFA-confirmed permanent deletion.

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
from models import AuditLog, Camera, Role, User  # noqa: E402
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


def test_delete_without_code_soft_deactivates_user(env):
    resp = env.client.delete(
        f"/api/v1/users/{env.victim.id}", headers=_auth("admin")
    )
    assert resp.status_code == 200
    env.db.expire_all()
    assert env.db.get(User, env.victim.id).is_active is False


def test_user_list_can_filter_deactivated_accounts(env):
    env.victim.is_active = False
    env.db.commit()

    resp = env.client.get(
        "/api/v1/users/?active_only=false&is_active=false",
        headers=_auth("admin"),
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [user["username"] for user in data["users"]] == ["victim"]
    assert data["total"] == 1


def test_delete_with_valid_code_succeeds(env):
    resp = env.client.delete(
        f"/api/v1/users/{env.victim.id}",
        headers={**_auth("admin"), "X-MFA-Code": _code(env)},
    )
    assert resp.status_code == 200, resp.text
    env.db.expire_all()
    assert env.db.get(User, env.victim.id).is_active is False


def test_permanent_delete_requires_mfa(env):
    resp = env.client.delete(
        f"/api/v1/users/{env.victim.id}/permanent", headers=_auth("admin")
    )
    assert resp.status_code == 401
    env.db.expire_all()
    assert env.db.get(User, env.victim.id) is not None

    resp = env.client.delete(
        f"/api/v1/users/{env.victim.id}/permanent",
        headers={**_auth("admin"), "X-MFA-Code": "000000"},
    )
    assert resp.status_code == 401
    env.db.expire_all()
    assert env.db.get(User, env.victim.id) is not None


def test_permanent_delete_preserves_audit_history(env):
    victim_id = env.victim.id
    history = AuditLog(
        action="user.update",
        entity_type="user",
        entity_id=str(victim_id),
        user_id=victim_id,
    )
    env.db.add(history)
    env.db.commit()
    history_id = history.id

    resp = env.client.delete(
        f"/api/v1/users/{victim_id}/permanent",
        headers={**_auth("admin"), "X-MFA-Code": _code(env)},
    )

    assert resp.status_code == 200, resp.text
    env.db.expire_all()
    assert env.db.get(User, victim_id) is None
    retained_history = env.db.get(AuditLog, history_id)
    assert retained_history is not None
    assert retained_history.user_id is None
    assert retained_history.entity_id == str(victim_id)


def test_permanent_delete_is_blocked_by_camera_ownership(env):
    camera = Camera(
        name="Victim camera",
        ip_address="192.0.2.10",
        owner_id=env.victim.id,
    )
    env.db.add(camera)
    env.db.commit()

    resp = env.client.delete(
        f"/api/v1/users/{env.victim.id}/permanent",
        headers={**_auth("admin"), "X-MFA-Code": _code(env)},
    )

    assert resp.status_code == 409
    assert "cameras" in resp.json()["detail"]
    env.db.expire_all()
    assert env.db.get(User, env.victim.id) is not None


def test_superuser_without_mfa_cannot_delete(env):
    resp = env.client.delete(
        f"/api/v1/users/{env.victim.id}/permanent",
        headers={**_auth("admin2"), "X-MFA-Code": "123456"},
    )
    # An account with no second factor is held at the enrolment wall: the
    # API refuses it before the endpoint's own MFA check is reached.
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "mfa_enrollment_required"


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


def _deactivate_victim(env):
    victim = env.db.get(User, env.victim.id)
    victim.is_active = False
    env.db.commit()


def test_activate_requires_valid_code(env):
    _deactivate_victim(env)
    resp = env.client.post(
        f"/api/v1/users/{env.victim.id}/activate", headers=_auth("admin")
    )
    assert resp.status_code == 401
    resp = env.client.post(
        f"/api/v1/users/{env.victim.id}/activate",
        headers={**_auth("admin"), "X-MFA-Code": "000000"},
    )
    assert resp.status_code == 401
    env.db.expire_all()
    assert env.db.get(User, env.victim.id).is_active is False


def test_activate_with_valid_code_succeeds(env):
    _deactivate_victim(env)
    resp = env.client.post(
        f"/api/v1/users/{env.victim.id}/activate",
        headers={**_auth("admin"), "X-MFA-Code": _code(env)},
    )
    assert resp.status_code == 200, resp.text
    env.db.expire_all()
    assert env.db.get(User, env.victim.id).is_active is True


def test_update_cannot_bypass_mfa_gated_reactivation(env):
    _deactivate_victim(env)
    resp = env.client.put(
        f"/api/v1/users/{env.victim.id}",
        json={"is_active": True},
        headers=_auth("admin"),
    )
    assert resp.status_code == 400
    assert "MFA" in resp.json()["detail"]
    env.db.expire_all()
    assert env.db.get(User, env.victim.id).is_active is False
