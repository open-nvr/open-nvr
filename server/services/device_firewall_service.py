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

Enrollment model (trust-on-first-use):
* the FIRST device to authenticate on a fresh install is auto-approved, so the
  installer can never lock themselves out;
* any later unknown device is recorded ``pending`` and blocked until an admin
  approves it;
* loopback and sibling containers (MediaMTX/KAI-C/adapters) are never firewalled.

Lockout safety is structural: enforcement is off until the admin turns it on,
the env ``DEVICE_FIREWALL_KILL`` is a hard override that forces it off for
recovery, loopback is always allowed (``docker exec`` recovery), and the CLI
(``python -m manage_devices``) can approve an IP or disable enforcement without
touching the database by hand.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from core.client_ip import is_internal_service
from core.config import settings
from models import DeviceStatus, SecuritySetting, TrustedDevice

# The admin's on/off lives in security_settings under this key. The env
# ``device_firewall_kill`` is a hard override on top of it (break-glass).
_ACTIVE_KEY = "device_firewall_active"


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
    return enforcement_active(db)


def _now() -> datetime:
    return datetime.now(UTC)


def has_any_approved(db: Session) -> bool:
    return (
        db.query(TrustedDevice)
        .filter(TrustedDevice.status == DeviceStatus.approved)
        .first()
        is not None
    )


def get_device(db: Session, ip: str) -> TrustedDevice | None:
    return (
        db.query(TrustedDevice).filter(TrustedDevice.ip_address == ip).first()
    )


# touch() write-load and table-growth caps. A port scanner hammering the API
# must not translate into one DB write per blocked request, nor into an
# unbounded pile of pending rows.
TOUCH_THROTTLE_SECONDS = 30.0
MAX_PENDING_DEVICES = 200
_recent_touches: dict[str, float] = {}  # ip -> monotonic ts of last DB write


def touch(db: Session, ip: str, user_agent: str | None = None) -> TrustedDevice | None:
    """Record that ``ip`` was seen; create it as pending if new.

    Throttled: repeat sightings of the same IP within ``TOUCH_THROTTLE_SECONDS``
    skip the DB write (returns None). New pending rows are capped at
    ``MAX_PENDING_DEVICES`` by evicting the least-recently-seen pending row, so
    a scanner sweeping addresses can't inflate the table.
    """
    import time as _time

    now_m = _time.monotonic()
    last = _recent_touches.get(ip)
    if last is not None and now_m - last < TOUCH_THROTTLE_SECONDS:
        return None
    if len(_recent_touches) > 4096:  # bound the throttle map itself
        cutoff = now_m - TOUCH_THROTTLE_SECONDS
        for k in [k for k, v in _recent_touches.items() if v < cutoff]:
            _recent_touches.pop(k, None)
    _recent_touches[ip] = now_m

    dev = get_device(db, ip)
    if dev is None:
        pending = (
            db.query(TrustedDevice)
            .filter(TrustedDevice.status == DeviceStatus.pending)
            .count()
        )
        if pending >= MAX_PENDING_DEVICES:
            evict = (
                db.query(TrustedDevice)
                .filter(TrustedDevice.status == DeviceStatus.pending)
                .order_by(TrustedDevice.last_seen.asc())
                .first()
            )
            if evict is not None:
                db.delete(evict)
        dev = TrustedDevice(
            ip_address=ip,
            status=DeviceStatus.pending,
            user_agent=(user_agent or "")[:400] or None,
            attempt_count=1,
        )
        db.add(dev)
    else:
        dev.last_seen = _now()
        dev.attempt_count = (dev.attempt_count or 0) + 1
        if user_agent:
            dev.user_agent = user_agent[:400]
    db.commit()
    db.refresh(dev)
    return dev


def is_allowed(db: Session, ip: str) -> bool:
    """Whether a request from ``ip`` may proceed.

    Fail-open on any registry error (a DB hiccup must never lock every admin
    out — see feature.md); the caller logs the failure.
    """
    if not enforcement_active(db):
        return True
    if is_internal_service(ip):
        return True
    try:
        dev = get_device(db, ip)
    except Exception:
        return True  # fail-open
    return bool(dev and dev.status == DeviceStatus.approved)


def register_authenticated_device(
    db: Session, ip: str, user_agent: str | None, user_id: int | None
) -> TrustedDevice:
    """Called on a successful login.

    Auto-approves the first device on a fresh install; otherwise ensures the
    device is recorded (pending, unless already approved/blocked) so an admin
    can act on it. Never downgrades an already-approved device.
    """
    dev = get_device(db, ip)
    first_ever = not has_any_approved(db)

    if dev is None:
        dev = TrustedDevice(
            ip_address=ip,
            user_agent=(user_agent or "")[:400] or None,
            attempt_count=1,
        )
        db.add(dev)

    dev.last_seen = _now()
    if user_agent:
        dev.user_agent = user_agent[:400]

    if first_ever and dev.status != DeviceStatus.blocked:
        dev.status = DeviceStatus.approved
        dev.auto_enrolled = True
        dev.approved_at = _now()
        dev.label = dev.label or "First device (auto-enrolled)"
    elif dev.status is None:
        dev.status = DeviceStatus.pending

    db.commit()
    db.refresh(dev)
    return dev


def approve(
    db: Session, ip: str, *, user_id: int | None = None, label: str | None = None
) -> TrustedDevice:
    dev = get_device(db, ip)
    if dev is None:
        dev = TrustedDevice(ip_address=ip, attempt_count=0)
        db.add(dev)
    dev.status = DeviceStatus.approved
    dev.approved_by = user_id
    dev.approved_at = _now()
    if label is not None:
        dev.label = label
    db.commit()
    db.refresh(dev)
    return dev


def block(db: Session, ip: str) -> TrustedDevice:
    dev = get_device(db, ip)
    if dev is None:
        dev = TrustedDevice(ip_address=ip, attempt_count=0)
        db.add(dev)
    dev.status = DeviceStatus.blocked
    db.commit()
    db.refresh(dev)
    return dev


def delete(db: Session, ip: str) -> bool:
    dev = get_device(db, ip)
    if dev is None:
        return False
    db.delete(dev)
    db.commit()
    return True


def list_devices(db: Session) -> list[TrustedDevice]:
    return (
        db.query(TrustedDevice)
        .order_by(TrustedDevice.status.asc(), TrustedDevice.last_seen.desc())
        .all()
    )
