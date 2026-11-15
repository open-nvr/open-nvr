# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""First-time setup finishes only once a code from the authenticator is seen.

Step 1 (``/auth/first-time-setup``) used to confirm the authenticator on the
strength of the QR code having been *shown*. An operator who closed the tab,
or whose phone never scanned, was locked out of the account they had just
created. Now step 1 leaves the authenticator pending and returns a setup
session; step 2 (``/auth/first-time-setup/verify``) takes a code from the
scanned secret, confirms the authenticator, issues the recovery codes and
signs the operator in.

Run with:
    cd server && pytest tests/test_first_time_setup_verify.py -v
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
os.environ.setdefault("DATABASE_URL", "sqlite:///./_first_time_setup_verify_test.db")
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

from core.auth import get_password_hash  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import Role, User, UserMfaFactor, UserRecoveryCode  # noqa: E402
from routers import auth as auth_router  # noqa: E402
from services import first_time_setup_service as setup_svc  # noqa: E402
from services import mfa_service  # noqa: E402

PASSWORD = "Str0ng!passw0rd"
SETUP = "/api/v1/auth/first-time-setup"
VERIFY = "/api/v1/auth/first-time-setup/verify"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(auth_router, "auth_logger", _L(), raising=False)
    monkeypatch.setattr(setup_svc, "auth_logger", _L(), raising=False)

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    session_factory = sessionmaker(bind=eng)

    db = session_factory()
    role = Role(name="admin", description="admin")
    db.add(role)
    db.flush()
    # What init_db creates for DEFAULT_ADMIN_USERNAME=sparky.
    db.add(User(
        username="sparky", email="sparky@opennvr.local",
        hashed_password=get_password_hash(secrets.token_urlsafe(32)),
        is_active=True, is_superuser=True, password_set=False,
        mfa_enabled=True, role_id=role.id,
    ))
    db.commit()

    setup_svc.clear()
    token = setup_svc.maybe_arm(db)
    db.close()
    assert token

    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api/v1")

    def _get_db():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db
    yield TestClient(app), token, session_factory
    setup_svc.clear()


def _claim(client, token):
    r = client.post(SETUP, json={
        "username": "sparky", "password": PASSWORD, "setup_token": token,
    })
    assert r.status_code == 200, r.text
    return r.json()


def _wrong_code(secret: str) -> str:
    totp = pyotp.TOTP(secret)
    valid = {totp.at(totp.timecode(__import__("datetime").datetime.now()) * 30 + d)
             for d in (-30, 0, 30)}
    for candidate in ("000000", "111111", "222222"):
        if candidate not in valid:
            return candidate
    raise AssertionError("no wrong code available")


def _factor(session_factory) -> UserMfaFactor:
    db = session_factory()
    try:
        user = db.query(User).filter_by(username="sparky").one()
        rows = db.query(UserMfaFactor).filter_by(user_id=user.id).all()
        assert len(rows) == 1
        db.refresh(rows[0])
        return rows[0]
    finally:
        db.close()


def test_claiming_the_account_leaves_the_authenticator_pending(env):
    client, token, session_factory = env
    body = _claim(client, token)
    assert body["mfa_secret"] and body["mfa_qr_uri"]
    assert body["setup_session"]

    f = _factor(session_factory)
    assert f.confirmed_at is None and f.enabled is False
    db = session_factory()
    try:
        user = db.query(User).filter_by(username="sparky").one()
        assert user.password_set is True
        assert not mfa_service.has_active_factor(db, user)
        # The pending enrolment is the one the login wall picks up again.
        _f, secret = mfa_service.begin_totp(db, user)
        assert secret == body["mfa_secret"]
    finally:
        db.close()


def test_a_code_from_the_secret_finishes_setup_and_signs_in(env):
    client, token, session_factory = env
    body = _claim(client, token)
    secret, session = body["mfa_secret"], body["setup_session"]

    wrong = client.post(VERIFY, json={"setup_session": session, "code": _wrong_code(secret)})
    assert wrong.status_code == 400
    assert _factor(session_factory).confirmed_at is None

    r = client.post(VERIFY, json={"setup_session": session, "code": pyotp.TOTP(secret).now()})
    assert r.status_code == 200, r.text
    done = r.json()
    assert len(done["recovery_codes"]) == mfa_service.RECOVERY_CODE_COUNT
    assert done["access_token"] and done["refresh_token"]

    f = _factor(session_factory)
    assert f.confirmed_at is not None and f.enabled is True
    db = session_factory()
    try:
        user = db.query(User).filter_by(username="sparky").one()
        assert user.mfa_enabled is True
        assert db.query(UserRecoveryCode).filter_by(user_id=user.id).count() == \
            mfa_service.RECOVERY_CODE_COUNT
    finally:
        db.close()

    # The session is spent: the recovery codes cannot be fetched again.
    again = client.post(VERIFY, json={"setup_session": session, "code": pyotp.TOTP(secret).now()})
    assert again.status_code == 403


def test_unknown_session_is_refused(env):
    client, token, _ = env
    body = _claim(client, token)
    r = client.post(VERIFY, json={
        "setup_session": secrets.token_urlsafe(32),
        "code": pyotp.TOTP(body["mfa_secret"]).now(),
    })
    assert r.status_code == 403


def test_too_many_wrong_codes_end_the_session_but_not_the_enrolment(env):
    client, token, session_factory = env
    body = _claim(client, token)
    secret, session = body["mfa_secret"], body["setup_session"]
    wrong = _wrong_code(secret)

    statuses = [client.post(VERIFY, json={"setup_session": session, "code": wrong}).status_code
                for _ in range(setup_svc._ENROLMENT_MAX_FAILURES)]
    assert statuses[:-1] == [400] * (len(statuses) - 1)
    assert statuses[-1] == 403

    # Even the right code no longer works through this session...
    r = client.post(VERIFY, json={"setup_session": session, "code": pyotp.TOTP(secret).now()})
    assert r.status_code == 403
    # ...but the account is not stranded: the password login's enrolment wall
    # continues the same pending authenticator.
    db = session_factory()
    try:
        user = db.query(User).filter_by(username="sparky").one()
        assert user.password_set is True
        _f, pending = mfa_service.begin_totp(db, user)
        assert pending == secret
        assert mfa_service.confirm_totp(db, user, pyotp.TOTP(secret).now()) is not None
    finally:
        db.close()


def test_setup_token_stays_single_use(env):
    client, token, _ = env
    _claim(client, token)
    assert not setup_svc.is_armed()
    r = client.post(SETUP, json={
        "username": "sparky", "password": PASSWORD, "setup_token": token,
    })
    assert r.status_code == 403
