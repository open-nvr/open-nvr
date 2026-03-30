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
General settings router.

Exposes GET/PUT for general sections and persists as keyed JSON via SecuritySetting.
Sections: system, network, alarm, rs232, live-view, exceptions, user, pos
"""

import json

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from core.auth import get_current_active_user, get_current_superuser
from core.database import get_db
from models import SecuritySetting
from schemas import (
    GeneralAlarmSettings,
    GeneralExceptionsSettings,
    GeneralLiveViewSettings,
    GeneralNetworkSettings,
    GeneralPosSettings,
    GeneralRs232Settings,
    GeneralSystemSettings,
    GeneralUserSettings,
    WindowDivisionSettings,
)
from services.audit_service import write_audit_log

router = APIRouter(prefix="/general", tags=["general-settings"])


def _get_or_init(db: Session, key: str, default_obj) -> SecuritySetting:
    row = db.query(SecuritySetting).filter(SecuritySetting.key == key).first()
    if not row:
        row = SecuritySetting(key=key, json_value=json.dumps(default_obj.model_dump()))
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


# Helper to load, merge with defaults, and validate


def _get_validated(db: Session, key: str, model_cls):
    row = _get_or_init(db, key, model_cls())
    try:
        val = json.loads(row.json_value or "{}")
    except Exception:
        val = {}
    merged = {**model_cls().model_dump(), **val}
    return model_cls(**merged)


@router.get("/system")
async def get_system(
    db: Session = Depends(get_db), current_user=Depends(get_current_superuser)
):
    return _get_validated(db, "general_system", GeneralSystemSettings).model_dump()


@router.put("/system")
async def update_system(
    payload: GeneralSystemSettings,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    row = _get_or_init(db, "general_system", GeneralSystemSettings())
    row.json_value = json.dumps(payload.model_dump())
    db.commit()
    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="general_system",
            entity_id="system",
            details=payload.model_dump(),
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return payload.model_dump()


@router.get("/network")
async def get_network(
    db: Session = Depends(get_db), current_user=Depends(get_current_superuser)
):
    return _get_validated(db, "general_network", GeneralNetworkSettings).model_dump()


@router.put("/network")
async def update_network(
    payload: GeneralNetworkSettings,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    row = _get_or_init(db, "general_network", GeneralNetworkSettings())
    row.json_value = json.dumps(payload.model_dump())
    db.commit()
    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="general_network",
            entity_id="network",
            details=payload.model_dump(),
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return payload.model_dump()


@router.get("/alarm")
async def get_alarm(
    db: Session = Depends(get_db), current_user=Depends(get_current_superuser)
):
    return _get_validated(db, "general_alarm", GeneralAlarmSettings).model_dump()


@router.put("/alarm")
async def update_alarm(
    payload: GeneralAlarmSettings,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    row = _get_or_init(db, "general_alarm", GeneralAlarmSettings())
    row.json_value = json.dumps(payload.model_dump())
    db.commit()
    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="general_alarm",
            entity_id="alarm",
            details=payload.model_dump(),
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return payload.model_dump()


@router.get("/rs232")
async def get_rs232(
    db: Session = Depends(get_db), current_user=Depends(get_current_superuser)
):
    return _get_validated(db, "general_rs232", GeneralRs232Settings).model_dump()


@router.put("/rs232")
async def update_rs232(
    payload: GeneralRs232Settings,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    row = _get_or_init(db, "general_rs232", GeneralRs232Settings())
    row.json_value = json.dumps(payload.model_dump())
    db.commit()
    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="general_rs232",
            entity_id="rs232",
            details=payload.model_dump(),
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return payload.model_dump()


@router.get("/live-view")
async def get_live_view(
    db: Session = Depends(get_db), current_user=Depends(get_current_superuser)
):
    return _get_validated(db, "general_live_view", GeneralLiveViewSettings).model_dump()


@router.put("/live-view")
async def update_live_view(
    payload: GeneralLiveViewSettings,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    row = _get_or_init(db, "general_live_view", GeneralLiveViewSettings())
    row.json_value = json.dumps(payload.model_dump())
    db.commit()
    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="general_live_view",
            entity_id="live-view",
            details=payload.model_dump(),
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return payload.model_dump()


@router.get("/exceptions")
async def get_exceptions(
    db: Session = Depends(get_db), current_user=Depends(get_current_superuser)
):
    return _get_validated(
        db, "general_exceptions", GeneralExceptionsSettings
    ).model_dump()


@router.put("/exceptions")
async def update_exceptions(
    payload: GeneralExceptionsSettings,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    row = _get_or_init(db, "general_exceptions", GeneralExceptionsSettings())
    row.json_value = json.dumps(payload.model_dump())
    db.commit()
    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="general_exceptions",
            entity_id="exceptions",
            details=payload.model_dump(),
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return payload.model_dump()


@router.get("/user")
async def get_user_settings(
    db: Session = Depends(get_db), current_user=Depends(get_current_superuser)
):
    return _get_validated(db, "general_user", GeneralUserSettings).model_dump()


@router.put("/user")
async def update_user_settings(
    payload: GeneralUserSettings,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    row = _get_or_init(db, "general_user", GeneralUserSettings())
    row.json_value = json.dumps(payload.model_dump())
    db.commit()
    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="general_user",
            entity_id="user",
            details=payload.model_dump(),
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return payload.model_dump()


@router.get("/pos")
async def get_pos(
    db: Session = Depends(get_db), current_user=Depends(get_current_superuser)
):
    return _get_validated(db, "general_pos", GeneralPosSettings).model_dump()


@router.put("/pos")
async def update_pos(
    payload: GeneralPosSettings,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    row = _get_or_init(db, "general_pos", GeneralPosSettings())
    row.json_value = json.dumps(payload.model_dump())
    db.commit()
    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="general_pos",
            entity_id="pos",
            details=payload.model_dump(),
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return payload.model_dump()


@router.get("/window-settings")
async def get_window_settings(
    db: Session = Depends(get_db), current_user=Depends(get_current_active_user)
):
    """Get window division settings. Available to all authenticated users."""
    return _get_validated(
        db, "window_division_settings", WindowDivisionSettings
    ).model_dump()


@router.put("/window-settings")
async def update_window_settings(
    payload: WindowDivisionSettings,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
    request: Request = None,
):
    """Update window division settings. Available to all authenticated users."""
    row = _get_or_init(db, "window_division_settings", WindowDivisionSettings())
    row.json_value = json.dumps(payload.model_dump())
    db.commit()
    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="window_settings",
            entity_id="window-settings",
            details=payload.model_dump(),
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return payload.model_dump()
