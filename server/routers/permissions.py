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
Permissions router to manage permissions and role-permission assignments.
Superuser-only endpoints for CRUD and assignment.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from core.auth import get_current_superuser
from core.database import get_db
from models import Permission, Role, RolePermission
from schemas import (
    PermissionCreate,
    PermissionList,
    PermissionResponse,
    PermissionUpdate,
    RolePermissionsSet,
)

router = APIRouter(prefix="/permissions", tags=["permissions"])


@router.get("/", response_model=PermissionList)
async def list_permissions(
    skip: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    perms: list[Permission] = db.query(Permission).offset(skip).limit(limit).all()
    total: int = db.query(Permission).count()
    return PermissionList(permissions=perms, total=total)


@router.post(
    "/", response_model=PermissionResponse, status_code=status.HTTP_201_CREATED
)
async def create_permission(
    payload: PermissionCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    existing = db.query(Permission).filter(Permission.name == payload.name).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Permission name already exists",
        )
    perm = Permission(name=payload.name, description=payload.description)
    db.add(perm)
    db.commit()
    db.refresh(perm)
    return perm


@router.put("/{permission_id}", response_model=PermissionResponse)
async def update_permission(
    permission_id: int,
    payload: PermissionUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    perm = db.query(Permission).filter(Permission.id == permission_id).first()
    if not perm:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Permission not found"
        )
    if payload.name and payload.name != perm.name:
        exists = db.query(Permission).filter(Permission.name == payload.name).first()
        if exists:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Permission name already exists",
            )
        perm.name = payload.name
    if payload.description is not None:
        perm.description = payload.description
    db.commit()
    db.refresh(perm)
    return perm


@router.delete("/{permission_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_permission(
    permission_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    perm = db.query(Permission).filter(Permission.id == permission_id).first()
    if not perm:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Permission not found"
        )
    # Deleting a permission will cascade delete RolePermission rows due to model config
    db.delete(perm)
    db.commit()
    return None


@router.get("/roles/{role_id}", response_model=PermissionList)
async def list_role_permissions(
    role_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Role not found"
        )
    # role.permissions via relationship
    perms: list[Permission] = role.permissions
    return PermissionList(permissions=perms, total=len(perms))


@router.put("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def set_role_permissions(
    role_id: int,
    payload: RolePermissionsSet,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Role not found"
        )
    # Validate all permission IDs
    if payload.permission_ids:
        count = (
            db.query(Permission)
            .filter(Permission.id.in_(payload.permission_ids))
            .count()
        )
        if count != len(set(payload.permission_ids)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="One or more permission IDs are invalid",
            )
    # Clear existing
    db.query(RolePermission).filter(RolePermission.role_id == role_id).delete()
    # Insert new
    for pid in sorted(set(payload.permission_ids)):
        db.add(RolePermission(role_id=role_id, permission_id=pid))
    db.commit()
    return None
