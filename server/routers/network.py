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
Network router for Camera LAN and Uplink configuration.

Stores settings in SecuritySetting keys and exposes computed lists like whitelisted
IPs (provisioned cameras) and blacklisted IPs.

Also provides an endpoint to create a firewall rule to isolate the Camera LAN
from the internet (DB-only rule; OS-level enforcement depends on an external agent).
"""

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from core.auth import get_current_superuser
from core.database import get_db
from models import Camera, CameraConfig, FirewallRule, SecuritySetting
from schemas import NetworkConfig
from services.audit_service import write_audit_log

router = APIRouter(prefix="/network", tags=["network"])


def _get_or_init(
    db: Session, key: str, default_value: dict[str, Any]
) -> SecuritySetting:
    row = db.query(SecuritySetting).filter(SecuritySetting.key == key).first()
    if not row:
        row = SecuritySetting(key=key, json_value=json.dumps(default_value))
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _load_json(row: SecuritySetting) -> dict[str, Any]:
    try:
        return json.loads(row.json_value or "{}")
    except Exception:
        return {}


def _default_camera_lan() -> dict[str, Any]:
    return {
        "interface_name": "eth0",
        "dhcp_enabled": True,
        "ipv4_address": None,
        "ipv4_subnet_mask": None,
        "ipv4_gateway": None,
        "preferred_dns": None,
        "alternate_dns": None,
        "mtu": 1500,
        "description": "Isolated camera network (no internet).",
        "subnet_cidr": None,
    }


def _default_uplink() -> dict[str, Any]:
    return {
        "interface_name": "eth1",
        "dhcp_enabled": True,
        "ipv4_address": None,
        "ipv4_subnet_mask": None,
        "ipv4_gateway": None,
        "preferred_dns": None,
        "alternate_dns": None,
        "mtu": 1500,
        "description": "External uplink to the internet.",
        "blacklisted_ips": [],
    }


@router.get("/camera-lan")
async def get_camera_lan(
    db: Session = Depends(get_db), current_user=Depends(get_current_superuser)
):
    row = _get_or_init(db, "network_camera_lan", _default_camera_lan())
    val = {**_default_camera_lan(), **_load_json(row)}
    # Compute whitelist from provisioned cameras (cameras with a config row)
    cam_rows = (
        db.query(Camera).join(CameraConfig, Camera.id == CameraConfig.camera_id).all()
    )
    whitelist = sorted({c.ip_address for c in cam_rows if c.ip_address})
    return {"settings": val, "whitelisted_ips": whitelist}


@router.put("/camera-lan")
async def update_camera_lan(
    payload: NetworkConfig,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    row = _get_or_init(db, "network_camera_lan", _default_camera_lan())
    current_val = {**_default_camera_lan(), **_load_json(row)}

    # Convert payload to dict, excluding None to simulate PATCH behavior if needed,
    # but here we might want to allow setting to None.
    # NetworkConfig has defaults.
    payload_dict = payload.model_dump(exclude_unset=True)

    # Shallow merge
    for k, v in payload_dict.items():
        if k in current_val:
            current_val[k] = v

    row.json_value = json.dumps(current_val)
    db.commit()
    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="network",
            entity_id="camera-lan",
            details=payload_dict,
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return {"settings": current_val}


@router.post("/camera-lan/isolate")
async def isolate_camera_lan(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    """Create a high-priority firewall rule that denies outbound traffic from the camera LAN subnet.

    Note: This stores the rule in DB; applying it at OS level requires an external agent.
    """
    row = _get_or_init(db, "network_camera_lan", _default_camera_lan())
    cfg = {**_default_camera_lan(), **_load_json(row)}
    subnet = cfg.get("subnet_cidr") or cfg.get("ipv4_address")
    if not subnet:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide subnet_cidr or ipv4_address in Camera LAN settings",
        )

    rule = FirewallRule(
        name="Isolate Camera LAN",
        direction="outbound",
        protocol="any",
        port_from=None,
        port_to=None,
        sources=subnet,
        action="deny",
        enabled=True,
        priority=10,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="firewall_rule",
            entity_id=rule.id,
            details={"auto": True, "reason": "camera-lan isolate"},
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return {"status": "ok", "rule": {"id": rule.id, "name": rule.name}}


@router.get("/uplink")
async def get_uplink(
    db: Session = Depends(get_db), current_user=Depends(get_current_superuser)
):
    row = _get_or_init(db, "network_uplink", _default_uplink())
    val = {**_default_uplink(), **_load_json(row)}
    return {"settings": val}


@router.put("/uplink")
async def update_uplink(
    payload: NetworkConfig,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    row = _get_or_init(db, "network_uplink", _default_uplink())
    current_val = {**_default_uplink(), **_load_json(row)}

    payload_dict = payload.model_dump(exclude_unset=True)

    for k, v in payload_dict.items():
        if k in current_val:
            current_val[k] = v

    row.json_value = json.dumps(current_val)
    db.commit()
    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="network",
            entity_id="uplink",
            details=payload_dict,
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return {"settings": current_val}
    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="network",
            entity_id="uplink",
            details=payload or {},
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return {"settings": current_val}
