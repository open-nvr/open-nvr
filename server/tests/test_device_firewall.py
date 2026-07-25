# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
Device-firewall tests — the app-layer access control (trust-on-first-use).

Focus on the lockout-safety invariants: first device auto-approved, loopback and
internal services never blocked, env break-glass forces off, and the
X-Forwarded-For trust decision (the classic spoofing bypass).
"""

from __future__ import annotations

import os
import secrets
import sys
import types as _types
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

os.environ.setdefault("DATABASE_URL", "sqlite:///./_fw_test.db")
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

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from core.config import settings  # noqa: E402
from core.database import Base  # noqa: E402
from services import device_firewall_service as dfw  # noqa: E402


@pytest.fixture
def db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    make_session = sessionmaker(bind=eng)
    s = make_session()
    dfw.set_enforcement(s, True)  # enable so is_allowed actually gates
    try:
        yield s
    finally:
        s.close()


# --- enrollment (trust-on-first-use) ---


def test_first_device_auto_approved(db):
    dev = dfw.register_authenticated_device(db, "10.0.0.10", "UA", 1)
    assert dev.status.value == "approved"
    assert dev.auto_enrolled is True
    assert dfw.is_allowed(db, "10.0.0.10") is True


def test_second_device_pending_and_blocked(db):
    dfw.register_authenticated_device(db, "10.0.0.10", "UA", 1)  # first
    second = dfw.register_authenticated_device(db, "10.0.0.20", "UA", 1)
    assert second.status.value == "pending"
    assert dfw.is_allowed(db, "10.0.0.20") is False


def test_never_downgrades_approved_device(db):
    dfw.register_authenticated_device(db, "10.0.0.10", "UA", 1)
    dfw.register_authenticated_device(db, "10.0.0.20", "UA", 1)
    dfw.approve(db, "10.0.0.20")
    # a later login must not knock it back to pending
    again = dfw.register_authenticated_device(db, "10.0.0.20", "UA", 1)
    assert again.status.value == "approved"


# --- lockout safety ---


def test_loopback_always_allowed_even_when_blocked(db):
    dfw.register_authenticated_device(db, "10.0.0.10", "UA", 1)  # someone else first
    dfw.block(db, "127.0.0.1")  # explicitly try to block loopback
    assert dfw.is_allowed(db, "127.0.0.1") is True


def test_internal_service_always_allowed(db):
    dfw.register_authenticated_device(db, "10.0.0.10", "UA", 1)
    # Docker bridge sibling (MediaMTX/KAI-C) — must never be gated
    assert dfw.is_allowed(db, "172.18.0.5") is True


def test_env_kill_forces_off(db, monkeypatch):
    dfw.register_authenticated_device(db, "10.0.0.10", "UA", 1)
    blocked = dfw.register_authenticated_device(db, "10.0.0.20", "UA", 1)
    assert dfw.is_allowed(db, "10.0.0.20") is False
    monkeypatch.setattr(settings, "device_firewall_kill", True)
    assert dfw.is_allowed(db, "10.0.0.20") is True  # break-glass
    assert blocked.status.value == "pending"  # state unchanged, just not enforced


def test_disabled_allows_everyone(db):
    dfw.set_enforcement(db, False)
    assert dfw.is_allowed(db, "203.0.113.9") is True


def test_admin_block_then_approve(db):
    dfw.register_authenticated_device(db, "10.0.0.10", "UA", 1)
    dfw.block(db, "10.0.0.99")
    assert dfw.is_allowed(db, "10.0.0.99") is False
    dfw.approve(db, "10.0.0.99", user_id=1)
    assert dfw.is_allowed(db, "10.0.0.99") is True


# --- touch() growth caps (scanner resilience) ---


def test_touch_throttles_repeat_writes(db, monkeypatch):
    dfw._recent_touches.clear()
    d1 = dfw.touch(db, "10.1.0.1", "UA")
    assert d1 is not None and d1.attempt_count == 1
    # Same IP hammering again inside the window -> no DB write.
    assert dfw.touch(db, "10.1.0.1", "UA") is None
    # Past the window it records again.
    monkeypatch.setattr(dfw, "TOUCH_THROTTLE_SECONDS", 0.0)
    d2 = dfw.touch(db, "10.1.0.1", "UA")
    assert d2 is not None and d2.attempt_count == 2


def test_touch_caps_pending_rows(db, monkeypatch):
    from models import DeviceStatus, TrustedDevice

    dfw._recent_touches.clear()
    monkeypatch.setattr(dfw, "MAX_PENDING_DEVICES", 5)
    for i in range(9):  # a scanner sweeping distinct source IPs
        dfw.touch(db, f"10.2.0.{i}", "scanner")
    pending = (
        db.query(TrustedDevice)
        .filter(TrustedDevice.status == DeviceStatus.pending)
        .count()
    )
    assert pending <= 5


def test_touch_cap_never_evicts_approved_or_blocked(db, monkeypatch):
    from models import TrustedDevice

    dfw._recent_touches.clear()
    monkeypatch.setattr(dfw, "MAX_PENDING_DEVICES", 2)
    dfw.approve(db, "10.3.0.1")
    dfw.block(db, "10.3.0.2")
    for i in range(6):
        dfw.touch(db, f"10.4.0.{i}", "scanner")
    statuses = {d.ip_address: d.status.value for d in db.query(TrustedDevice).all()}
    assert statuses["10.3.0.1"] == "approved"
    assert statuses["10.3.0.2"] == "blocked"


# --- X-Forwarded-For trust (the spoofing bypass) ---


class _FakeReq:
    def __init__(self, peer, headers):
        self.client = type("C", (), {"host": peer})()
        self.headers = headers


def test_xff_trusted_from_proxy(monkeypatch):
    from core import client_ip

    client_ip._trusted_proxy_nets.cache_clear()
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "172.16.0.0/12")
    req = _FakeReq("172.20.0.2", {"x-forwarded-for": "203.0.113.7, 172.20.0.2"})
    assert client_ip.get_client_ip(req) == "203.0.113.7"
    client_ip._trusted_proxy_nets.cache_clear()


def test_xff_ignored_from_untrusted_peer(monkeypatch):
    from core import client_ip

    client_ip._trusted_proxy_nets.cache_clear()
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "172.16.0.0/12")
    # A direct client sending a spoofed XFF must NOT be believed
    req = _FakeReq("203.0.113.50", {"x-forwarded-for": "10.0.0.10"})
    assert client_ip.get_client_ip(req) == "203.0.113.50"
    client_ip._trusted_proxy_nets.cache_clear()
