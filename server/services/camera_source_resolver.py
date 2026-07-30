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
Camera source resolver — derive an RTSP URL (and device identity) from just an
IP + credentials, so operators don't have to know their camera's RTSP path.

Order of attempts:
1. **ONVIF direct-connect** (not broadcast discovery) on the common HTTP ports.
   Returns the camera's own advertised stream URI *and* GetDeviceInformation
   (manufacturer/model/firmware/serial). This works for most cameras even when
   broadcast discovery failed.
2. **Vendor RTSP templates + DESCRIBE probe** for non-ONVIF devices
   (Hikvision /Streaming/Channels/101, Dahua/CP Plus /cam/realmonitor?...).

Credentials are embedded into the returned URL so MediaMTX can authenticate to
the source (it pulls the source URL as-is).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import html
import re
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

from core.logging_config import main_logger
from services import onvif_digest_service as ods

# ONVIF control endpoint resolution is delegated to
# ``onvif_digest_service.resolve_control_endpoint`` (the single source of truth
# for candidate ports AND scheme — http/https), so a camera on any port/scheme
# resolves the same way everywhere. No private port list here anymore.

# Vendor RTSP path templates for the main stream (1-based channel 1).
_VENDOR_TEMPLATES = {
    "hikvision": "/Streaming/Channels/101",
    "dahua": "/cam/realmonitor?channel=1&subtype=0",
    "cpplus": "/cam/realmonitor?channel=1&subtype=0",
}
# What we probe when the brand is unknown (covers the bulk of the SMB market).
_FALLBACK_PATHS = (
    "/Streaming/Channels/101",  # Hikvision / Uniview
    "/cam/realmonitor?channel=1&subtype=0",  # Dahua / CP Plus
)


def derive_substream_url(main_url: str | None) -> str | None:
    """Derive a camera's low-res SUBSTREAM RTSP URL from its main-stream URL
    using vendor path conventions. Returns None when the URL doesn't match a
    known convention — callers must fall back to the main stream rather than
    guess (a wrong sub URL just fails to pull and the agent shows stills).

    Conventions covered (the bulk of the SMB market):
    * Hikvision / Uniview: ``/Streaming/Channels/N01`` (main) -> ``/N02`` (sub).
    * Dahua / CP Plus: ``subtype=0`` (main) -> ``subtype=1`` (sub).
    """
    if not main_url:
        return None
    # Dahua / CP Plus — flip the subtype query flag.
    if "subtype=0" in main_url:
        return main_url.replace("subtype=0", "subtype=1")
    # Hikvision / Uniview — /Streaming/Channels/<channel><stream>, stream 01 =
    # main, 02 = sub (e.g. 101 -> 102, 201 -> 202, 1001 -> 1002).
    m = re.search(r"(/Streaming/Channels/)(\d+)01(?=$|[/?&])", main_url)
    if m:
        return (main_url[: m.start()] + m.group(1) + m.group(2)
                + "02" + main_url[m.end():])
    return None


def inject_credentials(url: str | None, username: str | None, password: str | None) -> str | None:
    """Embed ``user:pass@`` into an rtsp(s) URL's authority (no-op if the URL
    already has userinfo, isn't rtsp, or no username is given)."""
    if not url or not username:
        return url
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if parsed.scheme.lower() not in ("rtsp", "rtsps") or "@" in parsed.netloc:
        return url
    userinfo = f"{quote(username, safe='')}:{quote(password or '', safe='')}@"
    return urlunparse(parsed._replace(netloc=userinfo + parsed.netloc))


# --- RTSP DESCRIBE probe (auth-aware) --------------------------------------


def _parse_auth_params(header: str) -> dict[str, str]:
    _scheme, _, rest = header.strip().partition(" ")
    params: dict[str, str] = {}
    for m in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,]+))', rest):
        params[m.group(1).lower()] = (
            m.group(2) if m.group(2) is not None else (m.group(3) or "").strip()
        )
    return params


def _build_rtsp_auth(method: str, uri: str, user: str, pw: str, www: str) -> str | None:
    low = (www or "").strip().lower()
    if low.startswith("basic"):
        return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
    if low.startswith("digest"):
        p = _parse_auth_params(www)
        realm, nonce = p.get("realm", ""), p.get("nonce", "")
        if not nonce:
            return None

        def md5(s: str) -> str:
            return hashlib.md5(s.encode(), usedforsecurity=False).hexdigest()

        ha1, ha2 = md5(f"{user}:{realm}:{pw}"), md5(f"{method}:{uri}")
        if p.get("qop"):
            nc, cnonce = "00000001", "0a4f113b9812"
            resp = md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
            return (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
                    f'uri="{uri}", qop=auth, nc={nc}, cnonce="{cnonce}", '
                    f'response="{resp}", algorithm=MD5')
        resp = md5(f"{ha1}:{nonce}:{ha2}")
        return (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
                f'uri="{uri}", response="{resp}", algorithm=MD5')
    return None


async def _describe(host: str, port: int, uri: str, auth: str | None, timeout: float):
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout
        )
    except (TimeoutError, OSError):
        return None, {}
    try:
        lines = [f"DESCRIBE {uri} RTSP/1.0", "CSeq: 1", "Accept: application/sdp",
                 "User-Agent: OpenNVR"]
        if auth:
            lines.append(f"Authorization: {auth}")
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        await asyncio.wait_for(writer.drain(), timeout)
        status = await asyncio.wait_for(reader.readline(), timeout)
        parts = status.decode("latin-1", "replace").split()
        code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None
        headers: dict[str, str] = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout)
            if not line or line in (b"\r\n", b"\n"):
                break
            k, _, v = line.decode("latin-1", "replace").partition(":")
            headers[k.strip().lower()] = v.strip()
        return code, headers
    except (TimeoutError, OSError, ValueError):
        return None, {}
    finally:
        writer.close()
        with contextlib.suppress(TimeoutError, OSError):
            await asyncio.wait_for(writer.wait_closed(), timeout)


async def _rtsp_path_works(host: str, port: int, path: str, user: str, pw: str,
                           timeout: float = 3.0) -> bool:
    """True if a DESCRIBE of ``path`` answers 200 (after one auth challenge)."""
    uri = f"rtsp://{host}:{port}{path}"
    code, headers = await _describe(host, port, uri, None, timeout)
    if code == 401 and user:
        auth = _build_rtsp_auth("DESCRIBE", uri, user, pw or "",
                                headers.get("www-authenticate", ""))
        if auth:
            code, _ = await _describe(host, port, uri, auth, timeout)
    return code == 200


# --- resolver --------------------------------------------------------------


def _identity_from_device_info(
    dev: dict[str, Any], onvif_port: int, scheme: str = "http"
) -> dict[str, Any]:
    """Map an ONVIF GetDeviceInformation dict to our camera identity fields."""
    return {
        "manufacturer": dev.get("manufacturer"),
        "model": dev.get("model"),
        "firmware_version": dev.get("firmwareversion"),
        "serial_number": dev.get("serialnumber"),
        "hardware_id": dev.get("hardwareid"),
        "onvif_port": onvif_port,
        "control_scheme": scheme,
    }


async def fetch_identity(
    ip: str, username: str | None, password: str | None
) -> dict[str, Any] | None:
    """Best-effort ONVIF GetDeviceInformation → identity dict (manufacturer, model,
    firmware_version, serial_number, hardware_id) + the working ``onvif_port``
    and ``control_scheme``.

    Used to enrich a camera's metadata even when its RTSP URL was supplied
    manually (so identity back-fill isn't coupled to URL derivation). The control
    endpoint (any port, http or https) is resolved by ``connect_and_get_profiles``.
    Returns None if ONVIF didn't answer. Never raises."""
    username = username or ""
    password = password or ""
    try:
        info = await ods.connect_and_get_profiles(ip, username, password)
    except Exception:
        return None
    dev = info.get("device_info") or {}
    if dev:
        return _identity_from_device_info(
            dev, info.get("port", 80), info.get("scheme", "http")
        )
    return None


async def resolve_source(
    ip: str, username: str | None, password: str | None, rtsp_port: int = 554
) -> dict[str, Any] | None:
    """Derive ``{rtsp_url, manufacturer, model, firmware_version, serial_number,
    hardware_id}`` from an IP + credentials, or ``None`` if nothing worked.
    ``rtsp_url`` always carries embedded credentials when available."""
    username = username or ""
    password = password or ""

    # 1. ONVIF direct-connect (also yields device identity). The control
    #    endpoint (any port, http or https) is resolved inside the call.
    try:
        info = await ods.connect_and_get_profiles(ip, username, password)
    except Exception:
        info = None
    if info:
        stream_uri = next(
            (p.get("stream_uri") for p in info.get("profiles", []) if p.get("stream_uri")),
            None,
        )
        if stream_uri:
            # ONVIF returns the URI XML-escaped (e.g. &amp;) — unescape before use.
            stream_uri = html.unescape(stream_uri)
            dev = info.get("device_info", {}) or {}
            return {
                "rtsp_url": inject_credentials(stream_uri, username, password),
                **_identity_from_device_info(
                    dev, info.get("port", 80), info.get("scheme", "http")
                ),
                "source": "onvif",
            }

    # 2. Vendor RTSP template + DESCRIBE probe (non-ONVIF / ONVIF-off cameras).
    for path in _FALLBACK_PATHS:
        try:
            if await _rtsp_path_works(ip, rtsp_port, path, username, password):
                url = f"rtsp://{ip}:{rtsp_port}{path}"
                return {
                    "rtsp_url": inject_credentials(url, username, password),
                    "manufacturer": None, "model": None, "firmware_version": None,
                    "serial_number": None, "hardware_id": None, "source": "rtsp_probe",
                }
        except Exception as e:
            main_logger.debug("RTSP probe %s failed: %s", path, e)

    return None


async def sync_camera_time(
    ip: str,
    username: str,
    password: str,
    onvif_port: int | None = None,
    control_scheme: str | None = None,
) -> bool:
    """Best-effort: push the server's current UTC to the camera so its clock
    (and the timestamp it burns into the video) is correct. The server itself
    is internet-time-synced by its host, so 'server UTC' is the correct time.

    Resolves the control endpoint (any port, http or https) when not supplied,
    then uses the ONVIF SetSystemDateAndTime primitive. Returns True if the
    camera accepted it; never raises."""
    try:
        scheme, port = await ods.resolve_control_endpoint(
            ip, onvif_port, control_scheme
        )
    except Exception:
        return False
    try:
        await ods.set_system_datetime(ip, username, password, port, scheme)
        main_logger.info("Synced clock on camera %s (%s:%s)", ip, scheme, port)
        return True
    except Exception:
        return False
