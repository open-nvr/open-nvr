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
async def test_subnet_scan_liveness_first_and_probe_only_open_ports(monkeypatch):
    """Regression: the /24 scan must be liveness-first — dead IPs get ONLY the
    cheap liveness pair (not the full candidate-port sweep), because the old
    host×port flood (~2000 SYNs at concurrency 128) wedged the Docker-Desktop
    NAT path for ~13s and made cameras vanish from back-to-back scans. A live
    host that answers RST ("closed") on the liveness ports but serves ONVIF on
    a high port (8088) must still be swept and found, and the expensive ONVIF
    probe must run only on OPEN ports."""
    from services import onvif_service as osvc

    camera = "192.168.1.108"  # RSTs 80/443, serves ONVIF on 8088
    gated: list[tuple[str, int]] = []
    probed: list[tuple[str, int]] = []

    async def fake_tcp_state(ip, port, timeout=osvc._GATE_TIMEOUT):
        gated.append((ip, port))
        if ip == camera:
            return "open" if port == 8088 else "closed"
        return "dead"

    async def fake_probe(ip, port, timeout=0.5, scheme="http"):
        probed.append((ip, port))
        if (ip, port) == (camera, 8088) and scheme == "http":
            return {"ip": ip, "port": port, "scheme": scheme,
                    "service_urls": [f"http://{ip}:{port}/onvif/device_service"]}
        return None

    monkeypatch.setattr(osvc, "_tcp_state", fake_tcp_state)
    monkeypatch.setattr(osvc, "probe_onvif_device", fake_probe)

    devices = await osvc.scan_onvif_subnet("192.168.1.104/29")
    assert devices == [{"ip": camera, "scheme": "http",
                        "service_urls": [f"http://{camera}:8088/onvif/device_service"]}]
    # The ONVIF probe ran ONLY for the open port, never for closed/dead ones.
    assert all(p == (camera, 8088) for p in probed)
    # Dead hosts got only the liveness pair; the camera got the full sweep.
    for ip, port in gated:
        if ip != camera:
            assert port in osvc._LIVENESS_PORTS, (
                f"dead host {ip} was gated on non-liveness port {port}"
            )
    assert {p for i, p in gated if i == camera} == set(osvc._ONVIF_CANDIDATE_PORTS)


@pytest.mark.asyncio
async def test_subnet_scan_retries_flaky_onvif_probe(monkeypatch):
    """A camera whose first probe is dropped (flaky embedded stack / NAT-path
    loss) must still be discovered via the single retry."""
    from services import onvif_service as osvc

    camera = "192.168.1.105"
    attempts: list[int] = []

    async def fake_tcp_state(ip, port, timeout=osvc._GATE_TIMEOUT):
        return "open" if (ip, port) == (camera, 80) else "dead"

    async def fake_probe(ip, port, timeout=0.5, scheme="http"):
        attempts.append(port)
        if len(attempts) == 1:
            return None  # first attempt dropped
        return {"ip": ip, "port": port, "scheme": scheme,
                "service_urls": [f"http://{ip}:{port}/onvif/device_service"]}

    monkeypatch.setattr(osvc, "_tcp_state", fake_tcp_state)
    monkeypatch.setattr(osvc, "probe_onvif_device", fake_probe)

    devices = await osvc.scan_onvif_subnet("192.168.1.104/30")
    assert [d["ip"] for d in devices] == [camera]
    assert attempts == [80, 80]  # one retry, same port


@pytest.mark.asyncio
async def test_stream_uri_unescapes_xml_entities(monkeypatch):
    """Regression: SOAP escapes ``&`` as ``&amp;`` inside <tt:Uri>; the raw
    regex capture stored the entity, corrupting RTSP URLs with query strings
    (``?transmode=unicast&amp;profile=va`` reached MediaMTX verbatim)."""
    from services import onvif_digest_service as ods

    soap = (
        "<s:Envelope><s:Body><trt:GetStreamUriResponse><trt:MediaUri>"
        "<tt:Uri>rtsp://10.0.0.9:554/1/1?transmode=unicast&amp;profile=va</tt:Uri>"
        "</trt:MediaUri></trt:GetStreamUriResponse></s:Body></s:Envelope>"
    )

    async def fake_caps(*a, **k):
        return {"media": "http://10.0.0.9/onvif/media_service"}

    async def fake_request(url, body, username, password):
        return 200, soap

    monkeypatch.setattr(ods, "get_capabilities", fake_caps)
    monkeypatch.setattr(ods, "_onvif_request", fake_request)

    uri = await ods.get_stream_uri_digest("10.0.0.9", "admin", "pw", "prof1")
    assert uri == "rtsp://10.0.0.9:554/1/1?transmode=unicast&profile=va"


@pytest.mark.asyncio
async def test_exclusive_scan_supersedes_previous(monkeypatch):
    """A new exclusive scan must cancel the one in flight (which then reports
    409 to its caller) — overlapping sweeps each bring their own semaphore and
    together exceed the NAT wedge threshold, silently emptying results."""
    import asyncio

    from fastapi import HTTPException

    from services import onvif_service as osvc

    monkeypatch.setattr(osvc, "_SUPERSEDE_GRACE_S", 0)
    first_running = asyncio.Event()
    first_cancelled = False
    calls = 0

    async def fake_scan(cidrs, ports=osvc._ONVIF_CANDIDATE_PORTS, concurrency=96):
        nonlocal first_cancelled, calls
        calls += 1
        if calls == 1:
            first_running.set()
            try:
                await asyncio.Event().wait()  # block until cancelled
            except asyncio.CancelledError:
                first_cancelled = True
                raise
        return [{"ip": "10.0.0.9", "scheme": "http", "service_urls": []}]

    monkeypatch.setattr(osvc, "scan_onvif_subnets", fake_scan)

    t1 = asyncio.create_task(osvc.scan_onvif_subnets_exclusive(["10.0.0.0/24"]))
    await first_running.wait()
    t2 = asyncio.create_task(osvc.scan_onvif_subnets_exclusive(["10.0.0.0/24"]))

    devices = await t2
    assert [d["ip"] for d in devices] == ["10.0.0.9"]
    with pytest.raises(HTTPException) as exc:
        await t1
    assert exc.value.status_code == 409
    assert first_cancelled


@pytest.mark.asyncio
async def test_exclusive_scan_single_flight(monkeypatch):
    """Never more than one sweep in flight across a supersede handoff."""
    import asyncio

    from services import onvif_service as osvc

    monkeypatch.setattr(osvc, "_SUPERSEDE_GRACE_S", 0)
    active = 0
    max_active = 0
    first_running = asyncio.Event()
    calls = 0

    async def fake_scan(cidrs, ports=osvc._ONVIF_CANDIDATE_PORTS, concurrency=96):
        nonlocal active, max_active, calls
        calls += 1
        active += 1
        max_active = max(max_active, active)
        try:
            if calls == 1:
                first_running.set()
                await asyncio.Event().wait()
            else:
                await asyncio.sleep(0)
            return []
        finally:
            active -= 1

    monkeypatch.setattr(osvc, "scan_onvif_subnets", fake_scan)

    t1 = asyncio.create_task(osvc.scan_onvif_subnets_exclusive(["10.0.0.0/24"]))
    await first_running.wait()
    t2 = asyncio.create_task(osvc.scan_onvif_subnets_exclusive(["10.0.0.0/24"]))
    await t2
    with pytest.raises(Exception):
        await t1
    assert max_active == 1


@pytest.mark.asyncio
async def test_probe_runs_under_scan_semaphore(monkeypatch):
    """The ONVIF probe phase must share the scan's semaphore: with
    concurrency=1 two live hosts may never be probed concurrently (the old
    unbounded probes were an extra connect burst on top of the gate budget)."""
    import asyncio

    from services import onvif_service as osvc

    live = {"192.168.1.105", "192.168.1.106"}
    probing = 0
    max_probing = 0

    async def fake_tcp_state(ip, port, timeout=osvc._GATE_TIMEOUT):
        return "open" if ip in live and port == 80 else "dead"

    async def fake_probe(ip, port, timeout=0.5, scheme="http"):
        nonlocal probing, max_probing
        probing += 1
        max_probing = max(max_probing, probing)
        await asyncio.sleep(0)
        probing -= 1
        return {"ip": ip, "port": port, "scheme": scheme,
                "service_urls": [f"http://{ip}:{port}/onvif/device_service"]}

    monkeypatch.setattr(osvc, "_tcp_state", fake_tcp_state)
    monkeypatch.setattr(osvc, "probe_onvif_device", fake_probe)

    devices = await osvc.scan_onvif_subnet("192.168.1.104/29", concurrency=1)
    assert {d["ip"] for d in devices} == live
    assert max_probing == 1


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
