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

from typing import TypeVar

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.database import get_db
from models import Camera, User

T = TypeVar("T")


class PermissionChecker:
    """
    Reusable dependency for checking ownership and permissions.
    Reduces code duplication in routers.
    """

    def __init__(self, model_class: type[T], ownership_field: str = "owner_id"):
        self.model_class = model_class
        self.ownership_field = ownership_field

    def check(self, resource_id: int, current_user: User, db: Session) -> T:
        resource = (
            db.query(self.model_class)
            .filter(self.model_class.id == resource_id)
            .first()
        )

        if not resource:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"{self.model_class.__name__} not found",
            )

        # Superuser bypass
        if current_user.is_superuser:
            return resource

        # Check ownership
        if getattr(resource, self.ownership_field) != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions"
            )

        return resource


# Specific dependency for Camera (matches "camera_id" path parameter)
def get_camera_or_403(
    camera_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> Camera:
    checker = PermissionChecker(Camera)
    return checker.check(camera_id, current_user, db)


def user_has_permission(user: User, permission_name: str) -> bool:
    """True if ``user`` holds ``permission_name``. Superusers and roles with the
    ``full_access`` wildcard hold every permission; otherwise the named
    permission must be on the user's role.
    """
    if getattr(user, "is_superuser", False):
        return True
    role = getattr(user, "role", None)
    if role is None:
        return False
    role_perms = getattr(role, "permissions", None) or []
    names = {p.name for p in role_perms}
    return "full_access" in names or permission_name in names


class RequirePermission:
    """FastAPI dependency factory gating a route on a named RBAC permission.

    Usage: ``user = Depends(RequirePermission("apps.install"))``.
    Returns the authenticated User, or 403 if the permission is absent
    (401 first if unauthenticated).
    """

    def __init__(self, permission_name: str):
        self.permission_name = permission_name
        # __name__ so FastAPI (and tests) introspect it like a plain function.
        self.__name__ = f"require_permission[{permission_name}]"

    def __call__(
        self,
        current_user: User = Depends(get_current_active_user),
    ) -> User:
        if not user_has_permission(current_user, self.permission_name):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Requires the '{self.permission_name}' permission"
                ),
            )
        return current_user
