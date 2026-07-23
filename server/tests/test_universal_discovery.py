# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Universal camera discovery — any port, any scheme (http/https), any brand.

Covers Pillar 1 (scheme+port resolution, unified port list) and Pillar 3
(Xiongmai/Sofia transport + package contract + safety). Deep-ONVIF extraction
(Pillar 2) is covered in test_camera_settings.py.
"""

from __future__ import annotations

import importlib.util
import os
import secrets
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))

# App settings are validated at import time; provide throwaway values so the
# driver/registry modules import (mirrors test_camera_settings.py).
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())


def _load_sofia():
    # Load the transport in isolation (it has no app-config dependency).
    spec = importlib.util.spec_from_file_location(
        "xm_sofia", _HERE / "services/camera_drivers/xiongmai/sofia.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- Pillar 1: scheme + port resolution ------------------------------------


@pytest.mark.asyncio
async def test_resolve_control_endpoint_http_any_port(monkeypatch):
    from services import onvif_digest_service as ods

    async def fake_dt(ip, port, scheme="http", timeout=10.0):
        if scheme == "http" and port == 8088:
            return {}
        raise RuntimeError("no answer")

    monkeypatch.setattr(ods, "get_system_datetime", fake_dt)
    scheme, port = await ods.resolve_control_endpoint("10.0.0.9")
    assert (scheme, port) == ("http", 8088)


@pytest.mark.asyncio
async def test_resolve_control_endpoint_https_only(monkeypatch):
    from services import onvif_digest_service as ods

    async def fake_dt(ip, port, scheme="http", timeout=10.0):
        if scheme == "https" and port == 443:
            return {}
        raise RuntimeError("no answer")

    monkeypatch.setattr(ods, "get_system_datetime", fake_dt)
    scheme, port = await ods.resolve_control_endpoint("10.0.0.9")
    assert scheme == "https" and port == 443


@pytest.mark.asyncio
async def test_resolve_control_endpoint_trusts_hint_first(monkeypatch):
    from services import onvif_digest_service as ods

    tried: list[tuple[str, int]] = []

    async def fake_dt(ip, port, scheme="http", timeout=10.0):
        tried.append((scheme, port))
        if (scheme, port) == ("https", 8088):
            return {}
        raise RuntimeError("no answer")

    monkeypatch.setattr(ods, "get_system_datetime", fake_dt)
    scheme, port = await ods.resolve_control_endpoint(
        "10.0.0.9", port_hint=8088, scheme_hint="https"
    )
    assert (scheme, port) == ("https", 8088)
    # The verified hint must be tried first (no full scan).
    assert tried[0] == ("https", 8088)


@pytest.mark.asyncio
async def test_subnet_scan_gates_onvif_probe_on_tcp_precheck(monkeypatch):
    """Regression: the /24 scan must TCP-precheck each host:port and only run the
    expensive ONVIF probe on OPEN ports — otherwise thousands of dead-IP SOAP
    probes saturate connections and real cameras (e.g. one on 8088) get missed."""
    from services import onvif_service as osvc

    probed: list[tuple[str, int]] = []
    open_ports = {("192.168.1.108", 8088)}  # only this host:port is "open"

    async def fake_tcp_open(ip, port, timeout=0.5):
        return (ip, port) in open_ports

    async def fake_probe(ip, port, timeout=0.5, scheme="http"):
        probed.append((ip, port))
        if (ip, port) in open_ports and scheme == "http":
            return {"ip": ip, "port": port, "scheme": scheme,
                    "service_urls": [f"http://{ip}:{port}/onvif/device_service"]}
        return None

    monkeypatch.setattr(osvc, "_tcp_open", fake_tcp_open)
    monkeypatch.setattr(osvc, "probe_onvif_device", fake_probe)

    devices = await osvc.scan_onvif_subnet("192.168.1.104/29")
    assert devices == [{"ip": "192.168.1.108", "scheme": "http",
                        "service_urls": ["http://192.168.1.108:8088/onvif/device_service"]}]
    # The ONVIF probe ran ONLY for the open port, never for the dead IPs.
    assert all(p == ("192.168.1.108", 8088) for p in probed)


def test_single_unified_port_list():
    """PTZ and the source resolver must not carry their own port lists — they
    route through the shared resolver (regression guard for the fragmentation
    that stranded cameras on 8088)."""
    import services.camera_source_resolver as csr

    assert not hasattr(csr, "_ONVIF_PORTS"), "resolver kept a private port list"
    src = (_HERE / "services/ptz_service.py").read_text(encoding="utf-8")
    assert "resolve_control_endpoint" in src
    assert "[80, 8000, 8080, 2020]" not in src  # the old private list is gone


# --- Pillar 3: Xiongmai / Sofia transport ----------------------------------


def test_sofia_hash_shape_and_determinism():
    sofia = _load_sofia()
    h = sofia.sofia_hash("India@123")
    assert len(h) == 8 and h.isalnum()
    assert h == sofia.sofia_hash("India@123")  # deterministic
    assert h != sofia.sofia_hash("India@124")  # sensitive to input
    # Every char maps into the documented [0-9A-Za-z] alphabet.
    assert all(c.isdigit() or c.isalpha() for c in h)


def test_sofia_packet_framing_roundtrip():
    sofia = _load_sofia()
    payload = sofia._payload({"Name": "General"})
    assert payload.endswith(b"\x0a\x00")  # newline + NUL terminator
    pkt = sofia._pack(0x0C, 3, sofia.LOGIN_REQ, payload)
    assert pkt[0] == 0xFF and pkt[1] == 0x00
    _, _, session, seq, msgid, dlen = sofia._HEADER.unpack(pkt[:20])
    assert session == 0x0C and seq == 3 and msgid == 1000
    assert dlen == len(payload)


@pytest.mark.asyncio
async def test_xiongmai_probe_uses_sofia_then_cgi(monkeypatch):
    import services.camera_drivers.xiongmai as xm

    calls: list[str] = []

    async def sofia_probe(ip, port=34567, timeout=2.0):
        calls.append("sofia")
        return False

    async def cgi_probe(ip, scheme="https", timeout=5.0):
        calls.append(f"cgi:{scheme}")
        return scheme == "https"  # AppWeb2.0 answers over https

    monkeypatch.setattr(xm.sofia, "probe", sofia_probe)
    monkeypatch.setattr(xm.cgi_session, "probe", cgi_probe)
    assert await xm.probe("10.0.0.9", 80, "admin", "pw") is True
    assert calls == ["sofia", "cgi:https"]  # https short-circuits before http


def test_xiongmai_matches_and_probe_selection():
    import services.camera_drivers.xiongmai as xm

    # "general"/"xiongmai" are string-matchable; the very common "ONVIF" report
    # has no hint substring and is instead caught by the fingerprint probe.
    assert xm.matches("general") is True
    assert xm.matches("xiongmai") is True
    assert xm.matches("onvif") is False  # → relies on probe, not matches
    assert xm.matches("hikvision") is False


def test_xiongmai_has_no_destructive_methods():
    from services.camera_drivers.xiongmai import XiongmaiDriver

    for forbidden in ("set_network", "set_ip", "factory_reset"):
        assert not hasattr(XiongmaiDriver, forbidden), (
            f"XiongmaiDriver exposes forbidden {forbidden!r}"
        )


def test_xiongmai_discovered_by_registry():
    from services.camera_drivers import registry

    names = [v.name for v in registry.get_vendors()]
    assert "xiongmai" in names
    spec = next(v for v in registry.get_vendors() if v.name == "xiongmai")
    assert spec.probe is not None  # fingerprint-selected (manufacturer lies)
    assert spec.priority == 40
