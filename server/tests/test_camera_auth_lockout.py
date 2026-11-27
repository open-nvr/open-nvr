# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""A refused camera login is not sent again at once (account lockout).

A Hikvision NVR with ONVIF switched off, or without an ONVIF user, answers
every ONVIF call with HTTP 401, and locks the account after a few wrong
logins. Reported 2026-11-27: the camera settings page showed a bare
"Authentication failed - check username/password" for a password that was
right for the web login, every area "not supported", and the operator got
locked out while trying.

Covered here:
* a plain 401 to an ONVIF Digest login is final (no WS-Security retry) and
  reads as the ONVIF-account problem it usually is;
* a 401 with no Digest challenge never sent the login, so WS-Security still
  gets its one try;
* refused logins (ONVIF and web) are remembered and not sent again for a
  minute; a different password, or the window running out, sends again;
* the Hikvision driver reads its identity over ISAPI when ONVIF refuses.
"""

from __future__ import annotations

import os
import secrets
import sys
import types as _types
from pathlib import Path

import httpx
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

from services import camera_auth_guard as guard  # noqa: E402
from services import onvif_digest_service as ods  # noqa: E402
from services.camera_drivers import _probe  # noqa: E402
from services.camera_drivers.hikvision import driver as hik_mod  # noqa: E402
from services.camera_drivers.hikvision import isapi as isapi_mod  # noqa: E402
from services.camera_drivers.onvif.driver import OnvifDriver  # noqa: E402

URL = "http://192.168.0.146:80/onvif/device_service"
DIGEST = {"WWW-Authenticate": 'Digest realm="IP Camera", nonce="abc", qop="auth"'}
OK = "<s:Envelope><s:Body><tds:GetDeviceInformationResponse/></s:Body></s:Envelope>"


class _Client:
    """Stand-in for httpx.AsyncClient: replays canned responses, records calls."""

    responses: list[httpx.Response] = []
    calls: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def post(self, url, **kwargs):
        _Client.calls.append({"url": url, **kwargs})
        return _Client.responses.pop(0)

    async def get(self, url, **kwargs):
        _Client.calls.append({"url": url, **kwargs})
        return _Client.responses.pop(0)

    async def request(self, method, url, **kwargs):
        _Client.calls.append({"url": url, "method": method, **kwargs})
        return _Client.responses.pop(0)


@pytest.fixture(autouse=True)
def _client(monkeypatch):
    _Client.responses = []
    _Client.calls = []
    ods._WSSE_PREFERRED_HOSTS.clear()
    ods._CLOCK_OFFSETS.clear()
    ods._DIGEST_AUTHS.clear()
    for mod in (ods, isapi_mod, _probe):
        monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    yield
    ods._WSSE_PREFERRED_HOSTS.clear()
    ods._CLOCK_OFFSETS.clear()
    ods._DIGEST_AUTHS.clear()


def _is_wsse(call: dict) -> bool:
    return "<wsse:Security" in (call.get("content") or "")


# --- ONVIF ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_plain_401_to_digest_is_final_and_names_the_onvif_account():
    _Client.responses = [httpx.Response(401, headers=DIGEST, text="Unauthorized")]

    with pytest.raises(HTTPException) as ei:
        await ods._onvif_request(URL, "<tds:GetDeviceInformation/>", "admin", "pw")

    assert ei.value.status_code == 401
    assert "ONVIF accounts separate from the web login" in ei.value.detail
    # One login sent; no WS-Security retry, no clock probe.
    assert len(_Client.calls) == 1


@pytest.mark.asyncio
async def test_refused_onvif_login_is_not_sent_again_within_the_window():
    _Client.responses = [httpx.Response(401, headers=DIGEST)]
    with pytest.raises(HTTPException):
        await ods._onvif_request(URL, "<tds:GetDeviceInformation/>", "admin", "pw")

    # Another service path and port on the same device: still held.
    other = "http://192.168.0.146:8000/onvif/media_service"
    with pytest.raises(HTTPException) as ei:
        await ods._onvif_request(other, "<trt:GetProfiles/>", "admin", "pw")

    assert ei.value.status_code == 401
    assert "Not tried again for" in ei.value.detail
    assert len(_Client.calls) == 1


@pytest.mark.asyncio
async def test_a_corrected_password_is_sent_at_once():
    _Client.responses = [httpx.Response(401, headers=DIGEST), httpx.Response(200, text=OK)]
    with pytest.raises(HTTPException):
        await ods._onvif_request(URL, "<tds:GetDeviceInformation/>", "admin", "wrong")

    status, _ = await ods._onvif_request(URL, "<tds:GetDeviceInformation/>", "admin", "right")

    assert status == 200
    assert len(_Client.calls) == 2


@pytest.mark.asyncio
async def test_the_hold_expires(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(guard.time, "monotonic", lambda: now[0])
    _Client.responses = [httpx.Response(401, headers=DIGEST), httpx.Response(200, text=OK)]
    with pytest.raises(HTTPException):
        await ods._onvif_request(URL, "<tds:GetDeviceInformation/>", "admin", "pw")

    now[0] += guard.HOLD_S + 1
    status, _ = await ods._onvif_request(URL, "<tds:GetDeviceInformation/>", "admin", "pw")

    assert status == 200
    assert len(_Client.calls) == 2


@pytest.mark.asyncio
async def test_401_without_a_digest_challenge_still_tries_ws_security():
    """No Digest offered means the login never left: WS-Security gets its try."""
    _Client.responses = [
        httpx.Response(401, headers={"WWW-Authenticate": 'Basic realm="x"'}),
        httpx.Response(200, text=OK),
    ]

    status, _ = await ods._onvif_request(URL, "<tds:GetDeviceInformation/>", "admin", "pw")

    assert status == 200
    assert len(_Client.calls) == 2
    assert _is_wsse(_Client.calls[1])


@pytest.mark.asyncio
async def test_no_credentials_returns_the_401_untouched():
    """Discovery's unauthenticated probe reads 401 as 'ONVIF is here'."""
    _Client.responses = [httpx.Response(401, headers=DIGEST, text="onvif.org")]

    status, _ = await ods._onvif_request(URL, "<tds:GetSystemDateAndTime/>")

    assert status == 401
    assert guard.refused_for(guard.ONVIF, URL, None, None) is None


# --- web login (ISAPI, vendor fingerprint) ---------------------------------


@pytest.mark.asyncio
async def test_refused_isapi_login_is_held_for_isapi_and_fingerprint():
    _Client.responses = [httpx.Response(401, headers=DIGEST)]

    assert (await isapi_mod.isapi_request("192.168.0.146", "/ISAPI/System/deviceInfo", "admin", "pw"))[0] == 401
    assert (await isapi_mod.isapi_request("192.168.0.146", "/ISAPI/Image/channels/1", "admin", "pw"))[0] == 401
    assert (await _probe.fingerprint_get("192.168.0.146", 80, "/ISAPI/System/deviceInfo", "admin", "pw"))[0] == 401

    assert len(_Client.calls) == 1


@pytest.mark.asyncio
async def test_onvif_refusal_does_not_hold_the_web_login():
    """Hikvision keeps ONVIF users apart from web users."""
    _Client.responses = [
        httpx.Response(401, headers=DIGEST),
        httpx.Response(200, text="<DeviceInfo/>"),
    ]
    with pytest.raises(HTTPException):
        await ods._onvif_request(URL, "<tds:GetDeviceInformation/>", "admin", "pw")

    status, _ = await isapi_mod.isapi_request("192.168.0.146", "/ISAPI/System/deviceInfo", "admin", "pw")

    assert status == 200
    assert len(_Client.calls) == 2


@pytest.mark.asyncio
async def test_basic_only_401_is_not_remembered_as_a_refusal():
    _Client.responses = [
        httpx.Response(401, headers={"WWW-Authenticate": 'Basic realm="x"'}),
        httpx.Response(200, text="ok"),
    ]
    await isapi_mod.isapi_request("192.168.0.146", "/ISAPI/x", "admin", "pw")
    status, _ = await isapi_mod.isapi_request("192.168.0.146", "/ISAPI/x", "admin", "pw")

    assert status == 200
    assert len(_Client.calls) == 2


# --- Hikvision identity: ONVIF, else ISAPI ---------------------------------

DEVICE_INFO = (
    '<DeviceInfo version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">'
    "<deviceName>NVR</deviceName><model>DS-7608NI-K2</model>"
    "<serialNumber>DS-7608NI-K20820201234</serialNumber>"
    "<firmwareVersion>V4.30.085</firmwareVersion><deviceType>NVR</deviceType>"
    "</DeviceInfo>"
)


def _hik():
    return hik_mod.HikvisionIsapiDriver(
        camera_id=1, ip="192.168.0.146", username="admin", password="pw"
    )


def _onvif_raises(status: int):
    async def _get_info(self):
        raise HTTPException(status_code=status, detail="onvif said no")

    return _get_info


@pytest.mark.asyncio
async def test_hikvision_identity_falls_back_to_isapi(monkeypatch):
    monkeypatch.setattr(OnvifDriver, "get_info", _onvif_raises(401))
    _Client.responses = [httpx.Response(200, text=DEVICE_INFO)]

    info = await _hik().get_info()

    assert info.model == "DS-7608NI-K2"
    assert info.firmware_version == "V4.30.085"
    assert info.serial_number == "DS-7608NI-K20820201234"
    assert info.manufacturer == "Hikvision"


@pytest.mark.asyncio
async def test_hikvision_refused_by_both_says_so(monkeypatch):
    monkeypatch.setattr(OnvifDriver, "get_info", _onvif_raises(401))
    _Client.responses = [httpx.Response(401, headers=DIGEST)]

    with pytest.raises(HTTPException) as ei:
        await _hik().get_info()

    assert ei.value.status_code == 401
    assert "both its web login and ONVIF" in ei.value.detail


@pytest.mark.asyncio
async def test_hikvision_unreachable_does_not_try_isapi(monkeypatch):
    monkeypatch.setattr(OnvifDriver, "get_info", _onvif_raises(504))

    with pytest.raises(HTTPException) as ei:
        await _hik().get_info()

    assert ei.value.status_code == 504
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_hikvision_refresh_sends_each_wrong_login_once():
    """The settings probe's sequence against an NVR refusing everything:
    ONVIF device info (driver pick), ISAPI fingerprint, then the driver's
    own get_info. One wrong ONVIF login and one wrong web login, no more."""
    _Client.responses = [
        httpx.Response(401, headers=DIGEST),  # ONVIF GetDeviceInformation
        httpx.Response(401, headers=DIGEST),  # ISAPI fingerprint
    ]
    drv = _hik()
    with pytest.raises(HTTPException):
        await OnvifDriver.get_info(drv)
    await _probe.fingerprint_get("192.168.0.146", 80, "/ISAPI/System/deviceInfo", "admin", "pw")

    with pytest.raises(HTTPException) as ei:
        await drv.get_info()

    assert "both its web login and ONVIF" in ei.value.detail
    assert len(_Client.calls) == 2
