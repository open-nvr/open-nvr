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


# --- enrollment (trust-on-first-use, identified by device token) ---


def _login(db, token=None, ip="10.0.0.10", ua="UA", user_id=1):
    """Simulate a successful login from a browser presenting ``token``."""
    return dfw.register_authenticated_browser(db, token, ip, ua, user_id)


def test_first_browser_auto_approved(db):
    dev, issued = _login(db)
    assert issued  # a fresh token was minted for the cookie
    assert dev.status.value == "approved"
    assert dev.auto_enrolled is True
    assert dfw.is_allowed_browser(db, issued) is True


def test_second_browser_pending_and_blocked(db):
    _, first = _login(db)  # first browser -> approved
    second, second_token = _login(db, ua="Other UA")
    assert second_token and second_token != first
    assert second.status.value == "pending"
    assert dfw.is_allowed_browser(db, second_token) is False
    assert dfw.is_allowed_browser(db, first) is True


def test_same_ip_different_browsers_are_distinct(db):
    """The NAT bug: two devices sharing one address (Docker Desktop makes every
    LAN client appear as the bridge gateway) must NOT share an approval."""
    _, phone_free = _login(db, ip="172.28.0.1")  # first -> approved
    laptop, laptop_token = _login(db, ip="172.28.0.1", ua="Another")
    assert laptop.status.value == "pending"
    assert dfw.is_allowed_browser(db, laptop_token) is False
    assert dfw.is_allowed_browser(db, phone_free) is True


def test_known_token_is_not_reissued_and_survives_logout(db):
    """Logging out must never cost an approval: the device cookie outlives the
    session, so the next login re-presents the SAME token and keeps its row."""
    dev, issued = _login(db)
    again, reissued = _login(db, token=issued)  # log out, log back in
    assert reissued is None  # no new cookie — the approval is preserved
    assert again.id == dev.id
    assert again.status.value == "approved"
    assert again.attempt_count == 2
    assert dfw.is_allowed_browser(db, issued) is True


def test_unknown_token_is_not_approved(db):
    _login(db)  # an approved browser exists
    assert dfw.is_allowed_browser(db, "forged-token-value") is False
    assert dfw.is_allowed_browser(db, None) is False


def test_only_the_hash_is_stored(db):
    """A database leak must not hand out approved-device access."""
    dev, issued = _login(db)
    assert dev.device_token_hash != issued
    assert dev.device_token_hash == dfw.hash_device_token(issued)
    assert len(dev.device_token_hash) == 64


def test_login_updates_ip_metadata_only(db):
    dev, issued = _login(db, ip="192.168.1.50")
    assert dev.ip_address == "192.168.1.50"
    # roaming to a new network keeps the SAME approved device (no DHCP churn)
    same, reissued = _login(db, token=issued, ip="10.9.9.9")
    assert reissued is None
    assert same.id == dev.id and same.status.value == "approved"
    assert same.ip_address == "10.9.9.9"


def test_never_downgrades_approved_browser(db):
    _login(db)  # first
    second, token = _login(db, ua="Other")
    dfw.approve(db, second.id)
    again, _ = _login(db, token=token)  # a later login must not undo approval
    assert again.status.value == "approved"


def test_blocked_browser_is_never_auto_approved(db):
    dev, token = _login(db)
    dfw.block(db, dev.id)
    # even as the only device (has_any_approved False), a block must hold
    again, _ = _login(db, token=token)
    assert again.status.value == "blocked"
    assert dfw.is_allowed_browser(db, token) is False


# --- admin actions address devices by id ---


def test_admin_approve_block_delete_by_id(db):
    _login(db)
    dev, token = _login(db, ua="Other")
    assert dfw.is_allowed_browser(db, token) is False
    dfw.approve(db, dev.id, user_id=1, label="Front desk")
    assert dfw.is_allowed_browser(db, token) is True
    dfw.block(db, dev.id)
    assert dfw.is_allowed_browser(db, token) is False
    assert dfw.delete(db, dev.id) is True
    assert dfw.delete(db, dev.id) is False  # already gone
    assert dfw.approve(db, 99999) is None  # unknown id
    assert dfw.block(db, 99999) is None


# --- lockout safety ---


def test_legacy_ip_rows_do_not_block_bootstrap(db):
    """Rows migrated from the IP era (no token) must not count as approved, or
    a fresh install could never enroll its first browser."""
    from models import DeviceStatus, TrustedDevice

    db.add(
        TrustedDevice(
            ip_address="10.0.0.99", status=DeviceStatus.approved, attempt_count=1
        )
    )
    db.commit()
    assert dfw.has_any_approved(db) is False
    dev, issued = _login(db)
    assert dev.status.value == "approved"  # bootstrap still works
    assert dfw.is_allowed_browser(db, issued) is True


def test_enforcement_off_allows_unknown_browser(db):
    _login(db)
    dfw.set_enforcement(db, False)
    assert dfw.is_allowed_browser(db, "no-such-token") is True


def test_registry_error_fails_open(db, monkeypatch):
    """A DB hiccup must never lock every admin out."""
    _login(db)

    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(dfw, "get_device_by_token", boom)
    assert dfw.is_allowed_browser(db, "anything") is True


# --- trust-zone configuration (X-Forwarded-For / internal CIDRs) ---


def test_trust_cidr_defaults_are_narrow():
    for value in (settings.trusted_proxy_cidrs, settings.internal_service_cidrs):
        assert "172.16.0.0/12" not in value
        assert "172.28.0.0/16" in value


def test_full_range_trust_cidr_rejected():
    from pydantic import ValidationError

    from core.config import Settings

    with pytest.raises(ValidationError):
        Settings(trusted_proxy_cidrs="0.0.0.0/0")
    with pytest.raises(ValidationError):
        Settings(internal_service_cidrs="::/0")
    with pytest.raises(ValidationError):
        Settings(trusted_proxy_cidrs="not-a-cidr")


def test_broad_trust_cidr_warns_loudly(capsys):
    from core.config import Settings

    Settings(trusted_proxy_cidrs="127.0.0.1/32,172.16.0.0/12")
    assert "SECURITY WARNING" in capsys.readouterr().err


def test_env_kill_forces_off(db, monkeypatch):
    _login(db)  # first browser -> approved
    blocked, token = _login(db, ua="Other")
    assert dfw.is_allowed_browser(db, token) is False
    monkeypatch.setattr(settings, "device_firewall_kill", True)
    assert dfw.is_allowed_browser(db, token) is True  # break-glass
    assert blocked.status.value == "pending"  # state unchanged, just not enforced


def test_no_rows_created_by_unauthenticated_traffic(db):
    """Enrollment happens only at authenticated login, so a scanner hammering
    the API can no longer inflate the table (nor cost a write per request)."""
    from models import TrustedDevice

    before = db.query(TrustedDevice).count()
    for _ in range(50):
        assert dfw.is_allowed_browser(db, "scanner-cookie") is False
    assert db.query(TrustedDevice).count() == before


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
