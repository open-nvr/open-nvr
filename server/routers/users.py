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
Users router for user management operations.
Handles CRUD operations for users with proper authentication and authorization.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from core.auth import get_current_active_user, get_current_superuser
from core.database import get_db
from core.logging_config import main_logger
from models import Permission, User
from schemas import UserCreate, UserList, UserResponse, UserUpdate
from services.audit_service import write_audit_log
from services.user_service import UserService

router = APIRouter(prefix="/users", tags=["users"])


@router.post("/", response_model=UserResponse)
def create_user(
    user_create: UserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
    request: Request = None,
):
    """Create a new user (superuser only)."""
    user = UserService.create_user(db=db, user_create=user_create)
    try:
        write_audit_log(
            db,
            action="user.create",
            user_id=current_user.id,
            entity_type="user",
            entity_id=user.id,
            details={
                "username": user.username,
                "email": user.email,
                "role_id": user.role_id,
            },
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception as e:
        main_logger.warning(f"Failed to write audit log (user.create): {e}")
    return user


@router.get("/", response_model=UserList)
def get_users(
    skip: int = 0,
    limit: int = 100,
    active_only: bool = True,
    q: str = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
):
    """Get list of users (superuser only). Optionally filter by username/email with q parameter."""
    users = UserService.get_users(
        db=db, skip=skip, limit=limit, active_only=active_only, q=q
    )
    total = db.query(User).count()
    return UserList(users=users, total=total)


@router.get("/me", response_model=UserResponse)
def get_current_user_info(current_user: User = Depends(get_current_active_user)):
    """Get current user information."""
    return current_user


@router.get("/me/permissions")
def get_current_user_permissions(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_active_user)
):
    """Get current user's permissions based on their role."""
    # Superuser has all permissions
    if current_user.is_superuser:
        all_perms = db.query(Permission).all()
        return {"permissions": [p.name for p in all_perms], "is_superuser": True}

    # Get permissions from user's role
    if current_user.role and current_user.role.permissions:
        perm_names = [p.name for p in current_user.role.permissions]
        # Check for full_access permission
        if "full_access" in perm_names:
            all_perms = db.query(Permission).all()
            return {"permissions": [p.name for p in all_perms], "is_superuser": False}
        return {"permissions": perm_names, "is_superuser": False}

    return {"permissions": [], "is_superuser": False}


@router.get("/{user_id}", response_model=UserResponse)
def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
):
    """Get user by ID (superuser only)."""
    user = UserService.get_user_by_id(db=db, user_id=user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    return user


@router.put("/{user_id}", response_model=UserResponse)
def update_user(
    user_id: int,
    user_update: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
    request: Request = None,
):
    """Update user information (superuser only)."""
    user = UserService.update_user(db=db, user_id=user_id, user_update=user_update)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    try:
        write_audit_log(
            db,
            action="user.update",
            user_id=current_user.id,
            entity_type="user",
            entity_id=user.id,
            details={
                "updated_fields": [
                    k for k in user_update.dict(exclude_unset=True).keys()
                ]
            },
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception as e:
        main_logger.warning(f"Failed to write audit log (user.update): {e}")
    return user


@router.delete("/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
    request: Request = None,
):
    """Delete a user (soft delete, superuser only)."""
    success = UserService.delete_user(db=db, user_id=user_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    try:
        write_audit_log(
            db,
            action="user.delete",
            user_id=current_user.id,
            entity_type="user",
            entity_id=user_id,
            details=None,
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception as e:
        main_logger.warning(f"Failed to write audit log (user.delete): {e}")
    return {"message": "User deleted successfully"}


@router.put("/me", response_model=UserResponse)
def update_current_user(
    user_update: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    request: Request = None,
):
    """Update current user information."""
    # Remove fields that regular users shouldn't be able to update
    if user_update.role_id is not None:
        user_update.role_id = None
    if user_update.is_active is not None:
        user_update.is_active = None
    if user_update.is_superuser is not None:
        user_update.is_superuser = None

    user = UserService.update_user(
        db=db, user_id=current_user.id, user_update=user_update
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    try:
        write_audit_log(
            db,
            action="user.update",
            user_id=current_user.id,
            entity_type="user",
            entity_id=current_user.id,
            details={
                "self_update": True,
                "updated_fields": [
                    k for k in user_update.dict(exclude_unset=True).keys()
                ],
            },
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception as e:
        main_logger.warning(f"Failed to write audit log (self user.update): {e}")
    return user
