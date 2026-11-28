# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Setup links for remote sites (docs/SITE_AGENT.md, plan S1).

An admin sends one link, ``https://<server>/site-setup#<id>.<secret>``, to the
person at the site. Opening it shows what to download; the agent then takes
the *setup code* (``<server>#<id>.<secret>``) and needs nothing else: no
short code to type in time, no certificate fingerprint to compare.

- The secret is in the URL **fragment**, which browsers never send to the
  server: it is in no access log, no Referer, no proxy log.
- ``<id>`` is random and separate from the secret, so the parts of the link
  that do travel (to setup-info and hello) tell nothing about the secret.
- Before the agent sends the secret, the server proves it knows it (hello):
  HMAC over the agent's nonce and the certificate key the agent sees, keyed
  by a key derived from the secret. A man in the middle shows its own key
  and cannot make that proof, so a self-signed or intercepted connection
  never gets the secret. After that the agent trusts that key only.
- Only derived values are kept: a hash to enrol with, and the proof key,
  encrypted with core's vault key. Someone who read the database AND
  holds the vault key AND sits between an agent and this server could
  impersonate the server to that agent before it enrols; nothing less.
- Single use (atomically), short-lived (72 h by default, 1 h to 7 days),
  revocable; using one voids the site's other links and its short code.

Links are offered only when this server has a publicly trusted certificate
for its PUBLIC_HOSTNAME, and no front proxy ends TLS in front of it: a
self-signed server would make the person at the site click through a
browser warning (and anyone in the path could read the link), and a front
proxy's key is not the one the server can prove. Those sites keep the
short code and its pin.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from core.config import settings
from models import Site, SiteSetupToken

LINK_HOURS_DEFAULT = 72
LINK_HOURS_MIN = 1
LINK_HOURS_MAX = 7 * 24
#: Links per site that are still usable at once.
LINKS_PER_SITE_MAX = 5


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# ── derivations (mirrored in host-agent/internal/site/setup.go) ──────────


def enrol_hash(secret: str) -> str:
    return hashlib.sha256(("opennvr-site-enrol\n" + secret).encode()).hexdigest()


def proof_key(secret: str) -> str:
    return hmac.new(secret.encode(), b"opennvr-site-proof", hashlib.sha256).hexdigest()


def hello_proof(key: str, nonce: str, pin: str) -> str:
    msg = "\n".join(("opennvr-site-hello", nonce, pin)).encode()
    return hmac.new(key.encode(), msg, hashlib.sha256).hexdigest()


def _new_id() -> str:
    return secrets.token_urlsafe(6)  # 8 characters, URL safe


def _new_secret() -> str:
    # 128 bits, base32 lower case without padding: 26 characters.
    return base64.b32encode(secrets.token_bytes(16)).decode().rstrip("=").lower()


def split_token(token: str | None) -> tuple[str, str] | None:
    """``<id>.<secret>`` -> (id, secret)."""
    tid, sep, secret = (token or "").strip().partition(".")
    if not sep or not (4 <= len(tid) <= 16) or not (16 <= len(secret) <= 64):
        return None
    return tid, secret


# ── readiness ────────────────────────────────────────────────────────────

_ready_cache: tuple[float, dict[str, Any]] | None = None
READY_CACHE_S = 300


def _spki_pin_of(der: bytes) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    cert = x509.load_der_x509_certificate(der)
    spki = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return "sha256/" + base64.b64encode(hashlib.sha256(spki).digest()).decode()


def _tls_pin(host: str, port: int, server_name: str, timeout: float) -> str:
    """Connect with full verification (public CAs, the name) and return the
    peer's key pin. Raises on any failure."""
    import socket
    import ssl

    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:  # pragma: no cover - certifi ships with httpx
        ctx = ssl.create_default_context()
    with (
        socket.create_connection((host, port), timeout=timeout) as raw,
        ctx.wrap_socket(raw, server_hostname=server_name) as tls,
    ):
        return _spki_pin_of(tls.getpeercert(binary_form=True))


def readiness(force: bool = False) -> dict[str, Any]:
    """Can this server hand out setup links? ``{"ready": bool, "reason":
    code or None, "detail": text, "host": PUBLIC_HOSTNAME}``. Cached for a
    few minutes: it makes two TLS connections."""
    global _ready_cache
    import os

    from services.sites import server_pin

    if not force and _ready_cache and time.monotonic() - _ready_cache[0] < READY_CACHE_S:
        return _ready_cache[1]
    host = (getattr(settings, "public_hostname", "") or "").strip().rstrip(".").lower()
    out: dict[str, Any] = {"ready": False, "reason": None, "detail": "", "host": host or None}
    if not host:
        out.update(reason="no_public_hostname",
                   detail="Set PUBLIC_HOSTNAME (this server's name on the internet) to use setup links.")
    else:
        # This server's own nginx, as the agent will meet it, verified like a
        # browser would: a public CA and the public name.
        nginx = os.environ.get("SITE_SETUP_TLS_CHECK_HOST") or "nginx"
        try:
            own = _tls_pin(nginx, 443, host, 5.0)
        except Exception as e:  # any failure means "not trusted"
            out.update(reason="certificate_not_trusted",
                       detail=f"This server's certificate is not publicly trusted for {host} ({e}).")
        else:
            mine = server_pin()
            front: str | None = None
            try:
                front = _tls_pin(host, 443, host, 4.0)
            except Exception:  # the public name may not loop back; that is fine
                front = None
            if front and mine and front != mine and own == mine:
                out.update(reason="front_proxy_ends_tls",
                           detail=f"Something in front of this server ends TLS for {host} (it shows another "
                                  "certificate key). Let it pass TLS through to this server to use setup links.")
            else:
                out.update(ready=True)
    _ready_cache = (time.monotonic(), out)
    return out


# ── links ────────────────────────────────────────────────────────────────


def link_url(host: str, token_id: str, secret: str) -> str:
    return f"https://{host}/site-setup#{token_id}.{secret}"


def setup_code(host: str, token_id: str, secret: str) -> str:
    return f"{host}#{token_id}.{secret}"


def _active_query(db: Session, site_id: int):
    now = _now()
    return (
        db.query(SiteSetupToken)
        .filter(SiteSetupToken.site_id == site_id, SiteSetupToken.used_at.is_(None),
                SiteSetupToken.revoked_at.is_(None), SiteSetupToken.expires_at > now)
    )


def create_link(db: Session, site: Site, *, user_id: int | None, hours: int, host: str) -> dict[str, Any]:
    """A new link for ``site``: the secret is in the returned URL only."""
    from utils.encryption import encrypt_value

    hours = max(LINK_HOURS_MIN, min(LINK_HOURS_MAX, int(hours)))
    # Never more than a few usable links per site: the oldest go first.
    active = _active_query(db, site.id).order_by(SiteSetupToken.created_at).all()
    for old in active[: max(0, len(active) - (LINKS_PER_SITE_MAX - 1))]:
        old.revoked_at = _now()
    secret = _new_secret()
    row = SiteSetupToken(
        site_id=site.id, token_id=_new_id(), enrol_hash=enrol_hash(secret),
        proof_key_enc=encrypt_value(proof_key(secret)), created_by=user_id,
        expires_at=_now() + timedelta(hours=hours),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {**link_view(row), "url": link_url(host, row.token_id, secret),
            "setup_code": setup_code(host, row.token_id, secret)}


def link_view(row: SiteSetupToken) -> dict[str, Any]:
    expires = _aware(row.expires_at)
    state = "active"
    if row.used_at:
        state = "used"
    elif row.revoked_at:
        state = "revoked"
    elif expires and expires <= _now():
        state = "expired"
    return {"token_id": row.token_id, "state": state, "created_at": row.created_at,
            "expires_at": row.expires_at, "used_at": row.used_at, "used_ip": row.used_ip,
            "used_hostname": row.used_hostname}


def list_links(db: Session, site: Site, limit: int = 20) -> list[dict[str, Any]]:
    rows = (db.query(SiteSetupToken).filter(SiteSetupToken.site_id == site.id)
            .order_by(SiteSetupToken.created_at.desc(), SiteSetupToken.id.desc()).limit(limit).all())
    return [link_view(r) for r in rows]


def revoke_link(db: Session, site: Site, token_id: str) -> bool:
    row = (db.query(SiteSetupToken)
           .filter(SiteSetupToken.site_id == site.id, SiteSetupToken.token_id == token_id).first())
    if row is None or row.used_at or row.revoked_at:
        return False
    row.revoked_at = _now()
    db.commit()
    return True


def usable(db: Session, token_id: str | None) -> SiteSetupToken | None:
    """The link with this id while it can still be used, else None."""
    if not token_id or len(token_id) > 16:
        return None
    row = db.query(SiteSetupToken).filter(SiteSetupToken.token_id == token_id).first()
    if row is None or row.used_at or row.revoked_at:
        return None
    expires = _aware(row.expires_at)
    if expires is None or expires <= _now():
        return None
    return row


def prove(db: Session, token_id: str, nonce: str, pin: str | None) -> str:
    """The hello proof for this link, or random bytes of the same shape for
    a link that is unknown, used, expired or revoked (the answer tells
    nothing)."""
    from utils.encryption import decrypt_value

    row = usable(db, token_id)
    if row is None or not pin:
        return secrets.token_hex(32)
    try:
        key = decrypt_value(row.proof_key_enc)
    except Exception:  # a key that cannot be read proves nothing
        return secrets.token_hex(32)
    return hello_proof(key, nonce, pin)


def consume(db: Session, token: str | None, *, ip: str | None, hostname: str | None) -> Site | None:
    """Use a link once: atomically, so of two agents racing with one link
    only one goes on. Voids the site's other links and its short code.
    None for anything that does not fit (the caller answers alike)."""
    parts = split_token(token)
    if parts is None:
        return None
    tid, secret = parts
    row = usable(db, tid)
    if row is None or not hmac.compare_digest(row.enrol_hash, enrol_hash(secret)):
        return None
    now = _now()
    used = db.execute(
        update(SiteSetupToken)
        .where(SiteSetupToken.id == row.id, SiteSetupToken.used_at.is_(None),
               SiteSetupToken.revoked_at.is_(None), SiteSetupToken.expires_at > now)
        .values(used_at=now, used_ip=(ip or "")[:45] or None, used_hostname=(hostname or "")[:200] or None)
        .execution_options(synchronize_session=False)
    )
    if used.rowcount != 1:
        db.rollback()
        return None
    site_id = row.site_id
    db.execute(
        update(SiteSetupToken)
        .where(SiteSetupToken.site_id == site_id, SiteSetupToken.id != row.id,
               SiteSetupToken.used_at.is_(None), SiteSetupToken.revoked_at.is_(None))
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )
    site = db.get(Site, site_id)
    if site is None:
        db.rollback()
        return None
    site.enrol_code_hash = None
    site.enrol_expires_at = None
    db.commit()
    db.refresh(site)
    return site


def setup_info(db: Session, token_id: str | None) -> dict[str, Any] | None:
    """What the public setup page shows; never uses the link up."""
    row = usable(db, token_id)
    if row is None:
        return None
    site = db.get(Site, row.site_id)
    if site is None:
        return None
    return {"site_name": site.name, "expires_at": row.expires_at}
