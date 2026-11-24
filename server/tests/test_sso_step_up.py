# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A single sign-on administrator confirms sensitive actions by signing in at
the identity provider again (issue #998).

Such an account has no authenticator app and may not add one, yet resetting
a user's 2FA or password, deleting users and purging cameras all ask for the
caller's MFA code. The re-sign-in (``/auth/oidc/start?reauth=<id>``, opened
in a pop-up) yields a short-lived step-up token sent as ``X-MFA-Code:
sso:<token>``. Also covers ``scripts/reset_password.py``.

Uses the fake provider from test_auth_recovery_sso.

Run with:
    cd server && pytest tests/test_sso_step_up.py -v
"""
from __future__ import annotations

import re
import time
from urllib.parse import parse_qs, urlparse

import pyotp
import pytest

from tests.test_auth_recovery_sso import (  # noqa: F401  (fixtures)
    PASSWORD,
    _auth,
    _code,
    _finish,
    _start,
    env,
    idp,
)

from core.auth import verify_password
from models import User
from services import oidc

RID = "req-0123456789abcdef"
SSO_ADMIN = {"sub": "sso-1", "email": "boss@example.com", "email_verified": True,
             "preferred_username": "boss", "groups": ["nvr-superusers"]}


def _sso_admin(env, idp) -> dict:
    """Sign the SSO superuser in; their bearer headers."""
    idp.claims = dict(SSO_ADMIN)
    q = _finish(env, idp, _start(env, idp))
    r = env.client.post("/api/v1/auth/oidc/exchange", json={"code": q["sso"][0]})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _reauth(env, idp, claims: dict, rid: str = RID):
    """Run a re-sign-in; (step-up token or None, the page's html)."""
    r = env.client.get("/api/v1/auth/oidc/start", params={"reauth": rid}, follow_redirects=False)
    assert r.status_code == 302, r.text
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["prompt"] == ["login"] and q["max_age"] == ["0"]
    idp.pending_nonce = q["nonce"][0]
    idp.claims = claims
    r = env.client.get("/api/v1/auth/oidc/callback", params={"code": "abc", "state": q["state"][0]},
                       follow_redirects=False)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html"), r.text
    assert r.headers["cache-control"] == "no-store"
    m = re.search(r'"token": "([^"]+)"', r.text)
    assert f'"rid": "{rid}"' in r.text
    return (m.group(1) if m else None), r.text


def _fresh(**extra) -> dict:
    return {**SSO_ADMIN, "auth_time": int(time.time()), **extra}


def test_sso_admin_is_told_to_confirm_with_sso_not_to_set_up_mfa(env, idp):
    h = _sso_admin(env, idp)
    r = env.client.post(f"/api/v1/users/{env.plain.id}/mfa/reset", headers=h)
    assert r.status_code == 401 and "single sign-on" in r.json()["detail"]
    r = env.client.post(f"/api/v1/users/{env.plain.id}/mfa/reset", headers={**h, "X-MFA-Code": "123456"})
    assert r.status_code == 401 and "single sign-on" in r.json()["detail"]


def test_re_sign_in_confirms_a_2fa_reset(env, idp):
    h = _sso_admin(env, idp)
    token, _ = _reauth(env, idp, _fresh())
    assert token
    r = env.client.post(f"/api/v1/users/{env.plain.id}/mfa/reset", headers={**h, "X-MFA-Code": f"sso:{token}"})
    assert r.status_code == 200, r.text
    env.db.expire_all()
    assert env.db.get(User, env.plain.id).mfa_enabled is False


def test_one_confirmation_covers_several_changes_and_a_password_reset(env, idp):
    h = _sso_admin(env, idp)
    token, _ = _reauth(env, idp, _fresh())
    hh = {**h, "X-MFA-Code": f"sso:{token}"}
    r = env.client.put(f"/api/v1/users/{env.plain.id}", json={"password": "An0ther!passw0rd"}, headers=hh)
    assert r.status_code == 200, r.text
    r = env.client.post(f"/api/v1/users/{env.plain.id}/mfa/reset", headers=hh)
    assert r.status_code == 200, r.text
    env.db.expire_all()
    assert verify_password("An0ther!passw0rd", env.db.get(User, env.plain.id).hashed_password)


def test_a_reused_earlier_sign_in_does_not_confirm(env, idp):
    _sso_admin(env, idp)
    token, page = _reauth(env, idp, _fresh(auth_time=int(time.time()) - 3600))
    assert token is None and "did not ask you to sign in again" in page


def test_an_identity_that_is_not_an_sso_account_here_gets_nothing(env, idp):
    _sso_admin(env, idp)
    before = env.db.query(User).count()
    token, page = _reauth(env, idp, {"sub": "stranger", "email": "x@example.com", "email_verified": True,
                                     "groups": ["nvr-superusers"], "auth_time": int(time.time())})
    assert token is None and "not a single sign-on account" in page
    assert env.db.query(User).count() == before      # never creates or links an account


def test_the_token_is_bound_to_its_account_and_session_version(env, idp):
    h = _sso_admin(env, idp)
    token, _ = _reauth(env, idp, _fresh())
    # Not a session token.
    assert env.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    # Not another user's confirmation: root has an authenticator and must use it.
    r = env.client.post(f"/api/v1/users/{env.plain.id}/mfa/reset",
                        headers={**_auth("root"), "X-MFA-Code": f"sso:{token}"})
    assert r.status_code == 401
    # A bumped token version (sessions ended) voids it.
    boss = env.db.query(User).filter(User.username == "boss").one()
    boss.token_version = int(boss.token_version or 0) + 1
    env.db.commit()
    r = env.client.post(f"/api/v1/users/{env.plain.id}/mfa/reset",
                        headers={"Authorization": _auth("boss", boss.token_version)["Authorization"],
                                 "X-MFA-Code": f"sso:{token}"})
    assert r.status_code == 401 and "expired" in r.json()["detail"]


def test_a_forged_token_is_refused_without_counting_toward_lockout(env, idp):
    h = _sso_admin(env, idp)
    for _ in range(8):
        r = env.client.post(f"/api/v1/users/{env.plain.id}/mfa/reset", headers={**h, "X-MFA-Code": "sso:not-a-token"})
        assert r.status_code == 401
    token, _ = _reauth(env, idp, _fresh())
    r = env.client.post(f"/api/v1/users/{env.plain.id}/mfa/reset", headers={**h, "X-MFA-Code": f"sso:{token}"})
    assert r.status_code == 200, r.text


def test_local_accounts_still_need_their_code(env, idp):
    r = env.client.post(f"/api/v1/users/{env.plain.id}/mfa/reset", headers={**_auth("root"), "X-MFA-Code": _code(env)})
    assert r.status_code == 200, r.text


def test_reauth_ids_are_checked_and_provider_errors_reach_the_page(env, idp):
    r = env.client.get("/api/v1/auth/oidc/start", params={"reauth": "<script>"}, follow_redirects=False)
    assert r.status_code == 400
    r = env.client.get("/api/v1/auth/oidc/start", params={"reauth": RID}, follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = env.client.get("/api/v1/auth/oidc/callback",
                       params={"error": "access_denied", "error_description": "</script><b>x", "state": state})
    assert r.status_code == 200 and '"token": null' in r.text
    assert "</script><b>" not in r.text                  # escaped inside the script and the page


def test_an_ordinary_sign_in_is_unchanged_by_reauth(env, idp):
    idp.claims = dict(SSO_ADMIN)
    q = _finish(env, idp, _start(env, idp))
    assert "sso" in q


# ── scripts/reset_password.py ─────────────────────────────────────────

@pytest.fixture
def script(env, monkeypatch):
    from scripts import reset_password as mod

    monkeypatch.setattr(mod, "SessionLocal", lambda: env.db.__class__(bind=env.db.get_bind()))
    return mod


def test_reset_password_script_sets_a_password_and_ends_sessions(env, script, capsys):
    env.plain.failed_login_attempts = 3
    env.db.commit()
    assert script.reset_password("plain", "N3w!passphrase") is True
    env.db.expire_all()
    u = env.db.get(User, env.plain.id)
    assert verify_password("N3w!passphrase", u.hashed_password)
    assert u.token_version == 1 and u.failed_login_attempts == 0 and u.mfa_enabled is True
    assert "Two-factor sign-in is unchanged" in capsys.readouterr().out


def test_reset_password_script_generates_a_policy_compliant_password(env, script, capsys):
    assert script.reset_password("plain", None) is True
    out = capsys.readouterr().out
    pw = re.search(r"New password: (\S+)", out).group(1)
    env.db.expire_all()
    assert len(pw) >= 20 and verify_password(pw, env.db.get(User, env.plain.id).hashed_password)


def test_reset_password_script_refuses_weak_unknown_and_sso_accounts(env, idp, script, capsys):
    assert script.reset_password("plain", "password") is False
    assert script.reset_password("nobody", "N3w!passphrase") is False
    _sso_admin(env, idp)
    assert script.reset_password("boss", "N3w!passphrase") is False
    assert "identity provider" in capsys.readouterr().out
    env.db.expire_all()
    assert verify_password(PASSWORD, env.db.get(User, env.plain.id).hashed_password)


def test_step_up_token_helpers_round_trip(env, idp):
    _sso_admin(env, idp)
    boss = env.db.query(User).filter(User.username == "boss").one()
    t = oidc.issue_step_up_token(boss)
    assert oidc.verify_step_up_token(boss, t) is True
    assert oidc.verify_step_up_token(env.plain, t) is False
    assert oidc.verify_step_up_token(boss, None) is False
    assert pyotp  # imported for parity with the sibling suite
