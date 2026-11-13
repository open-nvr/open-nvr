# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Administrator "Reset 2FA", and the server-side MFA wall it relies on.

- POST /users/{id}/mfa/reset: superuser only, the caller's own code, never
  on yourself, refused for a single sign-on account; ends the user's
  sessions, is audited, and e-mails the user when mail is configured.
- Until an account enrols again, its session reaches only the enrolment
  routes (core.auth._refuse_until_enrolled), whatever token it holds.
- A superuser linked to single sign-on before linking was restricted stays
  refused by single sign-on after its two-factor sign-in is reset.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_mfa_admin_reset_test.db")
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
from models import AuditLog, Role, User  # noqa: E402
from routers import auth as auth_router  # noqa: E402
from routers import users as users_router  # noqa: E402
from services import mfa_service, oidc  # noqa: E402

PASSWORD = "Str0ng!passw0rd"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(core_auth, "auth_logger", _L(), raising=False)
    monkeypatch.setattr(auth_router, "auth_logger", _L(), raising=False)
    monkeypatch.setattr(users_router, "main_logger", _L(), raising=False)

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    session_factory = sessionmaker(bind=eng)

    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api/v1")
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
    secrets_by_user = {}

    def _user(username, *, superuser=False, enrolled=True, provider=None):
        u = User(username=username, email=f"{username}@example.com", hashed_password=hashed,
                 is_active=True, is_superuser=superuser, password_set=True,
                 mfa_enabled=enrolled, role_id=role.id, auth_provider=provider)
        if enrolled:
            secrets_by_user[username] = pyotp.random_base32()
            u.mfa_secret = secrets_by_user[username]
        db.add(u)
        return u

    root = _user("root", superuser=True)
    alice = _user("alice")
    sso = _user("sso", enrolled=False, provider="oidc")
    db.commit()
    try:
        yield _types.SimpleNamespace(client=TestClient(app), db=db, role=role,
                                     root=root, alice=alice, sso=sso,
                                     secrets=secrets_by_user, new_user=_user)
    finally:
        db.close()


def _code(env, username):
    return pyotp.TOTP(env.secrets[username]).now()


def _session(env, username):
    """A real sign-in: both tokens, as the SPA holds them."""
    body = {"username": username, "password": PASSWORD}
    if username in env.secrets:
        body["code"] = _code(env, username)
    r = env.client.post("/api/v1/auth/login-json", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _bearer(tokens):
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _reset(env, admin_tokens, target, code):
    headers = {**_bearer(admin_tokens)}
    if code is not None:
        headers["X-MFA-Code"] = code
    return env.client.post(f"/api/v1/users/{target.id}/mfa/reset", headers=headers)


def _row(env, username) -> User:
    env.db.expire_all()
    return env.db.query(User).filter_by(username=username).one()


# ── the reset itself ─────────────────────────────────────────────────

def test_reset_removes_the_factor_and_ends_sessions(env):
    admin = _session(env, "root")
    victim = _session(env, "alice")
    assert env.client.get("/api/v1/users/me", headers=_bearer(victim)).status_code == 200

    r = _reset(env, admin, env.alice, _code(env, "root"))
    assert r.status_code == 200, r.text
    row = _row(env, "alice")
    assert row.mfa_enabled is False and row.mfa_secret is None
    # Her open session and her refresh token are both dead.
    assert env.client.get("/api/v1/users/me", headers=_bearer(victim)).status_code == 401
    r = env.client.post("/api/v1/auth/refresh", json={"refresh_token": victim["refresh_token"]})
    assert r.status_code == 401
    audit = env.db.query(AuditLog).filter_by(action="mfa.admin_reset").one()
    assert audit.user_id == env.root.id and str(audit.entity_id) == str(env.alice.id)


def test_reset_needs_the_admins_code(env):
    admin = _session(env, "root")
    assert _reset(env, admin, env.alice, None).status_code == 401
    assert _reset(env, admin, env.alice, "000000" if _code(env, "root") != "000000" else "111111").status_code == 401
    assert _row(env, "alice").mfa_enabled is True


def test_reset_is_superuser_only(env):
    bob = env.new_user("bob")
    env.db.commit()
    r = _reset(env, _session(env, "bob"), env.alice, _code(env, "bob"))
    assert r.status_code == 403
    assert _row(env, "alice").mfa_enabled is True


def test_reset_refuses_your_own_account(env):
    admin = _session(env, "root")
    r = _reset(env, admin, env.root, _code(env, "root"))
    assert r.status_code == 400
    assert _row(env, "root").mfa_enabled is True


def test_reset_refuses_a_single_sign_on_account(env):
    admin = _session(env, "root")
    r = _reset(env, admin, env.sso, _code(env, "root"))
    assert r.status_code == 400 and "single sign-on" in r.json()["detail"]


def test_reset_emails_the_user_when_mail_is_configured(env, monkeypatch):
    sent = []

    async def _send(db, to, subject, text):
        sent.append((to, subject, text))

    monkeypatch.setattr(users_router.mailer, "load", lambda db: {})
    monkeypatch.setattr(users_router.mailer, "is_configured", lambda cfg: True)
    monkeypatch.setattr(users_router.mailer, "send", _send)
    r = _reset(env, _session(env, "root"), env.alice, _code(env, "root"))
    assert r.status_code == 200 and r.json()["notified"] is True
    assert sent and sent[0][0] == "alice@example.com" and "root" in sent[0][2]


def test_a_failing_mail_does_not_undo_the_reset(env, monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(users_router.mailer, "load", lambda db: {})
    monkeypatch.setattr(users_router.mailer, "is_configured", lambda cfg: True)
    monkeypatch.setattr(users_router.mailer, "send", _boom)
    r = _reset(env, _session(env, "root"), env.alice, _code(env, "root"))
    assert r.status_code == 200 and r.json()["notified"] is False
    assert _row(env, "alice").mfa_enabled is False


# ── the server-side wall ─────────────────────────────────────────────

def test_after_reset_a_password_sign_in_reaches_only_enrolment(env):
    _reset(env, _session(env, "root"), env.alice, _code(env, "root"))
    env.secrets.pop("alice")
    tokens = _session(env, "alice")                         # password only now
    h = _bearer(tokens)
    assert env.client.get("/api/v1/auth/me", headers=h).status_code == 200
    assert env.client.get("/api/v1/users/me/permissions", headers=h).status_code == 200
    r = env.client.get("/api/v1/users/", headers=h)
    assert r.status_code == 403 and r.json()["detail"]["error"] == "mfa_enrollment_required"
    assert env.client.put("/api/v1/users/me", json={"first_name": "x"}, headers=h).status_code == 403

    # A refreshed token is no way around it.
    r = env.client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 200
    assert env.client.get("/api/v1/users/", headers=_bearer(r.json())).status_code == 403


def test_enrolling_again_opens_the_same_session(env):
    _reset(env, _session(env, "root"), env.alice, _code(env, "root"))
    env.secrets.pop("alice")
    h = _bearer(_session(env, "alice"))
    secret = env.client.post("/api/v1/auth/mfa/setup", headers=h).json()["secret"]
    r = env.client.post("/api/v1/auth/mfa/verify", headers=h, json={"code": pyotp.TOTP(secret).now()})
    assert r.status_code == 200, r.text
    # Same token, no new sign-in: the wall reads the account, not the token.
    assert env.client.put("/api/v1/users/me", json={"first_name": "Alice"}, headers=h).status_code == 200


def test_single_sign_on_accounts_are_not_walled(env):
    assert mfa_service.is_sso_account(env.sso)
    assert core_auth.mfa_enrolment_pending(env.sso) is False
    assert core_auth.mfa_enrolment_pending(env.alice) is False


# ── legacy single sign-on link ───────────────────────────────────────

def test_reset_makes_a_legacy_linked_account_local(env):
    """Linked by e-mail before linking was restricted: marked single sign-on
    yet signing in with a password and TOTP. A reset must not leave it marked
    single sign-on, or it would never be asked to enrol again."""
    env.alice.auth_provider, env.alice.oidc_issuer, env.alice.oidc_subject = "oidc", "https://idp", "u-a"
    env.db.commit()
    r = _reset(env, _session(env, "root"), env.alice, _code(env, "root"))
    assert r.status_code == 200, r.text
    row = _row(env, "alice")
    assert row.auth_provider is None and core_auth.mfa_enrolment_pending(row)


def test_legacy_linked_superuser_stays_refused_by_sso_after_reset(env):
    second = env.new_user("second", superuser=True)
    second.auth_provider, second.oidc_issuer, second.oidc_subject = "oidc", "https://idp", "u-old"
    env.db.commit()
    assert _reset(env, _session(env, "root"), second, _code(env, "root")).status_code == 200
    with pytest.raises(ValueError, match="administrator"):
        oidc.resolve_user(env.db, {"sub": "u-old", "email": "second@example.com"},
                          {"issuer": "https://idp", "auto_create": True})
