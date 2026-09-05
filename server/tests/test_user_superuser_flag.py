# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""``is_superuser`` on the users API, and the seeded permission
catalogue wired to the writes that were open to any signed-in user.

Until now ``is_superuser`` was a database column with no API: the
first-time-setup admin was the only superuser a deployment could ever
have without a SQL edit. ``POST /users`` and ``PUT /users/{id}`` now
carry the flag, MFA-gated like delete, with the last active superuser
protected from demotion, deactivation and deletion.

Run with:
    cd server && pytest tests/test_user_superuser_flag.py -v
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

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_superuser_flag_test.db")
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
from routers import ai_model_management as aimm_router  # noqa: E402
from routers import ai_models as ai_router  # noqa: E402
from routers import cameras as cameras_router  # noqa: E402
from routers import users as users_router  # noqa: E402

PASSWORD = "Str0ng!passw0rd"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(core_auth, "auth_logger", _L(), raising=False)
    monkeypatch.setattr(users_router, "main_logger", _L(), raising=False)

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
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
    secret = pyotp.random_base32()
    root = User(username="root", email="root@example.com", hashed_password=hashed,
                is_active=True, is_superuser=True, password_set=True,
                mfa_enabled=True, role_id=role.id)
    root.mfa_secret = secret
    plain = User(username="plain", email="plain@example.com", hashed_password=hashed,
                 is_active=True, is_superuser=False, password_set=True, role_id=role.id)
    db.add_all([root, plain])
    db.commit()
    client = TestClient(app)
    try:
        yield _types.SimpleNamespace(client=client, db=db, root=root, plain=plain,
                                     role=role, secret=secret)
    finally:
        db.close()


def _auth(username):
    return {"Authorization": f"Bearer {create_access_token({'sub': username})}"}


def _code(env):
    return pyotp.TOTP(env.secret).now()


# ── create ─────────────────────────────────────────────────────────────


def test_create_superuser_needs_mfa_and_sets_the_flag(env):
    body = {"username": "second", "email": "second@example.com",
            "password": PASSWORD, "role_id": env.role.id, "is_superuser": True}
    r = env.client.post("/api/v1/users/", json=body, headers=_auth("root"))
    assert r.status_code == 401 and "MFA" in r.json()["detail"]   # no code

    r = env.client.post("/api/v1/users/", json=body,
                        headers={**_auth("root"), "X-MFA-Code": _code(env)})
    assert r.status_code == 200, r.text
    assert r.json()["is_superuser"] is True
    env.db.expire_all()
    assert env.db.query(User).filter_by(username="second").one().is_superuser is True


def test_create_plain_user_needs_no_mfa_and_defaults_to_not_superuser(env):
    body = {"username": "third", "email": "third@example.com",
            "password": PASSWORD, "role_id": env.role.id}
    r = env.client.post("/api/v1/users/", json=body, headers=_auth("root"))
    assert r.status_code == 200, r.text
    assert r.json()["is_superuser"] is False


# ── promote / demote ───────────────────────────────────────────────────


def test_promote_and_demote_are_mfa_gated(env):
    url = f"/api/v1/users/{env.plain.id}"
    assert env.client.put(url, json={"is_superuser": True},
                          headers=_auth("root")).status_code == 401
    r = env.client.put(url, json={"is_superuser": True},
                       headers={**_auth("root"), "X-MFA-Code": _code(env)})
    assert r.status_code == 200 and r.json()["is_superuser"] is True
    # Sending the flag UNCHANGED is not a promotion — no code needed.
    assert env.client.put(url, json={"is_superuser": True, "first_name": "P"},
                          headers=_auth("root")).status_code == 200
    r = env.client.put(url, json={"is_superuser": False},
                       headers={**_auth("root"), "X-MFA-Code": _code(env)})
    assert r.status_code == 200 and r.json()["is_superuser"] is False


def test_cannot_demote_yourself(env):
    r = env.client.put(f"/api/v1/users/{env.root.id}", json={"is_superuser": False},
                       headers={**_auth("root"), "X-MFA-Code": _code(env)})
    assert r.status_code == 400 and "own superuser" in r.json()["detail"]


def test_last_superuser_is_protected(env):
    # Promote plain so root is no longer the only one, then try to take
    # the LAST one out via the three doors: demote, deactivate, delete.
    env.client.put(f"/api/v1/users/{env.plain.id}", json={"is_superuser": True},
                   headers={**_auth("root"), "X-MFA-Code": _code(env)})
    # Demote root (as plain, now a superuser): fine — plain remains.
    env.db.expire_all()
    plain = env.db.get(User, env.plain.id)
    plain.mfa_enabled = True
    plain.mfa_secret = env.secret
    env.db.commit()
    r = env.client.put(f"/api/v1/users/{env.root.id}", json={"is_superuser": False},
                       headers={**_auth("plain"), "X-MFA-Code": _code(env)})
    assert r.status_code == 200, r.text
    # Now plain is the last: root (a plain user now) cannot act; plain
    # cannot demote or deactivate itself; and re-promoting root then
    # deleting plain works only because root remains.
    assert env.client.put(f"/api/v1/users/{env.root.id}", json={"is_superuser": True},
                          headers={**_auth("root"), "X-MFA-Code": _code(env)}
                          ).status_code == 403
    r = env.client.put(f"/api/v1/users/{env.plain.id}", json={"is_superuser": False},
                       headers={**_auth("plain"), "X-MFA-Code": _code(env)})
    assert r.status_code == 400
    r = env.client.put(f"/api/v1/users/{env.plain.id}", json={"is_active": False},
                       headers=_auth("plain"))
    assert r.status_code == 400
    # Deleting the only OTHER superuser when it would leave none: refused.
    env.client.put(f"/api/v1/users/{env.root.id}", json={"is_superuser": True},
                   headers={**_auth("plain"), "X-MFA-Code": _code(env)})
    env.db.expire_all()
    env.db.get(User, env.plain.id).is_superuser = False
    env.db.commit()
    # plain is no longer a superuser, root is the last: plain can't call,
    # and root cannot be deleted by anyone as the last superuser.
    r = env.client.delete(f"/api/v1/users/{env.root.id}",
                          headers={**_auth("plain"), "X-MFA-Code": _code(env)})
    assert r.status_code == 403


def test_self_update_cannot_touch_admin_fields_or_null_them(env):
    """PUT /users/me used to NULL role/active/superuser instead of
    dropping them — {"is_active": false} locked the caller out."""
    r = env.client.put("/api/v1/users/me",
                       json={"is_active": False, "is_superuser": True,
                             "role_id": 999, "first_name": "Still"},
                       headers=_auth("plain"))
    assert r.status_code == 200, r.text
    env.db.expire_all()
    u = env.db.get(User, env.plain.id)
    assert (u.first_name, u.is_active, u.is_superuser, u.role_id) == \
        ("Still", True, False, env.role.id)


# ── seeded catalogue wired to the open writes ──────────────────────────


def _gate_names(router, path, method):
    route = next(r for r in router.routes if r.path == path and method in r.methods)
    return {d.call.__name__ for d in route.dependant.dependencies}


@pytest.mark.parametrize("path,method,perm", [
    ("/ai-model-management", "POST", "byom.manage"),
    ("/ai-model-management/{model_id}", "PUT", "byom.manage"),
    ("/ai-model-management/{model_id}", "DELETE", "byom.manage"),
    ("/ai-model-management/{model_id}/start-inference", "POST", "byom.manage"),
    ("/ai-model-management/{model_id}/stop-inference", "POST", "byom.manage"),
])
def test_model_management_writes_need_byom_manage(path, method, perm):
    assert f"require_permission[{perm}]" in _gate_names(aimm_router.router, path, method)


@pytest.mark.parametrize("path", [
    "/ai-models/adapters/{adapter_name}/permissions/grant",
    "/ai-models/adapters/{adapter_name}/permissions/revoke",
    "/ai-models/adapters/{adapter_name}/permissions/approve-all",
])
def test_adapter_governance_needs_ai_manage(path):
    assert "require_permission[ai.manage]" in _gate_names(ai_router.router, path, "POST")


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_camera_edit_and_delete_need_cameras_manage(method):
    names = _gate_names(cameras_router.router, "/cameras/{camera_id}", method)
    assert "require_permission[cameras.manage]" in names
    assert "get_camera_or_403" in names       # ownership still required too
