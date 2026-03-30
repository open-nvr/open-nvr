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
