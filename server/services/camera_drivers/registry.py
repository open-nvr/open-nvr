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
Driver registry — discover vendor packages, pick the right CameraDriver for a
camera, and build it with a resolved HTTP control port + stored (decrypted)
credentials.

Vendor packages are auto-discovered: every subpackage of ``camera_drivers`` that
exports ``DRIVER``/``matches``/``PRIORITY`` (and optionally an async ``probe``)
is a vendor. Adding a vendor is therefore "drop in a directory" — no edit here.
See ``docs/adding-a-camera-vendor.md``.

Selection order for a camera:
  1. in-process driver cache (per process),
  2. persisted ``CameraCapability.driver_name`` from a prior successful probe,
  3. manufacturer pass — a fresh GetDeviceInformation.manufacturer matched
     against each vendor's ``matches()``, confirmed by its ``probe()`` if any,
  4. fingerprint pass — for OEM rebadges whose manufacturer string lies, run
     each vendor's ``probe()`` in priority order,
  5. the ONVIF baseline as the terminal fallback.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import Camera, CameraCapability
from services import onvif_digest_service as ods

from .base import CameraDriver
from .onvif.driver import OnvifDriver  # guaranteed baseline / terminal fallback

logger = logging.getLogger(__name__)

# camera_id -> resolved HTTP control port (ONVIF/ISAPI live on HTTP, not RTSP).
_ENDPOINT_CACHE: dict[int, int] = {}
# camera_id -> selected driver class (per-process; persisted row is the durable
# cache). Cleared on refresh / port invalidation.
_DRIVER_CACHE: dict[int, type[CameraDriver]] = {}

# The subpackage whose driver is the fallback; excluded from match/probe passes.
_FALLBACK_PACKAGE = "onvif"


@dataclass(frozen=True)
class VendorSpec:
    """One discovered vendor package, normalized for the selection passes."""

    name: str
    driver: type[CameraDriver]
    matches: Callable[[str], bool]
    probe: Callable[[str, int, str | None, str | None], Awaitable[bool]] | None
    priority: int


_VENDORS: list[VendorSpec] | None = None


def _discover() -> list[VendorSpec]:
    """Import every vendor subpackage and collect those honoring the contract.

    Malformed packages (import error, or missing DRIVER/matches) are logged and
    skipped rather than crashing startup — a bad third-party vendor package must
    not take the whole driver layer down. The ONVIF package is the fallback and
    is intentionally excluded here.
    """
    import services.camera_drivers as pkg

    vendors: list[VendorSpec] = []
    for info in pkgutil.iter_modules(pkg.__path__):
        if not info.ispkg or info.name == _FALLBACK_PACKAGE:
            continue
        try:
            mod = importlib.import_module(f"{pkg.__name__}.{info.name}")
        except Exception as e:
            logger.warning(
                "camera_drivers: skipping vendor package %r (import failed: %s)",
                info.name,
                e,
            )
            continue
        driver = getattr(mod, "DRIVER", None)
        matches = getattr(mod, "matches", None)
        if driver is None or matches is None:
            logger.warning(
                "camera_drivers: vendor package %r missing DRIVER/matches — skipped",
                info.name,
            )
            continue
        vendors.append(
            VendorSpec(
                name=info.name,
                driver=driver,
                matches=matches,
                probe=getattr(mod, "probe", None),
                priority=int(getattr(mod, "PRIORITY", 500)),
            )
        )
    vendors.sort(key=lambda v: (v.priority, v.name))
    logger.info(
        "camera_drivers: discovered vendors %s (fallback=%s)",
        [v.name for v in vendors],
        _FALLBACK_PACKAGE,
    )
    return vendors


def get_vendors() -> list[VendorSpec]:
    """Discovered vendor specs (cached after first call), priority-ordered."""
    global _VENDORS
    if _VENDORS is None:
        _VENDORS = _discover()
    return _VENDORS


def _driver_by_name(name: str | None) -> type[CameraDriver] | None:
    """Map a persisted ``driver_name`` back to a discovered driver class."""
    if not name:
        return None
    if name == OnvifDriver.driver_name:
        return OnvifDriver
    for spec in get_vendors():
        if spec.driver.driver_name == name:
            return spec.driver
    return None


def select_driver_class(manufacturer: str | None) -> type[CameraDriver]:
    """String-only vendor pick (no probe). Public + used by the manufacturer
    pass; unknown/other brands fall back to the ONVIF baseline."""
    m = (manufacturer or "").strip().lower()
    for spec in get_vendors():
        if spec.matches(m):
            return spec.driver
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
    """Clear both the resolved-port and selected-driver caches for a camera."""
    _ENDPOINT_CACHE.pop(camera_id, None)
    _DRIVER_CACHE.pop(camera_id, None)


# Back-compat / intent-revealing alias.
invalidate_driver_cache = invalidate_port_cache


async def _select_driver_cls(
    db: Session, camera: Camera, creds: dict, *, force: bool
) -> type[CameraDriver]:
    camera_id = camera.id
    if not force:
        if camera_id in _DRIVER_CACHE:
            return _DRIVER_CACHE[camera_id]
        row = (
            db.query(CameraCapability)
            .filter(CameraCapability.camera_id == camera_id)
            .first()
        )
        if row is not None and row.probe_result == "ok":
            cls = _driver_by_name(row.driver_name)
            if cls is not None:
                _DRIVER_CACHE[camera_id] = cls
                return cls

    ip, port = creds["ip"], creds["http_port"]
    user, pw = creds["username"], creds["password"]

    # Manufacturer pass: fresh device-info read, fall back to the stored column.
    reached = False
    try:
        manufacturer = (await OnvifDriver(**creds).get_info()).manufacturer or ""
        reached = True
    except Exception:
        manufacturer = camera.manufacturer or ""
    m = manufacturer.strip().lower()
    for spec in get_vendors():
        if spec.matches(m) and (
            spec.probe is None or await spec.probe(ip, port, user, pw)
        ):
            _DRIVER_CACHE[camera_id] = spec.driver
            return spec.driver

    # Fingerprint pass: manufacturer unknown/unmatched or its native probe
    # failed — identify OEM rebadges by what the device actually serves.
    for spec in get_vendors():
        if spec.probe is not None and await spec.probe(ip, port, user, pw):
            _DRIVER_CACHE[camera_id] = spec.driver
            return spec.driver

    # Fallback. Only cache when we actually reached the device, so a transient
    # outage doesn't pin ONVIF until the next explicit refresh.
    if reached:
        _DRIVER_CACHE[camera_id] = OnvifDriver
    return OnvifDriver


async def get_driver_for_camera(
    db: Session, camera_id: int, *, force: bool = False
) -> CameraDriver:
    """Build the correct driver for a stored camera (creds loaded from DB).

    ``force=True`` re-detects from scratch (used by the capability re-probe),
    ignoring and refreshing both caches.
    """
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    if force:
        invalidate_port_cache(camera_id)

    port = await resolve_http_port(camera_id, camera.ip_address, camera.port)
    creds = {
        "camera_id": camera_id,
        "ip": camera.ip_address,
        "username": camera.username,
        "password": camera.password,  # hybrid property auto-decrypts
        "http_port": port,
    }

    cls = await _select_driver_cls(db, camera, creds, force=force)
    return cls(**creds)
