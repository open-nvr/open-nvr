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
ONVIF routes: discovery, profiles, stream URI, PTZ controls.

Supports both WS-Security (via onvif-zeep) and HTTP Digest authentication.
HTTP Digest is more compatible with Hikvision and similar devices.
"""

from fastapi import APIRouter, HTTPException, Query

from services.onvif_digest_service import (
    connect_and_get_profiles,
    fetch_profiles_digest,
    get_stream_uri_digest,
)

# Import both services - digest is the preferred one for Hikvision compatibility
from services.onvif_service import (
    discover_onvif_devices,
    fetch_profiles,
    get_stream_uri,
    ptz_continuous_move,
    ptz_presets,
    ptz_stop,
)

router = APIRouter(tags=["onvif"])


@router.get("/discover")
async def discover():
    try:
        devices = await discover_onvif_devices()
        return {"devices": devices}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/connect")
async def connect_device(
    ip: str = Query(...),
    port: int = Query(80, ge=1, le=65535),
    username: str = Query(...),
    password: str = Query(...),
):
    """
    Connect to ONVIF device and get all profiles with stream URIs.

    Uses HTTP Digest authentication which is compatible with Hikvision and most devices.
    Returns device info and all available profiles with their RTSP stream URIs.
    """
    try:
        result = await connect_and_get_profiles(ip, username, password, port)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/camera/{ip}/profiles")
async def camera_profiles(
    ip: str,
    port: int = Query(80, ge=1, le=65535),
    username: str = Query(...),
    password: str = Query(...),
    use_digest: bool = Query(
        True, description="Use HTTP Digest auth (better Hikvision compatibility)"
    ),
):
    """Get media profiles from camera. Set use_digest=true for Hikvision devices."""
    try:
        if use_digest:
            profiles = await fetch_profiles_digest(ip, username, password, port)
        else:
            profiles = await fetch_profiles(ip, username, password, port)
        return {"ip": ip, "profiles": profiles}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/camera/{ip}/stream-uri")
async def camera_stream_uri(
    ip: str,
    profile_token: str = Query(..., alias="profileToken"),
    port: int = Query(80, ge=1, le=65535),
    username: str = Query(...),
    password: str = Query(...),
    use_digest: bool = Query(
        True, description="Use HTTP Digest auth (better Hikvision compatibility)"
    ),
):
    """Get stream URI for a profile. Set use_digest=true for Hikvision devices."""
    try:
        if use_digest:
            uri = await get_stream_uri_digest(
                ip, username, password, profile_token, port
            )
        else:
            uri = await get_stream_uri(ip, username, password, profile_token, port)
        return {"ip": ip, "profileToken": profile_token, "uri": uri}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/camera/{ip}/ptz/move")
async def camera_ptz_move(
    ip: str,
    x: float = Query(0.0, ge=-1.0, le=1.0),
    y: float = Query(0.0, ge=-1.0, le=1.0),
    z: float = Query(0.0, ge=-1.0, le=1.0),
    profile_token: str = Query(..., alias="profileToken"),
    port: int = Query(80, ge=1, le=65535),
    username: str = Query(...),
    password: str = Query(...),
):
    try:
        result = await ptz_continuous_move(
            ip, username, password, profile_token, x, y, z, port
        )
        return {"ip": ip, "profileToken": profile_token, "result": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/camera/{ip}/ptz/stop")
async def camera_ptz_stop(
    ip: str,
    profile_token: str = Query(..., alias="profileToken"),
    port: int = Query(80, ge=1, le=65535),
    username: str = Query(...),
    password: str = Query(...),
):
    try:
        result = await ptz_stop(ip, username, password, profile_token, port)
        return {"ip": ip, "profileToken": profile_token, "result": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/camera/{ip}/ptz/preset")
async def camera_ptz_preset(
    ip: str,
    action: str = Query(...),
    profile_token: str = Query(..., alias="profileToken"),
    name: str | None = None,
    preset_token: str | None = Query(None, alias="presetToken"),
    port: int = Query(80, ge=1, le=65535),
    username: str = Query(...),
    password: str = Query(...),
):
    try:
        result = await ptz_presets(
            ip,
            username,
            password,
            profile_token,
            action,
            name=name,
            preset_token=preset_token,
            port=port,
        )
        return {
            "ip": ip,
            "profileToken": profile_token,
            "action": action,
            "result": result,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
