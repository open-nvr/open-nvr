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

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from core.auth import get_current_active_user, get_current_superuser
from core.database import get_db
from core.logging_config import main_logger
from models import Permission, User
from schemas import UserCreate, UserList, UserResponse, UserUpdate
from services.audit_service import write_audit_log
from services.user_service import UserService
from utils.mfa_guard import require_mfa_code

router = APIRouter(prefix="/users", tags=["users"])


def _active_superusers(db: Session, *, excluding: int | None = None) -> int:
    q = db.query(User).filter(User.is_superuser == True,  # noqa: E712
                              User.is_active == True)  # noqa: E712
    if excluding is not None:
        q = q.filter(User.id != excluding)
    return q.count()


@router.post("/", response_model=UserResponse)
def create_user(
    user_create: UserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
    request: Request = None,
    mfa_code: str | None = Header(None, alias="X-MFA-Code"),
):
    """Create a new user (superuser only).

    ``is_superuser: true`` creates another superuser — the API's most
    privileged write (every permission, every camera), so it takes the
    caller's current TOTP code in ``X-MFA-Code`` like delete does.
    """
    if user_create.is_superuser:
        _require_mfa_code(current_user, mfa_code)
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
                "is_superuser": bool(user.is_superuser),
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


# NOTE: registered BEFORE the ``/{user_id}`` routes on purpose. Starlette
# matches in declaration order, and ``PUT /users/me`` declared after
# ``PUT /users/{user_id}`` was captured by the latter — its superuser
# dependency ran first, so every non-superuser's own profile edit was
# a 403 (and a superuser's became a 422 on ``user_id="me"``).
@router.put("/me", response_model=UserResponse)
def update_current_user(
    user_update: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    request: Request = None,
):
    """Update current user information (profile fields only)."""
    # Drop the fields only an administrator may set — role, active flag,
    # superuser — rather than nulling them: a nulled field is still "set"
    # to Pydantic, so the old ``x = None`` dance wrote NULL into the row
    # (``{"is_active": false}`` locked the caller out of their own
    # account, ``{"role_id": 1}`` hit the NOT NULL constraint).
    user_update = UserUpdate(**user_update.model_dump(
        exclude_unset=True, exclude={"role_id", "is_active", "is_superuser"}))

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
    mfa_code: str | None = Header(None, alias="X-MFA-Code"),
):
    """Update user information (superuser only).

    Changing ``is_superuser`` (promote or demote) requires the caller's
    current TOTP code in ``X-MFA-Code``; you cannot demote yourself, and
    the last active superuser cannot be demoted or deactivated.
    """
    # Deactivating yourself is the same lockout as self-deletion (issue #176).
    if user_id == current_user.id and user_update.is_active is False:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot deactivate your own account.",
        )
    if user_update.is_superuser is not None:
        target = db.query(User).filter(User.id == user_id).first()
        if target is not None and bool(target.is_superuser) != user_update.is_superuser:
            _require_mfa_code(current_user, mfa_code)
            if not user_update.is_superuser:
                if user_id == current_user.id:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="You cannot remove your own superuser status.",
                    )
                if _active_superusers(db, excluding=user_id) == 0:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Cannot demote the last active superuser.",
                    )
    if user_update.is_active is False:
        target = db.query(User).filter(User.id == user_id).first()
        if target is not None and target.is_superuser \
                and _active_superusers(db, excluding=user_id) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot deactivate the last active superuser.",
            )
    # Reactivation is MFA-gated via /users/{id}/activate; without this guard
    # the plain update would be a bypass around that check.
    if user_update.is_active is True:
        target = db.query(User).filter(User.id == user_id).first()
        if target and not target.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Reactivating a user requires an MFA code — use the Activate action.",
            )
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


def _require_mfa_code(current_user: User, mfa_code: str | None) -> None:
    """Verify the caller's current TOTP code for sensitive user actions."""
    require_mfa_code(current_user, mfa_code)


@router.delete("/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
    request: Request = None,
    mfa_code: str | None = Header(None, alias="X-MFA-Code"),
):
    """Delete a user (soft delete, superuser only).

    Requires the caller's current TOTP code in the X-MFA-Code header, and a
    user can never delete their own account — a soft-deleted user cannot log
    in, so self-deletion bricks single-admin deployments (issue #176).
    """
    if user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot delete your own account.",
        )

    _require_mfa_code(current_user, mfa_code)

    target = db.query(User).filter(User.id == user_id).first()
    if target is not None and target.is_superuser and target.is_active \
            and _active_superusers(db, excluding=user_id) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete the last active superuser.",
        )

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


@router.post("/{user_id}/activate", response_model=UserResponse)
def activate_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
    request: Request = None,
    mfa_code: str | None = Header(None, alias="X-MFA-Code"),
):
    """Reactivate a soft-deleted user (superuser only).

    Undoes a delete: the account can log in again. Requires the caller's
    current TOTP code in the X-MFA-Code header, same as deletion.
    """
    _require_mfa_code(current_user, mfa_code)

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    user.is_active = True
    db.commit()
    db.refresh(user)
    try:
        write_audit_log(
            db,
            action="user.activate",
            user_id=current_user.id,
            entity_type="user",
            entity_id=user.id,
            details={"username": user.username},
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception as e:
        main_logger.warning(f"Failed to write audit log (user.activate): {e}")
    return user
