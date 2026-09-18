# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""GET /system/info and the site-settings helper (HA-104).

Pinned here:

* the site id is created once and then stable (Home Assistant keys its config
  entry on it, so a new id would look like a different NVR);
* recording pause is OFF unless an admin stored exactly ``true``;
* the update check is off by default (offline-first: no outbound call), and
  when on it is cached and never lets a network failure break the endpoint;
* the endpoint reports the contract version and features clients rely on.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("SECRET_KEY", "s" * 64)
os.environ.setdefault("INTERNAL_API_KEY", "x" * 48)
os.environ.setdefault("MEDIAMTX_SECRET", "m" * 48)

import models  # noqa: E402
from core.config import settings  # noqa: E402
from services import site_settings, update_check  # noqa: E402


@pytest.fixture()
def db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


def test_site_id_is_created_once_and_stable(db):
    first = site_settings.get_site_id(db)
    assert len(first) == 36
    assert site_settings.get_site_id(db) == first
    assert site_settings.get_json(db, site_settings.SITE_ID_KEY) == first


def test_site_name_defaults_and_can_be_set(db):
    assert site_settings.get_site_name(db) == "OpenNVR"
    site_settings.set_json(db, site_settings.SITE_NAME_KEY, "Showroom")
    assert site_settings.get_site_name(db) == "Showroom"
    site_settings.set_json(db, site_settings.SITE_NAME_KEY, "   ")
    assert site_settings.get_site_name(db) == "OpenNVR"


def test_recording_pause_is_off_unless_exactly_true(db):
    assert site_settings.recording_pause_enabled(db) is False
    for loose in ("true", 1, "yes", {"on": True}):
        site_settings.set_json(db, site_settings.RECORDING_PAUSE_KEY, loose)
        assert site_settings.recording_pause_enabled(db) is False
    site_settings.set_json(db, site_settings.RECORDING_PAUSE_KEY, True)
    assert site_settings.recording_pause_enabled(db) is True


def test_keys_are_bounded_by_the_column(db):
    with pytest.raises(ValueError):
        site_settings.get_json(db, "k" * 51)
    with pytest.raises(ValueError):
        site_settings.set_json(db, "", 1)


def test_unreadable_value_reads_as_default(db):
    db.add(models.SecuritySetting(key="broken", json_value="{not json"))
    db.commit()
    assert site_settings.get_json(db, "broken", "dflt") == "dflt"


# ── update check ─────────────────────────────────────────────────────────


@pytest.fixture()
def fresh_update_cache(monkeypatch):
    update_check._reset_cache_for_tests()
    yield monkeypatch
    update_check._reset_cache_for_tests()


def test_update_check_is_off_by_default_and_makes_no_call(fresh_update_cache):
    fresh_update_cache.setattr(settings, "update_check", False)

    def _no_network(*a, **k):
        raise AssertionError("an outbound call was made while disabled")

    fresh_update_cache.setattr(update_check.httpx, "AsyncClient", _no_network)
    assert asyncio.run(update_check.latest_version()) is None


class _FakeClient:
    calls = 0

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        _FakeClient.calls += 1
        return type("R", (), {"status_code": 200,
                              "json": lambda self: {"tag_name": "v0.2.0"}})()


def test_update_check_when_enabled_is_cached(fresh_update_cache):
    fresh_update_cache.setattr(settings, "update_check", True)
    fresh_update_cache.setattr(update_check.httpx, "AsyncClient", _FakeClient)
    _FakeClient.calls = 0
    assert asyncio.run(update_check.latest_version()) == "0.2.0"
    assert asyncio.run(update_check.latest_version()) == "0.2.0"
    assert _FakeClient.calls == 1


def test_update_check_failure_reads_as_unknown(fresh_update_cache):
    fresh_update_cache.setattr(settings, "update_check", True)

    class _Down(_FakeClient):
        async def get(self, url, headers=None):
            raise OSError("network unreachable")

    fresh_update_cache.setattr(update_check.httpx, "AsyncClient", _Down)
    assert asyncio.run(update_check.latest_version()) is None


# ── the endpoint ─────────────────────────────────────────────────────────


def test_system_info_payload(db, fresh_update_cache):
    import importlib

    from core.contract import CONTRACT_VERSION

    # routers/__init__ re-exports the ROUTER as `system`; take the module.
    system_router = importlib.import_module("routers.system")

    fresh_update_cache.setattr(settings, "update_check", False)
    user = object()
    info = asyncio.run(system_router.get_system_info(db=db, current_user=user))

    assert info["site_id"] == site_settings.get_site_id(db)
    assert info["name"] == "OpenNVR"
    assert info["contract_version"] == CONTRACT_VERSION
    assert {"correlation_id", "ws_device_firewall", "system_info"} <= set(info["features"])
    assert info["recording_pause_enabled"] is False
    assert info["latest_version"] is None
    assert isinstance(info["uptime_s"], int) and info["uptime_s"] >= 0
    assert info["version"]
