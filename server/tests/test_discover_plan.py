# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
Tests for _resolve_scan_plan() — the shared CIDR-resolution behind
GET /discover and GET /discover/plan (override → configured → auto-detected).

    cd server && pytest tests/test_discover_plan.py -v
"""

from __future__ import annotations

import os
import secrets
import sys
import types as _types
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

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

import routers.onvif as onvif  # noqa: E402

_DB = object()  # _resolve_scan_plan only forwards this to the stubbed lookups


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(onvif, "get_camera_lan_subnets", lambda db: [])
    monkeypatch.setattr(onvif, "detect_local_subnets", lambda: [])


def test_override_wins_over_configured(monkeypatch):
    monkeypatch.setattr(onvif, "get_camera_lan_subnets", lambda db: ["10.1.0.0/24"])
    cidrs, source = onvif._resolve_scan_plan(_DB, ["192.168.1.0/24"])
    assert cidrs == ["192.168.1.0/24"]
    assert source == "override"


def test_override_rejects_invalid_cidr():
    with pytest.raises(HTTPException) as exc:
        onvif._resolve_scan_plan(_DB, ["not-a-cidr"])
    assert exc.value.status_code == 400


def test_override_rejects_public_range():
    with pytest.raises(HTTPException) as exc:
        onvif._resolve_scan_plan(_DB, ["8.8.8.0/24"])
    assert exc.value.status_code == 403


def test_override_rejects_too_large_range():
    with pytest.raises(HTTPException) as exc:
        onvif._resolve_scan_plan(_DB, ["10.0.0.0/8"])
    assert exc.value.status_code == 400


def test_configured_wins_over_autodetect(monkeypatch):
    monkeypatch.setattr(onvif, "get_camera_lan_subnets", lambda db: ["10.1.0.0/24"])
    monkeypatch.setattr(onvif, "detect_local_subnets", lambda: ["192.168.1.0/24"])
    cidrs, source = onvif._resolve_scan_plan(_DB, None)
    assert cidrs == ["10.1.0.0/24"]
    assert source == "configured"


def test_autodetect_when_nothing_configured(monkeypatch):
    monkeypatch.setattr(onvif, "detect_local_subnets", lambda: ["192.168.1.0/24"])
    cidrs, source = onvif._resolve_scan_plan(_DB, None)
    assert cidrs == ["192.168.1.0/24"]
    assert source == "auto-detected"


def test_empty_autodetect_returns_empty_without_raising():
    cidrs, source = onvif._resolve_scan_plan(_DB, None)
    assert cidrs == []
    assert source == "auto-detected"


def test_invalid_configured_cidrs_dropped_not_fatal(monkeypatch):
    monkeypatch.setattr(
        onvif, "get_camera_lan_subnets", lambda db: ["garbage", "10.1.0.0/24"]
    )
    cidrs, source = onvif._resolve_scan_plan(_DB, None)
    assert cidrs == ["10.1.0.0/24"]
    assert source == "configured"


@pytest.mark.asyncio
async def test_discover_stops_scan_on_disconnect(monkeypatch):
    """The Stop button only drops the HTTP connection; the disconnect watcher
    must turn that into an actual sweep cancellation."""
    import asyncio

    monkeypatch.setattr(onvif, "_DISCONNECT_POLL_S", 0.01)

    class _Req:
        async def is_disconnected(self):
            return True

    scan = asyncio.create_task(asyncio.Event().wait())  # never finishes
    with pytest.raises(HTTPException) as exc:
        await onvif._await_scan_or_disconnect(scan, _Req())
    assert exc.value.status_code == 409
    assert scan.cancelled()


@pytest.mark.asyncio
async def test_discover_returns_result_when_client_stays(monkeypatch):
    import asyncio

    monkeypatch.setattr(onvif, "_DISCONNECT_POLL_S", 0.01)

    class _Req:
        async def is_disconnected(self):
            return False

    async def _scan():
        await asyncio.sleep(0.03)
        return [{"ip": "10.0.0.9"}]

    scan = asyncio.create_task(_scan())
    devices = await onvif._await_scan_or_disconnect(scan, _Req())
    assert devices == [{"ip": "10.0.0.9"}]
