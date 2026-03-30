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
Cloud settings router.

Persists cloud streaming and recording server configuration under SecuritySetting
key 'cloud', with validation via pydantic schemas.

Also exposes a simple test endpoint to validate connectivity (placeholder).
"""

import json

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from core.auth import get_current_superuser
from core.database import get_db
from models import SecuritySetting
from schemas import CloudSettings
from services.audit_service import write_audit_log

router = APIRouter(prefix="/cloud", tags=["cloud"])


def _get_row(db: Session) -> SecuritySetting:
    row = db.query(SecuritySetting).filter(SecuritySetting.key == "cloud").first()
    if not row:
        row = SecuritySetting(
            key="cloud", json_value=json.dumps(CloudSettings().model_dump())
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


@router.get("/settings")
async def get_cloud_settings(
    db: Session = Depends(get_db), current_user=Depends(get_current_superuser)
):
    row = _get_row(db)
    try:
        val = json.loads(row.json_value or "{}")
    except Exception:
        val = {}
    merged = {**CloudSettings().model_dump(), **val}
    obj = CloudSettings(**merged)
    return obj.model_dump()


@router.put("/settings")
async def put_cloud_settings(
    payload: CloudSettings,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    row = _get_row(db)
    obj = CloudSettings(**payload.model_dump())
    row.json_value = json.dumps(obj.model_dump())
    db.commit()
    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="cloud",
            entity_id="cloud",
            details=obj.model_dump(),
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return obj.model_dump()


@router.post("/test")
async def test_cloud_connectivity(
    db: Session = Depends(get_db), current_user=Depends(get_current_superuser)
):
    """Lightweight connectivity test to the configured endpoints (optional).
    For now, just returns stored URLs; future versions may attempt real network calls.
    """
    row = _get_row(db)
    try:
        val = json.loads(row.json_value or "{}")
    except Exception:
        val = {}
    obj = CloudSettings(**({**CloudSettings().model_dump(), **val}))
    out = {
        "streaming_url": obj.streaming.server_url,
        "recording_url": obj.recording.server_url,
        "status": "ok",
    }
    return out
