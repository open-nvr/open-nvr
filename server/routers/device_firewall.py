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
Device-firewall admin API — approve/block the devices allowed to use OpenNVR.

``/status`` is intentionally reachable by anyone (it is in the middleware's open
list) so a blocked device can learn *why* it is blocked and show its own IP.
Everything else is superuser-only.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.auth import get_current_superuser
from core.client_ip import get_client_ip
from core.database import get_db
from services import device_firewall_service as dfw

router = APIRouter(prefix="/device-firewall", tags=["device-firewall"])


class EnforcementUpdate(BaseModel):
    active: bool


class DeviceLabel(BaseModel):
    label: str | None = None


def _serialize(dev) -> dict:
    return {
        "ip_address": dev.ip_address,
        "label": dev.label,
        "status": dev.status.value if dev.status else None,
        "user_agent": dev.user_agent,
        "attempt_count": dev.attempt_count,
        "auto_enrolled": dev.auto_enrolled,
        "first_seen": dev.first_seen.isoformat() if dev.first_seen else None,
        "last_seen": dev.last_seen.isoformat() if dev.last_seen else None,
        "approved_at": dev.approved_at.isoformat() if dev.approved_at else None,
    }


@router.get("/status")
async def my_status(request: Request, db: Session = Depends(get_db)):
    """This caller's device state — reachable even when blocked, so the UI can
    tell the user their device is pending and show its IP to give an admin."""
    ip = get_client_ip(request)
    dev = dfw.get_device(db, ip)
    return {
        "device_ip": ip,
        "status": dev.status.value if dev and dev.status else "unknown",
        "enforcement_active": dfw.enforcement_active(db),
    }


@router.get("/devices")
async def list_devices(
    _user=Depends(get_current_superuser), db: Session = Depends(get_db)
):
    return {
        "enforcement_active": dfw.enforcement_active(db),
        "devices": [_serialize(d) for d in dfw.list_devices(db)],
    }


@router.put("/enforcement")
async def set_enforcement(
    payload: EnforcementUpdate,
    _user=Depends(get_current_superuser),
    db: Session = Depends(get_db),
):
    """Turn enforcement on/off. Returns the *effective* state — the env
    break-glass can force it off even when the admin asks for on."""
    effective = dfw.set_enforcement(db, payload.active)
    return {"requested": payload.active, "enforcement_active": effective}


@router.post("/devices/{ip}/approve")
async def approve_device(
    ip: str,
    payload: DeviceLabel | None = None,
    user=Depends(get_current_superuser),
    db: Session = Depends(get_db),
):
    dev = dfw.approve(
        db, ip, user_id=user.id, label=(payload.label if payload else None)
    )
    return _serialize(dev)


@router.post("/devices/{ip}/block")
async def block_device(
    ip: str,
    _user=Depends(get_current_superuser),
    db: Session = Depends(get_db),
):
    return _serialize(dfw.block(db, ip))


@router.delete("/devices/{ip}")
async def delete_device(
    ip: str,
    _user=Depends(get_current_superuser),
    db: Session = Depends(get_db),
):
    return {"deleted": dfw.delete(db, ip)}
