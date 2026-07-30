# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
Secureye driver tests — fully mocked (no network, no hardware).

Shapes captured from a real Secureye SP-C2QN (firmware NVSS_V26.0.0, platform
CGI_V3.0.0), which serves a *partial*, namespace-free ISAPI subset.

The cross-vendor probe tests are the important ones: this camera answers
/ISAPI/System/deviceInfo with 200 + <DeviceInfo, so before the Hikvision probe
was tightened it was claimed by the Hikvision driver — and most of that driver's
endpoints then returned HTTP 400 against it.
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

from services.camera_drivers import hikvision, secureye  # noqa: E402
from services.camera_drivers.registry import (  # noqa: E402
    get_vendors,
    select_driver_class,
)
from services.camera_drivers.secureye import driver as sec_mod  # noqa: E402
from services.camera_drivers.secureye.driver import SecureyeDriver  # noqa: E402

CREDS = dict(camera_id=1, ip="10.0.0.8", username="admin", password="pw", http_port=80)

# --- captured live shapes (namespace-free, unlike Hikvision) ---

MOTION = (
    "<MotionDetection><enabled>false</enabled><regionType>grid</regionType>"
    "<MotionDetectionLayout><sensitivityLevel>0</sensitivityLevel>"
    "</MotionDetectionLayout></MotionDetection>"
)
IPFILTER = (
    "<IPFilter><enabled>false</enabled><permissionType>unset</permissionType>"
    '<IPFilterAddressList max="16"/></IPFilter>'
)
SEC_DEVINFO = (
    "<DeviceInfo><factoryNumber>ID124</factoryNumber>"
    "<model>SP-C2QN</model><firmwareVersion>NVSS_V26.0.0</firmwareVersion>"
    "</DeviceInfo>"
)
HIK_DEVINFO = (
    '<DeviceInfo version="2.0" '
    'xmlns="http://www.hikvision.com/ver20/XMLSchema">'
    "<model>DS-2CD204WFWD-I</model></DeviceInfo>"
)
COLOR = (
    "<Color><currentTemplate></currentTemplate><brightnessLevel>50</brightnessLevel>"
    "<contrastLevel>50</contrastLevel><saturationLevel>50</saturationLevel>"
    "<hueLevel>50</hueLevel></Color>"
)
NOISE = (
    "<NoiseReduce><mode>general</mode><GeneralMode>"
    "<generalLevel>25</generalLevel></GeneralMode></NoiseReduce>"
)
# Note <ID> capitalised and displayText carrying max= — unlike Hikvision.
OVERLAYS = (
    "<VideoOverlay><channelNameOverlay><enabled>true</enabled>"
    "</channelNameOverlay><DateTimeOverlay><enabled>true</enabled>"
    "</DateTimeOverlay><TextOverlayList><TextOverlay><ID>1</ID>"
    "<enabled>false</enabled><positionX>0</positionX><positionY>9953</positionY>"
    '<displayText max="48"></displayText></TextOverlay></TextOverlayList>'
    "</VideoOverlay>"
)
IRLIGHT = (
    "<IrLight><brightnessLevel>60</brightnessLevel>"
    "<nightBrightnessLevel>20</nightBrightnessLevel>"
    '<mode opt="inside,gray,color,schedule">color</mode></IrLight>'
)
SMART_CAPS = (
    "<SmartCapList><SmartTypeCap><MainType><Type>Behavior</Type>"
    "<IsSupport>true</IsSupport></MainType><SubTypeList>"
    "<subtype><Type>LineDetection</Type><IsSupport>true</IsSupport></subtype>"
    "<subtype><Type>FieldDetection</Type><IsSupport>true</IsSupport></subtype>"
    "<subtype><Type>Face</Type><IsSupport>false</IsSupport></subtype>"
    "</SubTypeList></SmartTypeCap></SmartCapList>"
)
DETECTOR = (
    "<Detector><enabled>false</enabled>"
    "<sensitivityLevel>50</sensitivityLevel></Detector>"
)
SERVER_IP = "192.168.1.20"


class _Fake:
    """Serves the ISAPI subset this hardware implements; 400 on everything else."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.stored = IPFILTER

    async def __call__(self, ip, path, user, pw, **kw):
        method = kw.get("method", "GET")
        body = kw.get("body")
        self.calls.append((path, method, body))
        if method != "GET":
            if "ipFilter" in path and body:
                self.stored = body
            return 200, "<statusCode>1</statusCode>"
        if "motionDetection" in path:
            return 200, MOTION
        if "ipFilter" in path:
            return 200, self.stored
        if "/CGI/Image" in path and "color" in path:
            return 200, COLOR
        if "/CGI/Image" in path and "noiseReduce" in path:
            return 200, NOISE
        if "/CGI/Image" in path and "sharpness" in path:
            return 200, "<Sharpness><sharpnessLevel>50</sharpnessLevel></Sharpness>"
        if "/CGI/Image" in path and "Defog" in path:
            # firmware misspells the element as <enbaled>
            return 200, "<Defog><enbaled>true</enbaled><defogStrength>50</defogStrength></Defog>"
        if "/CGI/Image" in path and "irLight" in path:
            return 200, IRLIGHT
        if "/CGI/Image" in path:
            return 400, "<ResponseStatus/>"
        if "Smart/channels/1/capabilities" in path:
            return 200, SMART_CAPS
        if "LineDetection" in path or "FieldDetection" in path:
            return 200, DETECTOR
        if "tamperDetection" in path:
            return 200, DETECTOR
        if "Network/SNMP" in path:
            return 200, "<SNMP><enabled>false</enabled></SNMP>"
        if "TelnetCtrl" in path:
            return 200, "<telnetCtrl><enable>false</enable></telnetCtrl>"
        if "/CGI/System/Video" in path and "overlays" in path:
            return 200, OVERLAYS
        if "Storage/hdd" in path:
            return 200, "<hddList/>"
        return 400, "<ResponseStatus/>"  # Hikvision-style ISAPI paths


def _drv(monkeypatch, fake=None, server_ip=SERVER_IP):
    fake = fake or _Fake()
    monkeypatch.setattr(sec_mod, "cgi_request", fake)
    d = SecureyeDriver(**CREDS)
    monkeypatch.setattr(type(d), "_local_source_ip", lambda self: server_ip)
    return d, fake


# ---------------------------------------------------------------------------
# Probe / selection — the false positive this package exists to prevent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hikvision_probe_rejects_isapi_lookalike(monkeypatch):
    """200 + <DeviceInfo without the Hikvision namespace must NOT be Hikvision."""

    async def fake_get(ip, port, path, u, p, **kw):
        return 200, SEC_DEVINFO

    monkeypatch.setattr(hikvision, "fingerprint_get", fake_get)
    assert await hikvision.probe("10.0.0.8", 80, "admin", "pw") is False


@pytest.mark.asyncio
async def test_hikvision_probe_still_accepts_real_hikvision(monkeypatch):
    async def fake_get(ip, port, path, u, p, **kw):
        return 200, HIK_DEVINFO

    monkeypatch.setattr(hikvision, "fingerprint_get", fake_get)
    assert await hikvision.probe("10.0.0.5", 80, "admin", "pw") is True


@pytest.mark.asyncio
async def test_secureye_probe_accepts_lookalike_rejects_hikvision(monkeypatch):
    async def sec(ip, port, path, u, p, **kw):
        return 200, SEC_DEVINFO

    monkeypatch.setattr(secureye, "fingerprint_get", sec)
    assert await secureye.probe("10.0.0.8", 80, "admin", "pw") is True

    async def hik(ip, port, path, u, p, **kw):
        return 200, HIK_DEVINFO

    monkeypatch.setattr(secureye, "fingerprint_get", hik)
    assert await secureye.probe("10.0.0.5", 80, "admin", "pw") is False


@pytest.mark.asyncio
async def test_secureye_probe_rejects_401(monkeypatch):
    """Unlike the Hikvision probe, this one cannot identify a vendor from a 401."""

    async def fake_get(ip, port, path, u, p, **kw):
        return 401, ""

    monkeypatch.setattr(secureye, "fingerprint_get", fake_get)
    assert await secureye.probe("10.0.0.8", 80, "admin", "pw") is False


def test_matches_and_selection():
    assert secureye.matches("secureye") is True
    assert secureye.matches("hikvision") is False
    assert select_driver_class("SECUREYE").__name__ == "SecureyeDriver"


def test_priority_after_native_vendors():
    prio = {v.name: v.priority for v in get_vendors()}
    assert prio["hikvision"] < prio["secureye"]
    assert prio["dahua"] < prio["secureye"]


# ---------------------------------------------------------------------------
# Capabilities — never advertise an area this hardware 400s on
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capabilities_hide_unsupported_areas(monkeypatch):
    from services.camera_drivers.base import Capabilities
    from services.camera_drivers.onvif.driver import OnvifDriver

    async def base_caps(self):
        return Capabilities(
            supported_areas={
                "info": True,
                "imaging": True,
                "encoder": True,
                "network": True,
                "time": True,
            }
        )

    monkeypatch.setattr(OnvifDriver, "get_capabilities", base_caps)
    d, _ = _drv(monkeypatch)
    caps = await d.get_capabilities()
    assert caps.driver_name == "secureye"
    assert caps.supported_areas["motion"] is True
    assert caps.supported_areas["security"] is True
    # Served by the vendor's own /CGI/ namespace — must NOT be hidden
    assert caps.supported_areas["osd"] is True
    assert caps.supported_areas["storage"] is True
    assert caps.supported_areas["imaging"] is True
    # Native surface now mapped for this vendor
    assert caps.supported_areas["services"] is True
    assert caps.supported_areas["maintenance"] is True
    assert caps.supported_areas["smart"] is True  # device reports 2 detectors
    # Users stay off: this firmware returns them obfuscated
    assert caps.supported_areas["users"] is False


# ---------------------------------------------------------------------------
# Motion + IP filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_motion(monkeypatch):
    d, _ = _drv(monkeypatch)
    m = await d.get_motion()
    assert m.supported and m.enabled is False and m.sensitivity == 0
    assert m.source == "secureye-isapi"


@pytest.mark.asyncio
async def test_set_motion_patches_and_clamps(monkeypatch):
    d, fake = _drv(monkeypatch)
    await d.set_motion({"enabled": True, "sensitivity": 999})
    put = next(c for c in fake.calls if c[1] == "PUT")
    assert "<enabled>true</enabled>" in put[2]
    assert "<sensitivityLevel>100</sensitivityLevel>" in put[2]


@pytest.mark.asyncio
async def test_security_read_parses_max_attribute(monkeypatch):
    """This firmware advertises capacity via max=; Hikvision uses size=."""
    d, _ = _drv(monkeypatch)
    s = await d.get_security()
    assert s.ip_filter_supported is True
    assert s.ip_filter_max == 16
    assert s.ip_filter_enabled is False
    # "unset" is not a real mode — must normalize to None
    assert s.ip_filter_mode is None
    assert s.server_ip == SERVER_IP


@pytest.mark.asyncio
async def test_ip_filter_refuses_empty_allowlist(monkeypatch):
    d, fake = _drv(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        await d.set_ip_filter(enabled=True, mode="allow", entries=[])
    assert ei.value.status_code == 400
    assert not [c for c in fake.calls if c[1] == "PUT"]


@pytest.mark.asyncio
async def test_ip_filter_force_includes_server_ip(monkeypatch):
    d, fake = _drv(monkeypatch)
    result = await d.set_ip_filter(enabled=True, mode="allow", entries=["10.9.9.9"])
    assert SERVER_IP in result.ip_filter_entries
    puts = [c for c in fake.calls if c[1] == "PUT"]
    assert "<enabled>false</enabled>" in puts[0][2]  # staged disabled first
    assert "<enabled>true</enabled>" in puts[-1][2]  # armed only after readback


@pytest.mark.asyncio
async def test_ip_filter_refuses_when_source_ip_unknown(monkeypatch):
    d, fake = _drv(monkeypatch, server_ip=None)
    with pytest.raises(HTTPException) as ei:
        await d.set_ip_filter(enabled=True, mode="allow", entries=["10.0.0.1"])
    assert ei.value.status_code == 502
    assert not [c for c in fake.calls if c[1] == "PUT"]


@pytest.mark.asyncio
async def test_ip_filter_enforces_device_capacity(monkeypatch):
    """The device holds 16 entries — refuse rather than silently truncate."""
    d, _ = _drv(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        await d.set_ip_filter(
            enabled=True,
            mode="allow",
            entries=[f"10.0.0.{i}" for i in range(1, 40)],
        )
    assert ei.value.status_code == 400
    assert "16" in ei.value.detail


def test_no_destructive_methods():
    for name in ("set_network", "set_ip", "factory_reset"):
        assert not hasattr(SecureyeDriver, name)


# ---------------------------------------------------------------------------
# Native /CGI/ endpoints — imaging + OSD (found in the device's own manifest)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_imaging_uses_cgi_not_isapi(monkeypatch):
    d, fake = _drv(monkeypatch)
    img = await d.get_imaging()
    assert img.supported is True and img.source == "secureye-cgi"
    assert img.settings["brightness"] == 50
    assert img.settings["hue"] == 50
    assert img.settings["noise_reduce_mode"] == "general"
    assert img.settings["noise_reduction"] == 25
    # must have gone to the vendor CGI namespace, never Hikvision's ISAPI
    assert any("/CGI/Image" in c[0] for c in fake.calls)
    assert not any("/ISAPI/Image" in c[0] for c in fake.calls)


@pytest.mark.asyncio
async def test_set_imaging_patches_and_clamps(monkeypatch):
    d, fake = _drv(monkeypatch)
    await d.set_imaging({"brightness": 999, "noise_reduction": -5})
    colour = next(c for c in fake.calls if c[1] == "PUT" and "color" in c[0])
    assert "<brightnessLevel>100</brightnessLevel>" in colour[2]
    assert "<contrastLevel>50</contrastLevel>" in colour[2]  # sibling untouched
    nr = next(c for c in fake.calls if c[1] == "PUT" and "noiseReduce" in c[0])
    assert "<generalLevel>0</generalLevel>" in nr[2]


@pytest.mark.asyncio
async def test_set_imaging_rejects_unknown_key(monkeypatch):
    d, _ = _drv(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        await d.set_imaging({"warp_drive": 9})
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_osd_parses_capitalised_id(monkeypatch):
    """This firmware uses <ID>, not Hikvision's <id> — the parser must cope."""
    d, _ = _drv(monkeypatch)
    osd = await d.get_osd()
    assert osd.supported is True and osd.source == "secureye-cgi"
    assert osd.datetime_enabled is True
    assert osd.channel_name_enabled is True
    assert [s["id"] for s in osd.slots] == [1]
    assert osd.slots[0]["y"] == 9953


@pytest.mark.asyncio
async def test_set_osd_writes_text_into_max_attributed_tag(monkeypatch):
    d, fake = _drv(monkeypatch)
    await d.set_osd({"text_enabled": True, "text": "Gate"})
    put = next(c for c in fake.calls if c[1] == "PUT" and "overlays" in c[0])
    assert "<enabled>true</enabled>" in put[2]
    assert ">Gate</displayText>" in put[2]


@pytest.mark.asyncio
async def test_storage_uses_hdd_path(monkeypatch):
    d, fake = _drv(monkeypatch)
    st = await d.get_storage()
    assert st.supported is True and st.present is False
    assert any("Storage/hdd" in c[0] for c in fake.calls)


# ---------------------------------------------------------------------------
# Expanded native surface: imaging table, smart gating, services
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_imaging_reads_across_multiple_endpoints(monkeypatch):
    d, _ = _drv(monkeypatch)
    img = await d.get_imaging()
    # colour endpoint
    assert img.settings["brightness"] == 50
    assert img.settings["hue"] == 50
    # separate endpoints, each fetched once
    assert img.settings["sharpness"] == 50
    assert img.settings["noise_reduce_mode"] == "general"


@pytest.mark.asyncio
async def test_imaging_handles_firmware_typo_enbaled(monkeypatch):
    """Defog's element really is misspelled <enbaled> in this firmware."""
    d, _ = _drv(monkeypatch)
    img = await d.get_imaging()
    assert img.settings["defog"] == "ON"
    assert img.ranges["defog"]["options"] == ["ON", "OFF"]


@pytest.mark.asyncio
async def test_imaging_uses_device_advertised_options(monkeypatch):
    """<mode opt="a,b,c"> — the option list must come from the device."""
    d, _ = _drv(monkeypatch)
    img = await d.get_imaging()
    assert img.settings["ir_light_mode"] == "color"
    assert img.ranges["ir_light_mode"]["options"] == [
        "inside", "gray", "color", "schedule",
    ]


@pytest.mark.asyncio
async def test_imaging_batches_edits_per_endpoint(monkeypatch):
    """brightness+contrast share one endpoint — one read, one write, not two."""
    d, fake = _drv(monkeypatch)
    await d.set_imaging({"brightness": 70, "contrast": 30})
    puts = [c for c in fake.calls if c[1] == "PUT" and "color" in c[0]]
    assert len(puts) == 1
    assert "<brightnessLevel>70</brightnessLevel>" in puts[0][2]
    assert "<contrastLevel>30</contrastLevel>" in puts[0][2]
    assert "<saturationLevel>50</saturationLevel>" in puts[0][2]  # untouched


@pytest.mark.asyncio
async def test_smart_gated_by_device_capabilities(monkeypatch):
    d, _ = _drv(monkeypatch)
    sm = await d.get_smart()
    by = {x.key: x for x in sm.detectors}
    assert sm.supported is True
    assert by["line_crossing"].supported is True
    assert by["intrusion"].supported is True
    # inert until a region is drawn — must be reported, not implied working
    assert by["line_crossing"].configured is False


@pytest.mark.asyncio
async def test_smart_respects_unsupported_capability(monkeypatch):
    """A detector the device reports IsSupport=false must not be claimed."""
    class _NoLine(_Fake):
        async def __call__(self, ip, path, user, pw, **kw):
            if "Smart/channels/1/capabilities" in path:
                return 200, SMART_CAPS.replace(
                    "<Type>LineDetection</Type><IsSupport>true</IsSupport>",
                    "<Type>LineDetection</Type><IsSupport>false</IsSupport>",
                )
            return await super().__call__(ip, path, user, pw, **kw)

    d, _ = _drv(monkeypatch, fake=_NoLine())
    sm = await d.get_smart()
    by = {x.key: x for x in sm.detectors}
    assert by["line_crossing"].supported is False
    assert by["intrusion"].supported is True


@pytest.mark.asyncio
async def test_services_read_and_toggle(monkeypatch):
    d, fake = _drv(monkeypatch)
    sv = await d.get_services()
    by = {x.key: x for x in sv.services}
    assert by["snmp"].enabled is False and by["snmp"].writable is True
    # telnet uses <enable>, not <enabled>
    assert by["telnet"].enabled is False
    await d.set_service("telnet", True)
    put = next(c for c in fake.calls if c[1] == "PUT" and "Telnet" in c[0])
    assert "<enable>true</enable>" in put[2]


@pytest.mark.asyncio
async def test_readonly_service_rejected(monkeypatch):
    d, fake = _drv(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        await d.set_service("qos", False)
    assert ei.value.status_code == 400
    assert not [c for c in fake.calls if c[1] == "PUT"]
