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
Driver registry — pick the right CameraDriver for a camera and build it with
resolved HTTP control port + stored (decrypted) credentials.

Vendor selection keys off a FRESH GetDeviceInformation.manufacturer, not the
possibly-stale ``Camera.manufacturer`` column. Unknown/other brands fall back
to the universal OnvifDriver (so CP Plus, Dahua, etc. still work today; native
CGI drivers can be added here later without touching callers).
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import Camera
from services import onvif_digest_service as ods

from .base import CameraDriver
from .hikvision_isapi_driver import HikvisionIsapiDriver
from .onvif_driver import OnvifDriver

# camera_id -> resolved HTTP control port (ONVIF/ISAPI live on HTTP, not RTSP).
_ENDPOINT_CACHE: dict[int, int] = {}


def select_driver_class(manufacturer: str | None) -> type[CameraDriver]:
    m = (manufacturer or "").strip().lower()
    if "hikvision" in m:
        return HikvisionIsapiDriver
    # Dahua / CP Plus / Uniview / unknown → ONVIF baseline for now.
    return OnvifDriver


async def resolve_http_port(camera_id: int, ip: str, camera_port: int | None) -> int:
    """Find the camera's HTTP/ONVIF control port (cached per camera).

    Cameras stream RTSP on 554 but serve ONVIF/ISAPI over HTTP (usually 80,
    sometimes 8000). Probe candidates with an unauthenticated
    GetSystemDateAndTime (a 200/401 means "ONVIF HTTP is here") and cache the
    winner. Defaults to 80 if nothing answers.
    """
    if camera_id in _ENDPOINT_CACHE:
        return _ENDPOINT_CACHE[camera_id]
    candidates = [80, 8000, 8080, 2020]
    if camera_port and camera_port not in (554, 0) and camera_port not in candidates:
        candidates.insert(1, camera_port)
    for port in candidates:
        try:
            await ods.get_system_datetime(ip, port)  # raises unless 200/401
            _ENDPOINT_CACHE[camera_id] = port
            return port
        except Exception:
            continue
    _ENDPOINT_CACHE[camera_id] = 80
    return 80


def invalidate_port_cache(camera_id: int) -> None:
    _ENDPOINT_CACHE.pop(camera_id, None)


async def get_driver_for_camera(db: Session, camera_id: int) -> CameraDriver:
    """Build the correct driver for a stored camera (creds loaded from DB)."""
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    port = await resolve_http_port(camera_id, camera.ip_address, camera.port)
    creds = {
        "camera_id": camera_id,
        "ip": camera.ip_address,
        "username": camera.username,
        "password": camera.password,  # hybrid property auto-decrypts
        "http_port": port,
    }

    # Detect vendor from a fresh device-info read; fall back to stored value.
    probe = OnvifDriver(**creds)
    manufacturer: str | None
    try:
        manufacturer = (await probe.get_info()).manufacturer
    except Exception:
        manufacturer = camera.manufacturer

    cls = select_driver_class(manufacturer)
    if cls is OnvifDriver:
        return probe  # reuse the instance we already built
    return cls(**creds)
