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
Security router exposing endpoints for firewall rules and generic security settings
(port settings, platform access, NAT). Superuser-only.
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from core.auth import get_current_superuser
from core.database import get_db
from core.logging_config import main_logger
from models import FirewallRule, SecuritySetting
from schemas import (
    FirewallRuleCreate,
    FirewallRuleList,
    FirewallRuleResponse,
    FirewallRuleUpdate,
    SecuritySettingPayload,
    SecuritySettingResponse,
)
from services.audit_service import write_audit_log

router = APIRouter(prefix="/security", tags=["security"])


# Firewall endpoints
@router.get("/firewall/rules", response_model=FirewallRuleList)
def list_firewall_rules(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    rules = db.query(FirewallRule).order_by(FirewallRule.priority.asc()).all()
    total = db.query(FirewallRule).count()
    return FirewallRuleList(rules=rules, total=total)


@router.post(
    "/firewall/rules",
    response_model=FirewallRuleResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_firewall_rule(
    payload: FirewallRuleCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    rule = FirewallRule(**payload.dict())
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
            details=payload.dict(),
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception as e:
        main_logger.warning(f"Failed to write audit log (firewall create): {e}")
    return rule


@router.put("/firewall/rules/{rule_id}", response_model=FirewallRuleResponse)
def update_firewall_rule(
    rule_id: int,
    payload: FirewallRuleUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    rule = db.query(FirewallRule).filter(FirewallRule.id == rule_id).first()
    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found"
        )
    for k, v in payload.dict(exclude_unset=True).items():
        setattr(rule, k, v)
    db.commit()
    db.refresh(rule)
    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="firewall_rule",
            entity_id=rule.id,
            details=payload.dict(exclude_unset=True),
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception as e:
        main_logger.warning(f"Failed to write audit log (firewall update): {e}")
    return rule


@router.delete("/firewall/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_firewall_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    rule = db.query(FirewallRule).filter(FirewallRule.id == rule_id).first()
    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found"
        )
    db.delete(rule)
    db.commit()
    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="firewall_rule",
            entity_id=rule_id,
            details={"deleted": True},
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception as e:
        main_logger.warning(f"Failed to write audit log (firewall delete): {e}")
    return None


# Generic security settings (keyed JSON)
def _get_setting(db: Session, key: str) -> SecuritySetting:
    row = db.query(SecuritySetting).filter(SecuritySetting.key == key).first()
    if not row:
        row = SecuritySetting(key=key, json_value=json.dumps({}))
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


@router.get("/settings/{key}", response_model=SecuritySettingResponse)
def get_security_setting(
    key: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    row = _get_setting(db, key)
    return SecuritySettingResponse(
        key=row.key, value=json.loads(row.json_value or "{}")
    )


@router.put("/settings/{key}", response_model=SecuritySettingResponse)
def set_security_setting(
    key: str,
    payload: SecuritySettingPayload,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    if payload.key and payload.key != key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Key mismatch"
        )
    row = _get_setting(db, key)
    row.json_value = json.dumps(payload.value or {})
    db.commit()
    db.refresh(row)
    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="security_setting",
            entity_id=key,
            details=payload.value or {},
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception as e:
        main_logger.warning(f"Failed to write audit log (settings update): {e}")
    return SecuritySettingResponse(
        key=row.key, value=json.loads(row.json_value or "{}")
    )
