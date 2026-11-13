# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""A failed first-time-setup attempt must not burn the setup token.

Issue #632: with DEFAULT_ADMIN_USERNAME=sparky the setup page submitted
``admin``. The endpoint consumed the token, then found no pending ``admin``
account, and every retry after that failed with "token missing or invalid"
until the server restarted. The token is now put back whenever the request
fails after the token check, and is still single-use once setup succeeds.

Run with:
    cd server && pytest tests/test_first_time_setup_token_retry.py -v
"""
from __future__ import annotations

import os
import secrets
import sys
import types as _types
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_first_time_setup_test.db")
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
from models import Role, User  # noqa: E402
from routers import auth as auth_router  # noqa: E402
from services import first_time_setup_service as setup_svc  # noqa: E402

PASSWORD = "Str0ng!passw0rd"
URL = "/api/v1/auth/first-time-setup"


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


def _setup(client, token, username="sparky", password=PASSWORD):
    return client.post(URL, json={
        "username": username, "password": password, "setup_token": token,
    })


def test_wrong_username_keeps_token(env):
    client, token, _ = env
    r = _setup(client, token, username="admin")
    assert r.status_code == 400
    assert setup_svc.is_armed()
    # The retry with the right name succeeds on the SAME token.
    assert _setup(client, token).status_code == 200


def test_rejected_password_keeps_token(env):
    client, token, session_factory = env
    r = _setup(client, token, password="password123")
    assert r.status_code == 400
    assert setup_svc.is_armed()
    db = session_factory()
    try:
        assert db.query(User).filter_by(username="sparky").one().password_set is False
    finally:
        db.close()
    assert _setup(client, token).status_code == 200


def test_token_is_single_use_after_success(env):
    client, token, _ = env
    assert _setup(client, token).status_code == 200
    assert not setup_svc.is_armed()
    assert _setup(client, token).status_code == 403


def test_wrong_token_is_rejected_and_real_one_survives(env):
    client, token, _ = env
    assert _setup(client, "not-the-token-at-all").status_code == 403
    assert setup_svc.is_armed()
    assert _setup(client, token).status_code == 200


def test_restore_does_not_replace_an_armed_token():
    setup_svc.clear()
    setup_svc.restore("first-token-value")
    setup_svc.restore("second-token-value")
    try:
        assert not setup_svc.verify_and_consume("second-token-value")
        assert setup_svc.verify_and_consume("first-token-value")
    finally:
        setup_svc.clear()


CHECK_URL = "/api/v1/auth/check-setup"


def test_check_setup_reports_the_pending_admin_name(env, monkeypatch):
    # The configured name and the real pending account disagree, as on any
    # Docker install from before compose passed DEFAULT_ADMIN_USERNAME.
    client, _, _ = env
    monkeypatch.setattr(auth_router.settings, "default_admin_username", "admin")
    body = client.post(CHECK_URL).json()
    assert body["setup_required"] is True
    assert body["username"] == "sparky"


def test_check_setup_done_even_if_configured_name_is_missing(env, monkeypatch):
    # Setup finished as "sparky"; .env names someone else. That must not
    # send the UI back to first-time setup for an account that doesn't exist.
    client, token, _ = env
    assert _setup(client, token).status_code == 200
    monkeypatch.setattr(auth_router.settings, "default_admin_username", "admin")
    assert client.post(CHECK_URL).json()["setup_required"] is False
