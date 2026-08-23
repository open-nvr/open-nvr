# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
#
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""
ONVIF helper service: discovery, connection, media and PTZ operations.

This module wraps onvif-zeep sync clients in asyncio-friendly helpers via run_in_executor.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

from core.logging_config import main_logger

# Single source of truth for ONVIF control ports (discovery scan, connect flow,
# and driver registry all share it, so a new vendor port is added in one place).
from services.onvif_digest_service import (
    ONVIF_CANDIDATE_PORTS as _ONVIF_CANDIDATE_PORTS,
)


# Lazy imports for optional deps
def _import_onvif():
    try:
        from onvif import ONVIFCamera  # type: ignore
        from onvif.client import ONVIFService  # type: ignore

        return ONVIFCamera, ONVIFService
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ONVIF library import failed: {e}")


def _import_ws_discovery():
    """Try multiple WS-Discovery implementations and return a unified adapter.

    Returns a tuple (Factory, QName, Scope) where Factory() -> instance with start(), stop(), searchServices(timeout).
    """
    # Try wsdiscovery (newer API with ThreadedWSDiscovery)
    try:
        from wsdiscovery import (
            QName,  # type: ignore
            Scope,  # type: ignore
        )
        from wsdiscovery.discovery import (
            ThreadedWSDiscovery as _ThreadedWSDiscovery,  # type: ignore
        )

        class _Factory:
            def __call__(self):
                return _ThreadedWSDiscovery()

        return _Factory(), QName, Scope
    except Exception:
        pass

    # Try WSDiscovery (older API)
    try:
        from WSDiscovery import (
            QName,  # type: ignore
            WSDiscovery as _WSDiscovery,  # type: ignore
        )

        class _Adapter:
            def __init__(self):
                self._impl = _WSDiscovery()

            def start(self):
                return self._impl.start()

            def stop(self):
                return self._impl.stop()

            def searchServices(self, timeout: int = 4):
                return self._impl.searchServices(timeout=timeout)

        class _Factory:
            def __call__(self):
                return _Adapter()

        class _Scope:  # placeholder, not used in our code path
            pass

        return _Factory(), QName, _Scope
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"WS-Discovery import failed: {e}. Install 'wsdiscovery' or 'WSDiscovery'.",
        )


# Default ONVIF port
DEFAULT_ONVIF_PORT = 80


async def _to_thread(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


async def discover_onvif_devices(timeout: int = 4) -> list[dict[str, Any]]:
    """Discover ONVIF devices on local network using WS-Discovery.

    Returns list of dicts: {ip, xaddrs: [urls]}
    """
    _WSD, _QName, _Scope = _import_ws_discovery()

    def _discover_sync() -> list[dict[str, Any]]:
        wsd = _WSD()
        try:
            wsd.start()
            # Type filter for ONVIF devices is typically "dn:NetworkVideoTransmitter" but not all devices set it.
            # We'll broadcast without type filter and parse XAddrs.
            services = wsd.searchServices(timeout=timeout)
            results: list[dict[str, Any]] = []
            for svc in services:
                xaddrs = (
                    list(svc.getXAddrs()) if getattr(svc, "getXAddrs", None) else []
                )
                # Try to extract IP from XAddr, fallback to None
                ip = None
                for url in xaddrs:
                    # Example: http://192.168.1.50/onvif/device_service
                    try:
                        # Avoid dependency on urllib due to threading, keep simple parse
                        if "://" in url:
                            hostpart = url.split("://", 1)[1].split("/", 1)[0]
                            if ":" in hostpart:
                                ip = hostpart.split(":", 1)[0]
                            else:
                                ip = hostpart
                            break
                    except Exception as e:
                        main_logger.debug(f"XAddr parse error: {e}")
                        continue
                results.append(
                    {
                        "ip": ip,
                        "service_urls": xaddrs,
                    }
                )
            return results
        finally:
            try:
                wsd.stop()
            except Exception:
                pass

    try:
        return await _to_thread(_discover_sync)
    except HTTPException:
        raise
    except Exception as e:
        main_logger.error(f"WS-Discovery failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


_ONVIF_PROBE_BODY = "<tds:GetSystemDateAndTime/>"

_SOAP_ENVELOPE = """\
<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
               xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
  <soap:Body>
    {body}
  </soap:Body>
</soap:Envelope>"""

async def probe_onvif_device(
    ip: str, port: int = 80, timeout: float = 0.5, scheme: str = "http"
) -> dict[str, Any] | None:
    """Probe a single IP:port for an ONVIF device service (no auth needed).

    Sends unauthenticated GetSystemDateAndTime — the one ONVIF operation
    cameras must answer without credentials.  Returns a device dict on hit,
    None on miss / timeout.  ``verify=False`` so TLS-only cameras (self-signed
    certs) are discovered too.
    """
    url = f"{scheme}://{ip}:{port}/onvif/device_service"
    envelope = _SOAP_ENVELOPE.format(body=_ONVIF_PROBE_BODY)
    headers = {"Content-Type": "application/soap+xml; charset=utf-8"}
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            resp = await client.post(url, content=envelope, headers=headers)
            # Some cameras (e.g. CP Plus/Dahua with "force HTTPS") answer HTTP
            # with a 3xx redirect to their HTTPS endpoint instead of serving
            # ONVIF. Follow one hop to the HTTPS device_service and re-POST so the
            # device is still discovered (and reported on its real https port).
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location", "")
                if loc.lower().startswith("https://"):
                    pu = urlparse(loc)
                    port = pu.port or 443
                    scheme = "https"
                    url = f"https://{pu.hostname}:{port}/onvif/device_service"
                    resp = await client.post(url, content=envelope, headers=headers)
        # A real ONVIF device returns 200 or 401; either confirms ONVIF service presence.
        # A non-ONVIF host typically gives connection refused, 404, or non-XML.
        if resp.status_code in (200, 401) and (
            "onvif.org" in resp.text or "SystemDateAndTime" in resp.text
        ):
            return {
                "ip": ip,
                "port": port,
                "scheme": scheme,
                "service_urls": [url],
            }
    except (httpx.ConnectError, httpx.TimeoutException, OSError):
        pass
    except Exception as exc:
        main_logger.debug(f"probe_onvif_device {scheme}://{ip}:{port} unexpected: {exc}")
    return None


# Docker Desktop's NAT relay (vpnkit) delivers a closed-port RST to the
# container ~2s after the SYN, and a dead-IP connect only ever times out. The
# gate timeout must sit above that RST latency or a live host's "closed"
# answer is indistinguishable from a dead IP (and under NAT load even an OPEN
# port's SYN-ACK can arrive late, which is how real cameras got missed).
_GATE_TIMEOUT = 2.5

# Any normal TCP stack answers a SYN on a *closed* port with an RST, so a few
# silent ports are enough to call a host dead. 80/443 are the universal web
# ports; 8000 is the most common alternate ONVIF control port.
#
# 8000 is in this list for RELIABILITY, not coverage -- phase 2 sweeps it
# anyway. A camera listening on NONE of the liveness ports is judged alive
# purely by how fast its RST comes back, and under sweep load Docker Desktop's
# NAT relay delivers that RST *later* than _GATE_TIMEOUT: both liveness ports
# read "dead", the host is dropped, and phase 2 never probes the port the
# camera actually serves. Measured on a /24 against a camera serving ONVIF on
# 8000 only (nothing on 80/443): 80/443 alone found it in 2 of 5 sweeps;
# adding 8000 found it in 5 of 5, costing ~9s on a /24. An open port answers
# with a SYN-ACK from the device itself, so it doesn't depend on RST latency --
# positive evidence beats waiting for silence to be disproved.
_LIVENESS_PORTS = (80, 443, 8000)


async def _tcp_state(ip: str, port: int, timeout: float = _GATE_TIMEOUT) -> str:
    """Classify ip:port with one TCP connect: 'open', 'closed', or 'dead'.

    'closed' (RST received) proves a live host even when nothing listens on
    that port; 'dead' (silence) on every liveness port means no device at all.
    """
    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout
        )
        return "open"
    except ConnectionRefusedError:
        return "closed"
    except Exception:
        return "dead"
    finally:
        if writer is not None:
            try:
                writer.close()
                # Wait for the FIN handshake so the semaphore slot isn't
                # released while the socket is still alive — otherwise real
                # in-flight connects transiently exceed the concurrency bound.
                # Capped: a wedged NAT path must not hang the sweep.
                await asyncio.wait_for(writer.wait_closed(), 1.0)
            except Exception:
                pass


async def scan_onvif_subnets(
    cidrs: list[str],
    ports: tuple[int, ...] = _ONVIF_CANDIDATE_PORTS,
    concurrency: int = 96,
) -> list[dict[str, Any]]:
    """Scan CIDRs for ONVIF devices using unicast TCP probes (liveness-first).

    Works across Docker bridge SNAT — no multicast required. The scan is
    deliberately gentle: firing host×port connects for every candidate port
    (~2000 SYNs on a /24) at high concurrency saturates the container's NAT
    path — measured on Docker Desktop, a concurrency-128 sweep leaves
    container→LAN connects failing for ~13s *after* the scan returns, so a
    rescan (or the tail of the same scan) misses real cameras intermittently.

    Instead: phase 1 sends only the liveness pair per host — an RST ("closed")
    counts as alive, silence on both means dead. Phase 2 sweeps the full
    candidate-port list on live hosts only and runs the ONVIF probe (with one
    retry — embedded stacks and the NAT path both drop occasional requests) on
    open ports until the first hit. One shared semaphore bounds in-flight
    connects across all subnets — the ONVIF probes included, so a subnet full
    of live web hosts can't burst past the budget; ~40 launches/s stays
    comfortably inside the measured wedge-free regime (clean at 64/s, wedged
    at 256/s).

    Returns device dicts with the {ip, scheme, service_urls} shape, deduped by
    IP across subnets.
    """
    networks = []
    for cidr in cidrs:
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid CIDR: {exc}")

    sem = asyncio.Semaphore(concurrency)

    # Ports that speak TLS get an https probe; everything else gets http (and
    # ``probe_onvif_device`` itself follows a 302 http->https for cameras that
    # force HTTPS). ONE probe per open port — firing both schemes at each host
    # hammered flaky embedded cameras, causing intermittent discovery misses.
    tls_ports = (443, 8443)

    liveness = tuple(p for p in _LIVENESS_PORTS if p in ports) or tuple(ports[:2])

    async def _gate(ip: str, port: int) -> str:
        async with sem:
            return await _tcp_state(ip, port)

    async def _scan_host(ip: str) -> dict[str, Any] | None:
        # Phase 1: liveness. Any TCP answer (SYN-ACK or RST) marks the host
        # alive; silence on all liveness ports means nothing is there.
        states = await asyncio.gather(*(_gate(ip, p) for p in liveness))
        if all(s == "dead" for s in states):
            return None

        open_ports = {p for p, s in zip(liveness, states) if s == "open"}
        # Phase 2: sweep the remaining candidate ports on this live host.
        rest = [p for p in ports if p not in liveness]
        rest_states = await asyncio.gather(*(_gate(ip, p) for p in rest))
        open_ports.update(p for p, s in zip(rest, rest_states) if s == "open")

        # Probe open ports in candidate-list order; first ONVIF answer wins.
        # Probes take a semaphore slot too: on a subnet with many live web
        # hosts (NAS, printers, ...) unbounded probes were an extra connect
        # burst on top of the gate budget.
        for port in sorted(open_ports, key=ports.index):
            scheme = "https" if port in tls_ports else "http"
            async with sem:
                dev = await probe_onvif_device(ip, port, timeout=3.0, scheme=scheme)
            if dev is None:
                async with sem:
                    dev = await probe_onvif_device(
                        ip, port, timeout=3.0, scheme=scheme
                    )
            if dev:
                return {
                    "ip": dev["ip"],
                    "scheme": dev.get("scheme", "http"),
                    "service_urls": dev["service_urls"],
                }
        return None

    seen: set[str] = set()
    hosts: list[str] = []
    for network in networks:
        for host in network.hosts():
            ip = str(host)
            if ip not in seen:
                seen.add(ip)
                hosts.append(ip)

    results = await asyncio.gather(
        *(_scan_host(ip) for ip in hosts), return_exceptions=True
    )
    return [r for r in results if isinstance(r, dict)]


# --- Exclusive discovery slot ------------------------------------------------
# At most one subnet sweep runs per process. Two overlapping sweeps each bring
# their own Semaphore(96), doubling the connect rate past the vpnkit wedge
# threshold documented above — the wedged sweep then sees every host as dead
# and silently returns an empty device list. A new scan SUPERSEDES the running
# one (cancels it and waits for it to wind down) instead of queueing,
# mirroring the frontend's abort-and-restart pattern. Discovery is a
# superuser-only feature: two admin sessions scanning at once also supersede
# each other (last scan wins; the earlier request gets a 409).
_scan_handoff = asyncio.Lock()
_scan_task: asyncio.Task | None = None
# After cancelling a sweep its in-flight connects tear down immediately, but
# SYNs already queued in the NAT relay are not. A short drain keeps the
# combined launch rate of old+new sweep inside the wedge-free regime. The
# full ~13s wedge tail does NOT need waiting out — that wedge only follows
# sustained over-rate scanning, which single-flight prevents.
_SUPERSEDE_GRACE_S = 1.0


async def scan_onvif_subnets_exclusive(
    cidrs: list[str],
    ports: tuple[int, ...] = _ONVIF_CANDIDATE_PORTS,
    concurrency: int = 96,
) -> list[dict[str, Any]]:
    """Run :func:`scan_onvif_subnets` as the process's only sweep.

    Cancels and drains any sweep already in flight before starting (see the
    module comment above). Raises 409 when THIS sweep is in turn superseded
    by a newer one. Cancelling the awaiting task (e.g. the HTTP handler on
    client disconnect) cancels the underlying sweep too.
    """
    global _scan_task
    async with _scan_handoff:
        prev = _scan_task
        if prev is not None and not prev.done():
            prev.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await prev
            await asyncio.sleep(_SUPERSEDE_GRACE_S)
        task = asyncio.create_task(
            scan_onvif_subnets(cidrs, ports=ports, concurrency=concurrency)
        )
        _scan_task = task
    try:
        return await task
    except asyncio.CancelledError:
        if task.cancelled():
            # The task itself was cancelled: a newer scan superseded it.
            raise HTTPException(
                status_code=409,
                detail="Scan superseded by a newer discovery scan.",
            )
        # Our awaiter was cancelled (client disconnect): stop the sweep too,
        # then let the cancellation propagate.
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        raise


async def scan_onvif_subnet(
    cidr: str,
    ports: tuple[int, ...] = _ONVIF_CANDIDATE_PORTS,
    concurrency: int = 96,
) -> list[dict[str, Any]]:
    """Single-CIDR wrapper around :func:`scan_onvif_subnets`."""
    return await scan_onvif_subnets([cidr], ports=ports, concurrency=concurrency)


def _make_camera(
    ip: str, username: str | None, password: str | None, port: int = DEFAULT_ONVIF_PORT
):
    ONVIFCamera, _ = _import_onvif()
    # onvif-zeep: wsdl dir is handled internally; pass None for autodetect
    return ONVIFCamera(ip, port, username or "", password or "")


async def get_media_service(
    ip: str, username: str, password: str, port: int = DEFAULT_ONVIF_PORT
):
    cam = await _to_thread(_make_camera, ip, username, password, port)
    return await _to_thread(cam.create_media_service)


async def get_device_service(
    ip: str, username: str, password: str, port: int = DEFAULT_ONVIF_PORT
):
    cam = await _to_thread(_make_camera, ip, username, password, port)
    return await _to_thread(cam.create_devicemgmt_service)


async def get_ptz_service(
    ip: str, username: str, password: str, port: int = DEFAULT_ONVIF_PORT
):
    cam = await _to_thread(_make_camera, ip, username, password, port)
    return await _to_thread(cam.create_ptz_service)


async def fetch_profiles(
    ip: str, username: str, password: str, port: int = DEFAULT_ONVIF_PORT
) -> list[dict[str, Any]]:
    try:
        media = await get_media_service(ip, username, password, port)
        profiles = await _to_thread(media.GetProfiles)
        out = []
        for p in profiles:
            token = getattr(p, "token", None)
            name = getattr(p, "Name", None)
            out.append({"token": token, "name": name})
        return out
    except HTTPException:
        raise
    except Exception as e:
        main_logger.error(f"GetProfiles failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"GetProfiles failed: {e}")


async def get_stream_uri(
    ip: str,
    username: str,
    password: str,
    profile_token: str,
    port: int = DEFAULT_ONVIF_PORT,
) -> str:
    try:
        media = await get_media_service(ip, username, password, port)
        req = await _to_thread(media.create_type, "GetStreamUri")
        # Transport: RTSP / UDP default; many NVRs use TCP, but we return whatever camera provides.
        req.StreamSetup = {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}}
        req.ProfileToken = profile_token
        resp = await _to_thread(media.GetStreamUri, req)
        uri = getattr(resp, "Uri", None)
        if not uri:
            raise HTTPException(
                status_code=500, detail="Camera did not return a stream URI"
            )
        return uri
    except HTTPException:
        raise
    except Exception as e:
        main_logger.error(f"GetStreamUri failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"GetStreamUri failed: {e}")


async def ptz_continuous_move(
    ip: str,
    username: str,
    password: str,
    profile_token: str,
    x: float,
    y: float,
    z: float,
    port: int = DEFAULT_ONVIF_PORT,
) -> dict[str, Any]:
    try:
        ptz = await get_ptz_service(ip, username, password, port)
        req = await _to_thread(ptz.create_type, "ContinuousMove")
        req.ProfileToken = profile_token
        req.Velocity = {"PanTilt": {"x": x, "y": y}, "Zoom": {"x": z}}
        await _to_thread(ptz.ContinuousMove, req)
        return {"status": "moving"}
    except HTTPException:
        raise
    except Exception as e:
        main_logger.error(f"PTZ ContinuousMove failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"PTZ move failed: {e}")


async def ptz_stop(
    ip: str,
    username: str,
    password: str,
    profile_token: str,
    port: int = DEFAULT_ONVIF_PORT,
) -> dict[str, Any]:
    try:
        ptz = await get_ptz_service(ip, username, password, port)
        req = await _to_thread(ptz.create_type, "Stop")
        req.ProfileToken = profile_token
        req.PanTilt = True
        req.Zoom = True
        await _to_thread(ptz.Stop, req)
        return {"status": "stopped"}
    except HTTPException:
        raise
    except Exception as e:
        main_logger.error(f"PTZ Stop failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"PTZ stop failed: {e}")


async def ptz_presets(
    ip: str,
    username: str,
    password: str,
    profile_token: str,
    action: str,
    name: str | None = None,
    preset_token: str | None = None,
    port: int = DEFAULT_ONVIF_PORT,
) -> dict[str, Any]:
    try:
        ptz = await get_ptz_service(ip, username, password, port)
        action_lower = action.lower()
        if action_lower == "getpresets":
            req = await _to_thread(ptz.create_type, "GetPresets")
            req.ProfileToken = profile_token
            resp = await _to_thread(ptz.GetPresets, req)
            presets = []
            for p in resp or []:
                presets.append(
                    {
                        "token": getattr(p, "token", None),
                        "name": getattr(p, "Name", None),
                    }
                )
            return {"presets": presets}
        elif action_lower == "setpreset":
            req = await _to_thread(ptz.create_type, "SetPreset")
            req.ProfileToken = profile_token
            if name:
                req.PresetName = name
            if preset_token:
                req.PresetToken = preset_token
            resp = await _to_thread(ptz.SetPreset, req)
            # Some cameras return the new PresetToken
            return {
                "status": "ok",
                "preset_token": getattr(resp, "PresetToken", preset_token),
            }
        elif action_lower == "gotopreset":
            if not preset_token:
                raise HTTPException(
                    status_code=400, detail="preset_token is required for gotoPreset"
                )
            req = await _to_thread(ptz.create_type, "GotoPreset")
            req.ProfileToken = profile_token
            req.PresetToken = preset_token
            await _to_thread(ptz.GotoPreset, req)
            return {"status": "moving"}
        else:
            raise HTTPException(
                status_code=400,
                detail="Unsupported action. Use setPreset, getPresets, gotoPreset",
            )
    except HTTPException:
        raise
    except Exception as e:
        main_logger.error(f"PTZ Preset action failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"PTZ preset failed: {e}")
