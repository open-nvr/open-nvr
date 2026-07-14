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

import asyncio
import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from core.auth import get_current_superuser
from core.config import _host_is_internal
from core.database import get_db
from routers.network import (
    detect_local_subnets,
    get_camera_lan_subnet,
    get_camera_lan_subnets,
)
from services.onvif_digest_service import (
    connect_and_get_profiles,
    fetch_profiles_digest,
    get_stream_uri_digest,
    get_system_datetime,
    set_system_datetime,
)

# Import both services - digest is the preferred one for Hikvision compatibility
from services.onvif_service import (
    discover_onvif_devices,
    fetch_profiles,
    get_stream_uri,
    ptz_continuous_move,
    ptz_presets,
    ptz_stop,
    scan_onvif_subnet,
)

router = APIRouter(tags=["onvif"])


def _assert_ip_in_camera_lan(ip: str, db: Session) -> None:
    """SSRF guard for the per-camera ONVIF endpoints.

    The ``ip`` these endpoints connect to is fully client-supplied. Without a
    check, an authenticated caller (or a stolen token / CSRF) could drive the
    server to open connections to arbitrary hosts and ports — internal service
    probing or public / cloud-metadata addresses. Two layers:

    * **Floor** — refuse anything that isn't an internal address
      (loopback / RFC1918 / IPv6 ULA / link-local), reusing the V-015
      trust-zone classifier so a public IP is always rejected.
    * **Subnet** — when the operator has configured Camera LAN subnet(s),
      require the target to be inside one of them (the same restriction
      ``/discover`` already enforces on its scan ranges).
    """
    try:
        target = ipaddress.ip_address(ip)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid IP address: {ip!r}")

    if not _host_is_internal(ip):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Refusing ONVIF connection to non-internal address {ip!r}; "
                "ONVIF is restricted to the camera LAN."
            ),
        )

    configured = get_camera_lan_subnets(db)
    if configured:
        for cidr in configured:
            try:
                if target in ipaddress.ip_network(cidr, strict=False):
                    return
            except ValueError:
                continue
        raise HTTPException(
            status_code=403,
            detail=(
                f"IP {ip} is outside the configured Camera LAN subnet(s) "
                f"({', '.join(configured)})."
            ),
        )


@router.get("/discover")
async def discover(
    cidr: list[str] | None = Query(
        None,
        description="One or more subnet CIDRs to scan (e.g. cidr=192.168.1.0/24&cidr=10.0.0.0/24). "
                    "Falls back to all subnets configured in Camera LAN settings.",
    ),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Discover ONVIF cameras via unicast subnet scan (works in Docker bridge mode).

    Scans all configured Camera LAN subnets in parallel.  An optional cidr parameter
    (repeatable) overrides the configured list; each must be within the primary subnet
    when one is configured.  Multicast WS-Discovery runs as a best-effort fallback.
    """
    configured_subnets = get_camera_lan_subnets(db)
    primary_cidr = get_camera_lan_subnet(db)

    if cidr:
        scan_cidrs: list[str] = []
        for c in cidr:
            try:
                requested = ipaddress.ip_network(c, strict=False)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"Invalid CIDR '{c}': {exc}")
            if primary_cidr:
                try:
                    configured = ipaddress.ip_network(primary_cidr, strict=False)
                    if not requested.subnet_of(configured):
                        raise HTTPException(
                            status_code=400,
                            detail=f"CIDR {c} is not within the configured Camera LAN subnet {primary_cidr}.",
                        )
                except HTTPException:
                    raise
                except ValueError:
                    pass
            scan_cidrs.append(c)
    elif configured_subnets:
        scan_cidrs = configured_subnets
    else:
        # Nothing configured — auto-detect the host's own subnet so discovery
        # "just works" on a flat LAN without any manual Camera LAN setup.
        scan_cidrs = detect_local_subnets()
        if not scan_cidrs:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Couldn't auto-detect your network. Add the camera manually, "
                    "or set the Camera LAN subnet under Network settings."
                ),
            )

    # Scan all subnets in parallel, deduplicate by IP
    try:
        results_per_subnet = await asyncio.gather(
            *[scan_onvif_subnet(c) for c in scan_cidrs],
            return_exceptions=True,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    seen_ips: set[str] = set()
    devices: list[dict] = []
    for result in results_per_subnet:
        if isinstance(result, list):
            for d in result:
                if d.get("ip") and d["ip"] not in seen_ips:
                    seen_ips.add(d["ip"])
                    devices.append(d)

    # Best-effort multicast fallback (works only in host-mode or on native LAN)
    try:
        for md in await discover_onvif_devices():
            if md.get("ip") and md["ip"] not in seen_ips:
                seen_ips.add(md["ip"])
                devices.append(md)
    except Exception:
        pass

    return {"devices": devices, "scan_cidrs": scan_cidrs}


@router.post("/connect")
async def connect_device(
    ip: str = Query(...),
    port: int = Query(80, ge=1, le=65535),
    username: str = Query(...),
    password: str = Query(...),
    db: Session = Depends(get_db),
):
    """
    Connect to ONVIF device and get all profiles with stream URIs.

    Uses HTTP Digest authentication which is compatible with Hikvision and most devices.
    Returns device info and all available profiles with their RTSP stream URIs.
    """
    _assert_ip_in_camera_lan(ip, db)
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
    db: Session = Depends(get_db),
):
    """Get media profiles from camera. Set use_digest=true for Hikvision devices."""
    _assert_ip_in_camera_lan(ip, db)
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
    db: Session = Depends(get_db),
):
    """Get stream URI for a profile. Set use_digest=true for Hikvision devices."""
    _assert_ip_in_camera_lan(ip, db)
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
    db: Session = Depends(get_db),
):
    _assert_ip_in_camera_lan(ip, db)
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
    db: Session = Depends(get_db),
):
    _assert_ip_in_camera_lan(ip, db)
    try:
        result = await ptz_stop(ip, username, password, profile_token, port)
        return {"ip": ip, "profileToken": profile_token, "result": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/camera/{ip}/time")
async def camera_get_time(
    ip: str,
    port: int = Query(80, ge=1, le=65535),
    db: Session = Depends(get_db),
):
    """Read the camera clock via GetSystemDateAndTime (no credentials needed)."""
    _assert_ip_in_camera_lan(ip, db)
    try:
        result = await get_system_datetime(ip, port)
        return {"ip": ip, **result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/camera/{ip}/time/sync")
async def camera_sync_time(
    ip: str,
    port: int = Query(80, ge=1, le=65535),
    username: str = Query(...),
    password: str = Query(...),
    current_user=Depends(get_current_superuser),
    db: Session = Depends(get_db),
):
    """Push the NVR's current UTC time to the camera (superuser only)."""
    _assert_ip_in_camera_lan(ip, db)
    try:
        result = await set_system_datetime(ip, username, password, port)
        return {"ip": ip, **result}
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
    db: Session = Depends(get_db),
):
    _assert_ip_in_camera_lan(ip, db)
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
