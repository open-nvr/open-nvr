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
ONVIF service with HTTP Digest authentication support.

This module provides ONVIF functionality using raw SOAP requests with HTTP Digest auth,
which is more compatible with Hikvision and other devices that don't properly support
WS-Security (UsernameToken).

The standard onvif-zeep library uses WS-Security which many devices reject.
This implementation uses HTTP Digest authentication which is more widely supported.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

from core.logging_config import main_logger


def _xml_text(raw: str) -> str:
    """Unescape XML character entities from regex-extracted element text.

    SOAP responses escape ``&`` as ``&amp;`` (etc.) inside text nodes; a raw
    regex capture keeps the entity, which corrupted RTSP URLs with query
    strings — ``?transmode=unicast&amp;profile=va`` was stored and handed to
    MediaMTX verbatim."""
    return html.unescape(raw)

# ONVIF XML namespaces
SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
TDS_NS = "http://www.onvif.org/ver10/device/wsdl"
TRT_NS = "http://www.onvif.org/ver10/media/wsdl"
TT_NS = "http://www.onvif.org/ver10/schema"
TPT_NS = "http://www.onvif.org/ver20/ptz/wsdl"
TIMG_NS = "http://www.onvif.org/ver20/imaging/wsdl"


def _soap_envelope(body: str) -> str:
    """Wrap body XML in SOAP envelope with namespaces.

    Extra (unused-by-a-given-request) xmlns declarations are harmless, so all
    ONVIF service prefixes live here — device (tds), media (trt), ptz (tptz),
    imaging (timg), and shared schema types (tt)."""
    return f'''<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="{SOAP_NS}"
               xmlns:tds="{TDS_NS}"
               xmlns:trt="{TRT_NS}"
               xmlns:tt="{TT_NS}"
               xmlns:tptz="{TPT_NS}"
               xmlns:timg="{TIMG_NS}">
  <soap:Body>
    {body}
  </soap:Body>
</soap:Envelope>'''


async def _onvif_request(
    url: str,
    body_xml: str,
    username: str | None = None,
    password: str | None = None,
    timeout: float = 10.0,
) -> tuple[int, str]:
    """
    Make an ONVIF SOAP request using HTTP Digest authentication.

    Args:
        url: Full URL to the ONVIF service endpoint
        body_xml: The SOAP body XML content (without envelope)
        username: Authentication username
        password: Authentication password
        timeout: Request timeout in seconds

    Returns:
        Tuple of (status_code, response_text)
    """
    envelope = _soap_envelope(body_xml)
    headers = {"Content-Type": "application/soap+xml; charset=utf-8"}

    auth = None
    if username and password:
        auth = httpx.DigestAuth(username, password)

    # verify=False: cameras use self-signed certs on the LAN; the whole point of
    # supporting https here is reaching devices whose control API is TLS-only.
    async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
        try:
            response = await client.post(
                url, content=envelope, headers=headers, auth=auth
            )
            return response.status_code, response.text
        except httpx.TimeoutException:
            raise HTTPException(
                status_code=504, detail=f"ONVIF request timeout to {url}"
            )
        except httpx.ConnectError as e:
            raise HTTPException(
                status_code=503, detail=f"Cannot connect to ONVIF device: {e}"
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"ONVIF request failed: {e}")


def _extract_xaddr(capabilities_xml: str, service_tag: str) -> str | None:
    """Extract XAddr URL for a service from GetCapabilities response."""
    # Pattern: <tt:ServiceTag>...<tt:XAddr>http://...</tt:XAddr>...</tt:ServiceTag>
    pattern = rf"<tt:{service_tag}>.*?<tt:XAddr>([^<]+)</tt:XAddr>"
    match = re.search(pattern, capabilities_xml, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def _extract_path(url: str) -> str:
    """Extract path from URL."""
    parsed = urlparse(url)
    return parsed.path or "/"


def _device_url(ip: str, port: int, scheme: str = "http") -> str:
    """Build the ONVIF device_service URL for a given scheme (http/https)."""
    return f"{scheme}://{ip}:{port}/onvif/device_service"


async def get_device_info(
    ip: str,
    username: str,
    password: str,
    port: int = 80,
    scheme: str = "http",
) -> dict[str, Any]:
    """
    Get device information from ONVIF device.

    Returns device manufacturer, model, firmware version, etc.
    """
    url = _device_url(ip, port, scheme)
    body = "<tds:GetDeviceInformation/>"

    status, text = await _onvif_request(url, body, username, password)

    if status == 401:
        raise HTTPException(
            status_code=401, detail="Authentication failed - check username/password"
        )
    if status != 200:
        raise HTTPException(
            status_code=status, detail=f"GetDeviceInformation failed: {text[:500]}"
        )

    # Parse device info
    info = {}
    for field in [
        "Manufacturer",
        "Model",
        "FirmwareVersion",
        "SerialNumber",
        "HardwareId",
    ]:
        match = re.search(rf"<tds:{field}>([^<]*)</tds:{field}>", text)
        if match:
            info[field.lower()] = match.group(1)

    return info


async def get_capabilities(
    ip: str,
    username: str,
    password: str,
    port: int = 80,
    scheme: str = "http",
) -> dict[str, str]:
    """
    Get device capabilities and service endpoints.

    Returns dict of service name -> XAddr URL.
    """
    url = _device_url(ip, port, scheme)
    body = "<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>"

    status, text = await _onvif_request(url, body, username, password)

    if status == 401:
        raise HTTPException(
            status_code=401, detail="Authentication failed - check username/password"
        )
    if status != 200:
        raise HTTPException(
            status_code=status, detail=f"GetCapabilities failed: {text[:500]}"
        )

    # Extract service XAddrs
    services = {}
    for service in ["Device", "Media", "PTZ", "Events", "Imaging", "Analytics"]:
        xaddr = _extract_xaddr(text, service)
        if xaddr:
            services[service.lower()] = xaddr

    return services


# GetServices returns the FULL service list (including ver20 media2 and
# analytics) that GetCapabilities (ver10) omits — this is what lets the baseline
# driver discover OSD (media2), analytics rules, recording, etc. on any camera.
_SERVICE_NS_MAP = {
    "http://www.onvif.org/ver10/device/wsdl": "device",
    "http://www.onvif.org/ver10/media/wsdl": "media",
    "http://www.onvif.org/ver20/media/wsdl": "media2",
    "http://www.onvif.org/ver20/imaging/wsdl": "imaging",
    "http://www.onvif.org/ver20/ptz/wsdl": "ptz",
    "http://www.onvif.org/ver10/events/wsdl": "events",
    "http://www.onvif.org/ver20/analytics/wsdl": "analytics",
    "http://www.onvif.org/ver10/recording/wsdl": "recording",
    "http://www.onvif.org/ver10/replay/wsdl": "replay",
    "http://www.onvif.org/ver10/search/wsdl": "search",
}


async def get_services(
    ip: str,
    username: str,
    password: str,
    port: int = 80,
    scheme: str = "http",
) -> dict[str, str]:
    """Return a map of {short_service_name: XAddr} via ONVIF GetServices.

    Falls back to GetCapabilities on any failure, so a camera that only
    implements the older call still yields the services it does advertise.
    """
    url = _device_url(ip, port, scheme)
    body = (
        "<tds:GetServices><tds:IncludeCapability>false"
        "</tds:IncludeCapability></tds:GetServices>"
    )
    try:
        status, text = await _onvif_request(url, body, username, password)
    except Exception:
        status, text = 0, ""

    services: dict[str, str] = {}
    if status == 200 and text:
        # Each <tds:Service> pairs a <tds:Namespace> with a <tds:XAddr>. Be
        # namespace-prefix tolerant (vendors vary tds:/ter:/no-prefix).
        for m in re.finditer(
            r"<(?:\w+:)?Service\b[^>]*>(.*?)</(?:\w+:)?Service>", text, re.DOTALL
        ):
            block = m.group(1)
            ns_m = re.search(r"<(?:\w+:)?Namespace>([^<]+)</(?:\w+:)?Namespace>", block)
            xa_m = re.search(r"<(?:\w+:)?XAddr>([^<]+)</(?:\w+:)?XAddr>", block)
            if ns_m and xa_m:
                key = _SERVICE_NS_MAP.get(ns_m.group(1).strip())
                if key:
                    services[key] = _xml_text(xa_m.group(1).strip())

    if not services:
        # Fallback: older GetCapabilities (no media2/analytics granularity).
        try:
            services = await get_capabilities(ip, username, password, port, scheme)
        except Exception:
            services = {}
    return services


async def fetch_profiles_digest(
    ip: str,
    username: str,
    password: str,
    port: int = 80,
    scheme: str = "http",
) -> list[dict[str, Any]]:
    """
    Get media profiles from ONVIF device using HTTP Digest auth.

    Returns list of profiles with token, name, and video configuration.
    """
    # First get the media service URL
    try:
        capabilities = await get_capabilities(ip, username, password, port, scheme)
        media_url = capabilities.get("media")
        if not media_url:
            # Fallback to common Hikvision path
            media_url = f"{scheme}://{ip}:{port}/onvif/media_service"
    except Exception:
        # Fallback to common Hikvision path
        media_url = f"{scheme}://{ip}:{port}/onvif/media_service"

    body = "<trt:GetProfiles/>"
    status, text = await _onvif_request(media_url, body, username, password)

    if status == 401:
        raise HTTPException(
            status_code=401, detail="Authentication failed - check username/password"
        )
    if status == 404:
        # Try alternate path
        alt_url = f"{scheme}://{ip}:{port}/onvif/Media"
        status, text = await _onvif_request(alt_url, body, username, password)
        if status != 200:
            raise HTTPException(status_code=404, detail="Media service not found")
    if status != 200:
        raise HTTPException(
            status_code=status, detail=f"GetProfiles failed: {text[:500]}"
        )

    # Parse profiles. Be tolerant of the two things ONVIF vendors vary:
    #   * the XML namespace PREFIX (trt:/tt: on Hikvision; other vendors differ),
    #   * ATTRIBUTE ORDER — Hikvision emits <Profiles token="..">, but Secureye/
    #     Tiandy emits <Profiles fixed="true" token="..">, so ``token`` is not
    #     always the first attribute. A regex that assumed either produced zero
    #     profiles on the other vendor.
    profiles = []
    for m in re.finditer(
        r"<(?:\w+:)?Profiles\b([^>]*)>(.*?)</(?:\w+:)?Profiles>", text, re.DOTALL
    ):
        attrs, block = m.group(1), m.group(2)
        tok = re.search(r'token="([^"]+)"', attrs)
        if not tok:
            continue
        token = tok.group(1)
        name_m = re.search(r"<(?:\w+:)?Name>([^<]+)</(?:\w+:)?Name>", block)
        profile_info = {
            "token": token,
            "name": _xml_text(name_m.group(1)) if name_m else token,
        }

        bounds_match = re.search(r'width="(\d+)"\s+height="(\d+)"', block)
        if bounds_match:
            profile_info["width"] = int(bounds_match.group(1))
            profile_info["height"] = int(bounds_match.group(2))

        profiles.append(profile_info)

    return profiles


async def get_stream_uri_digest(
    ip: str,
    username: str,
    password: str,
    profile_token: str,
    port: int = 80,
    scheme: str = "http",
) -> str:
    """
    Get RTSP stream URI for a profile using HTTP Digest auth.

    Returns the RTSP URL for the specified profile.
    """
    # Get media service URL
    try:
        capabilities = await get_capabilities(ip, username, password, port, scheme)
        media_url = capabilities.get("media")
        if not media_url:
            media_url = f"{scheme}://{ip}:{port}/onvif/media_service"
    except Exception:
        media_url = f"{scheme}://{ip}:{port}/onvif/media_service"

    body = f"""<trt:GetStreamUri>
      <trt:StreamSetup>
        <tt:Stream>RTP-Unicast</tt:Stream>
        <tt:Transport>
          <tt:Protocol>RTSP</tt:Protocol>
        </tt:Transport>
      </trt:StreamSetup>
      <trt:ProfileToken>{profile_token}</trt:ProfileToken>
    </trt:GetStreamUri>"""

    status, text = await _onvif_request(media_url, body, username, password)

    if status == 401:
        raise HTTPException(status_code=401, detail="Authentication failed")
    if status == 404:
        # Try alternate path
        alt_url = f"{scheme}://{ip}:{port}/onvif/Media"
        status, text = await _onvif_request(alt_url, body, username, password)
    if status != 200:
        raise HTTPException(
            status_code=status, detail=f"GetStreamUri failed: {text[:500]}"
        )

    # Extract URI
    uri_match = re.search(r"<tt:Uri>([^<]+)</tt:Uri>", text)
    if not uri_match:
        raise HTTPException(status_code=500, detail="No stream URI in response")

    return _xml_text(uri_match.group(1))


# Common ONVIF control ports across vendors (kept in sync with the discovery
# scan in onvif_service._ONVIF_CANDIDATE_PORTS). 443/8443 are included so a
# camera that serves ONVIF over TLS only is still resolved (each port is probed
# http-then-https, so plain-HTTP cameras still hit on the first try).
ONVIF_CANDIDATE_PORTS = (80, 8000, 8080, 8088, 2020, 8899, 443, 8443)


async def resolve_control_endpoint(
    ip: str,
    port_hint: int | None = None,
    scheme_hint: str | None = None,
) -> tuple[str, int]:
    """Return ``(scheme, port)`` that actually answers ONVIF, any port, http or https.

    Probes an unauthenticated GetSystemDateAndTime (which ONVIF devices serve
    without credentials) across the unified candidate ports, trying HTTP first
    for speed and HTTPS as a fallback (some cameras serve ONVIF over TLS only).
    A ``(scheme_hint, port_hint)`` that verifies is returned immediately so a
    once-resolved endpoint is trusted without a full re-scan.

    Falls back to the hints (or http/80) if nothing answers, so the caller
    always gets a deterministic value.
    """
    # 1) trust a verified hint first (cheapest path for a known camera).
    if port_hint:
        for s in ([scheme_hint] if scheme_hint else []) + ["http", "https"]:
            if not s:
                continue
            try:
                await get_system_datetime(ip, port_hint, scheme=s, timeout=4.0)
                return s, port_hint
            except Exception:
                continue

    # 2) scan candidate ports; http then https per port so the common (http)
    #    camera resolves on the first hit and only TLS-only devices pay for a
    #    handshake.
    ports = list(ONVIF_CANDIDATE_PORTS)
    if port_hint and port_hint not in ports:
        ports.insert(0, port_hint)
    schemes = ["https", "http"] if scheme_hint == "https" else ["http", "https"]
    for p in ports:
        for s in schemes:
            try:
                await get_system_datetime(ip, p, scheme=s, timeout=4.0)
                return s, p
            except Exception:
                continue
    return (scheme_hint or "http"), (port_hint or 80)


async def resolve_onvif_port(ip: str, port_hint: int = 80) -> int:
    """Back-compat shim: resolve only the port (scheme via the newer helper)."""
    _, port = await resolve_control_endpoint(ip, port_hint)
    return port


async def connect_and_get_profiles(
    ip: str,
    username: str,
    password: str,
    port: int = 80,
    scheme: str | None = None,
) -> dict[str, Any]:
    """
    Connect to ONVIF device and get all profiles with stream URIs.

    This is a convenience function that:
    1. Validates credentials
    2. Gets device info
    3. Gets all profiles
    4. Gets stream URI for each profile

    Returns complete device info with profiles and their stream URIs
    (including the resolved ``scheme``).
    """
    # Resolve the real ONVIF control endpoint. Cameras serve ONVIF on a range of
    # ports (Hikvision 80, Secureye/Tiandy 8088, …) and either scheme (http or,
    # for TLS-only devices, https); the caller often only has the default 80, so
    # probe candidates and use whichever answers ONVIF. This makes both
    # discovered and manually-entered cameras connect regardless of port/scheme.
    scheme, port = await resolve_control_endpoint(ip, port, scheme)
    main_logger.info(f"Connecting to ONVIF device at {scheme}://{ip}:{port}")

    # Get device info to validate credentials
    try:
        device_info = await get_device_info(ip, username, password, port, scheme)
    except HTTPException as e:
        if e.status_code == 401:
            raise
        # Some devices may not support GetDeviceInformation
        device_info = {"manufacturer": "Unknown", "model": "Unknown"}

    # Get profiles
    profiles = await fetch_profiles_digest(ip, username, password, port, scheme)

    # Get stream URI for each profile
    profiles_with_uri = []
    for profile in profiles:
        try:
            uri = await get_stream_uri_digest(
                ip, username, password, profile["token"], port, scheme
            )
            profile["stream_uri"] = uri
        except Exception as e:
            main_logger.warning(
                f"Failed to get stream URI for profile {profile['token']}: {e}"
            )
            profile["stream_uri"] = None
        profiles_with_uri.append(profile)

    return {
        "ip": ip,
        "port": port,
        "scheme": scheme,
        "device_info": device_info,
        "profiles": profiles_with_uri,
    }


# PTZ operations using HTTP Digest


async def get_ptz_service_url(
    ip: str,
    username: str,
    password: str,
    port: int = 80,
    scheme: str = "http",
) -> str | None:
    """Get PTZ service URL from device capabilities."""
    try:
        capabilities = await get_capabilities(ip, username, password, port, scheme)
        return capabilities.get("ptz")
    except Exception:
        # Fallback to common paths
        return f"{scheme}://{ip}:{port}/onvif/ptz_service"


async def ptz_continuous_move_digest(
    ip: str,
    username: str,
    password: str,
    profile_token: str,
    x: float,
    y: float,
    z: float,
    port: int = 80,
    scheme: str = "http",
) -> dict[str, Any]:
    """PTZ continuous move using HTTP Digest auth."""
    ptz_url = await get_ptz_service_url(ip, username, password, port, scheme)
    if not ptz_url:
        raise HTTPException(status_code=404, detail="PTZ service not available")

    body = f'''<tptz:ContinuousMove>
      <tptz:ProfileToken>{profile_token}</tptz:ProfileToken>
      <tptz:Velocity>
        <tt:PanTilt x="{x}" y="{y}"/>
        <tt:Zoom x="{z}"/>
      </tptz:Velocity>
    </tptz:ContinuousMove>'''

    status, text = await _onvif_request(ptz_url, body, username, password)

    if status != 200:
        raise HTTPException(status_code=status, detail=f"PTZ move failed: {text[:500]}")

    return {"status": "moving"}


async def ptz_stop_digest(
    ip: str,
    username: str,
    password: str,
    profile_token: str,
    port: int = 80,
    scheme: str = "http",
) -> dict[str, Any]:
    """PTZ stop using HTTP Digest auth."""
    ptz_url = await get_ptz_service_url(ip, username, password, port, scheme)
    if not ptz_url:
        raise HTTPException(status_code=404, detail="PTZ service not available")

    body = f"""<tptz:Stop>
      <tptz:ProfileToken>{profile_token}</tptz:ProfileToken>
      <tptz:PanTilt>true</tptz:PanTilt>
      <tptz:Zoom>true</tptz:Zoom>
    </tptz:Stop>"""

    status, text = await _onvif_request(ptz_url, body, username, password)

    if status != 200:
        raise HTTPException(status_code=status, detail=f"PTZ stop failed: {text[:500]}")

    return {"status": "stopped"}


async def get_system_datetime(
    ip: str,
    port: int = 80,
    scheme: str = "http",
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Read the camera clock via GetSystemDateAndTime (no auth needed).

    Returns a dict with utc_datetime (ISO string) and camera_timezone if present.
    Also the ONVIF-presence probe used by ``resolve_control_endpoint`` — a short
    ``timeout`` keeps scheme/port scanning responsive.
    """
    url = _device_url(ip, port, scheme)
    body = "<tds:GetSystemDateAndTime/>"
    status, text = await _onvif_request(
        url, body, username=None, password=None, timeout=timeout
    )

    if status not in (200, 401):
        raise HTTPException(
            status_code=status,
            detail=f"GetSystemDateAndTime failed: {text[:500]}",
        )

    result: dict[str, Any] = {}

    # Parse UTC date/time fields from SOAP response
    fields = {
        "year": r"<tt:Year>(\d+)</tt:Year>",
        "month": r"<tt:Month>(\d+)</tt:Month>",
        "day": r"<tt:Day>(\d+)</tt:Day>",
        "hour": r"<tt:Hour>(\d+)</tt:Hour>",
        "minute": r"<tt:Minute>(\d+)</tt:Minute>",
        "second": r"<tt:Second>(\d+)</tt:Second>",
    }
    parsed = {}
    for key, pattern in fields.items():
        m = re.search(pattern, text)
        if m:
            parsed[key] = int(m.group(1))

    if len(parsed) == 6:
        try:
            dt = datetime(
                parsed["year"],
                parsed["month"],
                parsed["day"],
                parsed["hour"],
                parsed["minute"],
                parsed["second"],
                tzinfo=UTC,
            )
            result["utc_datetime"] = dt.isoformat()
        except ValueError:
            pass

    tz_match = re.search(r"<tt:TimeZone><tt:TZ>([^<]+)</tt:TZ>", text)
    if tz_match:
        result["camera_timezone"] = tz_match.group(1)

    return result


async def set_system_datetime(
    ip: str,
    username: str,
    password: str,
    port: int = 80,
    scheme: str = "http",
) -> dict[str, Any]:
    """Push the NVR's current UTC clock to the camera via SetSystemDateAndTime.

    Uses Manual DateTimeType with the NVR's UTC time. Requires auth.
    """
    url = _device_url(ip, port, scheme)
    now = datetime.now(UTC)

    body = f"""<tds:SetSystemDateAndTime>
      <tds:DateTimeType>Manual</tds:DateTimeType>
      <tds:DaylightSavings>false</tds:DaylightSavings>
      <tds:UTCDateTime>
        <tt:Time>
          <tt:Hour>{now.hour}</tt:Hour>
          <tt:Minute>{now.minute}</tt:Minute>
          <tt:Second>{now.second}</tt:Second>
        </tt:Time>
        <tt:Date>
          <tt:Year>{now.year}</tt:Year>
          <tt:Month>{now.month}</tt:Month>
          <tt:Day>{now.day}</tt:Day>
        </tt:Date>
      </tds:UTCDateTime>
    </tds:SetSystemDateAndTime>"""

    status, text = await _onvif_request(url, body, username, password)

    if status == 401:
        raise HTTPException(status_code=401, detail="Authentication failed")
    if status != 200:
        raise HTTPException(
            status_code=status,
            detail=f"SetSystemDateAndTime failed: {text[:500]}",
        )

    return {"synced_utc": now.isoformat()}
