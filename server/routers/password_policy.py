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
Password policy router to view and update global password policy.
Superuser-only for updates; readable by superuser (and optionally by others if needed).
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from core.auth import get_current_superuser
from core.database import get_db
from models import PasswordPolicy
from schemas import PasswordPolicyResponse, PasswordPolicyUpdate
from services.audit_service import write_audit_log

router = APIRouter(prefix="/password-policy", tags=["password-policy"])


def _get_singleton(db: Session) -> PasswordPolicy:
    policy = db.query(PasswordPolicy).first()
    if not policy:
        policy = PasswordPolicy()  # defaults
        db.add(policy)
        db.commit()
        db.refresh(policy)
    return policy


@router.get("/", response_model=PasswordPolicyResponse)
async def get_policy(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    return _get_singleton(db)


@router.put("/", response_model=PasswordPolicyResponse)
async def update_policy(
    payload: PasswordPolicyUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    policy = _get_singleton(db)
    data = payload.dict()
    for k, v in data.items():
        setattr(policy, k, v)
    db.commit()
    db.refresh(policy)
    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="password_policy",
            entity_id="password_policy",
            details=payload.dict(exclude_unset=True),
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return policy
