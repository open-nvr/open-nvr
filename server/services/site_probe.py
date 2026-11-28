# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Probing the cameras a site's agent found, to add them in one go (plan S5).

The admin ticks devices on the Sites page and types the cameras' user and
password once. For each ticked device, through the site's agent:

1. ONVIF (services/onvif_digest_service.py: Digest, or WS-Security): the
   device's name and model, its channels (one for a camera, one per channel
   for an NVR), each channel's main and sub stream, and a snapshot to show.
2. Without ONVIF: the usual vendor RTSP paths (Hikvision/Uniview, Dahua/CP
   Plus), each tried with a DESCRIBE until one answers 200.

Only devices the agent itself reported are reached (site_relay device
routes), and only ticked ones get the password: never everything on the
network. Every probe is audited with the number of devices the password
was sent to.

The result for each device is one of ``ok`` (with its channels),
``wrong_password``, ``no_stream_found`` or ``unreachable``; nothing is saved
until the admin adds the cameras (``bulk_add``).
"""

from __future__ import annotations

import asyncio
import base64
import html
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException

from services import onvif_digest_service as ods, site_relay

#: Devices probed at once, and the time one may take.
PROBE_PARALLEL = 8
PROBE_DEVICE_S = 25.0
#: A preview larger than this is not shown.
SNAPSHOT_MAX = 400_000


def strip_credentials(url: str | None) -> str | None:
    """A stream URL without user:password (they go in the camera's fields)."""
    if not url:
        return None
    u = urlsplit(html.unescape(url))
    host = u.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = host + (f":{u.port}" if u.port else "")
    return urlunsplit((u.scheme, netloc, u.path, u.query, u.fragment))


def _on_lan(url: str | None, lan_ip: str) -> str | None:
    """A stream URL with the camera's LAN address (never a site route)."""
    url = strip_credentials(url)
    if not url:
        return None
    u = urlsplit(url)
    if site_relay.is_site_host(u.hostname):
        netloc = lan_ip + (f":{u.port}" if u.port else "")
        url = urlunsplit((u.scheme, netloc, u.path, u.query, u.fragment))
    return url


async def _snapshot(host: str, username: str, password: str, port: int, scheme: str,
                    profile_token: str) -> str | None:
    """A JPEG from the device (ONVIF GetSnapshotUri), base64, or None."""
    import httpx

    try:
        uri = await ods.get_snapshot_uri_digest(host, username, password, profile_token, port, scheme)
    except Exception:  # a camera without snapshots shows no preview
        return None
    if not uri:
        return None
    uri = ods.route_xaddr(html.unescape(uri), host)
    try:
        async with site_relay.camera_http_client(timeout=10.0, verify=False) as client:
            r = await client.get(uri, auth=httpx.DigestAuth(username, password))
            if r.status_code == 401:
                return None
    except Exception:
        return None
    ctype = r.headers.get("content-type", "")
    if r.status_code != 200 or not ctype.startswith("image/") or len(r.content) > SNAPSHOT_MAX:
        return None
    return base64.b64encode(r.content).decode()


async def _probe_onvif(site_id: int, ip: str, username: str, password: str) -> dict[str, Any] | None:
    host = site_relay.device_host(site_id, ip)
    try:
        info = await ods.connect_and_get_profiles(host, username, password)
    except HTTPException as e:
        if e.status_code == 401:
            return {"status": "wrong_password", "detail": "The camera refused this user and password."}
        return None
    except Exception:
        return None
    dev = info.get("device_info") or {}
    channels = []
    for ch in info.get("channels") or []:
        main, sub = ch.get("main"), ch.get("sub")
        if not main or not main.get("stream_uri"):
            continue
        snap = None
        if len(channels) < 4:  # previews for the first few channels of an NVR
            snap = await _snapshot(host, username, password, info.get("port", 80),
                                   info.get("scheme", "http"), main["token"])
        channels.append({
            "index": ch.get("index"),
            "name": ch.get("name"),
            "main_url": _on_lan(main.get("stream_uri"), ip),
            "sub_url": _on_lan(sub.get("stream_uri"), ip) if sub else None,
            "width": main.get("width"),
            "height": main.get("height"),
            "snapshot_b64": snap,
        })
    if not channels:
        return {"status": "no_stream_found", "detail": "The camera answered ONVIF but offered no stream.",
                "manufacturer": dev.get("manufacturer"), "model": dev.get("model")}
    return {"status": "ok", "via": "onvif", "manufacturer": dev.get("manufacturer"),
            "model": dev.get("model"), "channels": channels}


async def _probe_rtsp(site_id: int, ip: str, username: str, password: str) -> dict[str, Any]:
    from services.camera_source_resolver import (
        _FALLBACK_PATHS,
        _build_rtsp_auth,
        _describe,
        derive_substream_url,
    )

    host = site_relay.device_host(site_id, ip)
    answered = False
    refused = False
    for port in (554, 8554):
        for path in _FALLBACK_PATHS:
            uri = f"rtsp://{ip}:{port}{path}"
            code, headers = await _describe(host, port, uri, None, 4.0)
            if code is None:
                break  # nothing on this port
            answered = True
            if code == 401 and username:
                auth = _build_rtsp_auth("DESCRIBE", uri, username, password or "",
                                        headers.get("www-authenticate", ""))
                if auth:
                    code, _ = await _describe(host, port, uri, auth, 4.0)
                if code == 401:
                    refused = True
                    continue
            if code == 200:
                return {"status": "ok", "via": "rtsp", "manufacturer": None, "model": None, "channels": [{
                    "index": 1, "name": "Ch 1", "main_url": uri, "sub_url": derive_substream_url(uri),
                    "width": None, "height": None, "snapshot_b64": None,
                }]}
    if refused:
        return {"status": "wrong_password", "detail": "The camera refused this user and password."}
    if answered:
        return {"status": "no_stream_found",
                "detail": "No ONVIF and none of the usual stream paths: add it with its stream URL."}
    return {"status": "unreachable", "detail": "The site's agent could not reach it."}


async def probe_device(site_id: int, ip: str, username: str, password: str) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        got = await _probe_onvif(site_id, ip, username, password)
        if got is not None and got["status"] != "no_stream_found":
            return got
        rtsp = await _probe_rtsp(site_id, ip, username, password)
        if got is not None and rtsp["status"] != "ok":
            return got
        return rtsp

    try:
        res = await asyncio.wait_for(run(), PROBE_DEVICE_S)
    except TimeoutError:
        res = {"status": "unreachable", "detail": "No answer in time (is the site online?)."}
    return {"ip": ip, **res}


async def probe_devices(site_id: int, ips: list[str], username: str, password: str) -> list[dict[str, Any]]:
    """Probe the given devices (already checked to be the site's), a few at
    a time; results in the order asked."""
    sem = asyncio.Semaphore(PROBE_PARALLEL)

    async def one(ip: str) -> dict[str, Any]:
        async with sem:
            return await probe_device(site_id, ip, username, password)

    return list(await asyncio.gather(*(one(ip) for ip in ips)))
