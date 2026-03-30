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

import re
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

from core.logging_config import main_logger

# ONVIF XML namespaces
SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
TDS_NS = "http://www.onvif.org/ver10/device/wsdl"
TRT_NS = "http://www.onvif.org/ver10/media/wsdl"
TT_NS = "http://www.onvif.org/ver10/schema"
TPT_NS = "http://www.onvif.org/ver20/ptz/wsdl"


def _soap_envelope(body: str) -> str:
    """Wrap body XML in SOAP envelope with namespaces."""
    return f'''<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="{SOAP_NS}"
               xmlns:tds="{TDS_NS}"
               xmlns:trt="{TRT_NS}"
               xmlns:tt="{TT_NS}"
               xmlns:tptz="{TPT_NS}">
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

    async with httpx.AsyncClient(timeout=timeout) as client:
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


async def get_device_info(
    ip: str,
    username: str,
    password: str,
    port: int = 80,
) -> dict[str, Any]:
    """
    Get device information from ONVIF device.

    Returns device manufacturer, model, firmware version, etc.
    """
    url = f"http://{ip}:{port}/onvif/device_service"
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
) -> dict[str, str]:
    """
    Get device capabilities and service endpoints.

    Returns dict of service name -> XAddr URL.
    """
    url = f"http://{ip}:{port}/onvif/device_service"
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
    for service in ["Device", "Media", "PTZ", "Events", "Imaging"]:
        xaddr = _extract_xaddr(text, service)
        if xaddr:
            services[service.lower()] = xaddr

    return services


async def fetch_profiles_digest(
    ip: str,
    username: str,
    password: str,
    port: int = 80,
) -> list[dict[str, Any]]:
    """
    Get media profiles from ONVIF device using HTTP Digest auth.

    Returns list of profiles with token, name, and video configuration.
    """
    # First get the media service URL
    try:
        capabilities = await get_capabilities(ip, username, password, port)
        media_url = capabilities.get("media")
        if not media_url:
            # Fallback to common Hikvision path
            media_url = f"http://{ip}:{port}/onvif/media_service"
    except Exception:
        # Fallback to common Hikvision path
        media_url = f"http://{ip}:{port}/onvif/media_service"

    body = "<trt:GetProfiles/>"
    status, text = await _onvif_request(media_url, body, username, password)

    if status == 401:
        raise HTTPException(
            status_code=401, detail="Authentication failed - check username/password"
        )
    if status == 404:
        # Try alternate path
        alt_url = f"http://{ip}:{port}/onvif/Media"
        status, text = await _onvif_request(alt_url, body, username, password)
        if status != 200:
            raise HTTPException(status_code=404, detail="Media service not found")
    if status != 200:
        raise HTTPException(
            status_code=status, detail=f"GetProfiles failed: {text[:500]}"
        )

    # Parse profiles
    profiles = []
    # Find all profile blocks
    profile_pattern = r'<trt:Profiles\s+token="([^"]+)"[^>]*>.*?<tt:Name>([^<]+)</tt:Name>.*?</trt:Profiles>'
    matches = re.findall(profile_pattern, text, re.DOTALL)

    for token, name in matches:
        # Check for video source configuration to determine resolution
        profile_block_match = re.search(
            rf'<trt:Profiles\s+token="{re.escape(token)}"[^>]*>.*?</trt:Profiles>',
            text,
            re.DOTALL,
        )

        profile_info = {"token": token, "name": name}

        if profile_block_match:
            block = profile_block_match.group(0)
            # Extract video resolution if available
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
) -> str:
    """
    Get RTSP stream URI for a profile using HTTP Digest auth.

    Returns the RTSP URL for the specified profile.
    """
    # Get media service URL
    try:
        capabilities = await get_capabilities(ip, username, password, port)
        media_url = capabilities.get("media")
        if not media_url:
            media_url = f"http://{ip}:{port}/onvif/media_service"
    except Exception:
        media_url = f"http://{ip}:{port}/onvif/media_service"

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
        alt_url = f"http://{ip}:{port}/onvif/Media"
        status, text = await _onvif_request(alt_url, body, username, password)
    if status != 200:
        raise HTTPException(
            status_code=status, detail=f"GetStreamUri failed: {text[:500]}"
        )

    # Extract URI
    uri_match = re.search(r"<tt:Uri>([^<]+)</tt:Uri>", text)
    if not uri_match:
        raise HTTPException(status_code=500, detail="No stream URI in response")

    return uri_match.group(1)


async def connect_and_get_profiles(
    ip: str,
    username: str,
    password: str,
    port: int = 80,
) -> dict[str, Any]:
    """
    Connect to ONVIF device and get all profiles with stream URIs.

    This is a convenience function that:
    1. Validates credentials
    2. Gets device info
    3. Gets all profiles
    4. Gets stream URI for each profile

    Returns complete device info with profiles and their stream URIs.
    """
    main_logger.info(f"Connecting to ONVIF device at {ip}:{port}")

    # Get device info to validate credentials
    try:
        device_info = await get_device_info(ip, username, password, port)
    except HTTPException as e:
        if e.status_code == 401:
            raise
        # Some devices may not support GetDeviceInformation
        device_info = {"manufacturer": "Unknown", "model": "Unknown"}

    # Get profiles
    profiles = await fetch_profiles_digest(ip, username, password, port)

    # Get stream URI for each profile
    profiles_with_uri = []
    for profile in profiles:
        try:
            uri = await get_stream_uri_digest(
                ip, username, password, profile["token"], port
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
        "device_info": device_info,
        "profiles": profiles_with_uri,
    }


# PTZ operations using HTTP Digest


async def get_ptz_service_url(
    ip: str,
    username: str,
    password: str,
    port: int = 80,
) -> str | None:
    """Get PTZ service URL from device capabilities."""
    try:
        capabilities = await get_capabilities(ip, username, password, port)
        return capabilities.get("ptz")
    except Exception:
        # Fallback to common paths
        return f"http://{ip}:{port}/onvif/ptz_service"


async def ptz_continuous_move_digest(
    ip: str,
    username: str,
    password: str,
    profile_token: str,
    x: float,
    y: float,
    z: float,
    port: int = 80,
) -> dict[str, Any]:
    """PTZ continuous move using HTTP Digest auth."""
    ptz_url = await get_ptz_service_url(ip, username, password, port)
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
) -> dict[str, Any]:
    """PTZ stop using HTTP Digest auth."""
    ptz_url = await get_ptz_service_url(ip, username, password, port)
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
