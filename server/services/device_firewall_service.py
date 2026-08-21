# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
#
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""
Device firewall — the app-layer access-control registry.

App-layer is the correct enforcement point for a Docker-bridge deployment:
bridge-mode NAT rewrites the source IP before it reaches the container kernel,
so only the application (which sees the real IP via the proxy's
``X-Forwarded-For``) can enforce on it. An OS-level backend for bare-metal
appliances can be added later behind the same registry.

Identity is a server-issued token, NOT the client IP. The IP cannot identify a
device: NAT collapses every client behind one address — with Docker Desktop's
port forwarding, *all* LAN clients arrive as the bridge gateway — so IP-keyed
approval let a second device silently inherit the first one's approval. Instead,
at login the server mints a long random token, stores its SHA-256, and returns
it in an HttpOnly cookie. The cookie's lifetime is independent of the session,
so logging out never costs an approval. Granularity is per browser profile (a
second profile / another browser / cleared cookies = a new device to approve).

Enrollment model (trust-on-first-use):
* the FIRST browser to authenticate on a fresh install is auto-approved, so the
  installer can never lock themselves out;
* any later browser is recorded ``pending`` and blocked until an admin approves;
* loopback and internal service callers are never firewalled.

Lockout safety is structural: enforcement is off until the admin turns it on,
the env ``DEVICE_FIREWALL_KILL`` is a hard override that forces it off for
recovery, loopback is always allowed (``docker exec`` recovery), and the CLI
(``python -m manage_devices``) can approve a device or disable enforcement
without touching the database by hand.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from core.config import settings
from models import DeviceStatus, SecuritySetting, TrustedDevice

# How a browser presents its device token. The SPA sends it as a HEADER: its
# fetch client uses ``credentials: 'omit'`` (deliberate — the API authenticates
# with bearer tokens, not cookies), so a Set-Cookie would be ignored by the
# browser and every device would look unenrolled. The cookie is still set and
# accepted as a fallback for non-SPA clients. Both are long-lived and NEVER
# cleared on logout — they identify the browser, not the session.
DEVICE_HEADER_NAME = "x-device-token"
DEVICE_COOKIE_NAME = "opennvr_device"
DEVICE_COOKIE_MAX_AGE = 400 * 24 * 3600  # 400d is the browser-enforced ceiling


def token_from_request(request) -> str | None:
    """The device token presented by ``request`` — header first, cookie second."""
    try:
        return (
            request.headers.get(DEVICE_HEADER_NAME)
            or request.cookies.get(DEVICE_COOKIE_NAME)
            or None
        )
    except Exception:
        return None

# The admin's on/off lives in security_settings under this key. The env
# ``device_firewall_kill`` is a hard override on top of it (break-glass).
_ACTIVE_KEY = "device_firewall_active"

# ── Per-request decision cache ──────────────────────────────────────────
# The middleware runs on every /api/* request — including every HLS video
# byte-range fetch during playback — and used to open a fresh DB session
# each time. Enforcement state and per-token approval change rarely, so a
# short TTL cache keeps the hot path DB-free. Mutations (approve/block/
# delete/set_enforcement) invalidate immediately; the TTL only bounds
# staleness for changes made by another process.
_CACHE_TTL_SECONDS = 15.0
_TOKEN_CACHE_MAX = 2048

_enforcement_cache: tuple[float, bool] | None = None
_token_cache: dict[str, tuple[float, bool]] = {}


def invalidate_decision_cache() -> None:
    """Drop cached firewall decisions (called after any registry mutation)."""
    global _enforcement_cache
    _enforcement_cache = None
    _token_cache.clear()


def _new_session():
    """Open a DB session for the cached decision helpers.

    A function (not a module-level import) so tests can monkeypatch it with
    their own session factory.
    """
    from core.database import SessionLocal

    return SessionLocal()


def enforcement_active_cached() -> bool:
    """``enforcement_active`` with a short TTL cache and its own DB session.

    Fail-open like the uncached version: any error reading is treated as OFF.
    """
    global _enforcement_cache
    import time

    now = time.monotonic()
    cached = _enforcement_cache
    if cached is not None and cached[0] > now:
        return cached[1]

    try:
        db = _new_session()
        try:
            value = enforcement_active(db)
        finally:
            db.close()
    except Exception:
        return False  # fail-open, and don't cache the failure
    _enforcement_cache = (now + _CACHE_TTL_SECONDS, value)
    return value


def is_allowed_browser_cached(raw_token: str | None) -> bool:
    """``is_allowed_browser`` with a short TTL cache keyed by token hash.

    Assumes the caller already established that enforcement is active.
    Fail-open on registry errors, matching the uncached version.
    """
    if not raw_token:
        return False
    import time

    key = hash_device_token(raw_token)
    now = time.monotonic()
    cached = _token_cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]

    try:
        db = _new_session()
        try:
            dev = get_device_by_token(db, raw_token)
        finally:
            db.close()
    except Exception:
        return True  # fail-open, and don't cache the failure
    allowed = bool(dev and dev.status == DeviceStatus.approved)
    if len(_token_cache) >= _TOKEN_CACHE_MAX:
        _token_cache.clear()
    _token_cache[key] = (now + _CACHE_TTL_SECONDS, allowed)
    return allowed


def enforcement_active(db: Session) -> bool:
    """Whether the firewall is currently blocking.

    False if the env break-glass is set, or the admin has not turned it on.
    Fail-safe: any error reading the flag is treated as OFF (never blocks by
    accident)."""
    if settings.device_firewall_kill:
        return False
    try:
        row = (
            db.query(SecuritySetting)
            .filter(SecuritySetting.key == _ACTIVE_KEY)
            .first()
        )
        return bool(row and (row.json_value or "").strip() == "true")
    except Exception:
        return False


def set_enforcement(db: Session, active: bool) -> bool:
    """Turn enforcement on/off. Returns the effective state (env can force off)."""
    row = (
        db.query(SecuritySetting).filter(SecuritySetting.key == _ACTIVE_KEY).first()
    )
    if row is None:
        row = SecuritySetting(key=_ACTIVE_KEY, json_value="")
        db.add(row)
    row.json_value = "true" if active else "false"
    db.commit()
    invalidate_decision_cache()
    return enforcement_active(db)


def _now() -> datetime:
    return datetime.now(UTC)


def new_device_token() -> str:
    """A fresh, unguessable device token (the cookie value)."""
    return secrets.token_urlsafe(32)


def hash_device_token(raw_token: str) -> str:
    """Only the hash is stored, so a database leak grants no access."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


def has_any_approved(db: Session) -> bool:
    """Whether any TOKEN-identified browser is approved.

    Rows without a token (legacy IP-era rows, or CLI-created stubs) are ignored
    so they can never block the trust-on-first-use bootstrap.
    """
    return (
        db.query(TrustedDevice)
        .filter(
            TrustedDevice.status == DeviceStatus.approved,
            TrustedDevice.device_token_hash.isnot(None),
        )
        .first()
        is not None
    )


def get_device_by_token(db: Session, raw_token: str | None) -> TrustedDevice | None:
    if not raw_token:
        return None
    return (
        db.query(TrustedDevice)
        .filter(TrustedDevice.device_token_hash == hash_device_token(raw_token))
        .first()
    )


def get_device_by_id(db: Session, device_id: int) -> TrustedDevice | None:
    return db.query(TrustedDevice).filter(TrustedDevice.id == device_id).first()


def is_allowed_browser(db: Session, raw_token: str | None) -> bool:
    """Whether the browser presenting ``raw_token`` may proceed.

    Fail-open on any registry error (a DB hiccup must never lock every admin
    out — see feature.md); the caller logs the failure. An absent or unknown
    token is NOT an error: it is simply not approved.
    """
    if not enforcement_active(db):
        return True
    try:
        dev = get_device_by_token(db, raw_token)
    except Exception:
        return True  # fail-open
    return bool(dev and dev.status == DeviceStatus.approved)


def register_authenticated_browser(
    db: Session,
    raw_token: str | None,
    ip: str | None,
    user_agent: str | None,
    user_id: int | None,
) -> tuple[TrustedDevice, str | None]:
    """Called on a successful login. Enrolls / refreshes this browser.

    Returns ``(row, token_to_set)`` where ``token_to_set`` is a NEW token the
    caller must write to the device cookie (None when the browser already
    presented a known one — re-issuing would orphan the existing approval).

    The first token-identified browser on a fresh install is auto-approved so
    the installer can never lock themselves out; any later browser is recorded
    pending. Never downgrades an already-approved or blocked browser.
    """
    dev = get_device_by_token(db, raw_token)
    issued: str | None = None
    first_ever = not has_any_approved(db)

    if dev is None:
        issued = new_device_token()
        dev = TrustedDevice(
            device_token_hash=hash_device_token(issued),
            status=DeviceStatus.pending,
            attempt_count=0,
        )
        db.add(dev)

    dev.last_seen = _now()
    dev.attempt_count = (dev.attempt_count or 0) + 1
    if ip:
        dev.ip_address = ip  # metadata: "last seen from"
    if user_agent:
        dev.user_agent = user_agent[:400]

    if first_ever and dev.status != DeviceStatus.blocked:
        dev.status = DeviceStatus.approved
        dev.auto_enrolled = True
        dev.approved_at = _now()
        dev.label = dev.label or "First browser (auto-enrolled)"
    elif dev.status is None:
        dev.status = DeviceStatus.pending

    db.commit()
    db.refresh(dev)
    invalidate_decision_cache()
    return dev, issued


def approve(
    db: Session, device_id: int, *, user_id: int | None = None, label: str | None = None
) -> TrustedDevice | None:
    dev = get_device_by_id(db, device_id)
    if dev is None:
        return None
    dev.status = DeviceStatus.approved
    dev.approved_by = user_id
    dev.approved_at = _now()
    if label is not None:
        dev.label = label
    db.commit()
    db.refresh(dev)
    invalidate_decision_cache()
    return dev


def block(db: Session, device_id: int) -> TrustedDevice | None:
    dev = get_device_by_id(db, device_id)
    if dev is None:
        return None
    dev.status = DeviceStatus.blocked
    db.commit()
    db.refresh(dev)
    invalidate_decision_cache()
    return dev


def delete(db: Session, device_id: int) -> bool:
    dev = get_device_by_id(db, device_id)
    if dev is None:
        return False
    db.delete(dev)
    db.commit()
    invalidate_decision_cache()
    return True


def list_devices(db: Session) -> list[TrustedDevice]:
    return (
        db.query(TrustedDevice)
        .order_by(TrustedDevice.status.asc(), TrustedDevice.last_seen.desc())
        .all()
    )
