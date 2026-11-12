# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""USB webcams on the OpenNVR machine, as the Add Camera dialog lists them.

``GET /api/v1/webcams/local`` answers ``available: false`` on an install
where MediaMTX cannot see USB (macOS, Windows, a Linux install without
``WEBCAMS=on``); the dialog then offers the host agent instead.

Each webcam says which camera it already feeds, if any: one webcam feeds
one camera (``CameraService.webcam_device_owner``), so the dialog shows it
as taken. Camera names make this a ``cameras.manage`` read, like the host
agent's list."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.camera_source import SOURCE_PUBLISHER
from core.database import get_db
from core.permissions import RequirePermission
from models import Camera
from services.local_webcams import available, is_local_device, list_local_webcams

router = APIRouter(prefix="/webcams", tags=["webcams"])

_require_cameras_manage = RequirePermission("cameras.manage")


@router.get("/local")
def local_webcams(db: Session = Depends(get_db), _user=Depends(_require_cameras_manage)) -> dict:
    if not available():
        return {"available": False, "webcams": []}
    assigned = {
        cam.source_device: cam
        for cam in (
            db.query(Camera)
            .filter(Camera.source_type == SOURCE_PUBLISHER)
            .filter(Camera.source_device.isnot(None))
            .filter(Camera.deleted_at.is_(None))
            .all()
        )
        if is_local_device(cam.source_device)
    }
    webcams = []
    for w in list_local_webcams():
        cam = assigned.get(w.id)
        webcams.append({
            **w.to_wire(),
            "assigned_camera_id": cam.id if cam else None,
            "assigned_camera_name": cam.name if cam else None,
        })
    return {"available": True, "webcams": webcams}
