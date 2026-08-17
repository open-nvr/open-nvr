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
System control endpoints (shutdown/reboot).

Guarded by superuser. In debug mode, actions are NO-OP for safety and only log/acknowledge.
"""

import json
import platform
import subprocess

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from core.auth import get_current_active_user, get_current_superuser
from core.config import settings
from core.database import get_db
from core.policy import current_posture
from models import Camera, CameraEvent, SecuritySetting, SystemEvent
from schemas import SystemMonitoringSettings

router = APIRouter(prefix="/system", tags=["system"])  # mounted at /api/v1


@router.get("/posture")
async def get_security_posture(current_user=Depends(get_current_active_user)):
    """Expose the active offline-first policy so the UI can show a
    deployment-mode badge and the operator can confirm the sovereignty profile.
    See V-009 / V-022.

    Read-only and authenticated-user-scope (not just superuser) — operators
    monitoring the system should be able to see this without admin rights.
    Mirrors the boot-time audit entry written by core.policy.audit_boot_posture.
    """
    return current_posture()


# =============================================================================
# Host resource monitoring (see services/system_monitor_service.py)
# =============================================================================


@router.get("/resources")
async def get_system_resources(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Latest host resource sample (CPU / memory / recordings-volume disk)
    plus active alerts and the effective thresholds. Active-user scope: this
    powers the dashboard health card, same rationale as /posture."""
    from services.system_monitor_service import (
        get_system_monitor,
        load_monitoring_settings,
    )

    return get_system_monitor().snapshot(load_monitoring_settings(db))


@router.get("/resources/history")
async def get_system_resources_history(
    minutes: int = Query(60, ge=1, le=1440),
    current_user=Depends(get_current_active_user),
):
    """Recent resource samples from the in-memory ring buffers (15s grain up
    to 1h, 5-min averages up to 24h). History does not survive restarts —
    persistent history would mean constant writes on the monitored volume."""
    from services.system_monitor_service import get_system_monitor

    return {"minutes": minutes, "samples": get_system_monitor().history(minutes)}


@router.get("/events")
async def list_system_events(
    limit: int = Query(200, ge=1, le=1000),
    event_type: str | None = Query(None, max_length=50),
    include_camera_alerts: bool = Query(True),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """System alert history, newest first. With ``include_camera_alerts``
    (default), recent ``recording_stalled`` camera events are merged in (with
    camera id/name) so the Alerts view gets both from one call."""
    q = db.query(SystemEvent)
    if event_type:
        q = q.filter(SystemEvent.event_type == event_type)
    rows = q.order_by(SystemEvent.id.desc()).limit(limit).all()
    events = [
        {
            "id": f"sys-{r.id}",
            "event_type": r.event_type,
            "event_state": r.event_state,
            "severity": r.severity,
            "description": r.description,
            "data": json.loads(r.data) if r.data else None,
            "camera_id": None,
            "camera_name": None,
            "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
        }
        for r in rows
    ]

    if include_camera_alerts and not event_type:
        cam_rows = (
            db.query(CameraEvent, Camera.name)
            .outerjoin(Camera, Camera.id == CameraEvent.camera_id)
            .filter(CameraEvent.event_type == "recording_stalled")
            .order_by(CameraEvent.id.desc())
            .limit(limit)
            .all()
        )
        events.extend(
            {
                "id": f"cam-{r.id}",
                "event_type": r.event_type,
                "event_state": r.event_state,
                "severity": "warning" if r.event_state == "active" else "info",
                "description": r.description,
                "data": None,
                "camera_id": r.camera_id,
                "camera_name": cam_name,
                "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
            }
            for r, cam_name in cam_rows
        )
        events.sort(key=lambda e: e["occurred_at"] or "", reverse=True)
        events = events[:limit]

    return {"events": events}


MONITORING_SETTINGS_KEY = "system_monitoring"


def _get_or_init_monitoring(db: Session) -> SecuritySetting:
    row = (
        db.query(SecuritySetting)
        .filter(SecuritySetting.key == MONITORING_SETTINGS_KEY)
        .first()
    )
    if not row:
        row = SecuritySetting(
            key=MONITORING_SETTINGS_KEY,
            json_value=json.dumps(SystemMonitoringSettings().model_dump()),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


@router.get("/monitoring-settings")
async def get_monitoring_settings(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Get host monitoring thresholds."""
    from services.system_monitor_service import load_monitoring_settings

    _get_or_init_monitoring(db)
    return load_monitoring_settings(db).model_dump()


@router.put("/monitoring-settings")
async def update_monitoring_settings(
    payload: SystemMonitoringSettings,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Update host monitoring thresholds."""
    row = _get_or_init_monitoring(db)
    obj = SystemMonitoringSettings(**payload.model_dump())
    row.json_value = json.dumps(obj.model_dump())
    db.commit()
    return obj.model_dump()


def _run_command(cmd: list[str]):
    # Security: Whitelist allowed executables to prevent command injection
    allowed_executables = {"shutdown", "reboot", "sudo"}
    if not cmd or cmd[0] not in allowed_executables:
        raise HTTPException(
            status_code=500, detail="Security Violation: Unauthorized command"
        )

    try:
        # Security: shell=False prevents shell injection attacks
        subprocess.Popen(
            cmd, shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to execute command: {e}")


@router.post("/shutdown")
async def shutdown(current_user=Depends(get_current_superuser)):
    if settings.debug:
        return {"accepted": True, "message": "Shutdown requested (debug mode: no-op)"}
    system = platform.system().lower()
    if system.startswith("win"):
        _run_command(["shutdown", "/s", "/t", "0"])
    else:
        _run_command(["sudo", "shutdown", "-h", "now"])
    return {"accepted": True}


@router.post("/reboot")
async def reboot(current_user=Depends(get_current_superuser)):
    if settings.debug:
        return {"accepted": True, "message": "Reboot requested (debug mode: no-op)"}
    system = platform.system().lower()
    if system.startswith("win"):
        _run_command(["shutdown", "/r", "/t", "0"])
    else:
        _run_command(["sudo", "reboot"])
    return {"accepted": True}
