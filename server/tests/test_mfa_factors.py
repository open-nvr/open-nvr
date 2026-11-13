# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Several authenticators per account (user_mfa_factors).

- The user moves to a new app on their own: add it (password + current
  code), confirm it, sign in with it, remove the old one. The last working
  one can't be removed or turned off.
- A sign-in code works once; a step-up code may confirm several actions.
- users.encrypted_mfa_secret keeps the oldest working factor (rollback).
- A secret only in that column (older core) is adopted, not lost.
- An administrator sees, turns off and removes another user's factors;
  doing that to the last one is a reset.
- The migration carries every enrolled secret over, idempotently.
"""

from __future__ import annotations

import importlib.util
import os
import secrets
import sys
import time
import types as _types
from pathlib import Path

import pyotp
import pytest
from cryptography.fernet import Fernet

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_mfa_factors_test.db")
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

import sqlalchemy as sa  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import core.auth as core_auth  # noqa: E402
from core.auth import get_password_hash  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import AuditLog, Role, User, UserMfaFactor  # noqa: E402
from routers import auth as auth_router  # noqa: E402
from routers import mfa as mfa_router  # noqa: E402
from routers import users as users_router  # noqa: E402
from services import mfa_service  # noqa: E402
from services.user_service import UserService  # noqa: E402
from utils.mfa_guard import require_mfa_code  # noqa: E402

PASSWORD = "Str0ng!passw0rd"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(core_auth, "auth_logger", _L(), raising=False)
    monkeypatch.setattr(auth_router, "auth_logger", _L(), raising=False)
    monkeypatch.setattr(users_router, "main_logger", _L(), raising=False)
    monkeypatch.setattr(mfa_router, "main_logger", _L(), raising=False)

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    session_factory = sessionmaker(bind=eng)

    app = FastAPI()
    for r in (auth_router.router, mfa_router.router, users_router.router):
        app.include_router(r, prefix="/api/v1")

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
    keys = {}

    def _user(username, *, superuser=False):
        u = User(username=username, email=f"{username}@example.com",
                 hashed_password=get_password_hash(PASSWORD), is_active=True,
                 is_superuser=superuser, password_set=True, mfa_enabled=True, role_id=role.id)
        keys[username] = pyotp.random_base32()
        u.mfa_secret = keys[username]          # legacy column: adopted on first use
        db.add(u)
        db.commit()
        return u

    root = _user("root", superuser=True)
    alice = _user("alice")
    try:
        yield _types.SimpleNamespace(client=TestClient(app), db=db, root=root, alice=alice,
                                     keys=keys, new_user=_user)
    finally:
        db.close()


def _fresh_step():
    """Codes minted late in a step may verify in the next; start early in one."""
    if 30 - int(time.time()) % 30 < 4:
        time.sleep(5)


def _next_step():
    time.sleep(30 - int(time.time()) % 30 + 1)


def _code(secret):
    return pyotp.TOTP(secret).now()


def _signin(env, username, secret=None):
    secret = secret or env.keys[username]
    r = env.client.post("/api/v1/auth/login-json",
                        json={"username": username, "password": PASSWORD, "code": _code(secret)})
    return r


def _h(env, username):
    """A session token without spending a sign-in code (keeps replay checks out of the way)."""
    return {"Authorization": f"Bearer {core_auth.create_access_token({'sub': username, 'ver': 0})}"}


def _row(env, username) -> User:
    env.db.expire_all()
    return env.db.query(User).filter_by(username=username).one()


def _add_app(env, username, label="New phone"):
    """Self-service: begin (password + current code) and confirm a second app."""
    _fresh_step()
    r = env.client.post("/api/v1/auth/mfa/factors/totp",
                        headers={**_h(env, username), "X-MFA-Code": _code(env.keys[username])},
                        json={"label": label, "current_password": PASSWORD})
    assert r.status_code == 200, r.text
    new = r.json()
    r = env.client.post(f"/api/v1/auth/mfa/factors/{new['factor_id']}/confirm",
                        headers=_h(env, username), json={"code": _code(new["secret"])})
    assert r.status_code == 200, r.text
    return new


def _factors(env, username):
    r = env.client.get("/api/v1/auth/mfa/factors", headers=_h(env, username))
    assert r.status_code == 200, r.text
    return r.json()["factors"]


# ── moving to a new authenticator app without an administrator ────────

def test_legacy_secret_is_adopted_as_the_first_factor(env):
    fs = _factors(env, "alice")
    assert len(fs) == 1 and fs[0]["label"] == "Authenticator" and fs[0]["confirmed"]
    assert "secret" not in fs[0]
    assert env.db.query(UserMfaFactor).filter_by(user_id=env.alice.id).count() == 1


def test_user_switches_apps_end_to_end(env):
    old = env.keys["alice"]
    new = _add_app(env, "alice")
    fs = _factors(env, "alice")
    assert [f["label"] for f in fs] == ["Authenticator", "New phone"]
    assert _row(env, "alice").mfa_factor_count == 2

    # The code that confirmed the new app is spent; it can't sign in too.
    assert _signin(env, "alice", new["secret"]).status_code == 401
    # Either app signs in.
    _next_step()
    assert _signin(env, "alice", new["secret"]).status_code == 200
    _next_step()
    assert _signin(env, "alice", old).status_code == 200

    # Remove the old one: password + a code from an app still in use.
    _fresh_step()
    r = env.client.request("DELETE", f"/api/v1/auth/mfa/factors/{fs[0]['id']}",
                           headers={**_h(env, "alice"), "X-MFA-Code": _code(new["secret"])},
                           json={"current_password": PASSWORD})
    assert r.status_code == 200, r.text
    _next_step()
    assert _signin(env, "alice", old).status_code == 401
    assert _signin(env, "alice", new["secret"]).status_code == 200
    actions = {a.action for a in env.db.query(AuditLog).all()}
    assert {"mfa.factor_added", "mfa.factor_removed"} <= actions


def test_adding_an_app_needs_password_and_a_current_code(env):
    url, h = "/api/v1/auth/mfa/factors/totp", _h(env, "alice")
    _fresh_step()
    assert env.client.post(url, headers=h, json={"current_password": PASSWORD}).status_code == 401
    assert env.client.post(url, headers={**h, "X-MFA-Code": _code(env.keys["alice"])},
                           json={}).status_code == 401
    r = env.client.post(url, headers={**h, "X-MFA-Code": _code(env.keys["alice"])},
                        json={"current_password": "wrong-" + PASSWORD})
    assert r.status_code == 401
    assert _row(env, "alice").failed_login_attempts == 1          # a wrong password is counted


def test_begin_is_idempotent_until_confirmed(env):
    h = {**_h(env, "alice"), "X-MFA-Code": None}
    _fresh_step()
    first = env.client.post("/api/v1/auth/mfa/factors/totp",
                            headers={**_h(env, "alice"), "X-MFA-Code": _code(env.keys["alice"])},
                            json={"current_password": PASSWORD}).json()
    again = env.client.post("/api/v1/auth/mfa/factors/totp",
                            headers={**_h(env, "alice"), "X-MFA-Code": _code(env.keys["alice"])},
                            json={"current_password": PASSWORD}).json()
    assert first["secret"] == again["secret"] and first["factor_id"] == again["factor_id"]
    del h
    # Unconfirmed: it signs nothing in and doesn't count.
    assert _row(env, "alice").mfa_factor_count == 1
    r = env.client.post(f"/api/v1/auth/mfa/factors/{first['factor_id']}/confirm",
                        headers=_h(env, "alice"), json={"code": "000000"})
    assert r.status_code == 400


def test_the_last_working_app_cannot_be_removed_or_turned_off(env):
    fid = _factors(env, "alice")[0]["id"]
    h = {**_h(env, "alice")}
    _fresh_step()
    r = env.client.request("DELETE", f"/api/v1/auth/mfa/factors/{fid}",
                           headers={**h, "X-MFA-Code": _code(env.keys["alice"])},
                           json={"current_password": PASSWORD})
    assert r.status_code == 400 and "only working authenticator" in r.json()["detail"]
    r = env.client.patch(f"/api/v1/auth/mfa/factors/{fid}",
                         headers={**h, "X-MFA-Code": _code(env.keys["alice"])},
                         json={"enabled": False, "current_password": PASSWORD})
    assert r.status_code == 400
    assert _row(env, "alice").mfa_enabled is True


def test_a_disabled_app_no_longer_signs_in(env):
    old = env.keys["alice"]
    new = _add_app(env, "alice")
    fid = _factors(env, "alice")[0]["id"]
    _fresh_step()
    r = env.client.patch(f"/api/v1/auth/mfa/factors/{fid}",
                         headers={**_h(env, "alice"), "X-MFA-Code": _code(new["secret"])},
                         json={"enabled": False, "label": "Old phone", "current_password": PASSWORD})
    assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["label"] == "Old phone"
    _next_step()
    assert _signin(env, "alice", old).status_code == 401


# ── codes ─────────────────────────────────────────────────────────────

def test_a_sign_in_code_works_once(env):
    _fresh_step()
    assert _signin(env, "alice").status_code == 200
    assert _signin(env, "alice").status_code == 401


def test_a_step_up_code_may_confirm_two_actions(env):
    _fresh_step()
    code = _code(env.keys["root"])
    require_mfa_code(_row(env, "root"), code)
    require_mfa_code(_row(env, "root"), code)


# ── the legacy column (rollback) ──────────────────────────────────────

def test_legacy_column_follows_the_oldest_working_factor(env):
    from utils.encryption import decrypt_value

    old = env.keys["alice"]
    new = _add_app(env, "alice")
    assert decrypt_value(_row(env, "alice").encrypted_mfa_secret) == old
    fid = _factors(env, "alice")[0]["id"]
    _fresh_step()
    env.client.request("DELETE", f"/api/v1/auth/mfa/factors/{fid}",
                       headers={**_h(env, "alice"), "X-MFA-Code": _code(new["secret"])},
                       json={"current_password": PASSWORD})
    assert decrypt_value(_row(env, "alice").encrypted_mfa_secret) == new["secret"]


# ── administrator ─────────────────────────────────────────────────────

def _admin(env, method, path, **kw):
    _fresh_step()
    return env.client.request(method, f"/api/v1/users/{env.alice.id}/mfa{path}",
                              headers={**_h(env, "root"), "X-MFA-Code": _code(env.keys["root"])}, **kw)


def test_admin_lists_turns_off_and_removes_one_factor(env):
    new = _add_app(env, "alice")
    r = env.client.get(f"/api/v1/users/{env.alice.id}/mfa/factors", headers=_h(env, "root"))
    assert r.status_code == 200 and len(r.json()["factors"]) == 2
    assert all("secret" not in f for f in r.json()["factors"])
    first = r.json()["factors"][0]["id"]

    r = _admin(env, "PATCH", f"/factors/{first}", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert _row(env, "alice").token_version == 0            # not a reset: one app still works
    _next_step()
    assert _signin(env, "alice", new["secret"]).status_code == 200

    r = _admin(env, "DELETE", f"/factors/{first}")
    assert r.status_code == 200 and len(r.json()["factors"]) == 1


def test_admin_turning_off_the_last_factor_is_a_reset(env):
    fid = _factors(env, "alice")[0]["id"]
    r = _admin(env, "PATCH", f"/factors/{fid}", json={"enabled": False})
    assert r.status_code == 200, r.text
    row = _row(env, "alice")
    assert row.mfa_enabled is False and row.token_version == 1 and row.encrypted_mfa_secret is None
    assert env.db.query(UserMfaFactor).filter_by(user_id=env.alice.id).count() == 0
    assert env.db.query(AuditLog).filter_by(action="mfa.admin_reset").count() == 1


def test_admin_factor_routes_need_the_admins_code_and_not_self(env):
    fid = _factors(env, "alice")[0]["id"]
    r = env.client.patch(f"/api/v1/users/{env.alice.id}/mfa/factors/{fid}",
                         headers=_h(env, "root"), json={"enabled": False})
    assert r.status_code == 401
    _fresh_step()
    r = env.client.request("DELETE", f"/api/v1/users/{env.root.id}/mfa/factors/1",
                           headers={**_h(env, "root"), "X-MFA-Code": _code(env.keys["root"])})
    assert r.status_code == 400


def test_reset_removes_every_factor(env):
    _add_app(env, "alice")
    r = _admin(env, "POST", "/reset")
    assert r.status_code == 200, r.text
    assert env.db.query(UserMfaFactor).filter_by(user_id=env.alice.id).count() == 0
    assert _row(env, "alice").mfa_factor_count == 0


def test_a_user_with_factors_can_still_be_deleted_permanently(env):
    _add_app(env, "alice")
    env.alice.is_active = False
    env.db.commit()
    assert UserService.permanently_delete_user(env.db, env.alice.id) == []
    assert env.db.query(UserMfaFactor).filter_by(user_id=env.alice.id).count() == 0


# ── enrolment wall ────────────────────────────────────────────────────

def test_enrolment_screen_reload_keeps_the_same_qr(env):
    _admin(env, "POST", "/reset")
    env.keys.pop("alice")
    h = {"Authorization": f"Bearer {core_auth.create_access_token({'sub': 'alice', 'ver': 1})}"}
    a = env.client.post("/api/v1/auth/mfa/setup", headers=h).json()["secret"]
    b = env.client.post("/api/v1/auth/mfa/setup", headers=h).json()["secret"]
    assert a == b
    assert env.client.get("/api/v1/auth/mfa/factors", headers=h).status_code == 200
    _fresh_step()
    assert env.client.post("/api/v1/auth/mfa/verify", headers=h, json={"code": _code(a)}).status_code == 200
    assert env.client.post("/api/v1/auth/mfa/setup", headers=h).status_code == 409


# ── the migration ─────────────────────────────────────────────────────

def _migration():
    path = SERVER / "migrations" / "versions" / "e7a3c9f1b5d2_user_mfa_factors.py"
    spec = importlib.util.spec_from_file_location("m_e7a3c9f1b5d2", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(eng, fn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with eng.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


@pytest.mark.parametrize("table_already_there", [False, True])
def test_migration_carries_enrolled_secrets_over(table_already_there):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    tables = [t for n, t in Base.metadata.tables.items()
              if table_already_there or n != "user_mfa_factors"]
    Base.metadata.create_all(eng, tables=tables)        # create_all runs before Alembic
    with eng.begin() as c:
        c.execute(sa.text("INSERT INTO roles (id, name) VALUES (1, 'r')"))
        for i, (name, enabled, secret) in enumerate(
                [("enrolled", True, "ct-1"), ("half", False, "ct-2"), ("none", False, None)], 1):
            c.execute(sa.text(
                "INSERT INTO users (id, username, email, hashed_password, role_id, mfa_enabled, "
                "encrypted_mfa_secret, failed_login_attempts, token_version) "
                "VALUES (:i, :n, :e, 'h', 1, :m, :s, 0, 0)"),
                {"i": i, "n": name, "e": f"{name}@x", "m": enabled, "s": secret})
    mod = _migration()
    _run(eng, mod.upgrade)
    _run(eng, mod.upgrade)                               # a second run adds nothing
    with eng.connect() as c:
        rows = c.execute(sa.text(
            "SELECT user_id, label, encrypted_secret, enabled, confirmed_at IS NOT NULL "
            "FROM user_mfa_factors")).all()
    assert [tuple(r) for r in rows] == [(1, "Authenticator", "ct-1", 1, 1)]

    _run(eng, mod.downgrade)
    with eng.connect() as c:
        assert not sa.inspect(c).has_table("user_mfa_factors")
        back = dict(c.execute(sa.text("SELECT username, encrypted_mfa_secret FROM users")).all())
    assert back["enrolled"] == "ct-1"
