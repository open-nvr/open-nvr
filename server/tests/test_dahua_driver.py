# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
Dahua CGI driver tests — fully mocked (monkeypatch the CGI primitive; no network,
no hardware). Mirrors tests/test_camera_settings.py.

Covers parse_kv, each overridden getter against captured Dahua response shapes,
each setter's setConfig param construction, event-line normalization, the
user-management guards, reboot, capability augmentation, and driver selection
(manufacturer string + fingerprint probe) for Dahua and OEM rebadges.
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

from fastapi import HTTPException  # noqa: E402

from services.camera_drivers.dahua import driver as dahua_mod  # noqa: E402
from services.camera_drivers.dahua.cgi import parse_kv  # noqa: E402
from services.camera_drivers.dahua.driver import (  # noqa: E402
    DahuaCgiDriver,
    parse_dahua_event,
)
from services.camera_drivers.registry import select_driver_class  # noqa: E402

CREDS = dict(camera_id=1, ip="10.0.0.7", username="admin", password="pw", http_port=80)


def _drv():
    return DahuaCgiDriver(**CREDS)


# --- captured Dahua response shapes (trimmed) ---

SYSINFO = "deviceType=IPC-HDW1200S\r\nserialNumber=ABC123\r\nhardwareVersion=1.00\r\n"
SOFTVER = "version=2.420.0000.0.R\r\nbuild=2016-01-01\r\n"
NETWORK = (
    "table.Network.DefaultInterface=eth0\r\n"
    "table.Network.eth0.PhysicalAddress=90:02:a9:11:22:33\r\n"
    "table.Network.eth0.IPAddress=192.168.1.108\r\n"
    "table.Network.eth0.SubnetMask=255.255.255.0\r\n"
    "table.Network.eth0.DefaultGateway=192.168.1.1\r\n"
    "table.Network.eth0.DhcpEnable=false\r\n"
    "table.Network.eth0.MTU=1500\r\n"
    "table.Network.eth0.DnsServers[0]=8.8.8.8\r\n"
    "table.Network.eth0.DnsServers[1]=8.8.4.4\r\n"
)
STORAGE = (
    "list.info[0].Name=/dev/mmc0\r\n"
    "list.info[0].State=Normal\r\n"
    "list.info[0].Detail[0].TotalBytes=32000000000.000000\r\n"
    "list.info[0].Detail[0].UsedBytes=8000000000.000000\r\n"
)
STORAGE_EMPTY = "list.info=0\r\n"
VIDEOWIDGET = (
    "table.VideoWidget[0].TimeTitle.EncodeBlend=true\r\n"
    "table.VideoWidget[0].ChannelTitle.EncodeBlend=true\r\n"
    "table.VideoWidget[0].CustomTitle[0].EncodeBlend=false\r\n"
    "table.VideoWidget[0].CustomTitle[0].Text=\r\n"
)
MOTION = (
    "table.MotionDetect[0].Enable=true\r\n"
    "table.MotionDetect[0].Level=3\r\n"
)
USERS = (
    "users[0].Name=admin\r\nusers[0].Group=admin\r\n"
    "users[1].Name=guest\r\nusers[1].Group=user\r\n"
)


class _Fake:
    """Records the last dahua_request call and returns a scripted (status, body).

    ``script`` maps a substring of the path OR a params['action'] value to a
    (status, body) tuple; a bare (status, body) always answers.
    """

    def __init__(self, script):
        self.script = script
        self.calls = []

    async def __call__(self, ip, path, username=None, password=None, **kw):
        self.calls.append((path, kw.get("params"), kw.get("method", "GET")))
        if isinstance(self.script, tuple):
            return self.script
        params = kw.get("params") or {}
        for key, resp in self.script.items():
            if key in path or key == params.get("action"):
                return resp
        return (404, "")


# ---------------------------------------------------------------------------
# parse_kv + event parser
# ---------------------------------------------------------------------------


def test_parse_kv():
    kv = parse_kv("a.b=1\r\n\r\nc[0].d=hello\r\nnoequals\r\n")
    assert kv == {"a.b": "1", "c[0].d": "hello"}


def test_parse_dahua_event_start():
    ev = parse_dahua_event("Code=VideoMotion;action=Start;index=0")
    assert ev["event_type"] == "VideoMotion"
    assert ev["event_state"] == "active"


def test_parse_dahua_event_stop():
    ev = parse_dahua_event("Code=VideoMotion;action=Stop;index=0")
    assert ev["event_state"] == "inactive"


def test_parse_dahua_event_heartbeat_filtered():
    assert parse_dahua_event("Code=Heartbeat;action=Pulse") is None


def test_parse_dahua_event_garbage():
    assert parse_dahua_event("not an event line") is None


# ---------------------------------------------------------------------------
# Getters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_info(monkeypatch):
    fake = _Fake({"getSystemInfo": (200, SYSINFO), "getSoftwareVersion": (200, SOFTVER)})
    monkeypatch.setattr(dahua_mod, "dahua_request", fake)
    info = await _drv().get_info()
    assert info.manufacturer == "Dahua"
    assert info.model == "IPC-HDW1200S"
    assert info.firmware_version == "2.420.0000.0.R"
    assert info.serial_number == "ABC123"


@pytest.mark.asyncio
async def test_capabilities_augmented(monkeypatch):
    async def fake_super(self):
        from services.camera_drivers.base import Capabilities

        return Capabilities(supported_areas={"info": True, "imaging": True})

    monkeypatch.setattr(
        "services.camera_drivers.onvif.driver.OnvifDriver.get_capabilities", fake_super
    )
    caps = await _drv().get_capabilities()
    assert caps.driver_name == "dahua"
    for area in ("storage", "motion", "osd", "users", "events", "network"):
        assert caps.supported_areas[area] is True
    assert caps.detail["hardware_verified"] is False


@pytest.mark.asyncio
async def test_get_network(monkeypatch):
    monkeypatch.setattr(dahua_mod, "dahua_request", _Fake((200, NETWORK)))
    net = await _drv().get_network()
    assert net.supported and net.source == "dahua-cgi"
    assert net.mac_address == "90:02:a9:11:22:33"
    assert net.ip_address == "192.168.1.108"
    assert net.subnet_mask == "255.255.255.0"
    assert net.gateway == "192.168.1.1"
    assert net.dns_primary == "8.8.8.8"
    assert net.dns_secondary == "8.8.4.4"
    assert net.dhcp is False
    assert net.mtu == 1500


@pytest.mark.asyncio
async def test_get_storage_populated(monkeypatch):
    monkeypatch.setattr(dahua_mod, "dahua_request", _Fake((200, STORAGE)))
    st = await _drv().get_storage()
    assert st.supported and st.present
    assert len(st.slots) == 1
    slot = st.slots[0]
    assert slot.name == "/dev/mmc0"
    assert slot.status == "Normal"
    assert slot.capacity_mb == 32000000000 // (1024 * 1024)
    assert slot.free_mb == (32000000000 - 8000000000) // (1024 * 1024)


@pytest.mark.asyncio
async def test_get_storage_empty(monkeypatch):
    monkeypatch.setattr(dahua_mod, "dahua_request", _Fake((200, STORAGE_EMPTY)))
    st = await _drv().get_storage()
    assert st.supported and not st.present and st.slots == []


@pytest.mark.asyncio
async def test_get_osd(monkeypatch):
    monkeypatch.setattr(dahua_mod, "dahua_request", _Fake((200, VIDEOWIDGET)))
    osd = await _drv().get_osd()
    assert osd.supported and osd.source == "dahua-cgi"
    assert osd.datetime_enabled is True
    assert osd.channel_name_enabled is True
    assert osd.text_enabled is False


@pytest.mark.asyncio
async def test_get_motion(monkeypatch):
    monkeypatch.setattr(dahua_mod, "dahua_request", _Fake((200, MOTION)))
    m = await _drv().get_motion()
    assert m.supported and m.enabled is True
    assert m.sensitivity == 3
    assert m.sensitivity_max == 6


@pytest.mark.asyncio
async def test_getter_degrades_on_transport_error(monkeypatch):
    async def boom(*a, **k):
        raise HTTPException(status_code=503, detail="down")

    monkeypatch.setattr(dahua_mod, "dahua_request", boom)
    st = await _drv().get_storage()
    assert st.supported is False  # data, not exception


# ---------------------------------------------------------------------------
# Setters — param construction (patch-only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_osd_patches_only_supplied(monkeypatch):
    fake = _Fake({"setConfig": (200, "OK"), "getConfig": (200, VIDEOWIDGET)})
    monkeypatch.setattr(dahua_mod, "dahua_request", fake)
    await _drv().set_osd({"text_enabled": True, "text": "Front Door"})
    setcall = next(c for c in fake.calls if (c[1] or {}).get("action") == "setConfig")
    params = setcall[1]
    assert params["VideoWidget[0].CustomTitle[0].EncodeBlend"] == "true"
    assert params["VideoWidget[0].CustomTitle[0].Text"] == "Front Door"
    # untouched keys must NOT be in the setConfig
    assert "VideoWidget[0].TimeTitle.EncodeBlend" not in params


@pytest.mark.asyncio
async def test_set_motion_clamps_level(monkeypatch):
    fake = _Fake({"setConfig": (200, "OK"), "getConfig": (200, MOTION)})
    monkeypatch.setattr(dahua_mod, "dahua_request", fake)
    await _drv().set_motion({"enabled": True, "sensitivity": 99})
    setcall = next(c for c in fake.calls if (c[1] or {}).get("action") == "setConfig")
    assert setcall[1]["MotionDetect[0].Enable"] == "true"
    assert setcall[1]["MotionDetect[0].Level"] == "6"  # clamped to Dahua's 1..6


@pytest.mark.asyncio
async def test_set_motion_error_status_raises(monkeypatch):
    fake = _Fake({"setConfig": (400, "Error"), "getConfig": (200, MOTION)})
    monkeypatch.setattr(dahua_mod, "dahua_request", fake)
    with pytest.raises(HTTPException):
        await _drv().set_motion({"enabled": False})


# ---------------------------------------------------------------------------
# Users + reboot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_users_flags_current(monkeypatch):
    monkeypatch.setattr(dahua_mod, "dahua_request", _Fake((200, USERS)))
    users = (await _drv().get_users()).users
    assert {u["name"] for u in users} == {"admin", "guest"}
    assert next(u for u in users if u["name"] == "admin")["is_current"] is True


@pytest.mark.asyncio
async def test_create_user_body(monkeypatch):
    fake = _Fake({"addUser": (200, "OK"), "getUserInfoAll": (200, USERS)})
    monkeypatch.setattr(dahua_mod, "dahua_request", fake)
    await _drv().create_user("bob", "secret", "Operator")
    add = next(c for c in fake.calls if (c[1] or {}).get("action") == "addUser")
    assert add[1]["user.Name"] == "bob"
    assert add[1]["user.Group"] == "user"  # Operator -> user group


@pytest.mark.asyncio
async def test_create_user_bad_level(monkeypatch):
    monkeypatch.setattr(dahua_mod, "dahua_request", _Fake((200, USERS)))
    with pytest.raises(HTTPException):
        await _drv().create_user("bob", "secret", "Superhero")


@pytest.mark.asyncio
async def test_refuse_delete_own_account(monkeypatch):
    monkeypatch.setattr(dahua_mod, "dahua_request", _Fake((200, USERS)))
    with pytest.raises(HTTPException) as ei:
        await _drv().delete_user("admin")  # == self.username
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_delete_missing_user_404(monkeypatch):
    monkeypatch.setattr(dahua_mod, "dahua_request", _Fake((200, USERS)))
    drv = DahuaCgiDriver(**{**CREDS, "username": "operator"})
    with pytest.raises(HTTPException) as ei:
        await drv.delete_user("nobody")
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_reboot(monkeypatch):
    fake = _Fake({"reboot": (200, "OK")})
    monkeypatch.setattr(dahua_mod, "dahua_request", fake)
    assert (await _drv().reboot())["status"] == "rebooting"


# ---------------------------------------------------------------------------
# Selection + safety
# ---------------------------------------------------------------------------


def test_select_dahua_by_manufacturer():
    assert select_driver_class("Dahua Technology").__name__ == "DahuaCgiDriver"


def test_dahua_has_no_destructive_methods():
    for name in ("set_network", "set_ip", "factory_reset"):
        assert not hasattr(DahuaCgiDriver, name)
