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
Roles router for listing and retrieving roles.
Primarily used by admin UI to populate role selectors.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from core.auth import get_current_superuser
from core.database import get_db
from models import Role, User
from schemas import RoleCreate, RoleList, RoleResponse, RoleUpdate

router = APIRouter(prefix="/roles", tags=["roles"])


@router.get("/", response_model=RoleList)
async def list_roles(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
):
    """Return a paginated list of roles (superuser only)."""
    roles: list[Role] = db.query(Role).offset(skip).limit(limit).all()
    total: int = db.query(Role).count()
    # Pydantic will convert ORM objects via from_attributes
    return RoleList(roles=roles, total=total)


@router.get("/{role_id}", response_model=RoleResponse)
async def get_role(
    role_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
):
    """Return a single role by ID (superuser only)."""
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Role not found"
        )
    return role


@router.post("/", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
async def create_role(
    payload: RoleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
):
    """Create a new role (superuser only)."""
    existing = db.query(Role).filter(Role.name == payload.name).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Role name already exists"
        )
    role = Role(name=payload.name, description=payload.description)
    db.add(role)
    db.commit()
    db.refresh(role)
    return role


@router.put("/{role_id}", response_model=RoleResponse)
async def update_role(
    role_id: int,
    payload: RoleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
):
    """Update an existing role (superuser only)."""
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Role not found"
        )
    # If renaming, ensure unique
    if payload.name and payload.name != role.name:
        exists = db.query(Role).filter(Role.name == payload.name).first()
        if exists:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Role name already exists",
            )
        role.name = payload.name
    if payload.description is not None:
        role.description = payload.description
    db.commit()
    db.refresh(role)
    return role


@router.delete("/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(
    role_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
):
    """Delete a role (superuser only). Prevent deletion if assigned to any user."""
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Role not found"
        )
    # Prevent deleting if any users are assigned
    assigned_count = db.query(User).filter(User.role_id == role_id).count()
    if assigned_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete role assigned to users",
        )
    db.delete(role)
    db.commit()
    return None
