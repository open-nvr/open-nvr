# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Recovery codes: signing in once without the phone, then replacing it.

- The first authenticator an account sets up comes with 10 codes, shown once.
- A code signs in once, typed loosely (case, dashes, spaces, 0/1/8).
- It also stands in for the app's code in the account's own two-factor
  settings (how a lost phone gets replaced) and is used up there, but never
  for destructive actions.
- A new set replaces the old one; a reset removes them.
"""

from __future__ import annotations

import importlib.util

import pyotp
import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from core.database import Base
from models import AuditLog, UserRecoveryCode
from services import mfa_service
from tests.test_mfa_factors import (  # noqa: F401 - shared fixture and helpers
    PASSWORD,
    SERVER,
    _code,
    _fresh_step,
    _h,
    _row,
    _run,
    env,
)
from utils.mfa_guard import require_mfa_code


def _codes(env, username="alice"):
    return mfa_service.generate_recovery_codes(env.db, _row(env, username))


def _login(env, username, code):
    return env.client.post("/api/v1/auth/login-json",
                           json={"username": username, "password": PASSWORD, "code": code})


def test_first_enrolment_comes_with_ten_codes(env):
    _fresh_step()
    env.client.post(f"/api/v1/users/{env.alice.id}/mfa/reset",
                    headers={**_h(env, "root"), "X-MFA-Code": _code(env.keys["root"])})
    h = {"Authorization": f"Bearer {__import__('core.auth', fromlist=['x']).create_access_token({'sub': 'alice', 'ver': 1})}"}
    secret = env.client.post("/api/v1/auth/mfa/setup", headers=h).json()["secret"]
    _fresh_step()
    r = env.client.post("/api/v1/auth/mfa/verify", headers=h, json={"code": pyotp.TOTP(secret).now()})
    codes = r.json()["recovery_codes"]
    assert len(codes) == 10 and len(set(codes)) == 10
    assert all(len(c) == 19 and c.count("-") == 3 for c in codes)
    assert env.client.get("/api/v1/auth/mfa/factors", headers=h).json()["recovery_codes_remaining"] == 10


def test_a_second_app_does_not_issue_new_codes(env):
    _codes(env)
    _fresh_step()
    r = env.client.post("/api/v1/auth/mfa/factors/totp",
                        headers={**_h(env, "alice"), "X-MFA-Code": _code(env.keys["alice"])},
                        json={"current_password": PASSWORD})
    new = r.json()
    r = env.client.post(f"/api/v1/auth/mfa/factors/{new['factor_id']}/confirm",
                        headers=_h(env, "alice"), json={"code": _code(new["secret"])})
    assert r.status_code == 200 and r.json()["recovery_codes"] is None
    assert mfa_service.recovery_codes_remaining(env.db, _row(env, "alice")) == 10


def test_a_code_signs_in_once_and_is_audited(env):
    codes = _codes(env)
    assert _login(env, "alice", codes[0]).status_code == 200
    assert _login(env, "alice", codes[0]).status_code == 401
    assert mfa_service.recovery_codes_remaining(env.db, _row(env, "alice")) == 9
    audit = env.db.query(AuditLog).filter_by(action="mfa.recovery_code_used").one()
    details = audit.details if isinstance(audit.details, dict) else __import__("json").loads(audit.details)
    assert details["remaining"] == 9


@pytest.mark.parametrize("mangle", [
    lambda c: c.lower(),
    lambda c: c.replace("-", ""),
    lambda c: " ".join(c.split("-")),
    lambda c: c.replace("O", "0").replace("I", "1").replace("B", "8"),
])
def test_codes_are_forgiving_about_how_they_are_typed(env, mangle):
    code = _codes(env)[3]
    assert _login(env, "alice", mangle(code)).status_code == 200


def test_normalize_rejects_what_is_not_a_code():
    assert mfa_service.normalize_recovery_code("123456") is None
    assert mfa_service.normalize_recovery_code("ABCD-EFGH-IJKL-MNO") is None
    assert mfa_service.normalize_recovery_code("ABCD-EFGH-IJKL-MNO9") is None    # 9 isn't base32
    assert mfa_service.normalize_recovery_code("abcd efgh ijkl mnop") == "ABCDEFGHIJKLMNOP"


def test_a_lost_phone_is_replaced_with_a_recovery_code(env):
    """Sign in with a code, add the new app with password + another code,
    remove the lost one."""
    codes = _codes(env)
    assert _login(env, "alice", codes[0]).status_code == 200
    r = env.client.post("/api/v1/auth/mfa/factors/totp",
                        headers={**_h(env, "alice"), "X-MFA-Code": codes[1]},
                        json={"label": "Replacement", "current_password": PASSWORD})
    assert r.status_code == 200, r.text
    new = r.json()
    _fresh_step()
    r = env.client.post(f"/api/v1/auth/mfa/factors/{new['factor_id']}/confirm",
                        headers=_h(env, "alice"), json={"code": _code(new["secret"])})
    assert r.status_code == 200
    lost = env.client.get("/api/v1/auth/mfa/factors", headers=_h(env, "alice")).json()["factors"][0]
    r = env.client.request("DELETE", f"/api/v1/auth/mfa/factors/{lost['id']}",
                           headers={**_h(env, "alice"), "X-MFA-Code": codes[2]},
                           json={"current_password": PASSWORD})
    assert r.status_code == 200, r.text
    assert mfa_service.recovery_codes_remaining(env.db, _row(env, "alice")) == 7


def test_a_wrong_password_does_not_use_up_a_code(env):
    codes = _codes(env)
    r = env.client.post("/api/v1/auth/mfa/factors/totp",
                        headers={**_h(env, "alice"), "X-MFA-Code": codes[0]},
                        json={"current_password": "nope-" + PASSWORD})
    assert r.status_code == 401
    assert mfa_service.recovery_codes_remaining(env.db, _row(env, "alice")) == 10


def test_a_recovery_code_never_confirms_a_destructive_action(env):
    codes = mfa_service.generate_recovery_codes(env.db, _row(env, "root"))
    with pytest.raises(HTTPException) as exc:
        require_mfa_code(_row(env, "root"), codes[0])
    assert exc.value.status_code == 401
    assert mfa_service.recovery_codes_remaining(env.db, _row(env, "root")) == 10


def test_a_new_set_replaces_the_old_one(env):
    old = _codes(env)
    _fresh_step()
    r = env.client.post("/api/v1/auth/mfa/recovery-codes",
                        headers={**_h(env, "alice"), "X-MFA-Code": _code(env.keys["alice"])},
                        json={"current_password": PASSWORD})
    assert r.status_code == 200 and len(r.json()["recovery_codes"]) == 10
    assert _login(env, "alice", old[0]).status_code == 401
    assert _login(env, "alice", r.json()["recovery_codes"][0]).status_code == 200


def test_new_codes_need_password_and_code(env):
    r = env.client.post("/api/v1/auth/mfa/recovery-codes", headers=_h(env, "alice"),
                        json={"current_password": PASSWORD})
    assert r.status_code == 401


def test_a_reset_removes_the_codes(env):
    _codes(env)
    _fresh_step()
    r = env.client.post(f"/api/v1/users/{env.alice.id}/mfa/reset",
                        headers={**_h(env, "root"), "X-MFA-Code": _code(env.keys["root"])})
    assert r.status_code == 200
    assert env.db.query(UserRecoveryCode).filter_by(user_id=env.alice.id).count() == 0


@pytest.mark.parametrize("table_already_there", [False, True])
def test_migration_is_idempotent(table_already_there):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    tables = [t for n, t in Base.metadata.tables.items()
              if table_already_there or n != "user_recovery_codes"]
    Base.metadata.create_all(eng, tables=tables)
    path = SERVER / "migrations" / "versions" / "f8b4d0a2c6e3_user_recovery_codes.py"
    spec = importlib.util.spec_from_file_location("m_f8b4", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _run(eng, mod.upgrade)
    _run(eng, mod.upgrade)
    with eng.connect() as c:
        assert sa.inspect(c).has_table("user_recovery_codes")
    _run(eng, mod.downgrade)
    with eng.connect() as c:
        assert not sa.inspect(c).has_table("user_recovery_codes")
