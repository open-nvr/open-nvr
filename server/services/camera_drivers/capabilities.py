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
Capability probe — talk to the camera, record what its driver can read/drive,
and refresh the device metadata on the Camera row.

Persisted to the ``camera_capabilities`` table (result + probed_at, mirroring
the transport-security probe). Never mutates the camera's IP/network — this is
a read-only interrogation that only writes metadata (manufacturer/model/fw)
back locally.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from core.logging_config import camera_logger
from models import Camera, CameraCapability

from .registry import get_driver_for_camera


def _get_or_create_row(db: Session, camera_id: int) -> CameraCapability:
    row = (
        db.query(CameraCapability)
        .filter(CameraCapability.camera_id == camera_id)
        .first()
    )
    if row is None:
        row = CameraCapability(camera_id=camera_id)
        db.add(row)
    return row


async def probe_and_store(db: Session, camera_id: int) -> CameraCapability:
    """Probe the camera and upsert its capability row. Always returns a row
    (with ``probe_result`` reflecting success/failure) rather than raising for
    an unreachable camera, so the UI can show a 'refresh' affordance."""
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    row = _get_or_create_row(db, camera_id)
    try:
        driver = await get_driver_for_camera(db, camera_id)
        info = await driver.get_info()
        caps = await driver.get_capabilities()

        row.driver_name = driver.driver_name
        row.manufacturer = info.manufacturer
        row.model = info.model
        row.firmware_version = info.firmware_version
        row.onvif_endpoints = caps.onvif_endpoints
        row.supported_areas = caps.supported_areas
        row.capabilities = caps.detail
        row.probe_result = "ok"
        row.probe_error = None

        # Refresh device METADATA on the camera (never touches IP/network).
        if info.manufacturer:
            camera.manufacturer = info.manufacturer
        if info.model:
            camera.model = info.model
        if info.firmware_version:
            camera.firmware_version = info.firmware_version
        if info.serial_number:
            camera.serial_number = info.serial_number
        if info.hardware_id:
            camera.hardware_id = info.hardware_id
    except HTTPException as e:
        row.probe_result = (
            "unreachable" if e.status_code in (503, 504) else "error"
        )
        row.probe_error = str(e.detail)[:500]
        camera_logger.warning(
            "Capability probe failed for camera %s: %s", camera_id, e.detail
        )
    except Exception as e:  # never let a probe crash the request
        row.probe_result = "error"
        row.probe_error = f"{type(e).__name__}: {e}"[:500]
        camera_logger.warning(
            "Capability probe error for camera %s: %s", camera_id, e
        )

    row.probed_at = datetime.now(UTC)
    db.commit()
    db.refresh(row)
    return row


async def get_or_probe(db: Session, camera_id: int) -> CameraCapability:
    """Return the cached capability row, probing once if never probed."""
    row = (
        db.query(CameraCapability)
        .filter(CameraCapability.camera_id == camera_id)
        .first()
    )
    if row is None or row.probe_result == "not_probed":
        return await probe_and_store(db, camera_id)
    return row
