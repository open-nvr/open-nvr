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
Audit logs router.

Provides paginated, filterable access to audit logs for administrators.
"""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_
from sqlalchemy.orm import Session

from core.auth import get_current_superuser
from core.database import get_db
from models import AuditLog, User
from schemas import AuditLogList, AuditLogResponse

router = APIRouter(prefix="/audit-logs", tags=["audit-logs"])


@router.get("/", response_model=AuditLogList)
async def list_audit_logs(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    action: str | None = None,
    entity_type: str | None = None,
    user_id: int | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    filters = []
    if action:
        filters.append(AuditLog.action == action)
    if entity_type:
        filters.append(AuditLog.entity_type == entity_type)
    if user_id is not None:
        filters.append(AuditLog.user_id == user_id)
    if start is not None:
        filters.append(AuditLog.timestamp >= start)
    if end is not None:
        filters.append(AuditLog.timestamp <= end)

    q = db.query(AuditLog, User.username).outerjoin(User, AuditLog.user_id == User.id)
    if filters:
        q = q.filter(and_(*filters))

    total = q.count()
    rows = q.order_by(AuditLog.timestamp.desc()).offset(skip).limit(limit).all()

    # Convert details JSON string to dict if possible
    def _convert(row) -> AuditLogResponse:
        # SQLAlchemy may return a Row object; convert to tuple for safe unpacking
        try:
            al, username = (tuple(row)[0], tuple(row)[1])  # type: ignore[index]
        except Exception:
            # Fallback if it's already a simple tuple or a single model
            if isinstance(row, tuple):
                al = row[0]
                username = row[1] if len(row) > 1 else None
            else:
                al = row
                username = None
        details_obj = None
        if getattr(al, "details", None):
            try:
                details_obj = json.loads(al.details)
            except Exception:
                details_obj = al.details
        return AuditLogResponse(
            id=al.id,
            timestamp=al.timestamp,
            action=al.action,
            entity_type=al.entity_type,
            entity_id=al.entity_id,
            user_id=al.user_id,
            username=username,
            details=details_obj,
            ip=al.ip,
            user_agent=al.user_agent,
        )

    return AuditLogList(logs=[_convert(r) for r in rows], total=total)
