# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign in through the site's OpenID Connect provider.

Any OIDC provider with a discovery document works: Dex, Authentik,
Keycloak, Zitadel, Entra ID, Google. The flow is the authorization code
grant with PKCE; the provider's ``id_token`` is verified against its
JWKS (issuer, audience, signature, nonce, expiry). The user is matched
by issuer + subject, else linked by e-mail to an existing local account,
else created when ``auto_create`` is on, with a role from the configured
claim mapping or the default role. Local password login stays available
unless the operator turns it off — and stays available to superusers
regardless, so a broken provider never locks the site.

Settings live under ``auth.oidc`` in the site settings table, the client
secret encrypted at rest.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from jose import JWTError, jwt

from core.config import settings
from services import site_settings

logger = logging.getLogger("opennvr.oidc")

OIDC_KEY = "auth.oidc"
STATE_COOKIE = "onvr_oidc"
STATE_TTL_SECONDS = 600
EXCHANGE_TTL_SECONDS = 90
_DISCOVERY_TTL = 600.0

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "issuer": "",
    "client_id": "",
    "scopes": "openid email profile",
    "button_label": "Single sign-on",
    "auto_create": True,
    "default_role_id": None,
    "role_claim": "groups",
    "role_map": {},            # claim value -> role name
    "superuser_values": [],    # claim values that make a superuser
    "allow_local_login": True,
}


# ── settings ────────────────────────────────────────────────────────

def load(db) -> dict[str, Any]:
    raw = site_settings.get_json(db, OIDC_KEY, {}) or {}
    cfg = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    cfg["issuer"] = str(cfg["issuer"] or "").strip().rstrip("/")
    cfg["client_secret"] = ""
    enc = raw.get("client_secret_enc")
    if enc:
        try:
            from utils.encryption import decrypt_value

            cfg["client_secret"] = decrypt_value(enc)
        except Exception:
            logger.warning("oidc: stored client secret could not be decrypted")
    if not isinstance(cfg["role_map"], dict):
        cfg["role_map"] = {}
    if not isinstance(cfg["superuser_values"], list):
        cfg["superuser_values"] = []
    return cfg


def save(db, patch: dict[str, Any]) -> dict[str, Any]:
    raw = site_settings.get_json(db, OIDC_KEY, {}) or {}
    for key in DEFAULTS:
        if key in patch and patch[key] is not None:
            raw[key] = patch[key]
    if "client_secret" in patch:
        if patch["client_secret"] is None:
            raw.pop("client_secret_enc", None)
        elif str(patch["client_secret"]):
            from utils.encryption import encrypt_value

            raw["client_secret_enc"] = encrypt_value(str(patch["client_secret"]))
    site_settings.set_json(db, OIDC_KEY, raw)
    return public(db)


def public(db) -> dict[str, Any]:
    cfg = load(db)
    return {**{k: v for k, v in cfg.items() if k != "client_secret"},
            "has_client_secret": bool(cfg["client_secret"])}


def login_offer(db) -> dict[str, Any]:
    """What the login page may know before anyone is signed in."""
    cfg = load(db)
    ready = bool(cfg["enabled"] and cfg["issuer"] and cfg["client_id"])
    return {"enabled": ready, "label": cfg["button_label"] or "Single sign-on",
            "allow_local_login": bool(cfg["allow_local_login"]) or not ready}


# ── provider ────────────────────────────────────────────────────────

_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_jwks_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10.0, trust_env=False)


async def discover(issuer: str, *, fresh: bool = False) -> dict[str, Any]:
    issuer = issuer.rstrip("/")
    hit = _discovery_cache.get(issuer)
    if hit and not fresh and hit[0] > time.monotonic():
        return hit[1]
    async with _client() as client:
        r = await client.get(f"{issuer}/.well-known/openid-configuration")
    r.raise_for_status()
    doc = r.json()
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not doc.get(key):
            raise ValueError(f"discovery document lacks {key}")
    if str(doc.get("issuer", "")).rstrip("/") != issuer:
        raise ValueError("discovery issuer does not match the configured issuer")
    _discovery_cache[issuer] = (time.monotonic() + _DISCOVERY_TTL, doc)
    return doc


async def jwks(doc: dict[str, Any], *, fresh: bool = False) -> dict[str, Any]:
    uri = doc["jwks_uri"]
    hit = _jwks_cache.get(uri)
    if hit and not fresh and hit[0] > time.monotonic():
        return hit[1]
    async with _client() as client:
        r = await client.get(uri)
    r.raise_for_status()
    keys = r.json()
    _jwks_cache[uri] = (time.monotonic() + _DISCOVERY_TTL, keys)
    return keys


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _state_token(payload: dict[str, Any]) -> str:
    now = datetime.now(UTC)
    return jwt.encode({**payload, "type": "oidc_state", "iat": now, "exp": now + timedelta(seconds=STATE_TTL_SECONDS)},
                      settings.secret_key, algorithm=settings.algorithm)


def read_state(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError:
        return None
    return payload if payload.get("type") == "oidc_state" else None


async def begin(db, redirect_uri: str, next_path: str | None = None) -> tuple[str, str]:
    """The provider's authorize URL and the state cookie value to set."""
    cfg = load(db)
    if not (cfg["enabled"] and cfg["issuer"] and cfg["client_id"]):
        raise ValueError("single sign-on is not configured")
    doc = await discover(cfg["issuer"])
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    verifier, challenge = _pkce()
    query = {
        "response_type": "code", "client_id": cfg["client_id"], "redirect_uri": redirect_uri,
        "scope": cfg["scopes"] or "openid email profile", "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256",
    }
    url = f"{doc['authorization_endpoint']}{'&' if '?' in doc['authorization_endpoint'] else '?'}{urlencode(query)}"
    cookie = _state_token({"state": state, "nonce": nonce, "verifier": verifier,
                           "redirect_uri": redirect_uri, "next": next_path or "/"})
    return url, cookie


async def complete(db, *, code: str, state: str, cookie: str | None) -> dict[str, Any]:
    """Exchange the code, verify the id_token, return the identity claims."""
    cfg = load(db)
    saved = read_state(cookie)
    if saved is None or not secrets.compare_digest(str(saved.get("state", "")), str(state)):
        raise ValueError("sign-in session expired or did not match; try again")
    doc = await discover(cfg["issuer"])
    data = {
        "grant_type": "authorization_code", "code": code, "redirect_uri": saved["redirect_uri"],
        "client_id": cfg["client_id"], "code_verifier": saved["verifier"],
    }
    if cfg["client_secret"]:
        data["client_secret"] = cfg["client_secret"]
    async with _client() as client:
        r = await client.post(doc["token_endpoint"], data=data, headers={"Accept": "application/json"})
    if r.status_code != 200:
        logger.warning("oidc: token endpoint answered %s: %s", r.status_code, r.text[:200])
        raise ValueError("the identity provider refused the sign-in")
    body = r.json()
    id_token = body.get("id_token")
    if not id_token:
        raise ValueError("the identity provider returned no id_token")
    claims = await verify_id_token(id_token, cfg, doc, nonce=saved.get("nonce"),
                                   access_token=body.get("access_token"))
    # Groups often ride in userinfo rather than the id_token.
    role_claim = cfg.get("role_claim") or "groups"
    if role_claim not in claims and body.get("access_token") and doc.get("userinfo_endpoint"):
        try:
            async with _client() as client:
                u = await client.get(doc["userinfo_endpoint"],
                                     headers={"Authorization": f"Bearer {body['access_token']}"})
            if u.status_code == 200:
                info = u.json()
                if isinstance(info, dict) and info.get("sub") == claims.get("sub"):
                    claims = {**info, **claims}
        except Exception:
            logger.debug("oidc: userinfo fetch failed", exc_info=True)
    claims["_next"] = saved.get("next") or "/"
    return claims


async def verify_id_token(id_token: str, cfg: dict[str, Any], doc: dict[str, Any], *,
                          nonce: str | None, access_token: str | None) -> dict[str, Any]:
    keys = await jwks(doc)
    algs = [a for a in (doc.get("id_token_signing_alg_values_supported") or ["RS256"])
            if a in ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256")] or ["RS256"]
    try:
        claims = jwt.decode(id_token, keys, algorithms=algs, audience=cfg["client_id"],
                            issuer=doc.get("issuer") or cfg["issuer"], access_token=access_token,
                            options={"verify_at_hash": bool(access_token)})
    except JWTError as exc:
        # A rotated key: fetch the set once more before giving up.
        try:
            keys = await jwks(doc, fresh=True)
            claims = jwt.decode(id_token, keys, algorithms=algs, audience=cfg["client_id"],
                                issuer=doc.get("issuer") or cfg["issuer"], access_token=access_token,
                                options={"verify_at_hash": bool(access_token)})
        except JWTError:
            raise ValueError(f"id_token rejected: {exc}") from None
    if nonce and claims.get("nonce") != nonce:
        raise ValueError("id_token nonce mismatch")
    if not claims.get("sub"):
        raise ValueError("id_token carries no subject")
    return claims


# ── the account ─────────────────────────────────────────────────────

def _username_for(claims: dict[str, Any], db, user_model) -> str:
    base = str(claims.get("preferred_username") or claims.get("email", "").split("@")[0]
               or f"sso-{claims['sub'][:8]}")
    base = "".join(ch for ch in base.lower() if ch.isalnum() or ch in "._-")[:40] or "sso-user"
    name, n = base, 1
    while db.query(user_model).filter(user_model.username == name).first() is not None:
        n += 1
        name = f"{base}{n}"[:50]
    return name


def _email_verified(claims: dict[str, Any]) -> bool:
    """Whether the provider vouches for the address. Absent means no: some
    providers (Entra among them) let a user put any address in ``email``."""
    v = claims.get("email_verified")
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


def resolve_user(db, claims: dict[str, Any], cfg: dict[str, Any]):
    """The local account for these claims: by issuer+subject, else linked by
    e-mail, else created. Role and superuser follow the claim mapping on
    every sign-in. Returns the User, or raises ValueError when sign-in
    must be refused.

    Linking by e-mail hands an existing account, and its sessions, to
    whoever controls that address at the provider, without that account's
    TOTP. So it needs an address the provider has verified, and never
    happens for a superuser: those sign in with their password."""
    from core.auth import get_password_hash
    from models import Role, User

    issuer, sub = cfg["issuer"], str(claims["sub"])
    email = str(claims.get("email") or "").strip().lower()
    user = db.query(User).filter(User.oidc_issuer == issuer, User.oidc_subject == sub).first()
    if user is None and email:
        user = db.query(User).filter(User.email == email).first()
        if user is not None:
            if user.oidc_subject and user.oidc_subject != sub:
                raise ValueError("this e-mail belongs to an account linked to another identity")
            if user.is_superuser:
                raise ValueError("an administrator account is never linked to single sign-on; "
                                 "sign in with your password")
            if not _email_verified(claims):
                raise ValueError("the identity provider has not verified this e-mail address, "
                                 "so it cannot be linked to an existing account")
    mapped_role, make_superuser = _role_from_claims(db, claims, cfg, Role)
    if user is None:
        if not cfg.get("auto_create", True):
            raise ValueError("no OpenNVR account is linked to this identity; ask an administrator")
        if not email:
            raise ValueError("the identity provider sent no e-mail address")
        role_id = mapped_role.id if mapped_role else cfg.get("default_role_id")
        if not role_id:
            fallback = db.query(Role).order_by(Role.id).first()
            role_id = fallback.id if fallback else None
        if not role_id:
            raise ValueError("no role to give new single sign-on users; set a default role")
        user = User(username=_username_for(claims, db, User), email=email,
                    hashed_password=get_password_hash(secrets.token_urlsafe(32)),
                    first_name=str(claims.get("given_name") or "")[:50] or None,
                    last_name=str(claims.get("family_name") or "")[:50] or None,
                    is_active=True, is_superuser=make_superuser, password_set=True,
                    mfa_enabled=False, role_id=role_id, auth_provider="oidc",
                    oidc_issuer=issuer, oidc_subject=sub)
        db.add(user)
        db.commit()
        db.refresh(user)
        return user
    if not user.is_active:
        raise ValueError("this account is deactivated")
    if user.is_superuser and (user.mfa_enabled or (user.auth_provider or "local") != "oidc"):
        # A superuser with TOTP enrolled, or one that is not a single sign-on
        # account, is a password account (single sign-on ones skip
        # enrolment). One found by subject here was linked by e-mail before
        # linking was restricted; refuse it all the same. The second half
        # keeps refusing it after an administrator resets its two-factor
        # sign-in (mfa_service.reset_mfa makes such an account local).
        raise ValueError("an administrator account is never linked to single sign-on; "
                         "sign in with your password")
    # A linked account keeps its kind (auth_provider): one that had a password
    # stays local, so its e-mailed reset and password sign-in keep working.
    user.oidc_issuer, user.oidc_subject = issuer, sub
    if mapped_role is not None:
        user.role_id = mapped_role.id
    if cfg.get("superuser_values"):
        user.is_superuser = make_superuser or bool(user.is_superuser and not _claim_values(claims, cfg))
    db.commit()
    db.refresh(user)
    return user


def _claim_values(claims: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    """The role names a claim carries: a list (most providers), a single
    string, or a map whose keys are the roles. Zitadel sends the map, as
    ``urn:zitadel:iam:org:project:roles: {"admin": {"<org id>": "<domain>"}}``;
    reading it as one value matched nothing and every user got the default role.

    The claim may also be a dotted path into nested claims, for Keycloak's
    ``realm_access.roles`` or ``resource_access.<client>.roles``. A claim whose
    own name has dots (``https://example.com/roles``) is found by name first."""
    name = cfg.get("role_claim") or "groups"
    raw = claims.get(name)
    if raw is None and "." in name:
        raw = claims
        for part in name.split("."):
            raw = raw.get(part) if isinstance(raw, dict) else None
    if raw is None:
        return []
    if isinstance(raw, dict):
        values = list(raw)
    elif isinstance(raw, list):
        values = raw
    else:
        values = [raw]
    return [str(v) for v in values]


def _role_from_claims(db, claims, cfg, role_model):
    values = _claim_values(claims, cfg)
    role = None
    for value in values:
        name = (cfg.get("role_map") or {}).get(value)
        if name:
            role = db.query(role_model).filter(role_model.name == name).first()
            if role is not None:
                break
    superuser = any(v in (cfg.get("superuser_values") or []) for v in values)
    return role, superuser


# ── the hand-off to the browser ─────────────────────────────────────

_exchange: dict[str, tuple[float, int, str]] = {}


def issue_exchange_code(user_id: int, next_path: str) -> str:
    """A one-time code the callback hands the browser; the page swaps it
    for tokens over POST, so tokens never sit in a URL or a history entry."""
    now = time.monotonic()
    for k in [k for k, v in _exchange.items() if v[0] < now]:
        _exchange.pop(k, None)
    code = secrets.token_urlsafe(32)
    _exchange[code] = (now + EXCHANGE_TTL_SECONDS, user_id, next_path or "/")
    return code


def redeem_exchange_code(code: str) -> tuple[int, str] | None:
    entry = _exchange.pop(code, None)
    if not entry or entry[0] < time.monotonic():
        return None
    return entry[1], entry[2]
