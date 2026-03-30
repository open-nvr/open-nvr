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
Camera service for business logic operations.
Handles camera creation, updates, and management.
"""

from fastapi import HTTPException, status
from sqlalchemy import and_
from sqlalchemy.orm import Session

from core.logging_config import camera_logger
from models import Camera, CameraPermission, User
from schemas import CameraCreate, CameraUpdate
from services.mediamtx_admin_service import MediaMtxAdminService


class CameraService:
    """Service class for camera-related operations."""

    @staticmethod
    async def create_camera(
        db: Session, camera_create: CameraCreate, owner_id: int
    ) -> Camera:
        """Create a new camera."""
        # Use nested transaction to ensure atomicity of DB creation
        # If auto-provisioning fails later, we might still want the camera record,
        # OR we might want to rollback. Here we assume we want the record to exist
        # even if MediaMTX provisioning fails, so we commit DB first.
        # But if DB insertion fails, we roll back.

        try:
            with db.begin_nested():
                camera_logger.log_action(
                    "camera.service_create_start",
                    message=f"Camera service: Creating camera '{camera_create.name}' for user {owner_id}",
                    user_id=owner_id,
                    extra_data={
                        "camera_name": camera_create.name,
                        "ip_address": camera_create.ip_address,
                        "owner_id": owner_id,
                    },
                )

                # Note: Removed camera name uniqueness check to allow duplicate names
                # Note: Removed IP address uniqueness check to allow multiple cameras per IP

                # Create camera
                db_camera = Camera(
                    name=camera_create.name,
                    description=camera_create.description,
                    ip_address=camera_create.ip_address,
                    port=camera_create.port,
                    username=camera_create.username,
                    password=camera_create.password,
                    rtsp_url=camera_create.rtsp_url,
                    location=camera_create.location,
                    vlan=camera_create.vlan,
                    status=camera_create.status or "unknown",
                    owner_id=owner_id,
                    # ONVIF device metadata
                    manufacturer=camera_create.manufacturer,
                    model=camera_create.model,
                    firmware_version=camera_create.firmware_version,
                    serial_number=camera_create.serial_number,
                    hardware_id=camera_create.hardware_id,
                )

                db.add(db_camera)
                db.flush()  # Flush to get ID, commit handled by begin_nested exit

                camera_logger.log_action(
                    "camera.service_create_success",
                    message=f"Camera created in service: {db_camera.name} (ID: {db_camera.id})",
                    user_id=owner_id,
                    camera_id=db_camera.id,
                    extra_data={
                        "camera_id": db_camera.id,
                        "camera_name": db_camera.name,
                        "ip_address": db_camera.ip_address,
                        "rtsp_url": db_camera.rtsp_url,
                    },
                )

            # Commit the transaction to persist the camera
            db.commit()
            db.refresh(db_camera)

            # Auto-provision if RTSP URL is present
            if db_camera.rtsp_url:
                try:
                    # Default settings for auto-provisioning
                    # Recording disabled by default, TCP transport, 5 min segments
                    provision_result = await MediaMtxAdminService.push_rtsp_stream(
                        camera_id=db_camera.id,
                        camera_ip=db_camera.ip_address,
                        rtsp_url=db_camera.rtsp_url,
                        enable_recording=False,
                        rtsp_transport="tcp",
                        recording_segment_seconds=300,
                    )

                    if provision_result.get("status") == "ok":
                        db_camera.status = "provisioned"

                        # Create CameraConfig to persist settings
                        from sqlalchemy.sql import func

                        from models import CameraConfig

                        # Check if config exists (shouldn't for new camera, but good practice)
                        existing_config = (
                            db.query(CameraConfig)
                            .filter(CameraConfig.camera_id == db_camera.id)
                            .first()
                        )
                        if not existing_config:
                            new_config = CameraConfig(
                                camera_id=db_camera.id,
                                stream_protocol="rtsp",
                                source_url=db_camera.rtsp_url,
                                recording_enabled=False,
                                rtsp_transport="tcp",
                                recording_segment_seconds=300,
                                last_provisioned_at=func.now(),
                            )
                            db.add(new_config)

                        db.commit()
                        db.refresh(db_camera)

                        camera_logger.log_action(
                            "camera.auto_provision_success",
                            message=f"Camera {db_camera.id} auto-provisioned successfully",
                            user_id=owner_id,
                            camera_id=db_camera.id,
                            extra_data=provision_result,
                        )
                    else:
                        camera_logger.log_action(
                            "camera.auto_provision_failed",
                            message=f"Auto-provisioning failed for camera {db_camera.id}",
                            user_id=owner_id,
                            camera_id=db_camera.id,
                            extra_data=provision_result,
                        )
                except Exception as e:
                    camera_logger.error(
                        f"Exception during auto-provisioning for camera {db_camera.id}",
                        extra={
                            "user_id": owner_id,
                            "camera_id": db_camera.id,
                            "error_type": type(e).__name__,
                            "action": "camera.auto_provision_exception",
                        },
                        exc_info=True,
                    )
            else:
                # Camera created successfully - no auto-provisioning (no RTSP URL)
                camera_logger.log_action(
                    "camera.created_no_provision",
                    message=f"Camera {db_camera.id} created successfully - no RTSP URL provided",
                    user_id=owner_id,
                    camera_id=db_camera.id,
                    extra_data={"rtsp_url": None},
                )

            return db_camera

        except Exception as e:
            camera_logger.error(
                f"Database error creating camera: {camera_create.name}",
                extra={
                    "user_id": owner_id,
                    "camera_data": camera_create.model_dump(exclude={"password"}),
                    "error_type": type(e).__name__,
                    "action": "camera.service_create_db_error",
                },
                exc_info=True,
            )
            raise

    @staticmethod
    def get_camera_by_id(db: Session, camera_id: int, user_id: int) -> Camera | None:
        """Get camera by ID (only if user owns it or is superuser)."""
        camera = db.query(Camera).filter(Camera.id == camera_id).first()
        if not camera:
            return None

        # Check ownership (superusers can access all cameras)
        user = db.query(User).filter(User.id == user_id).first()
        if not user.is_superuser and camera.owner_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not enough permissions to access this camera",
            )

        return camera

    @staticmethod
    def get_cameras_by_owner(
        db: Session,
        owner_id: int,
        skip: int = 0,
        limit: int = 100,
        active_only: bool = True,
    ) -> list[Camera]:
        """Get list of cameras owned by a specific user."""
        query = db.query(Camera).filter(Camera.owner_id == owner_id)
        if active_only:
            query = query.filter(Camera.is_active == True)

        return query.offset(skip).limit(limit).all()

    @staticmethod
    def get_all_cameras(
        db: Session, skip: int = 0, limit: int = 100, active_only: bool = True
    ) -> list[Camera]:
        """Get all cameras (for superusers)."""
        query = db.query(Camera)
        if active_only:
            query = query.filter(Camera.is_active == True)

        return query.offset(skip).limit(limit).all()

    @staticmethod
    def get_cameras_permitted(
        db: Session,
        user_id: int,
        skip: int = 0,
        limit: int = 100,
        active_only: bool = True,
    ) -> list[Camera]:
        """Get cameras the user has explicit permission to view/manage (non-owner)."""
        subq = (
            db.query(CameraPermission.camera_id)
            .filter(
                and_(
                    CameraPermission.user_id == user_id,
                    CameraPermission.can_view == True,
                )
            )
            .subquery()
        )
        query = db.query(Camera).filter(Camera.id.in_(subq))
        if active_only:
            query = query.filter(Camera.is_active == True)
        return query.offset(skip).limit(limit).all()

    @staticmethod
    def update_camera(
        db: Session, camera_id: int, camera_update: CameraUpdate, user_id: int
    ) -> Camera | None:
        """Update camera information."""
        db_camera = db.query(Camera).filter(Camera.id == camera_id).first()
        if not db_camera:
            return None

        # Check ownership
        user = db.query(User).filter(User.id == user_id).first()
        if not user.is_superuser and db_camera.owner_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not enough permissions to update this camera",
            )

        # Note: Removed camera name uniqueness check to allow duplicate names
        # Note: Removed IP address uniqueness check to allow multiple cameras per IP

        # Update fields
        update_data = camera_update.dict(exclude_unset=True)
        for field, value in update_data.items():
            setattr(db_camera, field, value)

        db.commit()
        db.refresh(db_camera)
        return db_camera

    @staticmethod
    def delete_camera(db: Session, camera_id: int, user_id: int) -> bool:
        """Delete a camera (soft delete by setting is_active to False)."""
        db_camera = db.query(Camera).filter(Camera.id == camera_id).first()
        if not db_camera:
            return False

        # Check ownership
        user = db.query(User).filter(User.id == user_id).first()
        if not user.is_superuser and db_camera.owner_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not enough permissions to delete this camera",
            )

        db_camera.is_active = False
        db.commit()
        return True

    @staticmethod
    def test_camera_connection(db: Session, camera_id: int, user_id: int) -> dict:
        """Test camera connection (placeholder for actual connection testing)."""
        camera = db.query(Camera).filter(Camera.id == camera_id).first()
        if not camera:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found"
            )

        # Check ownership
        user = db.query(User).filter(User.id == user_id).first()
        if not user.is_superuser and camera.owner_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not enough permissions to test this camera",
            )

        return {
            "camera_id": camera_id,
            "status": "connection_test_placeholder",
            "message": "Connection testing not implemented yet",
        }

    # Permission management
    @staticmethod
    def assign_permission(
        db: Session,
        camera_id: int,
        target_user_id: int,
        can_view: bool = True,
        can_manage: bool = False,
        requester_id: int = None,
    ) -> CameraPermission:
        """Assign or update camera permission to a user. Only owner or superuser can assign."""
        camera = db.query(Camera).filter(Camera.id == camera_id).first()
        if not camera:
            raise HTTPException(status_code=404, detail="Camera not found")
        requester = db.query(User).filter(User.id == requester_id).first()
        if not requester or (
            not requester.is_superuser and camera.owner_id != requester_id
        ):
            raise HTTPException(
                status_code=403, detail="Not enough permissions to assign"
            )
        perm = (
            db.query(CameraPermission)
            .filter(
                and_(
                    CameraPermission.camera_id == camera_id,
                    CameraPermission.user_id == target_user_id,
                )
            )
            .first()
        )
        if perm:
            perm.can_view = can_view
            perm.can_manage = can_manage
        else:
            perm = CameraPermission(
                user_id=target_user_id,
                camera_id=camera_id,
                can_view=can_view,
                can_manage=can_manage,
            )
            db.add(perm)
        db.commit()
        db.refresh(perm)
        return perm

    @staticmethod
    def revoke_permission(
        db: Session, camera_id: int, target_user_id: int, requester_id: int
    ) -> bool:
        """Revoke camera permission from a user. Only owner or superuser can revoke."""
        camera = db.query(Camera).filter(Camera.id == camera_id).first()
        if not camera:
            raise HTTPException(status_code=404, detail="Camera not found")
        requester = db.query(User).filter(User.id == requester_id).first()
        if not requester or (
            not requester.is_superuser and camera.owner_id != requester_id
        ):
            raise HTTPException(
                status_code=403, detail="Not enough permissions to revoke"
            )
        perm = (
            db.query(CameraPermission)
            .filter(
                and_(
                    CameraPermission.camera_id == camera_id,
                    CameraPermission.user_id == target_user_id,
                )
            )
            .first()
        )
        if not perm:
            return False
        db.delete(perm)
        db.commit()
        return True

    @staticmethod
    def user_has_permission(
        db: Session, camera_id: int, user_id: int, require_manage: bool = False
    ) -> bool:
        """Check if user has permission to view or manage a camera."""
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return False
        if user.is_superuser:
            return True
        camera = db.query(Camera).filter(Camera.id == camera_id).first()
        if not camera:
            return False
        if camera.owner_id == user_id:
            return True
        perm = (
            db.query(CameraPermission)
            .filter(
                and_(
                    CameraPermission.camera_id == camera_id,
                    CameraPermission.user_id == user_id,
                )
            )
            .first()
        )
        if not perm:
            return False
        return perm.can_manage if require_manage else perm.can_view
