# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
Tests for the camera source resolver — deriving an RTSP URL (+ identity) from
IP + credentials (ONVIF-first, vendor RTSP fallback).

    cd server && pytest tests/test_camera_source_resolver.py -v
"""

from __future__ import annotations

import datetime as _dt  # noqa: I001

if not hasattr(_dt, "UTC"):
    _dt.UTC = _dt.timezone.utc

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

import services.camera_source_resolver as csr  # noqa: E402
from services import onvif_digest_service as ods  # noqa: E402
from services.camera_source_resolver import (  # noqa: E402
    fetch_identity,
    inject_credentials,
    resolve_source,
    rtsp_port_from_url,
    sync_camera_time,
)


@pytest.mark.parametrize(
    "url,user,pw,expected",
    [
        ("rtsp://10.0.0.5:554/s", "admin", "secret", "rtsp://admin:secret@10.0.0.5:554/s"),
        ("rtsp://a:b@10.0.0.5/s", "x", "y", "rtsp://a:b@10.0.0.5/s"),  # keeps existing
        ("http://10.0.0.5/s", "a", "b", "http://10.0.0.5/s"),  # non-rtsp untouched
        ("rtsp://10.0.0.5/s", None, "b", "rtsp://10.0.0.5/s"),  # no user
        ("rtsp://h/x", "ad min", "p@ss", "rtsp://ad%20min:p%40ss@h/x"),  # encoded
        (None, "a", "b", None),
    ],
)
def test_inject_credentials(url, user, pw, expected):
    assert inject_credentials(url, user, pw) == expected


def test_inject_credentials_fixes_bare_manual_url():
    """Regression: a manual RTSP URL with separate user/pass fields must get the
    credentials embedded, otherwise MediaMTX can't authenticate and the stream
    fails. inject_credentials is a no-op once userinfo is already present."""
    bare = "rtsp://192.168.1.100:554/stream1"
    embedded = inject_credentials(bare, "admin", "pass")
    assert embedded == "rtsp://admin:pass@192.168.1.100:554/stream1"
    # Re-applying (or a URL that already has creds) must not double-embed.
    assert inject_credentials(embedded, "admin", "pass") == embedded


@pytest.mark.asyncio
async def test_resolve_via_onvif_unescapes_and_injects(monkeypatch):
    async def fake_connect(ip, u, p, port=None, scheme=None):
        # connect_and_get_profiles self-resolves the endpoint; returns an
        # XML-escaped URI (as real cameras do) plus the resolved port/scheme.
        return {
            "port": 80,
            "scheme": "http",
            "device_info": {
                "manufacturer": "HIKVISION", "model": "DS-2CD204WFWD-I",
                "firmwareversion": "V5.5.61", "serialnumber": "SN123", "hardwareid": "88",
            },
            "profiles": [
                {"token": "P1", "stream_uri":
                 "rtsp://192.168.1.64:554/Streaming/Channels/101?a=1&amp;b=2"},
            ],
        }

    monkeypatch.setattr(ods, "connect_and_get_profiles", fake_connect)
    r = await resolve_source("192.168.1.64", "admin", "pw", 554)
    assert r["source"] == "onvif"
    assert r["manufacturer"] == "HIKVISION"
    assert r["model"] == "DS-2CD204WFWD-I"
    assert r["serial_number"] == "SN123"
    # credentials injected AND the &amp; unescaped to &
    assert r["rtsp_url"] == (
        "rtsp://admin:pw@192.168.1.64:554/Streaming/Channels/101?a=1&b=2"
    )


@pytest.mark.asyncio
async def test_resolve_falls_back_to_vendor_probe(monkeypatch):
    async def fail_connect(ip, u, p, port):
        raise Exception("no onvif")

    async def fake_probe(host, port, path, user, pw, timeout=3.0):
        return path == "/Streaming/Channels/101"  # only the Hik path answers

    monkeypatch.setattr(ods, "connect_and_get_profiles", fail_connect)
    monkeypatch.setattr(csr, "_rtsp_path_works", fake_probe)
    r = await resolve_source("10.0.0.9", "admin", "pw", 554)
    assert r["source"] == "rtsp_probe"
    assert r["rtsp_url"] == "rtsp://admin:pw@10.0.0.9:554/Streaming/Channels/101"
    assert r["manufacturer"] is None  # identity unknown via raw RTSP


@pytest.mark.asyncio
async def test_resolve_returns_none_when_nothing_works(monkeypatch):
    async def fail_connect(ip, u, p, port):
        raise Exception("no onvif")

    async def no_probe(host, port, path, user, pw, timeout=3.0):
        return False

    monkeypatch.setattr(ods, "connect_and_get_profiles", fail_connect)
    monkeypatch.setattr(csr, "_rtsp_path_works", no_probe)
    assert await resolve_source("10.0.0.9", "admin", "pw", 554) is None


@pytest.mark.asyncio
async def test_fetch_identity_returns_device_info(monkeypatch):
    async def fake_connect(ip, u, p, port=None, scheme=None):
        return {
            "port": 80,
            "scheme": "http",
            "device_info": {
                "manufacturer": "HIKVISION", "model": "DS-2CD204WFWD-I",
                "firmwareversion": "V5.5.61", "serialnumber": "SN123", "hardwareid": "88",
            },
            "profiles": [],
        }

    monkeypatch.setattr(ods, "connect_and_get_profiles", fake_connect)
    r = await fetch_identity("192.168.1.64", "admin", "pw")
    assert r["manufacturer"] == "HIKVISION"
    assert r["model"] == "DS-2CD204WFWD-I"
    assert r["serial_number"] == "SN123"
    assert r["onvif_port"] == 80  # reused by the caller for time-sync
    assert r["control_scheme"] == "http"


@pytest.mark.asyncio
async def test_fetch_identity_returns_none_when_no_onvif(monkeypatch):
    async def fail_connect(ip, u, p, port):
        raise Exception("no onvif")

    monkeypatch.setattr(ods, "connect_and_get_profiles", fail_connect)
    assert await fetch_identity("10.0.0.9", "admin", "pw") is None


@pytest.mark.asyncio
async def test_sync_camera_time_uses_preferred_endpoint(monkeypatch):
    calls = []

    async def fake_resolve(ip, port_hint=None, scheme_hint=None):
        return (scheme_hint or "http", port_hint or 80)

    async def fake_set(ip, u, p, port, scheme="http"):
        calls.append((port, scheme))
        return {"synced_utc": "2026-07-05T00:00:00Z"}

    monkeypatch.setattr(ods, "resolve_control_endpoint", fake_resolve)
    monkeypatch.setattr(ods, "set_system_datetime", fake_set)
    ok = await sync_camera_time(
        "10.0.0.5", "admin", "pw", onvif_port=8000, control_scheme="http"
    )
    assert ok is True
    assert calls == [(8000, "http")]  # the persisted endpoint is trusted


@pytest.mark.asyncio
async def test_sync_camera_time_resolves_endpoint_when_none_given(monkeypatch):
    calls = []

    async def fake_resolve(ip, port_hint=None, scheme_hint=None):
        return ("http", 80)  # shared resolver finds the endpoint

    async def fake_set(ip, u, p, port, scheme="http"):
        calls.append((port, scheme))
        return {}

    monkeypatch.setattr(ods, "resolve_control_endpoint", fake_resolve)
    monkeypatch.setattr(ods, "set_system_datetime", fake_set)
    ok = await sync_camera_time("10.0.0.5", "admin", "pw")
    assert ok is True
    assert calls[0] == (80, "http")


@pytest.mark.asyncio
async def test_sync_camera_time_returns_false_and_never_raises(monkeypatch):
    async def fake_resolve(ip, port_hint=None, scheme_hint=None):
        return ("http", 80)

    async def fake_set(ip, u, p, port, scheme="http"):
        raise Exception("unreachable")

    monkeypatch.setattr(ods, "resolve_control_endpoint", fake_resolve)
    monkeypatch.setattr(ods, "set_system_datetime", fake_set)
    assert await sync_camera_time("10.0.0.5", "admin", "pw") is False


@pytest.mark.asyncio
async def test_resolve_onvif_returns_substream_by_lowest_resolution(monkeypatch):
    """ONVIF advertises BOTH encodings; the resolver must hand back the
    camera's own substream (smallest resolution) so a fresh install taps
    the low-res stream by default instead of decoding full main (~5x CPU)
    until an operator pastes a URL by hand."""
    async def fake_connect(ip, u, p, port=None, scheme=None):
        return {
            "port": 80, "scheme": "http", "device_info": {},
            "profiles": [
                {"token": "main", "width": 1920, "height": 1080,
                 "stream_uri": "rtsp://192.168.0.104:554/avstream/channel=1/stream=0.sdp"},
                {"token": "third", "width": 704, "height": 576,
                 "stream_uri": "rtsp://192.168.0.104:554/avstream/channel=1/stream=2.sdp"},
                {"token": "sub", "width": 352, "height": 288,
                 "stream_uri": "rtsp://192.168.0.104:554/avstream/channel=1/stream=1.sdp"},
            ],
        }

    monkeypatch.setattr(ods, "connect_and_get_profiles", fake_connect)
    r = await resolve_source("192.168.0.104", "admin", "pw", 554)
    assert r["rtsp_url"].endswith("stream=0.sdp")            # main unchanged
    assert r["substream_url"] == (
        "rtsp://admin:pw@192.168.0.104:554/avstream/channel=1/stream=1.sdp"
    )


@pytest.mark.asyncio
async def test_resolve_onvif_substream_none_when_single_profile(monkeypatch):
    async def fake_connect(ip, u, p, port=None, scheme=None):
        return {"port": 80, "scheme": "http", "device_info": {},
                "profiles": [{"token": "only",
                              "stream_uri": "rtsp://c/main"}]}

    monkeypatch.setattr(ods, "connect_and_get_profiles", fake_connect)
    r = await resolve_source("c", "u", "p", 554)
    assert r["substream_url"] is None


@pytest.mark.asyncio
async def test_resolve_onvif_substream_skips_duplicate_uri(monkeypatch):
    """Some cameras report two profiles pointing at the SAME stream — that is
    not a substream; storing it would double-tap main."""
    async def fake_connect(ip, u, p, port=None, scheme=None):
        return {"port": 80, "scheme": "http", "device_info": {},
                "profiles": [
                    {"token": "a", "stream_uri": "rtsp://c/main"},
                    {"token": "b", "stream_uri": "rtsp://c/main"},
                ]}

    monkeypatch.setattr(ods, "connect_and_get_profiles", fake_connect)
    r = await resolve_source("c", "u", "p", 554)
    assert r["substream_url"] is None


# --- rtsp_port_from_url ----------------------------------------------------
# `cameras.port` is the RTSP port but was never derived from `rtsp_url`, so a
# camera on 8554 showed as ":554" in the list — an address nothing answers on.


@pytest.mark.parametrize(
    "url,expected",
    [
        # Explicit port wins, standard or not.
        ("rtsp://10.0.0.5:8554/stream", 8554),
        ("rtsp://10.0.0.5:554/stream", 554),
        ("rtsp://admin:p%40ss@192.168.29.226:8554/", 8554),
        # Credentials, paths and queries must not confuse the parse.
        ("rtsp://u:p@host:2020/cam/realmonitor?channel=1&subtype=0", 2020),
        # Omitted port falls back to the scheme default.
        ("rtsp://10.0.0.5/stream", 554),
        ("rtsps://10.0.0.5/stream", 322),
        ("rtsps://10.0.0.5:8555/stream", 8555),
        # IPv6 literals keep their brackets out of the port.
        ("rtsp://[fe80::1]:8554/s", 8554),
        ("rtsp://[fe80::1]/s", 554),
        # Nothing to derive — callers must leave the stored value alone rather
        # than overwrite it with a guess.
        (None, None),
        ("", None),
        ("http://10.0.0.5:8554/stream", None),
        ("10.0.0.5:8554", None),
        ("rtsp://10.0.0.5:abc/stream", None),
        ("rtsp://10.0.0.5:0/stream", None),
    ],
)
def test_rtsp_port_from_url(url, expected):
    assert rtsp_port_from_url(url) == expected
