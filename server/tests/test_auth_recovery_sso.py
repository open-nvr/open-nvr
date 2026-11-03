# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ways in besides a password, and the way out of a lost one:

* token versions — a reset ends the sessions issued before it;
* the e-mailed reset — same answer for every address, single-use link,
  nothing sent without a mail server;
* OpenID Connect — PKCE start, verified id_token, account resolved by
  subject / e-mail / created with a mapped role, one-time hand-off code,
  password login refused when the site is SSO-only (superusers excepted).

The provider is a fake: discovery, JWKS and token endpoints answered in
process, the id_token signed with a throwaway RSA key.

Run with:
    cd server && pytest tests/test_auth_recovery_sso.py -v
"""
from __future__ import annotations

import os
import secrets
import sys
import types as _types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pyotp
import pytest
from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_recovery_sso_test.db")
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

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from jose import jwk, jwt  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import core.auth as core_auth  # noqa: E402
from core.auth import create_access_token, get_password_hash  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import Role, User  # noqa: E402
from routers import (
    auth as auth_router,  # noqa: E402
    auth_settings as settings_router,  # noqa: E402
    sso as sso_router,  # noqa: E402
    users as users_router,  # noqa: E402
)
from services import mailer, oidc  # noqa: E402

PASSWORD = "Str0ng!passw0rd"
ISSUER = "https://idp.example.test/dex"
CLIENT_ID = "opennvr"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(auth_router, "auth_logger", _L(), raising=False)
    monkeypatch.setattr(core_auth, "auth_logger", _L(), raising=False)
    monkeypatch.setattr(users_router, "main_logger", _L(), raising=False)
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    session_factory = sessionmaker(bind=eng)
    app = FastAPI()
    for r in (auth_router, sso_router, settings_router, users_router):
        app.include_router(r.router, prefix="/api/v1")

    def _get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    db = session_factory()
    admin_role = Role(name="admin", description="admins")
    viewer_role = Role(name="viewer", description="viewers")
    db.add_all([admin_role, viewer_role])
    db.flush()
    secret = pyotp.random_base32()
    root = User(username="root", email="root@example.com", hashed_password=get_password_hash(PASSWORD),
                is_active=True, is_superuser=True, password_set=True, mfa_enabled=True, role_id=admin_role.id)
    root.mfa_secret = secret
    plain = User(username="plain", email="plain@example.com", hashed_password=get_password_hash(PASSWORD),
                 is_active=True, is_superuser=False, password_set=True, mfa_enabled=True, role_id=viewer_role.id)
    plain.mfa_secret = secret
    db.add_all([root, plain])
    db.commit()
    client = TestClient(app, base_url="https://nvr.example.test")
    oidc._discovery_cache.clear()
    oidc._jwks_cache.clear()
    oidc._exchange.clear()
    sso_router._forgot_calls.clear()
    try:
        yield _types.SimpleNamespace(client=client, db=db, root=root, plain=plain, secret=secret,
                                     admin_role=admin_role, viewer_role=viewer_role)
    finally:
        db.close()


def _auth(username, ver=0):
    return {"Authorization": f"Bearer {create_access_token({'sub': username, 'ver': ver})}"}


def _code(env):
    return pyotp.TOTP(env.secret).now()


def _login(env, username):
    r = env.client.post("/api/v1/auth/login-json", json={"username": username, "password": PASSWORD, "code": _code(env)})
    return r


# ── token version ──────────────────────────────────────────────────────

def test_admin_reset_ends_the_users_existing_sessions(env):
    r = _login(env, "plain")
    assert r.status_code == 200, r.text
    old = r.json()
    assert env.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {old['access_token']}"}).status_code == 200
    r = env.client.put(f"/api/v1/users/{env.plain.id}", json={"password": "An0ther!passw0rd"},
                       headers={**_auth("root"), "X-MFA-Code": _code(env)})
    assert r.status_code == 200, r.text
    assert env.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {old['access_token']}"}).status_code == 401
    assert env.client.post("/api/v1/auth/refresh", json={"refresh_token": old["refresh_token"]}).status_code == 401
    # Tokens from before the column existed (no ver claim) count as version 0:
    # they keep working until the account's first reset, and end with it.
    assert env.client.get("/api/v1/auth/me",
                          headers={"Authorization": f"Bearer {create_access_token({'sub': 'root'})}"}).status_code == 200
    assert env.client.get("/api/v1/auth/me",
                          headers={"Authorization": f"Bearer {create_access_token({'sub': 'plain'})}"}).status_code == 401
    old_refresh = core_auth.create_refresh_token({"sub": "plain"})
    assert env.client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh}).status_code == 401


# ── e-mailed reset ─────────────────────────────────────────────────────

def test_forgot_says_nothing_without_mail_and_sends_a_single_use_link_with_it(env, monkeypatch):
    r = env.client.post("/api/v1/auth/forgot", json={"email": "plain@example.com"})
    assert r.status_code == 200 and r.json() == {"ok": True, "mail_configured": False}
    assert env.client.get("/api/v1/auth/recovery-options").json()["email_reset"] is False

    sent: list[tuple[str, str, str]] = []

    async def fake_send(db, to, subject, text):
        sent.append((to, subject, text))

    monkeypatch.setattr(mailer, "send", fake_send)
    r = env.client.put("/api/v1/auth-settings/mail", json={"host": "smtp.example.test", "port": 587,
                                                           "security": "starttls", "from_address": "nvr@example.test",
                                                           "username": "nvr", "password": "s3cret"},
                       headers=_auth("root"))
    assert r.status_code == 200 and r.json()["configured"] is True and r.json()["has_password"] is True
    assert "password" not in r.json()
    assert env.client.get("/api/v1/auth/recovery-options").json()["email_reset"] is True

    # Unknown address: same answer, nothing sent.
    r = env.client.post("/api/v1/auth/forgot", json={"email": "nobody@example.com"})
    assert r.json() == {"ok": True, "mail_configured": True} and sent == []
    r = env.client.post("/api/v1/auth/forgot", json={"email": "plain@example.com"},
                        headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "nvr.example.test"})
    assert r.json() == {"ok": True, "mail_configured": True} and len(sent) == 1
    to, subject, text = sent[0]
    assert to == "plain@example.com" and "reset" in subject.lower()
    link = next(w for w in text.split() if w.startswith("https://nvr.example.test/reset-password?token="))
    token = parse_qs(urlparse(link).query)["token"][0]

    assert env.client.post("/api/v1/auth/reset", json={"token": token, "password": "short"}).status_code == 422
    r = env.client.post("/api/v1/auth/reset", json={"token": token, "password": "Fresh!passw0rd1"})
    assert r.status_code == 200 and r.json()["username"] == "plain"
    # Used once: the same link is dead.
    assert env.client.post("/api/v1/auth/reset", json={"token": token, "password": "Fresh!passw0rd2"}).status_code == 400
    env.db.expire_all()
    row = env.db.query(User).filter_by(username="plain").one()
    assert core_auth.verify_password("Fresh!passw0rd1", row.hashed_password) and row.token_version == 1


def test_reset_link_expires(env):
    expired = jwt.encode({"sub": "plain", "ver": 0, "type": "reset", "iat": datetime.now(UTC) - timedelta(hours=1),
                          "exp": datetime.now(UTC) - timedelta(minutes=1)},
                         core_auth.settings.secret_key, algorithm=core_auth.settings.algorithm)
    assert env.client.post("/api/v1/auth/reset", json={"token": expired, "password": "Fresh!passw0rd1"}).status_code == 400
    access = create_access_token({"sub": "plain", "ver": 0})     # an access token is not a reset token
    assert env.client.post("/api/v1/auth/reset", json={"token": access, "password": "Fresh!passw0rd1"}).status_code == 400


# ── OpenID Connect ─────────────────────────────────────────────────────

class FakeIdp:
    """Discovery, JWKS and token endpoints answered in process."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.pem = self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()).decode()
        pub = self.key.public_key().public_bytes(serialization.Encoding.PEM,
                                                 serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        self.jwks = {"keys": [{**jwk.construct(pub, "RS256").to_dict(), "kid": "k1", "use": "sig"}]}
        self.claims: dict = {}
        self.token_requests: list[dict] = []

    def id_token(self, nonce):
        now = datetime.now(UTC)
        return jwt.encode({"iss": ISSUER, "aud": CLIENT_ID, "nonce": nonce, "iat": now,
                           "exp": now + timedelta(minutes=5), **self.claims},
                          self.pem, algorithm="RS256", headers={"kid": "k1"})

    def client(self):
        idp = self

        class _Resp:
            def __init__(self, status, body):
                self.status_code, self._body = status, body
                self.text = str(body)

            def json(self):
                return self._body

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(self.status_code)

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                if url.endswith("/.well-known/openid-configuration"):
                    return _Resp(200, {"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/auth",
                                       "token_endpoint": f"{ISSUER}/token", "jwks_uri": f"{ISSUER}/keys",
                                       "userinfo_endpoint": f"{ISSUER}/userinfo",
                                       "id_token_signing_alg_values_supported": ["RS256"],
                                       "code_challenge_methods_supported": ["S256"]})
                if url.endswith("/keys"):
                    return _Resp(200, idp.jwks)
                if url.endswith("/userinfo"):
                    return _Resp(200, {"sub": idp.claims.get("sub"), "groups": idp.claims.get("_userinfo_groups", [])})
                return _Resp(404, {})

            async def post(self, url, data=None, headers=None):
                idp.token_requests.append(dict(data or {}))
                nonce = idp.pending_nonce
                return _Resp(200, {"access_token": "at-" + secrets.token_hex(4), "id_token": idp.id_token(nonce),
                                   "token_type": "bearer"})

        return _Client()


@pytest.fixture
def idp(env, monkeypatch):
    fake = FakeIdp()
    monkeypatch.setattr(oidc, "_client", fake.client)
    r = env.client.put("/api/v1/auth-settings/oidc",
                       json={"enabled": True, "issuer": ISSUER, "client_id": CLIENT_ID, "client_secret": "cs",
                             "button_label": "Company login", "default_role_id": env.viewer_role.id,
                             "role_claim": "groups", "role_map": {"nvr-admins": "admin"},
                             "superuser_values": ["nvr-superusers"]},
                       headers=_auth("root"))
    assert r.status_code == 200, r.text
    assert r.json()["has_client_secret"] is True and "client_secret" not in r.json()
    # The settings page redraws from this answer; its role picker needs the roles.
    assert {"id": env.viewer_role.id, "name": env.viewer_role.name} in r.json()["roles"]
    return fake


def _start(env, fake):
    r = env.client.get("/api/v1/auth/oidc/start", follow_redirects=False)
    assert r.status_code == 302, r.text
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert r.headers["location"].startswith(f"{ISSUER}/auth?")
    assert q["response_type"] == ["code"] and q["client_id"] == [CLIENT_ID] and q["code_challenge_method"] == ["S256"]
    assert q["redirect_uri"] == ["https://nvr.example.test/api/v1/auth/oidc/callback"]
    fake.pending_nonce = q["nonce"][0]
    return q["state"][0]


def _finish(env, fake, state):
    r = env.client.get("/api/v1/auth/oidc/callback", params={"code": "abc", "state": state}, follow_redirects=False)
    assert r.status_code == 302, r.text
    return parse_qs(urlparse(r.headers["location"]).query)


def test_login_page_sees_the_offer_only_when_configured(env, idp):
    opts = env.client.get("/api/v1/auth/recovery-options").json()
    assert opts["oidc"] == {"enabled": True, "label": "Company login", "allow_local_login": True}


def test_oidc_creates_a_user_with_the_mapped_role_and_hands_off_a_one_time_code(env, idp):
    idp.claims = {"sub": "u-1", "email": "Jane@Example.com", "preferred_username": "jane",
                  "given_name": "Jane", "family_name": "Doe", "groups": ["nvr-admins"]}
    state = _start(env, idp)
    q = _finish(env, idp, state)
    assert "sso" in q and "sso_error" not in q, q
    assert idp.token_requests[-1]["grant_type"] == "authorization_code" and "code_verifier" in idp.token_requests[-1]
    assert idp.token_requests[-1]["client_secret"] == "cs"
    r = env.client.post("/api/v1/auth/oidc/exchange", json={"code": q["sso"][0]})
    assert r.status_code == 200, r.text
    tokens = r.json()
    me = env.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}).json()
    assert me["username"] == "jane" and me["email"] == "jane@example.com" and me["auth_provider"] == "oidc"
    assert me["role_id"] == env.admin_role.id and me["is_superuser"] is False
    # The code is one-time.
    assert env.client.post("/api/v1/auth/oidc/exchange", json={"code": q["sso"][0]}).status_code == 401
    # Signing in again finds the same account by subject, even with a new e-mail.
    idp.claims = {**idp.claims, "email": "jane.doe@example.com", "groups": ["nvr-superusers"]}
    q = _finish(env, idp, _start(env, idp))
    r = env.client.post("/api/v1/auth/oidc/exchange", json={"code": q["sso"][0]})
    me = env.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"}).json()
    assert me["username"] == "jane" and me["is_superuser"] is True


def test_oidc_links_an_existing_local_account_by_verified_email(env, idp):
    idp.claims = {"sub": "u-plain", "email": "plain@example.com", "email_verified": True, "groups": []}
    q = _finish(env, idp, _start(env, idp))
    r = env.client.post("/api/v1/auth/oidc/exchange", json={"code": q["sso"][0]})
    me = env.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"}).json()
    assert me["username"] == "plain"
    env.db.expire_all()
    row = env.db.query(User).filter_by(username="plain").one()
    # Linked, but still a password account: its e-mailed reset keeps working.
    assert row.oidc_subject == "u-plain" and row.auth_provider != "oidc"


def test_oidc_never_links_by_an_unverified_email(env, idp):
    for verified in (None, False, "false"):
        claims = {"sub": "u-attacker", "email": "plain@example.com", "groups": []}
        if verified is not None:
            claims["email_verified"] = verified
        idp.claims = claims
        q = _finish(env, idp, _start(env, idp))
        assert "sso" not in q and "not verified" in q["sso_error"][0], (verified, q)
    env.db.expire_all()
    row = env.db.query(User).filter_by(username="plain").one()
    assert row.oidc_subject is None and env.db.query(User).count() == 2      # nothing linked, nothing created


def test_oidc_never_signs_in_as_a_superuser_by_email(env, idp):
    # Even with a verified address: a superuser's TOTP is never traded for the provider's word.
    idp.claims = {"sub": "u-root", "email": "root@example.com", "email_verified": True, "groups": []}
    q = _finish(env, idp, _start(env, idp))
    assert "sso" not in q and "administrator" in q["sso_error"][0], q
    env.db.expire_all()
    assert env.db.query(User).filter_by(username="root").one().oidc_subject is None


def test_oidc_refuses_a_superuser_linked_before_linking_was_restricted(env, idp):
    env.root.oidc_issuer, env.root.oidc_subject, env.root.auth_provider = ISSUER, "u-old-link", "oidc"
    env.db.commit()
    idp.claims = {"sub": "u-old-link", "email": "root@example.com", "groups": []}
    q = _finish(env, idp, _start(env, idp))
    assert "sso" not in q and "administrator" in q["sso_error"][0], q
    assert _login(env, "root").status_code == 200        # the password door stays open


def test_oidc_refuses_a_bad_state_nonce_or_audience(env, idp):
    idp.claims = {"sub": "u-2", "email": "two@example.com"}
    state = _start(env, idp)
    q = _finish(env, idp, "not-the-state")
    assert "sso_error" in q
    idp.pending_nonce = "wrong"
    q = _finish(env, idp, state)
    assert "sso_error" in q and "nonce" in q["sso_error"][0]
    # Unknown user with auto_create off.
    env.client.put("/api/v1/auth-settings/oidc", json={"auto_create": False}, headers=_auth("root"))
    state = _start(env, idp)
    q = _finish(env, idp, state)
    assert "sso_error" in q and "no OpenNVR account" in q["sso_error"][0]


def test_sso_only_refuses_password_login_except_for_superusers(env, idp):
    env.client.put("/api/v1/auth-settings/oidc", json={"allow_local_login": False}, headers=_auth("root"))
    assert env.client.get("/api/v1/auth/recovery-options").json()["oidc"]["allow_local_login"] is False
    r = _login(env, "plain")
    assert r.status_code == 403 and "Company login" in r.json()["detail"]
    assert _login(env, "root").status_code == 200
    # Switching the provider off brings password login back for everyone.
    env.client.put("/api/v1/auth-settings/oidc", json={"enabled": False}, headers=_auth("root"))
    assert _login(env, "plain").status_code == 200


def test_oidc_settings_are_superuser_only_and_validated(env):
    assert env.client.get("/api/v1/auth-settings/oidc", headers=_auth("plain")).status_code in (401, 403)
    r = env.client.put("/api/v1/auth-settings/oidc", json={"enabled": True}, headers=_auth("root"))
    assert r.status_code == 422                     # no issuer / client id yet
    r = env.client.put("/api/v1/auth-settings/oidc", json={"default_role_id": 999}, headers=_auth("root"))
    assert r.status_code == 422
    r = env.client.get("/api/v1/auth-settings/oidc", headers=_auth("root"))
    assert r.status_code == 200 and [x["name"] for x in r.json()["roles"]] == ["admin", "viewer"]


def test_migration_chains_off_head():
    text = (_HERE / "migrations" / "versions" / "cc44dd55ee66_add_user_token_version_and_oidc.py").read_text()
    assert 'revision: str = "cc44dd55ee66"' in text
    assert 'down_revision: str | None = "bb33cc44dd55"' in text


def test_role_claims_may_be_a_list_a_string_or_a_map_of_roles():
    """Zitadel sends project roles as a map keyed by role name."""
    cfg = {"role_claim": "urn:zitadel:iam:org:project:roles"}
    zitadel = {"urn:zitadel:iam:org:project:roles": {"admin": {"3979": "zitadel.example"}, "viewer": {"3979": "zitadel.example"}}}
    assert sorted(oidc._claim_values(zitadel, cfg)) == ["admin", "viewer"]
    assert oidc._claim_values({"groups": ["a", "b"]}, {}) == ["a", "b"]
    assert oidc._claim_values({"groups": "a"}, {}) == ["a"]
    assert oidc._claim_values({}, {}) == []
    # Keycloak nests them; a claim whose name has dots is still found by name.
    keycloak = {"realm_access": {"roles": ["operator", "offline_access"]}}
    assert oidc._claim_values(keycloak, {"role_claim": "realm_access.roles"}) == ["operator", "offline_access"]
    auth0 = {"https://example.com/roles": ["admin"]}
    assert oidc._claim_values(auth0, {"role_claim": "https://example.com/roles"}) == ["admin"]
    assert oidc._claim_values(keycloak, {"role_claim": "realm_access.missing"}) == []
