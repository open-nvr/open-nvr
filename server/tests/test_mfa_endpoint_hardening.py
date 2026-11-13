# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""A session alone must not change an account's second factor.

- /auth/mfa/setup re-keys only an account that has not enrolled yet.
- /auth/mfa/disable is retired (410).
- A wrong X-MFA-Code on a step-up action counts toward the lockout, and a
  locked account is refused there too.
"""

from __future__ import annotations

import pyotp
import pytest
from fastapi import HTTPException

from models import User
from tests.test_login_mfa_wall import PASSWORD, _login_json, env  # noqa: F401 - shared fixture
from utils.mfa_guard import require_mfa_code


def _token(env, username, code=None):
    resp = _login_json(env.client, username, PASSWORD, code=code)
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _user(env, username) -> User:
    env.db.expire_all()
    return env.db.query(User).filter(User.username == username).first()


def test_setup_refused_once_enrolled(env):
    headers = _token(env, "enrolled", pyotp.TOTP(env.totp_secret).now())
    resp = env.client.post("/api/v1/auth/mfa/setup", headers=headers)
    assert resp.status_code == 409, resp.text
    user = _user(env, "enrolled")
    assert user.mfa_enabled is True
    assert user.mfa_secret == env.totp_secret


def test_setup_still_works_before_enrolment(env):
    headers = _token(env, "unenrolled")
    resp = env.client.post("/api/v1/auth/mfa/setup", headers=headers)
    assert resp.status_code == 200, resp.text
    secret = resp.json()["secret"]
    resp = env.client.post(
        "/api/v1/auth/mfa/verify", headers=headers,
        json={"code": pyotp.TOTP(secret).now()},
    )
    assert resp.status_code == 200, resp.text
    assert _user(env, "unenrolled").mfa_enabled is True


def test_disable_is_gone(env):
    headers = _token(env, "enrolled", pyotp.TOTP(env.totp_secret).now())
    resp = env.client.post("/api/v1/auth/mfa/disable", headers=headers)
    assert resp.status_code == 410, resp.text
    user = _user(env, "enrolled")
    assert user.mfa_enabled is True
    assert user.mfa_secret == env.totp_secret


def _wrong(code: str) -> str:
    return "000000" if code != "000000" else "111111"


def test_step_up_counts_wrong_codes_and_locks(env):
    user = _user(env, "enrolled")
    bad = _wrong(pyotp.TOTP(env.totp_secret).now())
    for _ in range(4):
        with pytest.raises(HTTPException) as exc:
            require_mfa_code(user, bad)
        assert exc.value.status_code == 401
    assert _user(env, "enrolled").failed_login_attempts == 4
    with pytest.raises(HTTPException) as exc:
        require_mfa_code(user, bad)
    assert exc.value.status_code == 423
    # Locked now: even the right code is refused until the lock runs out.
    with pytest.raises(HTTPException) as exc:
        require_mfa_code(_user(env, "enrolled"), pyotp.TOTP(env.totp_secret).now())
    assert exc.value.status_code == 423


def test_step_up_missing_code_is_not_a_guess(env):
    user = _user(env, "enrolled")
    for missing in (None, "", "   "):
        with pytest.raises(HTTPException) as exc:
            require_mfa_code(user, missing)
        assert exc.value.status_code == 401
    assert _user(env, "enrolled").failed_login_attempts == 0


def test_step_up_right_code_clears_the_count(env):
    user = _user(env, "enrolled")
    with pytest.raises(HTTPException):
        require_mfa_code(user, _wrong(pyotp.TOTP(env.totp_secret).now()))
    assert _user(env, "enrolled").failed_login_attempts == 1
    require_mfa_code(_user(env, "enrolled"), pyotp.TOTP(env.totp_secret).now())
    assert _user(env, "enrolled").failed_login_attempts == 0
