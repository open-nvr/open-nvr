# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
Phase 0 tests for multi-vendor camera configuration (read-only device layer).

Run with:

    cd server && pytest tests/test_camera_settings.py -v

Coverage:
* Driver selection registry (manufacturer -> driver class).
* _prefix_to_netmask pure helper.
* OnvifDriver network + capability parsing against real ONVIF SOAP shapes
  (primitives mocked — no network).
* HikvisionIsapiDriver network + storage parsing against real ISAPI XML,
  including the empty-slot ("no SD card") case and a populated slot.
* Migration chain/source guard.
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

from services import onvif_digest_service as ods  # noqa: E402
from services.camera_drivers.hikvision import driver as hik  # noqa: E402
from services.camera_drivers.hikvision.driver import (  # noqa: E402
    HikvisionIsapiDriver,
)
from services.camera_drivers.onvif.driver import (  # noqa: E402
    OnvifDriver,
    _prefix_to_netmask,
)
from services.camera_drivers.registry import select_driver_class  # noqa: E402

CREDS = dict(camera_id=1, ip="10.0.0.5", username="admin", password="pw", http_port=80)


@pytest.fixture(autouse=True)
def _stable_core_env():
    """Survive a sibling suite (test_m1c) purging ``core.*`` from sys.modules:
    a forced re-import re-validates Settings, which trips V-015 if routable
    ``MEDIAMTX_*`` values leaked in from test_m1b. Pin loopback-safe values."""
    safe = {
        "MEDIAMTX_BASE_URL": "http://127.0.0.1:8889",
        "MEDIAMTX_ADMIN_API": "http://127.0.0.1:9997/v3",
        "MEDIAMTX_HLS_URL": "http://127.0.0.1:8888",
        "MEDIAMTX_RTSP_URL": "rtsp://127.0.0.1:8554",
        "MEDIAMTX_RTSPS_URL": "rtsps://127.0.0.1:8322",
        "MEDIAMTX_PLAYBACK_URL": "http://127.0.0.1:9996",
    }
    saved = {k: os.environ.get(k) for k in safe}
    os.environ.update(safe)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# --- real captured response shapes (trimmed) ---

ONVIF_NET = """<tds:NetworkInterfaces token="eth0"><tt:Enabled>true</tt:Enabled>
<tt:Info><tt:Name>eth0</tt:Name><tt:HwAddress>58:03:fb:76:a1:bc</tt:HwAddress>
<tt:MTU>1500</tt:MTU></tt:Info>
<tt:IPv4><tt:Enabled>true</tt:Enabled><tt:Config>
<tt:Manual><tt:Address>192.168.1.64</tt:Address><tt:PrefixLength>24</tt:PrefixLength></tt:Manual>
<tt:DHCP>false</tt:DHCP></tt:Config></tt:IPv4></tds:NetworkInterfaces>"""
ONVIF_GW = "<tds:NetworkGateway><tt:IPv4Address>192.168.1.1</tt:IPv4Address></tds:NetworkGateway>"
ONVIF_DNS = ("<tds:DNSInformation><tt:FromDHCP>false</tt:FromDHCP>"
             "<tt:DNSManual><tt:Type>IPv4</tt:Type><tt:IPv4Address>8.8.8.8</tt:IPv4Address>"
             "</tt:DNSManual></tds:DNSInformation>")

ISAPI_NET = """<?xml version="1.0" encoding="UTF-8"?>
<NetworkInterfaceList><NetworkInterface><id>1</id>
<IPAddress><addressingType>static</addressingType>
<ipAddress>192.168.1.64</ipAddress><subnetMask>255.255.255.0</subnetMask>
<DefaultGateway><ipAddress>192.168.1.1</ipAddress></DefaultGateway>
<PrimaryDNS><ipAddress>8.8.8.8</ipAddress></PrimaryDNS>
<SecondaryDNS><ipAddress>0.0.0.0</ipAddress></SecondaryDNS></IPAddress>
<Link><MACAddress>58:03:fb:76:a1:bc</MACAddress><MTU>1500</MTU></Link>
</NetworkInterface></NetworkInterfaceList>"""
ISAPI_STORAGE_EMPTY = '<storage><hddList size="8" ></hddList><workMode>quota</workMode></storage>'
ISAPI_STORAGE_FULL = """<storage><hddList size="1">
<hdd><id>1</id><hddName>sdcard</hddName><status>ok</status>
<capacity>61440</capacity><freeSpace>30720</freeSpace></hdd>
</hddList></storage>"""


# ---------------------------------------------------------------------------
# Registry + pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "manufacturer,expected",
    [
        ("HIKVISION", "HikvisionIsapiDriver"),
        ("Hikvision Digital Technology", "HikvisionIsapiDriver"),
        ("Dahua", "DahuaCgiDriver"),
        ("Dahua Technology", "DahuaCgiDriver"),
        ("CP Plus", "CpPlusDriver"),  # Dahua OEM — own package, priority over dahua
        ("Secureye", "SecureyeDriver"),  # partial ISAPI subset, own package
        ("", "OnvifDriver"),
        (None, "OnvifDriver"),
    ],
)
def test_select_driver_class(manufacturer, expected):
    assert select_driver_class(manufacturer).__name__ == expected


@pytest.mark.parametrize(
    "prefix,mask",
    [(24, "255.255.255.0"), (16, "255.255.0.0"), (8, "255.0.0.0"),
     (32, "255.255.255.255"), (0, "0.0.0.0")],
)
def test_prefix_to_netmask(prefix, mask):
    assert _prefix_to_netmask(prefix) == mask


def test_prefix_to_netmask_invalid():
    assert _prefix_to_netmask(33) is None


# ---------------------------------------------------------------------------
# OnvifDriver (universal baseline)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_onvif_get_network(monkeypatch):
    async def fake_req(url, body, user, pw):
        if "GetNetworkInterfaces" in body:
            return 200, ONVIF_NET
        if "GetNetworkDefaultGateway" in body:
            return 200, ONVIF_GW
        if "GetDNS" in body:
            return 200, ONVIF_DNS
        return 200, ""

    monkeypatch.setattr(ods, "_onvif_request", fake_req)
    net = await OnvifDriver(**CREDS).get_network()
    assert net.supported and net.source == "onvif"
    assert net.mac_address == "58:03:fb:76:a1:bc"
    assert net.ip_address == "192.168.1.64"
    assert net.subnet_mask == "255.255.255.0"  # from PrefixLength 24
    assert net.gateway == "192.168.1.1"
    assert net.dns_primary == "8.8.8.8"
    assert net.dhcp is False
    assert net.mtu == 1500


@pytest.mark.asyncio
async def test_onvif_capabilities_derivation(monkeypatch):
    async def fake_caps(ip, user, pw, port):
        # media + imaging + events present; no ptz (fixed camera)
        return {
            "device": "http://x/onvif/device_service",
            "media": "http://x/onvif/Media",
            "imaging": "http://x/onvif/Imaging",
            "events": "http://x/onvif/Events",
        }

    monkeypatch.setattr(ods, "get_capabilities", fake_caps)
    caps = await OnvifDriver(**CREDS).get_capabilities()
    a = caps.supported_areas
    assert a["imaging"] is True
    assert a["encoder"] is True  # media present
    # The device advertises an Events endpoint, but the baseline does not
    # implement subscribe_events() — so it must NOT claim the area (that would
    # render an Events tab that errors on use for generic ONVIF cameras).
    assert a["events"] is False
    assert a["ptz"] is False  # no ptz endpoint
    assert a["storage"] is False  # onvif baseline
    assert a["info"] and a["network"] and a["time"]


# ---------------------------------------------------------------------------
# HikvisionIsapiDriver (ISAPI extras)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hikvision_get_network(monkeypatch):
    async def fake_isapi(ip, path, user, pw, **kw):
        return 200, ISAPI_NET

    monkeypatch.setattr(hik, "isapi_request", fake_isapi)
    net = await HikvisionIsapiDriver(**CREDS).get_network()
    assert net.supported and net.source == "isapi"
    assert net.ip_address == "192.168.1.64"
    assert net.subnet_mask == "255.255.255.0"
    assert net.gateway == "192.168.1.1"
    assert net.dns_primary == "8.8.8.8"
    assert net.dns_secondary is None  # 0.0.0.0 filtered out
    assert net.mac_address == "58:03:fb:76:a1:bc"
    assert net.dhcp is False  # addressingType=static
    assert net.mtu == 1500


@pytest.mark.asyncio
async def test_hikvision_storage_empty_means_no_card(monkeypatch):
    async def fake_isapi(ip, path, user, pw, **kw):
        return 200, ISAPI_STORAGE_EMPTY

    monkeypatch.setattr(hik, "isapi_request", fake_isapi)
    st = await HikvisionIsapiDriver(**CREDS).get_storage()
    assert st.supported is True
    assert st.present is False
    assert st.slots == []


@pytest.mark.asyncio
async def test_hikvision_storage_populated(monkeypatch):
    async def fake_isapi(ip, path, user, pw, **kw):
        return 200, ISAPI_STORAGE_FULL

    monkeypatch.setattr(hik, "isapi_request", fake_isapi)
    st = await HikvisionIsapiDriver(**CREDS).get_storage()
    assert st.supported and st.present
    assert len(st.slots) == 1
    slot = st.slots[0]
    assert slot.name == "sdcard"
    assert slot.status == "ok"
    assert slot.capacity_mb == 61440
    assert slot.free_mb == 30720


@pytest.mark.asyncio
async def test_hikvision_capabilities_add_storage(monkeypatch):
    async def fake_caps(ip, user, pw, port):
        return {"device": "x", "media": "x"}

    monkeypatch.setattr(ods, "get_capabilities", fake_caps)
    caps = await HikvisionIsapiDriver(**CREDS).get_capabilities()
    assert caps.driver_name == "hikvision"
    assert caps.supported_areas["storage"] is True  # ISAPI adds it
    assert caps.supported_areas["motion"] is True


# ---------------------------------------------------------------------------
# Time / NTP (Phase 1)
# ---------------------------------------------------------------------------

SDT_XML = """<tds:SystemDateAndTime><tt:DateTimeType>Manual</tt:DateTimeType>
<tt:DaylightSavings>false</tt:DaylightSavings>
<tt:TimeZone><tt:TZ>CST-8:00:00</tt:TZ></tt:TimeZone>
<tt:UTCDateTime><tt:Time><tt:Hour>23</tt:Hour><tt:Minute>39</tt:Minute><tt:Second>42</tt:Second></tt:Time>
<tt:Date><tt:Year>1969</tt:Year><tt:Month>12</tt:Month><tt:Day>31</tt:Day></tt:Date></tt:UTCDateTime>
<tt:LocalDateTime><tt:Time><tt:Hour>7</tt:Hour><tt:Minute>39</tt:Minute><tt:Second>42</tt:Second></tt:Time>
<tt:Date><tt:Year>1970</tt:Year><tt:Month>1</tt:Month><tt:Day>1</tt:Day></tt:Date></tt:LocalDateTime>
</tds:SystemDateAndTime>"""
NTP_XML = ("<tds:NTPInformation><tt:FromDHCP>false</tt:FromDHCP><tt:NTPManual>"
           "<tt:Type>DNS</tt:Type><tt:DNSname>time.windows.com</tt:DNSname>"
           "</tt:NTPManual></tds:NTPInformation>")
FAULT_XML = "<env:Fault><env:Reason><env:Text>bad request</env:Text></env:Reason></env:Fault>"


class _FakeOnvif:
    """Records SOAP bodies and returns canned responses keyed by op."""

    def __init__(self, set_status=200, set_text="<ok/>"):
        self.bodies: list[str] = []
        self.set_status = set_status
        self.set_text = set_text

    async def __call__(self, url, body, user, pw):
        self.bodies.append(body)
        if "GetSystemDateAndTime" in body:
            return 200, SDT_XML
        if "GetNTP" in body:
            return 200, NTP_XML
        # Set* operations
        return self.set_status, self.set_text


@pytest.mark.asyncio
async def test_onvif_get_time(monkeypatch):
    monkeypatch.setattr(ods, "_onvif_request", _FakeOnvif())
    t = await OnvifDriver(**CREDS).get_time()
    assert t.mode == "manual"
    assert t.utc_datetime == "1969-12-31T23:39:42"
    assert t.local_datetime == "1970-01-01T07:39:42"
    assert t.timezone == "CST-8:00:00"
    assert t.daylight_savings is False
    assert t.ntp_from_dhcp is False
    assert t.ntp_server == "time.windows.com"


@pytest.mark.asyncio
async def test_set_time_manual_builds_manual_body_with_tz(monkeypatch):
    fake = _FakeOnvif()
    monkeypatch.setattr(ods, "_onvif_request", fake)
    await OnvifDriver(**CREDS).set_time_manual(timezone="IST-5:30:00")
    set_body = next(b for b in fake.bodies if "SetSystemDateAndTime" in b)
    assert "<tds:DateTimeType>Manual</tds:DateTimeType>" in set_body
    assert "<tt:TZ>IST-5:30:00</tt:TZ>" in set_body
    assert "<tds:UTCDateTime>" in set_body and "<tt:Year>" in set_body


@pytest.mark.asyncio
async def test_set_ntp_builds_setntp_and_ntp_mode(monkeypatch):
    fake = _FakeOnvif()
    monkeypatch.setattr(ods, "_onvif_request", fake)
    await OnvifDriver(**CREDS).set_ntp("pool.ntp.org", timezone="IST-5:30:00")
    ntp_body = next(b for b in fake.bodies if "SetNTP" in b)
    assert "<tt:DNSname>pool.ntp.org</tt:DNSname>" in ntp_body
    dt_body = next(
        b for b in fake.bodies
        if "SetSystemDateAndTime" in b and "NTP</tds:DateTimeType>" in b
    )
    assert "<tds:DateTimeType>NTP</tds:DateTimeType>" in dt_body


@pytest.mark.asyncio
async def test_set_ntp_ip_uses_ipv4_element(monkeypatch):
    fake = _FakeOnvif()
    monkeypatch.setattr(ods, "_onvif_request", fake)
    await OnvifDriver(**CREDS).set_ntp("192.168.1.1")
    ntp_body = next(b for b in fake.bodies if "SetNTP" in b)
    assert "<tt:IPv4Address>192.168.1.1</tt:IPv4Address>" in ntp_body
    assert "<tt:Type>IPv4</tt:Type>" in ntp_body


@pytest.mark.asyncio
async def test_set_ntp_requires_server():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        await OnvifDriver(**CREDS).set_ntp("")
    assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_set_time_raises_on_soap_fault(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(ods, "_onvif_request", _FakeOnvif(set_text=FAULT_XML))
    with pytest.raises(HTTPException) as e:
        await OnvifDriver(**CREDS).set_time_manual()
    assert e.value.status_code == 502
    assert "bad request" in str(e.value.detail)


# ---------------------------------------------------------------------------
# Imaging (Phase 2)
# ---------------------------------------------------------------------------

VS_XML = ('<trt:GetVideoSourcesResponse><trt:VideoSources token="VideoSource_1">'
          "</trt:VideoSources></trt:GetVideoSourcesResponse>")
IMG_SETTINGS_XML = """<timg:ImagingSettings>
<tt:BacklightCompensation><tt:Mode>OFF</tt:Mode></tt:BacklightCompensation>
<tt:Brightness>50</tt:Brightness><tt:ColorSaturation>50</tt:ColorSaturation>
<tt:Contrast>50</tt:Contrast><tt:IrCutFilter>AUTO</tt:IrCutFilter>
<tt:Sharpness>50</tt:Sharpness>
<tt:WideDynamicRange><tt:Mode>OFF</tt:Mode></tt:WideDynamicRange>
<tt:WhiteBalance><tt:Mode>AUTO</tt:Mode></tt:WhiteBalance></timg:ImagingSettings>"""
IMG_OPTIONS_XML = """<timg:ImagingOptions>
<tt:BacklightCompensation><tt:Mode>OFF</tt:Mode><tt:Mode>ON</tt:Mode></tt:BacklightCompensation>
<tt:Brightness><tt:Min>0</tt:Min><tt:Max>100</tt:Max></tt:Brightness>
<tt:ColorSaturation><tt:Min>0</tt:Min><tt:Max>100</tt:Max></tt:ColorSaturation>
<tt:Contrast><tt:Min>0</tt:Min><tt:Max>100</tt:Max></tt:Contrast>
<tt:IrCutFilterModes>ON</tt:IrCutFilterModes><tt:IrCutFilterModes>OFF</tt:IrCutFilterModes>
<tt:IrCutFilterModes>AUTO</tt:IrCutFilterModes>
<tt:Sharpness><tt:Min>0</tt:Min><tt:Max>100</tt:Max></tt:Sharpness>
<tt:WideDynamicRange><tt:Mode>ON</tt:Mode><tt:Mode>OFF</tt:Mode>
<tt:Level><tt:Min>0</tt:Min><tt:Max>100</tt:Max></tt:Level></tt:WideDynamicRange>
</timg:ImagingOptions>"""


class _FakeImaging:
    def __init__(self):
        self.bodies: list[str] = []

    async def caps(self, ip, user, pw, port):
        return {"media": "http://x/onvif/Media", "imaging": "http://x/onvif/Imaging"}

    async def req(self, url, body, user, pw):
        self.bodies.append(body)
        if "GetVideoSources" in body:
            return 200, VS_XML
        if "GetImagingSettings" in body:
            return 200, IMG_SETTINGS_XML
        if "GetOptions" in body:
            return 200, IMG_OPTIONS_XML
        return 200, "<ok/>"


@pytest.mark.asyncio
async def test_get_imaging(monkeypatch):
    fake = _FakeImaging()
    monkeypatch.setattr(ods, "get_capabilities", fake.caps)
    monkeypatch.setattr(ods, "_onvif_request", fake.req)
    img = await OnvifDriver(**CREDS).get_imaging()
    assert img.supported
    assert img.settings["brightness"] == 50
    assert img.settings["contrast"] == 50
    assert img.settings["ir_cut_filter"] == "AUTO"
    assert img.settings["wdr"] == "OFF"
    assert img.settings["backlight"] == "OFF"
    assert img.ranges["brightness"] == {"min": 0, "max": 100}
    assert img.ranges["ir_cut_filter"]["options"] == ["ON", "OFF", "AUTO"]
    assert img.ranges["wdr"]["options"] == ["ON", "OFF"]


@pytest.mark.asyncio
async def test_set_imaging_clamps_and_builds_body(monkeypatch):
    fake = _FakeImaging()
    monkeypatch.setattr(ods, "get_capabilities", fake.caps)
    monkeypatch.setattr(ods, "_onvif_request", fake.req)
    await OnvifDriver(**CREDS).set_imaging({"brightness": 150})  # out of range
    set_body = next(b for b in fake.bodies if "SetImagingSettings" in b)
    assert "<tt:Brightness>100</tt:Brightness>" in set_body  # clamped 150 -> 100
    assert "<timg:VideoSourceToken>VideoSource_1</timg:VideoSourceToken>" in set_body
    assert "<timg:ForcePersistence>true</timg:ForcePersistence>" in set_body


@pytest.mark.asyncio
async def test_set_imaging_day_night(monkeypatch):
    fake = _FakeImaging()
    monkeypatch.setattr(ods, "get_capabilities", fake.caps)
    monkeypatch.setattr(ods, "_onvif_request", fake.req)
    await OnvifDriver(**CREDS).set_imaging({"ir_cut_filter": "OFF"})
    set_body = next(b for b in fake.bodies if "SetImagingSettings" in b)
    assert "<tt:IrCutFilter>OFF</tt:IrCutFilter>" in set_body


@pytest.mark.asyncio
async def test_set_imaging_ignores_unknown_keys(monkeypatch):
    fake = _FakeImaging()
    monkeypatch.setattr(ods, "get_capabilities", fake.caps)
    monkeypatch.setattr(ods, "_onvif_request", fake.req)
    # 'bogus' is not a managed field -> must not appear in the body
    await OnvifDriver(**CREDS).set_imaging({"bogus": 1, "contrast": 30})
    set_body = next(b for b in fake.bodies if "SetImagingSettings" in b)
    assert "bogus" not in set_body
    assert "<tt:Contrast>30</tt:Contrast>" in set_body


# ---------------------------------------------------------------------------
# Video encoder (Phase 3)
# ---------------------------------------------------------------------------

ENC_CONFIGS_XML = """<trt:GetVideoEncoderConfigurationsResponse>
<trt:Configurations token="VideoEncoderToken_1"><tt:Name>VideoEncoder_1</tt:Name>
<tt:Encoding>H264</tt:Encoding>
<tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution>
<tt:Quality>4.000000</tt:Quality>
<tt:RateControl><tt:FrameRateLimit>25</tt:FrameRateLimit>
<tt:EncodingInterval>1</tt:EncodingInterval><tt:BitrateLimit>8192</tt:BitrateLimit></tt:RateControl>
<tt:H264><tt:GovLength>50</tt:GovLength><tt:H264Profile>Main</tt:H264Profile></tt:H264>
</trt:Configurations>
<trt:Configurations token="VideoEncoderToken_2"><tt:Name>VideoEncoder_2</tt:Name>
<tt:Encoding>H265</tt:Encoding>
<tt:Resolution><tt:Width>640</tt:Width><tt:Height>480</tt:Height></tt:Resolution>
<tt:RateControl><tt:FrameRateLimit>25</tt:FrameRateLimit>
<tt:BitrateLimit>384</tt:BitrateLimit></tt:RateControl>
</trt:Configurations></trt:GetVideoEncoderConfigurationsResponse>"""
ENC_OPTIONS_XML = """<trt:Options><tt:QualityRange><tt:Min>0</tt:Min><tt:Max>5</tt:Max></tt:QualityRange>
<tt:H264><tt:ResolutionsAvailable><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:ResolutionsAvailable>
<tt:ResolutionsAvailable><tt:Width>1280</tt:Width><tt:Height>720</tt:Height></tt:ResolutionsAvailable>
<tt:GovLengthRange><tt:Min>1</tt:Min><tt:Max>250</tt:Max></tt:GovLengthRange>
<tt:FrameRateRange><tt:Min>1</tt:Min><tt:Max>25</tt:Max></tt:FrameRateRange>
<tt:H264ProfilesSupported>Main</tt:H264ProfilesSupported></tt:H264></trt:Options>"""
ENC_SINGLE_XML = """<trt:GetVideoEncoderConfigurationResponse>
<trt:Configuration token="VideoEncoderToken_1"><tt:Name>VideoEncoder_1</tt:Name>
<tt:Encoding>H264</tt:Encoding>
<tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution>
<tt:Quality>4.000000</tt:Quality>
<tt:RateControl><tt:FrameRateLimit>25</tt:FrameRateLimit><tt:BitrateLimit>8192</tt:BitrateLimit></tt:RateControl>
<tt:H264><tt:GovLength>50</tt:GovLength></tt:H264></trt:Configuration>
</trt:GetVideoEncoderConfigurationResponse>"""


class _FakeEncoder:
    def __init__(self, single=ENC_SINGLE_XML):
        self.bodies: list[str] = []
        self._single = single

    async def caps(self, ip, u, p, port):
        return {"media": "http://x/onvif/Media", "imaging": "http://x/onvif/Imaging"}

    async def req(self, url, body, u, p):
        self.bodies.append(body)
        if "GetVideoEncoderConfigurationOptions" in body:
            return 200, ENC_OPTIONS_XML
        if "GetVideoEncoderConfigurations" in body:
            return 200, ENC_CONFIGS_XML
        if "GetVideoEncoderConfiguration" in body:
            return 200, self._single
        return 200, "<ok/>"  # SetVideoEncoderConfiguration


@pytest.mark.asyncio
async def test_get_encoder(monkeypatch):
    fake = _FakeEncoder()
    monkeypatch.setattr(ods, "get_capabilities", fake.caps)
    monkeypatch.setattr(ods, "_onvif_request", fake.req)
    enc = await OnvifDriver(**CREDS).get_encoder()
    assert enc.supported and len(enc.configs) == 2
    main = enc.configs[0]
    assert main["token"] == "VideoEncoderToken_1"
    assert main["encoding"] == "H264"
    assert (main["width"], main["height"]) == (1920, 1080)
    assert main["fps"] == 25 and main["bitrate"] == 8192 and main["gov_length"] == 50
    assert {"width": 1280, "height": 720} in main["options"]["resolutions"]
    assert main["options"]["fps_range"] == {"min": 1, "max": 25}
    assert enc.configs[1]["encoding"] == "H265"


@pytest.mark.asyncio
async def test_set_encoder_mutates_exact_block_and_preserves_rest(monkeypatch):
    fake = _FakeEncoder()
    monkeypatch.setattr(ods, "get_capabilities", fake.caps)
    monkeypatch.setattr(ods, "_onvif_request", fake.req)
    await OnvifDriver(**CREDS).set_encoder(
        "VideoEncoderToken_1", {"bitrate": 4096, "width": 1280, "height": 720}
    )
    body = next(b for b in fake.bodies if "SetVideoEncoderConfiguration" in b)
    assert "<tt:BitrateLimit>4096</tt:BitrateLimit>" in body  # changed
    assert "<tt:Width>1280</tt:Width>" in body and "<tt:Height>720</tt:Height>" in body
    # Untouched fields preserved verbatim from the camera's own XML:
    assert "<tt:Encoding>H264</tt:Encoding>" in body
    assert "<tt:GovLength>50</tt:GovLength>" in body
    assert '<trt:Configuration token="VideoEncoderToken_1"' in body
    assert "<trt:ForcePersistence>true</trt:ForcePersistence>" in body


@pytest.mark.asyncio
async def test_set_encoder_unknown_token_404(monkeypatch):
    from fastapi import HTTPException

    fake = _FakeEncoder(single="<trt:GetVideoEncoderConfigurationResponse/>")
    monkeypatch.setattr(ods, "get_capabilities", fake.caps)
    monkeypatch.setattr(ods, "_onvif_request", fake.req)
    with pytest.raises(HTTPException) as e:
        await OnvifDriver(**CREDS).set_encoder("nope", {"bitrate": 1000})
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_reconcile_skips_when_not_provisioned():
    """The MediaMTX reconcile is a no-op (not an error) for a camera that
    isn't provisioned to a stream — the encoder change still stands."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import models
    from routers.camera_settings import _reconcile_stream

    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    role = models.Role(name="admin")
    s.add(role)
    s.commit()
    user = models.User(username="u", email="u@x", hashed_password="x", role_id=role.id)
    s.add(user)
    s.commit()
    cam = models.Camera(name="c", ip_address="10.0.0.5", owner_id=user.id)
    s.add(cam)
    s.commit()
    s.refresh(cam)
    result = await _reconcile_stream(s, cam)
    assert result["reconciled"] is False
    assert "not provisioned" in result["reason"]


# ---------------------------------------------------------------------------
# OSD + Motion (Phase 4)
# ---------------------------------------------------------------------------

OVERLAY_XML = """<VideoOverlay version="2.0">
<TextOverlayList size="4">
<TextOverlay><id>1</id><enabled>false</enabled><displayText></displayText></TextOverlay>
<TextOverlay><id>2</id><enabled>false</enabled><displayText></displayText></TextOverlay>
</TextOverlayList>
<DateTimeOverlay><enabled>true</enabled><dateStyle>MM-DD-YYYY</dateStyle></DateTimeOverlay>
<channelNameOverlay version="2.0"><enabled>true</enabled></channelNameOverlay>
</VideoOverlay>"""
MOTION_XML = """<MotionDetection version="2.0"><enabled>false</enabled>
<enableHighlight>true</enableHighlight>
<MotionDetectionLayout><sensitivityLevel>60</sensitivityLevel>
<layout><gridMap>fffffc</gridMap></layout></MotionDetectionLayout></MotionDetection>"""
_OK = "<ResponseStatus><statusCode>1</statusCode><statusString>OK</statusString></ResponseStatus>"


class _FakeIsapiRW:
    def __init__(self, get_map):
        self.get_map = get_map
        self.puts: list[tuple[str, str]] = []

    async def __call__(self, ip, path, user, pw, *, method="GET", port=80,
                       body=None, timeout=15.0):
        if method == "PUT":
            self.puts.append((path, body))
            return 200, _OK
        for k, xml in self.get_map.items():
            if k in path:
                return 200, xml
        return 404, ""


@pytest.mark.asyncio
async def test_get_osd(monkeypatch):
    fake = _FakeIsapiRW({"overlays": OVERLAY_XML})
    monkeypatch.setattr(hik, "isapi_request", fake)
    osd = await HikvisionIsapiDriver(**CREDS).get_osd()
    assert osd.supported
    assert osd.datetime_enabled is True
    assert osd.channel_name_enabled is True
    assert osd.text_enabled is False
    assert osd.text == ""


@pytest.mark.asyncio
async def test_set_osd_toggles_datetime_preserves_channel(monkeypatch):
    fake = _FakeIsapiRW({"overlays": OVERLAY_XML})
    monkeypatch.setattr(hik, "isapi_request", fake)
    await HikvisionIsapiDriver(**CREDS).set_osd({"datetime_enabled": False})
    _, body = fake.puts[0]
    assert "<DateTimeOverlay><enabled>false</enabled>" in body
    # channel-name overlay untouched
    assert '<channelNameOverlay version="2.0"><enabled>true</enabled>' in body


@pytest.mark.asyncio
async def test_set_osd_custom_text(monkeypatch):
    fake = _FakeIsapiRW({"overlays": OVERLAY_XML})
    monkeypatch.setattr(hik, "isapi_request", fake)
    await HikvisionIsapiDriver(**CREDS).set_osd({"text_enabled": True, "text": "GATE"})
    _, body = fake.puts[0]
    assert "<TextOverlay><id>1</id><enabled>true</enabled>" in body
    assert "<displayText>GATE</displayText>" in body


@pytest.mark.asyncio
async def test_get_motion(monkeypatch):
    fake = _FakeIsapiRW({"motionDetection": MOTION_XML})
    monkeypatch.setattr(hik, "isapi_request", fake)
    m = await HikvisionIsapiDriver(**CREDS).get_motion()
    assert m.supported
    assert m.enabled is False
    assert m.sensitivity == 60


@pytest.mark.asyncio
async def test_set_motion_enable_and_clamp(monkeypatch):
    fake = _FakeIsapiRW({"motionDetection": MOTION_XML})
    monkeypatch.setattr(hik, "isapi_request", fake)
    await HikvisionIsapiDriver(**CREDS).set_motion({"enabled": True, "sensitivity": 150})
    _, body = fake.puts[0]
    assert "<enabled>true</enabled>" in body
    assert "<sensitivityLevel>100</sensitivityLevel>" in body  # clamped 150 -> 100


@pytest.mark.asyncio
async def test_set_motion_raises_on_error_status(monkeypatch):
    from fastapi import HTTPException

    async def fake(ip, path, user, pw, *, method="GET", port=80, body=None, timeout=15.0):
        if method == "PUT":
            return 200, "<ResponseStatus><statusCode>4</statusCode><statusString>Invalid</statusString></ResponseStatus>"
        return 200, MOTION_XML

    monkeypatch.setattr(hik, "isapi_request", fake)
    with pytest.raises(HTTPException) as e:
        await HikvisionIsapiDriver(**CREDS).set_motion({"enabled": True})
    assert e.value.status_code == 502
    assert "Invalid" in str(e.value.detail)


@pytest.mark.asyncio
async def test_onvif_baseline_osd_motion_unsupported():
    # The ONVIF baseline driver reports OSD/motion as unsupported (no vendor API).
    osd = await OnvifDriver(**CREDS).get_osd()
    motion = await OnvifDriver(**CREDS).get_motion()
    assert osd.supported is False
    assert motion.supported is False


# ---------------------------------------------------------------------------
# Camera events (Phase 5)
# ---------------------------------------------------------------------------

VMD_ALERT = """<EventNotificationAlert version="2.0">
<channelID>1</channelID><dateTime>2026-07-04T18:00:00+05:30</dateTime>
<eventType>VMD</eventType><eventState>active</eventState>
<eventDescription>Motion alarm</eventDescription></EventNotificationAlert>"""
HEARTBEAT_ALERT = """<EventNotificationAlert version="2.0">
<eventType>videoloss</eventType><eventState>inactive</eventState>
<eventDescription>videoloss alarm</eventDescription></EventNotificationAlert>"""


def test_parse_event_alert_motion():
    from services.camera_drivers.hikvision.driver import parse_event_alert

    ev = parse_event_alert(VMD_ALERT)
    assert ev is not None
    assert ev["event_type"] == "VMD"
    assert ev["event_state"] == "active"
    assert ev["description"] == "Motion alarm"
    assert ev["occurred_at"] == "2026-07-04T18:00:00+05:30"


def test_parse_event_alert_filters_heartbeat():
    from services.camera_drivers.hikvision.driver import parse_event_alert

    assert parse_event_alert(HEARTBEAT_ALERT) is None  # videoloss/inactive keepalive


@pytest.mark.asyncio
async def test_baseline_subscribe_events_unsupported():
    with pytest.raises(NotImplementedError):
        async for _ in OnvifDriver(**CREDS).subscribe_events():
            pass


@pytest.mark.asyncio
async def test_event_manager_persists_and_publishes(monkeypatch):
    import asyncio

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import core.database as cdb
    import models
    import services.camera_drivers.registry as reg
    import services.event_bus_service as ebs
    from services.camera_event_manager import CameraEventManager

    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.Base.metadata.create_all(eng)
    SessionLocal = sessionmaker(bind=eng)
    monkeypatch.setattr(cdb, "SessionLocal", SessionLocal)

    class FakeDriver:
        driver_name = "fake"

        async def subscribe_events(self):
            yield {"event_type": "VMD", "event_state": "active",
                   "description": "Motion", "occurred_at": "2026-07-04T18:00:00+05:30"}
            yield {"event_type": "tamperdetection", "event_state": "active",
                   "description": "Tamper", "occurred_at": "2026-07-04T18:00:05+05:30"}
            await asyncio.sleep(3600)  # keep the stream open

    async def fake_get_driver(db, cid):
        return FakeDriver()

    monkeypatch.setattr(reg, "get_driver_for_camera", fake_get_driver)

    published: list = []

    async def fake_pub(*, camera_id, event_type, payload):
        published.append((camera_id, event_type))

    monkeypatch.setattr(ebs, "publish_camera_event", fake_pub)

    s = SessionLocal()
    role = models.Role(name="admin")
    s.add(role)
    s.commit()
    user = models.User(username="u", email="u@x", hashed_password="x", role_id=role.id)
    s.add(user)
    s.commit()
    cam = models.Camera(name="c", ip_address="10.0.0.5", owner_id=user.id)
    s.add(cam)
    s.commit()
    cam_id = cam.id
    s.close()

    mgr = CameraEventManager()
    assert mgr.is_running(cam_id) is False
    await mgr.start(cam_id)
    assert mgr.is_running(cam_id) is True
    await asyncio.sleep(0.25)  # let the two events flow through
    await mgr.stop(cam_id)
    assert mgr.is_running(cam_id) is False

    check = SessionLocal()
    rows = check.query(models.CameraEvent).filter(
        models.CameraEvent.camera_id == cam_id
    ).all()
    check.close()
    types_seen = {r.event_type for r in rows}
    assert "VMD" in types_seen and "tamperdetection" in types_seen
    assert ("VMD" in {t for _, t in published})


# ---------------------------------------------------------------------------
# On-camera users + reboot (Phase 7)
# ---------------------------------------------------------------------------

USERS_XML = """<UserList version="2.0">
<User version="2.0"><id>1</id><userName>admin</userName><userLevel>Administrator</userLevel></User>
<User version="2.0"><id>2</id><userName>guest</userName><userLevel>Operator</userLevel></User>
</UserList>"""


class _FakeUsers:
    def __init__(self):
        self.calls: list[tuple[str, str, str | None]] = []

    async def __call__(self, ip, path, user, pw, *, method="GET", port=80,
                       body=None, timeout=15.0):
        self.calls.append((method, path, body))
        if method == "GET" and "users" in path:
            return 200, USERS_XML
        return 200, _OK


@pytest.mark.asyncio
async def test_get_users_flags_current(monkeypatch):
    fake = _FakeUsers()
    monkeypatch.setattr(hik, "isapi_request", fake)
    # CREDS username == "admin", so admin must be flagged is_current.
    info = await HikvisionIsapiDriver(**CREDS).get_users()
    assert info.supported
    by_name = {u["name"]: u for u in info.users}
    assert by_name["admin"]["is_current"] is True
    assert by_name["guest"]["is_current"] is False
    assert by_name["guest"]["id"] == "2"


@pytest.mark.asyncio
async def test_create_user_builds_body_with_next_id(monkeypatch):
    fake = _FakeUsers()
    monkeypatch.setattr(hik, "isapi_request", fake)
    await HikvisionIsapiDriver(**CREDS).create_user("bob", "pw123", "Operator")
    post = next(c for c in fake.calls if c[0] == "POST")
    body = post[2]
    assert "<id>3</id>" in body  # next after 1,2
    assert "<userName>bob</userName>" in body
    assert "<password>pw123</password>" in body
    assert "<userLevel>Operator</userLevel>" in body


@pytest.mark.asyncio
async def test_create_user_rejects_bad_level(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(hik, "isapi_request", _FakeUsers())
    with pytest.raises(HTTPException) as e:
        await HikvisionIsapiDriver(**CREDS).create_user("bob", "pw", "Superuser")
    assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_delete_user_refuses_openvr_account(monkeypatch):
    from fastapi import HTTPException

    fake = _FakeUsers()
    monkeypatch.setattr(hik, "isapi_request", fake)
    # CREDS username is "admin" — deleting it must be refused, and no DELETE sent.
    with pytest.raises(HTTPException) as e:
        await HikvisionIsapiDriver(**CREDS).delete_user("admin")
    assert e.value.status_code == 400
    assert "Refusing" in str(e.value.detail)
    assert not any(c[0] == "DELETE" for c in fake.calls)


@pytest.mark.asyncio
async def test_delete_user_targets_right_id(monkeypatch):
    fake = _FakeUsers()
    monkeypatch.setattr(hik, "isapi_request", fake)
    await HikvisionIsapiDriver(**CREDS).delete_user("guest")
    delete = next(c for c in fake.calls if c[0] == "DELETE")
    assert delete[1].endswith("/Security/users/2")


@pytest.mark.asyncio
async def test_delete_user_not_found(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(hik, "isapi_request", _FakeUsers())
    with pytest.raises(HTTPException) as e:
        await HikvisionIsapiDriver(**CREDS).delete_user("nobody")
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_reboot(monkeypatch):
    fake = _FakeUsers()
    monkeypatch.setattr(hik, "isapi_request", fake)
    result = await HikvisionIsapiDriver(**CREDS).reboot()
    assert result == {"status": "rebooting"}
    reboot = next(c for c in fake.calls if c[0] == "PUT")
    assert reboot[1].endswith("/System/reboot")


@pytest.mark.asyncio
async def test_onvif_baseline_users_reboot_unsupported():
    assert (await OnvifDriver(**CREDS).get_users()).supported is False
    with pytest.raises(NotImplementedError):
        await OnvifDriver(**CREDS).reboot()


def test_no_ip_change_or_reset_methods_exist():
    """The safety invariant: these methods must NOT exist anywhere in the
    driver hierarchy, so credential-loss + IP-move can't brick a camera."""
    from services.camera_drivers.base import CameraDriver

    for forbidden in ("set_network", "set_ip", "factory_reset"):
        assert not hasattr(CameraDriver, forbidden)
        assert not hasattr(HikvisionIsapiDriver, forbidden)
        assert not hasattr(OnvifDriver, forbidden)


# ---------------------------------------------------------------------------
# Migration guard
# ---------------------------------------------------------------------------

_MIG = (
    REPO_ROOT / "server" / "migrations" / "versions"
    / "e4a1c7b9d2f3_add_camera_capabilities.py"
)


def test_migration_chains_off_head_and_creates_table():
    text = _MIG.read_text()
    assert 'revision: str = "e4a1c7b9d2f3"' in text
    assert 'down_revision: str | None = "e3a7c92d4f18"' in text
    assert '"camera_capabilities"' in text
    for col in ("supported_areas", "onvif_endpoints", "probe_result", "probed_at"):
        assert col in text
    down = text.split("def downgrade()", 1)[1]
    assert "drop_table" in down and "camera_capabilities" in down


def test_camera_events_migration_chains_off_capabilities():
    mig = (
        REPO_ROOT / "server" / "migrations" / "versions"
        / "f2b8d3e6a9c1_add_camera_events.py"
    )
    text = mig.read_text()
    assert 'revision: str = "f2b8d3e6a9c1"' in text
    assert 'down_revision: str | None = "e4a1c7b9d2f3"' in text
    assert '"camera_events"' in text
    for col in ("event_type", "event_state", "occurred_at"):
        assert col in text
    down = text.split("def downgrade()", 1)[1]
    assert "drop_table" in down and "camera_events" in down


# ---------------------------------------------------------------------------
# Hikvision imaging extras (ImageFlip + noiseReduce over ISAPI)
# ---------------------------------------------------------------------------

FLIP_OFF = '<ImageFlip version="2.0"><enabled>false</enabled></ImageFlip>'
NOISE_XML = (
    '<NoiseReduce version="2.0"><mode>general</mode>'
    "<GeneralMode><generalLevel>50</generalLevel></GeneralMode></NoiseReduce>"
)


class _FakeIsapiImg:
    """Scripted ISAPI fake that records every call as (path, method, body)."""

    def __init__(self, put_status=200, put_text="<statusCode>1</statusCode>"):
        self.calls: list[tuple] = []
        self.put_status = put_status
        self.put_text = put_text

    async def __call__(self, ip, path, user, pw, **kw):
        method = kw.get("method", "GET")
        self.calls.append((path, method, kw.get("body")))
        if method != "GET":
            return self.put_status, self.put_text
        if "ImageFlip" in path:
            return 200, FLIP_OFF
        if "noiseReduce" in path:
            return 200, NOISE_XML
        return 404, ""


def _base_imaging(monkeypatch, source="onvif"):
    """Stub the ONVIF baseline so only the ISAPI augmentation is under test."""
    from services.camera_drivers.base import ImagingInfo

    async def fake(self):
        return ImagingInfo(
            supported=True,
            settings={"brightness": 50},
            ranges={"brightness": {"min": 0, "max": 100}},
            source=source,
        )

    monkeypatch.setattr(OnvifDriver, "get_imaging", fake)


@pytest.mark.asyncio
async def test_hik_imaging_adds_flip_and_noise(monkeypatch):
    _base_imaging(monkeypatch)
    monkeypatch.setattr(hik, "isapi_request", _FakeIsapiImg())
    info = await HikvisionIsapiDriver(**CREDS).get_imaging()
    assert info.settings["flip"] == "OFF"
    assert info.ranges["flip"]["options"] == ["ON", "OFF"]
    assert info.settings["noise_reduce_mode"] == "general"
    assert info.settings["noise_reduction"] == 50
    assert info.ranges["noise_reduction"] == {"min": 0, "max": 100}
    assert info.settings["brightness"] == 50  # ONVIF values preserved
    assert info.source == "onvif+isapi"


@pytest.mark.asyncio
async def test_hik_set_flip_patches_enabled(monkeypatch):
    _base_imaging(monkeypatch)
    fake = _FakeIsapiImg()
    monkeypatch.setattr(hik, "isapi_request", fake)
    await HikvisionIsapiDriver(**CREDS).set_imaging({"flip": "ON"})
    put = next(c for c in fake.calls if c[1] == "PUT" and "ImageFlip" in c[0])
    assert "<enabled>true</enabled>" in put[2]


@pytest.mark.asyncio
async def test_hik_set_noise_clamps_and_preserves_mode(monkeypatch):
    _base_imaging(monkeypatch)
    fake = _FakeIsapiImg()
    monkeypatch.setattr(hik, "isapi_request", fake)
    await HikvisionIsapiDriver(**CREDS).set_imaging({"noise_reduction": 999})
    put = next(c for c in fake.calls if c[1] == "PUT" and "noiseReduce" in c[0])
    assert "<generalLevel>100</generalLevel>" in put[2]
    assert "<mode>general</mode>" in put[2]  # untouched sibling preserved


@pytest.mark.asyncio
async def test_hik_set_noise_bad_mode_rejected(monkeypatch):
    from fastapi import HTTPException

    _base_imaging(monkeypatch)
    monkeypatch.setattr(hik, "isapi_request", _FakeIsapiImg())
    with pytest.raises(HTTPException) as ei:
        await HikvisionIsapiDriver(**CREDS).set_imaging({"noise_reduce_mode": "bogus"})
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_hik_set_imaging_strips_isapi_keys_before_onvif(monkeypatch):
    """ONVIF baseline rejects unknown keys, so flip/NR must not reach it."""
    _base_imaging(monkeypatch)
    seen = {}

    async def fake_set(self, patch):
        seen.update(patch)
        return None

    monkeypatch.setattr(OnvifDriver, "set_imaging", fake_set)
    monkeypatch.setattr(hik, "isapi_request", _FakeIsapiImg())
    patch = {"brightness": 60, "flip": "ON", "noise_reduction": 10}
    await HikvisionIsapiDriver(**CREDS).set_imaging(patch)
    assert seen == {"brightness": 60}
    assert patch["flip"] == "ON"  # caller's dict not mutated


# ---------------------------------------------------------------------------
# Smart analytics (line crossing / intrusion / face / tamper)
# ---------------------------------------------------------------------------

LINE_NO_REGION = (
    "<LineDetection><id>1</id><enabled>false</enabled>"
    "<LineItem><id>1</id><enabled>false</enabled>"
    "<sensitivityLevel>50</sensitivityLevel><CoordinatesList>"
    "<Coordinates><positionX>0</positionX><positionY>1000</positionY></Coordinates>"
    "<Coordinates><positionX>0</positionX><positionY>1000</positionY></Coordinates>"
    "</CoordinatesList></LineItem></LineDetection>"
)
LINE_WITH_REGION = LINE_NO_REGION.replace(
    "<positionX>0</positionX><positionY>1000</positionY></Coordinates>"
    "<Coordinates><positionX>0</positionX><positionY>1000</positionY>",
    "<positionX>10</positionX><positionY>20</positionY></Coordinates>"
    "<Coordinates><positionX>900</positionX><positionY>800</positionY>",
)
FACE_XML = (
    "<FaceDetect><enabled>true</enabled>"
    "<sensitivityLevel>3</sensitivityLevel></FaceDetect>"
)


class _FakeSmart:
    """Serves each detector path; 403 for the ones this model lacks."""

    def __init__(self, line=LINE_NO_REGION):
        self.calls: list[tuple] = []
        self.line = line

    async def __call__(self, ip, path, user, pw, **kw):
        method = kw.get("method", "GET")
        self.calls.append((path, method, kw.get("body")))
        if method != "GET":
            return 200, "<statusCode>1</statusCode>"
        if "LineDetection" in path:
            return 200, self.line
        if "FaceDetect" in path:
            return 200, FACE_XML
        if "FieldDetection" in path or "tamperDetection" in path:
            return 403, "<ResponseStatus/>"  # not supported on this model
        return 404, ""


@pytest.mark.asyncio
async def test_smart_reports_all_detectors(monkeypatch):
    monkeypatch.setattr(hik, "isapi_request", _FakeSmart())
    info = await HikvisionIsapiDriver(**CREDS).get_smart()
    assert info.supported is True
    by = {d.key: d for d in info.detectors}
    assert set(by) == {"line_crossing", "intrusion", "face", "tamper"}
    # unsupported detectors are reported, not omitted
    assert by["intrusion"].supported is False
    assert by["tamper"].supported is False
    assert by["face"].supported is True
    assert by["face"].enabled is True
    assert by["face"].sensitivity == 3
    assert by["face"].sensitivity_max == 5
    assert by["face"].configured is None  # face needs no region


@pytest.mark.asyncio
async def test_smart_flags_undrawn_region(monkeypatch):
    """A line whose points are all identical is inert — must not read as ready."""
    monkeypatch.setattr(hik, "isapi_request", _FakeSmart())
    info = await HikvisionIsapiDriver(**CREDS).get_smart()
    line = next(d for d in info.detectors if d.key == "line_crossing")
    assert line.supported is True
    assert line.configured is False


@pytest.mark.asyncio
async def test_smart_detects_drawn_region(monkeypatch):
    monkeypatch.setattr(hik, "isapi_request", _FakeSmart(line=LINE_WITH_REGION))
    info = await HikvisionIsapiDriver(**CREDS).get_smart()
    line = next(d for d in info.detectors if d.key == "line_crossing")
    assert line.configured is True


@pytest.mark.asyncio
async def test_smart_set_enables_and_clamps(monkeypatch):
    fake = _FakeSmart()
    monkeypatch.setattr(hik, "isapi_request", fake)
    await HikvisionIsapiDriver(**CREDS).set_smart(
        "face", {"enabled": True, "sensitivity": 99}
    )
    put = next(c for c in fake.calls if c[1] == "PUT")
    assert "<enabled>true</enabled>" in put[2]
    assert "<sensitivityLevel>5</sensitivityLevel>" in put[2]  # clamped to face max


@pytest.mark.asyncio
async def test_smart_unknown_detector_404(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(hik, "isapi_request", _FakeSmart())
    with pytest.raises(HTTPException) as ei:
        await HikvisionIsapiDriver(**CREDS).set_smart("teleport", {"enabled": True})
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_smart_unsupported_detector_rejected(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(hik, "isapi_request", _FakeSmart())
    with pytest.raises(HTTPException) as ei:
        await HikvisionIsapiDriver(**CREDS).set_smart("intrusion", {"enabled": True})
    assert ei.value.status_code == 502


# ---------------------------------------------------------------------------
# Camera-side security: IP filter guardrails, cloud, login lock
# ---------------------------------------------------------------------------

IPFILTER_OFF = (
    '<IPFilter version="2.0"><enabled>false</enabled>'
    "<permissionType>deny</permissionType>"
    '<IPFilterAddressList size="64"></IPFilterAddressList></IPFilter>'
)
LOGINLOCK = "<IllegalLoginLock><enabled>true</enabled></IllegalLoginLock>"
EZVIZ_ON = (
    "<EZVIZ><enabled>true</enabled><serverAddress>"
    "<hostName>dev.hik-connect.com</hostName></serverAddress></EZVIZ>"
)
ADMINACCESS = (
    "<AdminAccessProtocolList>"
    "<AdminAccessProtocol><id>1</id><enabled>true</enabled>"
    "<protocol>HTTP</protocol><portNo>80</portNo></AdminAccessProtocol>"
    "<AdminAccessProtocol><id>2</id><enabled>false</enabled>"
    "<protocol>HTTPS</protocol><portNo>443</portNo></AdminAccessProtocol>"
    "</AdminAccessProtocolList>"
)
SERVER_IP = "192.168.1.50"


class _FakeSec:
    """Serves the security endpoints; PUTs echo back into the stored list."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.stored = IPFILTER_OFF

    async def __call__(self, ip, path, user, pw, **kw):
        method = kw.get("method", "GET")
        body = kw.get("body")
        self.calls.append((path, method, body))
        if method != "GET":
            if "ipFilter" in path and body:
                self.stored = body
            return 200, "<statusCode>1</statusCode>"
        if "ipFilter" in path:
            return 200, self.stored
        if "illegalLoginLock" in path:
            return 200, LOGINLOCK
        if "EZVIZ" in path:
            return 200, EZVIZ_ON
        if "adminAccesses" in path:
            return 200, ADMINACCESS
        return 404, ""


def _sec_driver(monkeypatch, fake=None, server_ip=SERVER_IP):
    fake = fake or _FakeSec()
    monkeypatch.setattr(hik, "isapi_request", fake)
    drv = HikvisionIsapiDriver(**CREDS)
    monkeypatch.setattr(
        type(drv), "_local_source_ip", lambda self: server_ip, raising=True
    )
    return drv, fake


@pytest.mark.asyncio
async def test_security_read(monkeypatch):
    drv, _ = _sec_driver(monkeypatch)
    s = await drv.get_security()
    assert s.supported and s.ip_filter_supported
    assert s.ip_filter_enabled is False
    assert s.ip_filter_mode == "deny"
    assert s.ip_filter_max == 64
    assert s.server_ip == SERVER_IP
    assert s.illegal_login_lock is True
    assert s.cloud_supported and s.cloud_enabled is True
    assert s.cloud_host == "dev.hik-connect.com"
    assert {p["protocol"] for p in s.protocols} == {"HTTP", "HTTPS"}


@pytest.mark.asyncio
async def test_ip_filter_refuses_empty_allowlist(monkeypatch):
    from fastapi import HTTPException

    drv, fake = _sec_driver(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        await drv.set_ip_filter(enabled=True, mode="allow", entries=[])
    assert ei.value.status_code == 400
    assert not [c for c in fake.calls if c[1] == "PUT"]  # nothing written


@pytest.mark.asyncio
async def test_ip_filter_force_includes_server_ip(monkeypatch):
    """Operator forgot us — the driver must add itself or we lose the camera."""
    drv, fake = _sec_driver(monkeypatch)
    result = await drv.set_ip_filter(
        enabled=True, mode="allow", entries=["10.9.9.9"]
    )
    assert SERVER_IP in result.ip_filter_entries
    first_put = next(c for c in fake.calls if c[1] == "PUT")
    assert SERVER_IP in first_put[2]
    # staged disabled first, armed only afterwards
    assert "<enabled>false</enabled>" in first_put[2]
    puts = [c for c in fake.calls if c[1] == "PUT"]
    assert "<enabled>true</enabled>" in puts[-1][2]


@pytest.mark.asyncio
async def test_ip_filter_refuses_when_source_ip_unknown(monkeypatch):
    from fastapi import HTTPException

    drv, fake = _sec_driver(monkeypatch, server_ip=None)
    with pytest.raises(HTTPException) as ei:
        await drv.set_ip_filter(enabled=True, mode="allow", entries=["10.0.0.1"])
    assert ei.value.status_code == 502
    assert not [c for c in fake.calls if c[1] == "PUT"]


@pytest.mark.asyncio
async def test_ip_filter_bad_mode_rejected(monkeypatch):
    from fastapi import HTTPException

    drv, _ = _sec_driver(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        await drv.set_ip_filter(enabled=True, mode="sideways", entries=["1.2.3.4"])
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_ip_filter_disable_does_not_require_entries(monkeypatch):
    drv, _ = _sec_driver(monkeypatch)
    s = await drv.set_ip_filter(enabled=False, mode="allow", entries=[])
    assert s.ip_filter_enabled is False


@pytest.mark.asyncio
async def test_cloud_disable(monkeypatch):
    drv, fake = _sec_driver(monkeypatch)
    await drv.set_cloud(False)
    put = next(c for c in fake.calls if c[1] == "PUT" and "EZVIZ" in c[0])
    assert "<enabled>false</enabled>" in put[2]


def test_no_protocol_write_method():
    """Disabling HTTP would cut OpenNVR off — must stay read-only."""
    for name in ("set_protocol", "set_protocols", "set_admin_access"):
        assert not hasattr(HikvisionIsapiDriver, name)


# ---------------------------------------------------------------------------
# Network services, OSD slots, NAS
# ---------------------------------------------------------------------------

UPNP_XML = "<UPnP><enabled>false</enabled><ports><enabled>true</enabled></ports></UPnP>"
SNMP_XML = "<SNMP><SNMPv1c><notificationEnabled>true</notificationEnabled></SNMPv1c></SNMP>"
QOS_XML = "<DSCPList><DSCP><id>1</id><enabled>TRUE</enabled></DSCP></DSCPList>"
HOSTS_XML = "<HttpHostNotificationList><HttpHostNotification><id>1</id><url></url></HttpHostNotification></HttpHostNotificationList>"
NAS_XML = '<nasList><supportMountType opt="NFS,SMB/CIFS"/></nasList>'
OVERLAY_SLOTS = (
    "<VideoOverlay><TextOverlayList>"
    "<TextOverlay><id>1</id><enabled>true</enabled><positionX>0</positionX>"
    "<positionY>576</positionY><displayText>Gate</displayText></TextOverlay>"
    "<TextOverlay><id>2</id><enabled>false</enabled><positionX>10</positionX>"
    "<positionY>20</positionY><displayText></displayText></TextOverlay>"
    "</TextOverlayList></VideoOverlay>"
)


class _FakeSvc:
    def __init__(self):
        self.calls: list[tuple] = []

    async def __call__(self, ip, path, user, pw, **kw):
        method = kw.get("method", "GET")
        self.calls.append((path, method, kw.get("body")))
        if method != "GET":
            return 200, "<statusCode>1</statusCode>"
        if "UPnP" in path:
            return 200, UPNP_XML
        if "SNMP" in path:
            return 200, SNMP_XML
        if "QoS" in path:
            return 200, QOS_XML
        if "httpHosts" in path:
            return 200, HOSTS_XML
        return 403, "<ResponseStatus/>"  # DDNS/FTP/mail absent on this model


@pytest.mark.asyncio
async def test_services_read(monkeypatch):
    monkeypatch.setattr(hik, "isapi_request", _FakeSvc())
    info = await HikvisionIsapiDriver(**CREDS).get_services()
    by = {s.key: s for s in info.services}
    assert info.supported is True
    assert by["upnp"].enabled is False and by["upnp"].writable is True
    # SNMP state lives in notificationEnabled, not <enabled>
    assert by["snmp"].enabled is True
    # QoS/event-hosts are display-only
    assert by["qos"].writable is False
    assert by["http_hosts"].detail == "no destination configured"
    # unsupported services are reported, not dropped
    assert by["ftp"].supported is False


@pytest.mark.asyncio
async def test_service_toggle_patches_right_tag(monkeypatch):
    fake = _FakeSvc()
    monkeypatch.setattr(hik, "isapi_request", fake)
    await HikvisionIsapiDriver(**CREDS).set_service("snmp", False)
    put = next(c for c in fake.calls if c[1] == "PUT")
    assert "<notificationEnabled>false</notificationEnabled>" in put[2]


@pytest.mark.asyncio
async def test_readonly_service_rejected(monkeypatch):
    from fastapi import HTTPException

    fake = _FakeSvc()
    monkeypatch.setattr(hik, "isapi_request", fake)
    with pytest.raises(HTTPException) as ei:
        await HikvisionIsapiDriver(**CREDS).set_service("qos", False)
    assert ei.value.status_code == 400
    assert not [c for c in fake.calls if c[1] == "PUT"]


@pytest.mark.asyncio
async def test_unknown_service_404(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(hik, "isapi_request", _FakeSvc())
    with pytest.raises(HTTPException) as ei:
        await HikvisionIsapiDriver(**CREDS).set_service("teapot", True)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_osd_reports_all_slots(monkeypatch):
    async def fake(ip, path, user, pw, **kw):
        return 200, OVERLAY_SLOTS

    monkeypatch.setattr(hik, "isapi_request", fake)
    osd = await HikvisionIsapiDriver(**CREDS).get_osd()
    assert [s["id"] for s in osd.slots] == [1, 2]
    assert osd.slots[0]["text"] == "Gate"
    assert osd.slots[1]["enabled"] is False
    assert osd.slots[1]["x"] == 10 and osd.slots[1]["y"] == 20


@pytest.mark.asyncio
async def test_osd_slot_patch_targets_one_slot(monkeypatch):
    calls = []

    async def fake(ip, path, user, pw, **kw):
        calls.append((kw.get("method", "GET"), kw.get("body")))
        return 200, OVERLAY_SLOTS if kw.get("method", "GET") == "GET" else (
            "<statusCode>1</statusCode>"
        )

    monkeypatch.setattr(hik, "isapi_request", fake)
    await HikvisionIsapiDriver(**CREDS).set_osd(
        {"slots": [{"id": 2, "enabled": True, "text": "Rear"}]}
    )
    put = next(b for m, b in calls if m == "PUT")
    assert "Rear" in put
    assert "Gate" in put  # slot 1 left untouched


@pytest.mark.asyncio
async def test_storage_reports_nas(monkeypatch):
    async def fake(ip, path, user, pw, **kw):
        if "nas" in path:
            return 200, NAS_XML
        return 200, ISAPI_STORAGE_EMPTY

    monkeypatch.setattr(hik, "isapi_request", fake)
    st = await HikvisionIsapiDriver(**CREDS).get_storage()
    assert st.nas_supported is True
    assert st.nas_mount_types == ["NFS", "SMB/CIFS"]
    assert st.nas_mounts == []


@pytest.mark.asyncio
async def test_config_export_requires_secret_key():
    """The camera rejects a keyless export with 400 — fail fast, don't call it."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        await HikvisionIsapiDriver(**CREDS).export_config("")
    assert ei.value.status_code == 400


# ---------------------------------------------------------------------------
# Capability honesty: never advertise an area the driver cannot actually serve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_onvif_baseline_only_advertises_what_it_implements(monkeypatch):
    """A mixed-vendor fleet must not get dead tabs on generic ONVIF cameras.

    The baseline has no get_users/subscribe_events/get_osd/get_motion override,
    so advertising those areas would surface tabs that are empty or error on
    use. Vendor drivers turn them on when they really implement them.
    """
    async def fake_caps(ip, user, pw, port):
        return {
            "device": "http://x/onvif/device_service",
            "media": "http://x/onvif/Media",
            "imaging": "http://x/onvif/Imaging",
            "events": "http://x/onvif/Events",  # advertised by the device…
        }

    monkeypatch.setattr(ods, "get_capabilities", fake_caps)
    areas = (await OnvifDriver(**CREDS).get_capabilities()).supported_areas
    for unimplemented in ("users", "events", "osd", "motion", "storage"):
        assert areas[unimplemented] is False, (
            f"ONVIF baseline advertises '{unimplemented}' but does not "
            f"implement it — that renders a dead tab"
        )
    # …and the areas it does implement stay on
    assert areas["info"] and areas["imaging"] and areas["encoder"]
    assert areas["time"] and areas["network"]


@pytest.mark.asyncio
async def test_hikvision_declares_events_itself(monkeypatch):
    """Hikvision implements alertStream, so it must advertise events on its own
    rather than inheriting it from the (now honest) ONVIF baseline."""
    async def fake_caps(ip, user, pw, port):
        return {"device": "http://x/onvif/device_service"}

    monkeypatch.setattr(ods, "get_capabilities", fake_caps)
    areas = (await HikvisionIsapiDriver(**CREDS).get_capabilities()).supported_areas
    assert areas["events"] is True
    assert areas["users"] is True
