# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
Registry tests for the modular camera-driver layer.

Two concerns:
* the discovery *contract* — every vendor package that ships must expose a
  well-formed DRIVER / matches / PRIORITY (a malformed third-party package must
  fail here, in CI, not at runtime);
* the *selection algorithm* — manufacturer match + probe-confirm, the
  fingerprint pass for OEM rebadges, and the ONVIF fallback — exercised with
  synthetic vendors so the logic is tested independently of which real vendor
  packages happen to exist.
"""

from __future__ import annotations

import datetime as _dt

if not hasattr(_dt, "UTC"):
    _dt.UTC = _dt.timezone.utc  # noqa: UP017 - 3.10 sandbox polyfill

import os
import secrets
import sys
import types as _types
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
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

from services.camera_drivers import registry  # noqa: E402
from services.camera_drivers.base import (  # noqa: E402
    CameraDriver,
    Capabilities,
    DeviceInfo,
)
from services.camera_drivers.onvif.driver import OnvifDriver  # noqa: E402

# Methods that must NOT exist anywhere in the driver hierarchy — the structural
# guarantee that no driver can change a camera's IP or wipe it (lockout safety).
_FORBIDDEN = ("set_network", "set_ip", "factory_reset")


# ---------------------------------------------------------------------------
# Discovery contract (real, shipped vendor packages)
# ---------------------------------------------------------------------------


def test_vendors_discovered():
    vendors = registry.get_vendors()
    names = {v.name for v in vendors}
    # Hikvision ships today; ONVIF is the fallback and is excluded from the list.
    assert "hikvision" in names
    assert "onvif" not in names


def test_vendor_packages_honor_contract():
    for spec in registry.get_vendors():
        assert isinstance(spec.driver, type) and issubclass(spec.driver, CameraDriver)
        assert callable(spec.matches)
        assert isinstance(spec.priority, int)
        # matches() must accept a lowercased manufacturer string and return bool.
        assert isinstance(spec.matches("something"), bool)
        if spec.probe is not None:
            assert callable(spec.probe)


def test_vendors_priority_sorted():
    prios = [v.priority for v in registry.get_vendors()]
    assert prios == sorted(prios)


def test_no_destructive_methods_on_any_driver():
    classes = [OnvifDriver] + [v.driver for v in registry.get_vendors()]
    for cls in classes:
        for name in _FORBIDDEN:
            assert not hasattr(cls, name), f"{cls.__name__} exposes {name}"


def test_select_driver_class_string_only():
    assert registry.select_driver_class("HIKVISION").__name__ == "HikvisionIsapiDriver"
    assert registry.select_driver_class("Totally Unknown").__name__ == "OnvifDriver"
    assert registry.select_driver_class(None).__name__ == "OnvifDriver"


# ---------------------------------------------------------------------------
# Selection algorithm (synthetic vendors — logic isolated from real packages)
# ---------------------------------------------------------------------------


class _FakeDriver(CameraDriver):
    driver_name = "fake"

    async def get_info(self) -> DeviceInfo:  # pragma: no cover - unused
        return DeviceInfo()

    async def get_capabilities(self) -> Capabilities:  # pragma: no cover
        return Capabilities()


def _mk_driver(name):
    return type(f"{name.title()}Driver", (_FakeDriver,), {"driver_name": name})


def _spec(name, matches_terms, probe_result, priority):
    async def _probe(ip, port, user, pw):
        return probe_result

    return registry.VendorSpec(
        name=name,
        driver=_mk_driver(name),
        matches=lambda m: any(t in m for t in matches_terms),
        probe=_probe,
        priority=priority,
    )


class _FakeCamera:
    def __init__(self, manufacturer=None):
        self.id = 1
        self.ip_address = "10.0.0.9"
        self.port = 80
        self.username = "admin"
        self.password = "pw"
        self.manufacturer = manufacturer


class _NoRow:
    """A db.query(...).filter(...).first() that always returns None (no cached
    capability row → always take the fresh-detection path)."""

    def query(self, *a):
        return self

    def filter(self, *a):
        return self

    def first(self):
        return None


@pytest.fixture
def _isolate(monkeypatch):
    """Neutralize network + per-process caches for selection tests."""
    registry._DRIVER_CACHE.clear()

    async def _endpoint(cid, ip, onvif_port=None, control_scheme=None):
        return ("http", 80)

    monkeypatch.setattr(registry, "resolve_endpoint", _endpoint)
    yield monkeypatch
    registry._DRIVER_CACHE.clear()


def _set_manufacturer(monkeypatch, value):
    async def _get_info(self):
        if value is None:
            raise RuntimeError("device unreachable")
        return DeviceInfo(manufacturer=value)

    monkeypatch.setattr(OnvifDriver, "get_info", _get_info)


CREDS = dict(camera_id=1, ip="10.0.0.9", username="admin", password="pw", http_port=80)


@pytest.mark.asyncio
async def test_manufacturer_match_confirmed_by_probe(_isolate):
    vendors = [
        _spec("hik", ["hik"], probe_result=True, priority=10),
        _spec("dahua", ["dahua"], probe_result=True, priority=20),
    ]
    _isolate.setattr(registry, "get_vendors", lambda: vendors)
    _set_manufacturer(_isolate, "Dahua Technology")
    cls = await registry._select_driver_cls(
        _NoRow(), _FakeCamera("Dahua Technology"), CREDS, force=True
    )
    assert cls.driver_name == "dahua"


@pytest.mark.asyncio
async def test_string_match_but_probe_fails_falls_through_to_fingerprint(_isolate):
    # CP-Plus-branded unit with Hikvision internals: matches cpplus by string but
    # its Dahua probe fails; the fingerprint pass finds the real ISAPI vendor.
    vendors = [
        _spec("hik", ["hik"], probe_result=True, priority=10),
        _spec("cpplus", ["cp plus", "cpplus"], probe_result=False, priority=15),
    ]
    _isolate.setattr(registry, "get_vendors", lambda: vendors)
    _set_manufacturer(_isolate, "CP Plus")
    cls = await registry._select_driver_cls(
        _NoRow(), _FakeCamera("CP Plus"), CREDS, force=True
    )
    assert cls.driver_name == "hik"


@pytest.mark.asyncio
async def test_unknown_manufacturer_identified_by_fingerprint(_isolate):
    # Secureye rebadge: manufacturer matches nothing; only the Dahua probe hits.
    vendors = [
        _spec("hik", ["hik"], probe_result=False, priority=10),
        _spec("dahua", ["dahua"], probe_result=True, priority=20),
    ]
    _isolate.setattr(registry, "get_vendors", lambda: vendors)
    _set_manufacturer(_isolate, "Secureye")
    cls = await registry._select_driver_cls(
        _NoRow(), _FakeCamera("Secureye"), CREDS, force=True
    )
    assert cls.driver_name == "dahua"


@pytest.mark.asyncio
async def test_all_probes_fail_falls_back_to_onvif(_isolate):
    vendors = [_spec("dahua", ["dahua"], probe_result=False, priority=20)]
    _isolate.setattr(registry, "get_vendors", lambda: vendors)
    _set_manufacturer(_isolate, "Acme ONVIF Cam")
    cls = await registry._select_driver_cls(
        _NoRow(), _FakeCamera("Acme"), CREDS, force=True
    )
    assert cls is OnvifDriver


@pytest.mark.asyncio
async def test_unreachable_device_not_cached(_isolate):
    vendors = [_spec("dahua", ["dahua"], probe_result=False, priority=20)]
    _isolate.setattr(registry, "get_vendors", lambda: vendors)
    _set_manufacturer(_isolate, None)  # get_info raises, no stored manufacturer
    cls = await registry._select_driver_cls(
        _NoRow(), _FakeCamera(None), CREDS, force=True
    )
    assert cls is OnvifDriver
    assert 1 not in registry._DRIVER_CACHE  # transient outage must not pin ONVIF


@pytest.mark.asyncio
async def test_persisted_row_short_circuits_without_probing(_isolate):
    # A prior 'ok' probe stored driver_name='hikvision' → no network at all.
    class _Row:
        probe_result = "ok"
        driver_name = "hikvision"

    class _CachedDb(_NoRow):
        def first(self):
            return _Row()

    async def _boom(self):
        raise AssertionError("get_info must not be called on a cache hit")

    _isolate.setattr(OnvifDriver, "get_info", _boom)
    cls = await registry._select_driver_cls(
        _CachedDb(), _FakeCamera("whatever"), CREDS, force=False
    )
    assert cls.__name__ == "HikvisionIsapiDriver"
