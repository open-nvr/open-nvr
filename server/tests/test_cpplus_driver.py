# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
CP Plus driver tests — thin: identity/inheritance, matches/probe, and priority
ordering. The behavior itself is Dahua's and is covered by test_dahua_driver.py.
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

from services.camera_drivers import cpplus, dahua, hikvision  # noqa: E402
from services.camera_drivers.cpplus.driver import CpPlusDriver  # noqa: E402
from services.camera_drivers.dahua.driver import DahuaCgiDriver  # noqa: E402
from services.camera_drivers.registry import (  # noqa: E402
    get_vendors,
    select_driver_class,
)


def test_cpplus_is_dahua_subclass():
    assert issubclass(CpPlusDriver, DahuaCgiDriver)
    assert CpPlusDriver.driver_name == "cpplus"


def test_cpplus_matches():
    for s in ("cp plus", "cp-plus", "cpplus"):
        assert cpplus.matches(s) is True
    assert cpplus.matches("hikvision") is False


def test_cpplus_reuses_dahua_probe():
    assert cpplus.probe is dahua.probe


def test_cpplus_selected_by_manufacturer():
    assert select_driver_class("CP Plus").__name__ == "CpPlusDriver"


def test_priority_order():
    prio = {v.name: v.priority for v in get_vendors()}
    # Specific brand (cpplus) must be tried before the OEM it rebadges (dahua),
    # and a genuine Hikvision string still wins over both.
    assert prio["hikvision"] < prio["cpplus"] < prio["dahua"]


def test_cpplus_has_no_destructive_methods():
    for name in ("set_network", "set_ip", "factory_reset"):
        assert not hasattr(CpPlusDriver, name)


def test_probe_module_imported_for_coverage():
    assert hikvision.matches("hikvision") is True
