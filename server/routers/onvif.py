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
import contextlib
import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from core.auth import get_current_active_user, get_current_superuser
from core.config import _host_is_internal
from core.database import get_db
from core.logging_config import main_logger
from core.client_ip import get_client_ip
from routers.network import (
    detect_local_subnets,
    get_camera_lan_subnets,
    lan_cidr_for_ip,
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
    scan_onvif_subnets_exclusive,
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


# ── Per-camera authorization for the IP-keyed routes ───────────────────
#
# Reported (CWE-862 / CWE-639): every /camera/{ip}/... route and /connect
# checked only that the IP was inside the camera LAN. The sibling routes
# in routers/cameras.py (/cameras/{id}/ptz/...) require ownership; these
# IP-keyed twins let ANY authenticated user PTZ, read the stream URI of,
# or relay credentials at any camera on the LAN. The ceiling (internal
# address only) is a network-safety rule; this is the missing
# authorization rule beside it.
#
# Two tiers, matching what the routes are for:
#
# * "view"  — connect / profiles / stream-uri / time. Used by the Add
#   Camera wizard on devices that are NOT registered yet, so an
#   unregistered IP is allowed (any active user may create cameras);
#   a registered IP requires ownership, a can_view grant, or superuser.
# * "manage" — PTZ move / stop / presets. Never part of onboarding: a
#   registered IP requires ownership, a can_manage grant, or superuser;
#   an unregistered IP is refused outright.


def _authorize_camera_ip(db: Session, user, ip: str, *, action: str) -> None:
    """Raise 403 unless ``user`` may perform ``action`` ("view" |
    "manage") against the camera(s) registered at ``ip``. Superusers
    pass; deleted cameras do not count as registered."""
    if getattr(user, "is_superuser", False):
        return
    from models import Camera, CameraPermission

    registered = (
        db.query(Camera)
        .filter(Camera.ip_address == ip, Camera.deleted_at.is_(None))
        .all()
    )
    if not registered:
        if action == "view":
            return                       # onboarding a device nobody owns yet
        raise HTTPException(
            status_code=403,
            detail=f"No registered camera at {ip}; add it first to control it.",
        )
    if any(c.owner_id == user.id for c in registered):
        return
    flag = CameraPermission.can_manage if action == "manage" \
        else CameraPermission.can_view
    granted = (
        db.query(CameraPermission.id)
        .filter(
            CameraPermission.user_id == user.id,
            CameraPermission.camera_id.in_([c.id for c in registered]),
            flag == True,  # noqa: E712 — SQLAlchemy column comparison
        )
        .first()
    )
    if granted is None:
        raise HTTPException(
            status_code=403,
            detail="Not enough permissions to access this camera",
        )


def _resolve_scan_plan(db: Session, cidr: list[str] | None) -> tuple[list[str], str]:
    """Resolve which subnets a discovery scan would cover and where they came
    from: explicit ``cidr`` override → configured Camera LAN → auto-detection.

    Override CIDRs are validated (internal-only, /16 or smaller) and raise like
    ``/discover`` always has; unparseable *configured* CIDRs are dropped with a
    warning. Returns ``([], source)`` when nothing usable remains — callers
    decide whether that is an error."""
    source = "configured"
    if cidr:
        source = "override"
        scan_cidrs: list[str] = []
        for c in cidr:
            try:
                requested = ipaddress.ip_network(c, strict=False)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"Invalid CIDR '{c}': {exc}")
            if not _host_is_internal(str(requested.network_address)):
                raise HTTPException(
                    status_code=403,
                    detail=f"Refusing to scan non-internal range {c}; discovery is restricted to private networks.",
                )
            if requested.prefixlen < 16:
                raise HTTPException(
                    status_code=400,
                    detail=f"CIDR {c} is too large to scan; use /16 or smaller.",
                )
            scan_cidrs.append(c)
        return scan_cidrs, source

    configured_subnets = get_camera_lan_subnets(db)
    if configured_subnets:
        scan_cidrs = configured_subnets
    else:
        source = "auto-detected"
        # Nothing configured — auto-detect the host's own subnet so discovery
        # "just works" on a flat LAN without any manual Camera LAN setup.
        scan_cidrs = detect_local_subnets()

    # Drop unparseable configured CIDRs instead of failing the whole scan.
    valid_cidrs: list[str] = []
    for c in scan_cidrs:
        try:
            ipaddress.ip_network(c, strict=False)
            valid_cidrs.append(c)
        except ValueError:
            main_logger.warning(f"Skipping invalid Camera LAN CIDR {c!r} in discovery scan")
    return valid_cidrs, source


@router.get("/discover/plan")
async def discover_plan(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Return the subnets a discovery scan would cover, without scanning.

    Lets the UI show "Scanning 192.168.1.0/24 · auto-detected" while the actual
    (slow) scan is still running, and render a friendly prompt instead of an
    error when nothing is configured or detectable (``scan_cidrs`` comes back
    empty rather than a 400).

    ``detected_cidrs`` lists ALL of the host's detected LAN subnets regardless
    of the configured Camera LAN, so the UI can always offer "scan a host
    network" — an operator hunting for a camera on a second NIC shouldn't have
    to know its range by heart. Detection is Docker-aware (bridge ignored,
    host IPs come from OPENNVR_HOST_IP/OPENNVR_LAN_IPS)."""
    scan_cidrs, source = _resolve_scan_plan(db, None)
    try:
        detected_cidrs = detect_local_subnets()
    except Exception:
        detected_cidrs = []
    return {
        "scan_cidrs": scan_cidrs,
        "source": source,
        "detected_cidrs": detected_cidrs,
        # The requesting browser's own subnet (if it's a usable LAN address):
        # a live scan suggestion that can't go stale, unlike the env-derived
        # host hints in detected_cidrs.
        "client_cidr": lan_cidr_for_ip(get_client_ip(request)),
    }


# How often the /discover handler checks whether the client hung up. Polling
# is safe on a body-less GET (nothing else consumes the receive channel).
_DISCONNECT_POLL_S = 1.0


async def _await_scan_or_disconnect(scan: asyncio.Task, request: Request):
    """Await the sweep, polling for client abort ~1/s.

    The UI's Stop button (and its scan-supersede path) only drops the HTTP
    connection; uvicorn does not cancel the handler, so without this the
    sweep would run to completion and keep loading the NAT path — starving
    the very scan the operator started next.
    """
    while True:
        done, _ = await asyncio.wait({scan}, timeout=_DISCONNECT_POLL_S)
        if done:
            return scan.result()
        if await request.is_disconnected():
            scan.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scan
            raise HTTPException(
                status_code=409, detail="Client disconnected; scan stopped."
            )


@router.get("/discover")
async def discover(
    request: Request,
    cidr: list[str] | None = Query(
        None,
        description="One or more subnet CIDRs to scan (e.g. cidr=192.168.1.0/24&cidr=10.0.0.0/24). "
                    "Falls back to all subnets configured in Camera LAN settings.",
    ),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Discover ONVIF cameras via unicast subnet scan (works in Docker bridge mode).

    Scans all configured Camera LAN subnets under one concurrency bound. An
    optional ``cidr`` parameter (repeatable) overrides the configured list for
    this scan only — each must be an internal (RFC1918-class) range, but may lie
    outside the configured Camera LAN: the caller is a superuser looking for a
    camera before committing a Camera LAN change, and adding a camera found
    outside the boundary still requires persisting that change first (the
    connect/add flows keep enforcing the configured subnets). Multicast
    WS-Discovery runs as a best-effort fallback.
    """
    valid_cidrs, source = _resolve_scan_plan(db, cidr)
    if not valid_cidrs:
        if source == "auto-detected":
            raise HTTPException(
                status_code=400,
                detail=(
                    "Couldn't auto-detect your network. Add the camera manually, "
                    "or set the Camera LAN subnet under Network settings."
                ),
            )
        raise HTTPException(
            status_code=400,
            detail="No valid subnet to scan; check the Camera LAN settings.",
        )

    # One call scans all subnets under a single shared concurrency bound —
    # per-subnet parallel scans would multiply the connect burst and wedge the
    # container's NAT path (see scan_onvif_subnets). The exclusive wrapper
    # also supersedes any sweep already in flight, and the disconnect watcher
    # stops the sweep when the client aborts the request.
    try:
        scan = asyncio.create_task(scan_onvif_subnets_exclusive(valid_cidrs))
        devices = await _await_scan_or_disconnect(scan, request)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    seen_ips: set[str] = {d["ip"] for d in devices}
    scan_cidrs = valid_cidrs

    # Best-effort multicast fallback (works only in host-mode or on native
    # LAN). Deliberately not serialized with the exclusive sweep slot: a
    # superseded/disconnected request bails above before reaching this, and
    # the residual overlap window is one cheap UDP broadcast in a thread.
    try:
        for md in await discover_onvif_devices():
            if md.get("ip") and md["ip"] not in seen_ips:
                seen_ips.add(md["ip"])
                devices.append(md)
    except Exception:
        pass

    # Surface detected local subnets that the scan did NOT cover, so the UI can
    # suggest them when a scan comes up empty (e.g. the configured Camera LAN is
    # stale after a router change). detect_local_subnets() is Docker-aware — in
    # a container it ignores the bridge network and reports the host's LAN
    # (from OPENNVR_HOST_IP) — so suggestions are safe there too.
    suggested_cidrs: list[str] = []
    try:
        scanned_nets = [ipaddress.ip_network(c, strict=False) for c in scan_cidrs]
        for det in detect_local_subnets():
            det_net = ipaddress.ip_network(det, strict=False)
            if not any(det_net.overlaps(s) for s in scanned_nets):
                suggested_cidrs.append(det)
    except Exception:
        pass

    return {
        "devices": devices,
        "scan_cidrs": scan_cidrs,
        "source": source,
        "suggested_cidrs": suggested_cidrs,
        # Mirror of /discover/plan's client_cidr so scan responses can top up
        # the UI's suggestions too.
        "client_cidr": lan_cidr_for_ip(get_client_ip(request)),
    }


@router.post("/connect")
async def connect_device(
    ip: str = Query(...),
    port: int = Query(80, ge=1, le=65535),
    username: str = Query(...),
    password: str = Query(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """
    Connect to ONVIF device and get all profiles with stream URIs.

    Uses HTTP Digest authentication which is compatible with Hikvision and most devices.
    Returns device info and all available profiles with their RTSP stream URIs.
    """
    _assert_ip_in_camera_lan(ip, db)
    _authorize_camera_ip(db, current_user, ip, action="view")
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
    current_user=Depends(get_current_active_user),
):
    """Get media profiles from camera. Set use_digest=true for Hikvision devices."""
    _assert_ip_in_camera_lan(ip, db)
    _authorize_camera_ip(db, current_user, ip, action="view")
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
    current_user=Depends(get_current_active_user),
):
    """Get stream URI for a profile. Set use_digest=true for Hikvision devices."""
    _assert_ip_in_camera_lan(ip, db)
    _authorize_camera_ip(db, current_user, ip, action="view")
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
    current_user=Depends(get_current_active_user),
):
    _assert_ip_in_camera_lan(ip, db)
    _authorize_camera_ip(db, current_user, ip, action="manage")
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
    current_user=Depends(get_current_active_user),
):
    _assert_ip_in_camera_lan(ip, db)
    _authorize_camera_ip(db, current_user, ip, action="manage")
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
    current_user=Depends(get_current_active_user),
):
    """Read the camera clock via GetSystemDateAndTime (no credentials needed)."""
    _assert_ip_in_camera_lan(ip, db)
    _authorize_camera_ip(db, current_user, ip, action="view")
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
    current_user=Depends(get_current_active_user),
):
    _assert_ip_in_camera_lan(ip, db)
    _authorize_camera_ip(db, current_user, ip, action="manage")
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
