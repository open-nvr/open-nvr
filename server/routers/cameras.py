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
Cameras router for camera management operations.
Handles CRUD operations for cameras with proper authentication and ownership checks.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import and_
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.config import settings
from core.database import get_db
from core.logging_config import camera_logger
from core.permissions import get_camera_or_403
from models import Camera, CameraPermission, User
from schemas import (
    CameraCreate,
    CameraList,
    CameraPermissionAssign,
    CameraPermissionResponse,
    CameraResponse,
    CameraUpdate,
)
from services.audit_service import write_audit_log
from services.camera_service import CameraService
from services.stream_service import _build_stream_name, build_secure_whep_url_for_user

router = APIRouter(prefix="/cameras", tags=["cameras"])


def _log_camera_creation_start(
    user_id: int, camera_create: CameraCreate, request: Request
):
    camera_logger.log_action(
        "camera.create_start",
        user_id=user_id,
        message=f"Attempting to create camera: {camera_create.name}",
        extra_data=camera_create.model_dump(exclude={"password"}),
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
    )


def _log_camera_creation_success(user_id: int, cam: Camera, request: Request):
    camera_logger.log_action(
        "camera.create_success",
        user_id=user_id,
        camera_id=cam.id,
        message=f"Camera created successfully: {cam.name} (ID: {cam.id})",
        extra_data={
            "camera_id": cam.id,
            "name": cam.name,
            "ip": cam.ip_address,
            "owner_id": cam.owner_id,
        },
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
    )


def _record_audit_log(db: Session, user_id: int, cam: Camera, request: Request):
    try:
        write_audit_log(
            db,
            action="camera.create",
            user_id=user_id,
            entity_type="camera",
            entity_id=cam.id,
            details={"name": cam.name, "ip": cam.ip_address},
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception as e:
        camera_logger.error(f"Failed to write audit log: {e}", exc_info=True)


def _check_duplicate_ips(db: Session, user_id: int, cam: Camera):
    cameras_same_ip = (
        db.query(Camera)
        .filter(
            and_(
                Camera.ip_address == cam.ip_address,
                Camera.owner_id == user_id,
                Camera.is_active == True,
                Camera.id != cam.id,
            )
        )
        .count()
    )

    if cameras_same_ip > 0:
        camera_logger.log_action(
            "camera.multiple_ip_notice",
            user_id=user_id,
            camera_id=cam.id,
            message=f"Multiple cameras detected on IP {cam.ip_address} ({cameras_same_ip + 1} total)",
            extra_data={
                "ip_address": cam.ip_address,
                "total_cameras": cameras_same_ip + 1,
                "rtsp_url": cam.rtsp_url,
            },
        )


@router.post("/", response_model=CameraResponse)
async def create_camera(
    camera_create: CameraCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    request: Request = None,
):
    """Create a new camera."""
    _log_camera_creation_start(current_user.id, camera_create, request)

    try:
        cam = await CameraService.create_camera(
            db=db, camera_create=camera_create, owner_id=current_user.id
        )

        _log_camera_creation_success(current_user.id, cam, request)
        _record_audit_log(db, current_user.id, cam, request)
        _check_duplicate_ips(db, current_user.id, cam)

        # Helper to construct response with computed fields
        # Ideally this should be in a schema converter or helper
        response_data = {
            "id": cam.id,
            "name": cam.name,
            "description": cam.description,
            "ip_address": cam.ip_address,
            "port": cam.port,
            "username": cam.username,
            "password": cam.password,
            "rtsp_url": cam.rtsp_url,
            "is_active": cam.is_active,
            "location": cam.location,
            "vlan": cam.vlan,
            "status": cam.status,
            "created_at": cam.created_at,
            "updated_at": cam.updated_at,
            "owner_id": cam.owner_id,
            "mediamtx_provisioned": cam.status == "provisioned",
            "recording_enabled": False,
        }

        return CameraResponse(**response_data)

    except Exception as e:
        camera_logger.error(
            f"Failed to create camera: {camera_create.name}",
            extra={
                "user_id": current_user.id,
                "camera_data": camera_create.model_dump(exclude={"password"}),
                "ip_address": request.client.host
                if request and request.client
                else None,
                "user_agent": request.headers.get("user-agent") if request else None,
                "error_type": type(e).__name__,
                "action": "camera.create_failed",
            },
            exc_info=True,
        )
        raise


@router.get("/", response_model=CameraList)
def get_cameras(
    skip: int = 0,
    limit: int = 100,
    active_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    request: Request = None,
):
    """Get list of cameras (user's own cameras + permitted cameras; superusers see all)."""
    camera_logger.log_action(
        "camera.list_request",
        user_id=current_user.id,
        message=f"User requesting camera list (skip={skip}, limit={limit}, active_only={active_only})",
        extra_data={
            "skip": skip,
            "limit": limit,
            "active_only": active_only,
            "is_superuser": current_user.is_superuser,
        },
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
    )

    if current_user.is_superuser:
        cameras = CameraService.get_all_cameras(
            db=db, skip=skip, limit=limit, active_only=active_only
        )
        # Count only active (non-deleted) cameras when active_only is True
        if active_only:
            total = db.query(Camera).filter(Camera.is_active == True).count()
        else:
            total = db.query(Camera).count()
        camera_logger.log_action(
            "camera.list_success_admin",
            user_id=current_user.id,
            message=f"Superuser retrieved {len(cameras)} cameras (total: {total})",
            extra_data={"camera_count": len(cameras), "total": total},
            ip_address=request.client.host if request and request.client else None,
        )
    else:
        own = CameraService.get_cameras_by_owner(
            db=db,
            owner_id=current_user.id,
            skip=skip,
            limit=limit,
            active_only=active_only,
        )
        permitted = CameraService.get_cameras_permitted(
            db=db,
            user_id=current_user.id,
            skip=skip,
            limit=limit,
            active_only=active_only,
        )
        # Merge unique by id
        seen = set()
        cameras = []
        for c in own + permitted:
            if c.id not in seen:
                seen.add(c.id)
                cameras.append(c)
        total = len(cameras)
        camera_logger.log_action(
            "camera.list_success_user",
            user_id=current_user.id,
            message=f"User retrieved {len(cameras)} cameras ({len(own)} owned, {len(permitted)} permitted)",
            extra_data={
                "camera_count": len(cameras),
                "own_count": len(own),
                "permitted_count": len(permitted),
            },
            ip_address=request.client.host if request and request.client else None,
        )

    # Populate extra fields for response
    results = []
    for c in cameras:
        # Create response object from ORM model
        c_resp = CameraResponse.model_validate(c)

        # Set computed fields
        c_resp.mediamtx_provisioned = c.status == "provisioned"

        # Get recording status from config if available
        if c.config:
            c_resp.recording_enabled = c.config.recording_enabled
        else:
            c_resp.recording_enabled = False

        results.append(c_resp)

    return CameraList(cameras=results, total=total)


@router.get("/by-ip/{ip_address}", response_model=list[CameraResponse])
def get_cameras_by_ip(
    ip_address: str,
    active_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    request: Request = None,
):
    """Get all cameras for a specific IP address (user's own cameras + permitted cameras; superusers see all)."""
    camera_logger.log_action(
        "camera.list_by_ip_request",
        user_id=current_user.id,
        message=f"User requesting cameras for IP {ip_address} (active_only={active_only})",
        extra_data={
            "ip_address": ip_address,
            "active_only": active_only,
            "is_superuser": current_user.is_superuser,
        },
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
    )

    if current_user.is_superuser:
        query = db.query(Camera).filter(Camera.ip_address == ip_address)
        if active_only:
            query = query.filter(Camera.is_active == True)
        cameras = query.all()
        camera_logger.log_action(
            "camera.list_by_ip_success_admin",
            user_id=current_user.id,
            message=f"Superuser retrieved {len(cameras)} cameras for IP {ip_address}",
            extra_data={"camera_count": len(cameras), "ip_address": ip_address},
            ip_address=request.client.host if request and request.client else None,
        )
    else:
        # Get own cameras
        own_query = db.query(Camera).filter(
            and_(Camera.ip_address == ip_address, Camera.owner_id == current_user.id)
        )
        if active_only:
            own_query = own_query.filter(Camera.is_active == True)
        own_cameras = own_query.all()

        # Get permitted cameras
        permitted_subq = (
            db.query(CameraPermission.camera_id)
            .filter(
                and_(
                    CameraPermission.user_id == current_user.id,
                    CameraPermission.can_view == True,
                )
            )
            .subquery()
        )
        permitted_query = db.query(Camera).filter(
            and_(Camera.ip_address == ip_address, Camera.id.in_(permitted_subq))
        )
        if active_only:
            permitted_query = permitted_query.filter(Camera.is_active == True)
        permitted_cameras = permitted_query.all()

        # Merge unique cameras by id
        seen = set()
        cameras = []
        for c in own_cameras + permitted_cameras:
            if c.id not in seen:
                seen.add(c.id)
                cameras.append(c)

        camera_logger.log_action(
            "camera.list_by_ip_success_user",
            user_id=current_user.id,
            message=f"User retrieved {len(cameras)} cameras for IP {ip_address} ({len(own_cameras)} owned, {len(permitted_cameras)} permitted)",
            extra_data={
                "camera_count": len(cameras),
                "own_count": len(own_cameras),
                "permitted_count": len(permitted_cameras),
                "ip_address": ip_address,
            },
            ip_address=request.client.host if request and request.client else None,
        )

    return cameras


@router.get("/{camera_id}", response_model=CameraResponse)
def get_camera(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    request: Request = None,
):
    """Get camera by ID (owner, superuser, or permitted users)."""
    camera_logger.log_action(
        "camera.get_request",
        user_id=current_user.id,
        camera_id=camera_id,
        message=f"User requesting camera details: ID {camera_id}",
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
    )

    try:
        # Reuse existing logic; raises 403 if not allowed
        camera = CameraService.get_camera_by_id(
            db=db, camera_id=camera_id, user_id=current_user.id
        )

        camera_logger.log_action(
            "camera.get_success",
            user_id=current_user.id,
            camera_id=camera_id,
            message=f"Camera details retrieved: {camera.name}",
            extra_data={"camera_name": camera.name, "camera_ip": camera.ip_address},
            ip_address=request.client.host if request and request.client else None,
        )

        return camera
    except HTTPException as e:
        # If not owner, check explicit permission
        if e.status_code == status.HTTP_403_FORBIDDEN:
            allowed = CameraService.user_has_permission(
                db, camera_id, current_user.id, require_manage=False
            )
            if not allowed:
                camera_logger.log_action(
                    "camera.get_forbidden",
                    user_id=current_user.id,
                    camera_id=camera_id,
                    message=f"Access denied to camera {camera_id}: insufficient permissions",
                    ip_address=request.client.host
                    if request and request.client
                    else None,
                )
                raise
            camera = db.query(Camera).filter(Camera.id == camera_id).first()
            if not camera:
                camera_logger.log_action(
                    "camera.get_not_found",
                    user_id=current_user.id,
                    camera_id=camera_id,
                    message=f"Camera not found: ID {camera_id}",
                    ip_address=request.client.host
                    if request and request.client
                    else None,
                )
                raise HTTPException(status_code=404, detail="Camera not found")

            camera_logger.log_action(
                "camera.get_success_permitted",
                user_id=current_user.id,
                camera_id=camera_id,
                message=f"Camera details retrieved via permission: {camera.name}",
                extra_data={"camera_name": camera.name, "camera_ip": camera.ip_address},
                ip_address=request.client.host if request and request.client else None,
            )

            return camera
        else:
            camera_logger.error(
                f"Error retrieving camera {camera_id}: {e.detail}",
                extra={
                    "user_id": current_user.id,
                    "camera_id": camera_id,
                    "status_code": e.status_code,
                    "action": "camera.get_error",
                },
            )
        raise


@router.put("/{camera_id}", response_model=CameraResponse)
def update_camera(
    camera_id: int,
    camera_update: CameraUpdate,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    request: Request = None,
):
    """Update camera information (owner or superuser)."""
    camera_logger.log_action(
        "camera.update_start",
        user_id=current_user.id,
        camera_id=camera_id,
        message=f"Attempting to update camera {camera_id}",
        extra_data={
            "updated_fields": list(camera_update.model_dump(exclude_unset=True).keys()),
            "update_data": camera_update.model_dump(exclude_unset=True),
        },
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
    )

    try:
        # Note: We pass the pre-validated camera object to avoid duplicate DB query
        # But CameraService needs update to support passing model instance
        # For now, we rely on the service to re-check, or we update manually here.
        # Ideally CameraService should have update_camera_instance method.
        # Since we just want to remove duplication in router, let's keep service call
        # but acknowledging the redundancy for now, OR modify service.
        # Actually, let's modify the service call to skip the check if possible?
        # No, let's just use the service as is, the router check is now declarative.
        # Wait, if get_camera_or_403 raises 403, we won't reach here.
        # So CameraService will succeed since permission is valid.

        # Refactoring: Since we have the camera, we can use it.
        # But we need to update it.
        for field, value in camera_update.model_dump(exclude_unset=True).items():
            setattr(camera, field, value)

        db.commit()
        db.refresh(camera)

        camera_logger.log_action(
            "camera.update_success",
            user_id=current_user.id,
            camera_id=camera_id,
            message=f"Camera updated successfully: {camera.name}",
            extra_data={
                "camera_name": camera.name,
                "updated_fields": list(
                    camera_update.model_dump(exclude_unset=True).keys()
                ),
            },
            ip_address=request.client.host if request and request.client else None,
        )

        # Legacy audit log
        try:
            write_audit_log(
                db,
                action="camera.update",
                user_id=current_user.id,
                entity_type="camera",
                entity_id=camera.id,
                details={
                    "updated_fields": [
                        k for k in camera_update.model_dump(exclude_unset=True).keys()
                    ]
                },
                ip=request.client.host if request and request.client else None,
                user_agent=request.headers.get("user-agent") if request else None,
            )
        except Exception as e:
            camera_logger.error(f"Failed to write audit log: {e}", exc_info=True)

        return camera

    except HTTPException:
        raise
    except Exception as e:
        camera_logger.error(
            f"Failed to update camera {camera_id}",
            extra={
                "user_id": current_user.id,
                "camera_id": camera_id,
                "update_data": camera_update.model_dump(exclude_unset=True),
                "error_type": type(e).__name__,
                "action": "camera.update_failed",
            },
            exc_info=True,
        )
        raise


@router.delete("/{camera_id}")
def delete_camera(
    camera_id: int,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    request: Request = None,
):
    """Delete a camera (soft delete)."""
    camera_logger.log_action(
        "camera.delete_start",
        user_id=current_user.id,
        camera_id=camera_id,
        message=f"Attempting to delete camera {camera_id}",
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
    )

    camera_name = camera.name

    try:
        # We already have the camera via dependency and ownership is confirmed.
        # Direct soft delete.
        camera.is_active = False
        db.commit()

        camera_logger.log_action(
            "camera.delete_success",
            user_id=current_user.id,
            camera_id=camera_id,
            message=f"Camera deleted successfully: {camera_name}",
            extra_data={"camera_name": camera_name},
            ip_address=request.client.host if request and request.client else None,
        )

        # Legacy audit log
        try:
            write_audit_log(
                db,
                action="camera.delete",
                user_id=current_user.id,
                entity_type="camera",
                entity_id=camera_id,
                details={"camera_name": camera_name},
                ip=request.client.host if request and request.client else None,
                user_agent=request.headers.get("user-agent") if request else None,
            )
        except Exception as e:
            camera_logger.error(f"Failed to write audit log: {e}", exc_info=True)

        return {"message": "Camera deleted successfully"}

    except HTTPException:
        raise
    except Exception as e:
        camera_logger.error(
            f"Failed to delete camera {camera_id}",
            extra={
                "user_id": current_user.id,
                "camera_id": camera_id,
                "camera_name": camera_name,
                "error_type": type(e).__name__,
                "action": "camera.delete_failed",
            },
            exc_info=True,
        )
        raise


@router.post("/{camera_id}/permissions", response_model=CameraPermissionResponse)
def assign_camera_permission(
    camera_id: int,
    payload: CameraPermissionAssign,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    request: Request = None,
):
    """Assign or update a user's permission for a camera (owner or superuser)."""
    perm = CameraService.assign_permission(
        db=db,
        camera_id=camera.id,
        target_user_id=payload.user_id,
        can_view=payload.can_view,
        can_manage=payload.can_manage,
        requester_id=current_user.id,
    )
    try:
        write_audit_log(
            db,
            action="camera.permission.assign",
            user_id=current_user.id,
            entity_type="camera",
            entity_id=camera_id,
            details={
                "target_user_id": payload.user_id,
                "can_view": payload.can_view,
                "can_manage": payload.can_manage,
            },
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return perm


@router.delete("/{camera_id}/permissions/{user_id}")
def revoke_camera_permission(
    camera_id: int,
    user_id: int,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    request: Request = None,
):
    """Revoke a user's permission for a camera (owner or superuser)."""
    success = CameraService.revoke_permission(
        db, camera_id=camera_id, target_user_id=user_id, requester_id=current_user.id
    )
    if not success:
        raise HTTPException(status_code=404, detail="Permission not found")
    try:
        write_audit_log(
            db,
            action="camera.permission.revoke",
            user_id=current_user.id,
            entity_type="camera",
            entity_id=camera_id,
            details={"target_user_id": user_id},
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return {"message": "Permission revoked"}


@router.get("/{camera_id}/permissions/check")
def check_camera_permission(
    camera_id: int,
    require_manage: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Check if current user has permission to view/manage a camera."""
    allowed = CameraService.user_has_permission(
        db, camera_id, current_user.id, require_manage=require_manage
    )
    return {
        "camera_id": camera_id,
        "allowed": allowed,
        "require_manage": require_manage,
    }


@router.get("/{camera_id}/mediamtx-status")
async def get_camera_mediamtx_status(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get MediaMTX status for a camera including recording configuration."""
    from services.mediamtx_admin_service import MediaMtxAdminService

    # Get camera and check permissions
    camera = CameraService.get_camera_by_id(
        db=db, camera_id=camera_id, user_id=current_user.id
    )
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    # Get path status
    path_status = await MediaMtxAdminService.path_status(camera_id, camera.ip_address)

    # Get active path info
    active_path = await MediaMtxAdminService.get_active_path(
        camera_id, camera.ip_address
    )

    # Get recording status
    recording_status = await MediaMtxAdminService.get_recording_status(camera_id, db)

    # Check if camera is actually streaming (receiving data)
    # MediaMTX returns bytesReceived > 0 when data is actually flowing
    details = active_path.get("details", {})
    is_streaming = False

    if isinstance(details, dict):
        # Check if bytes are being received (actual data flowing)
        bytes_received = details.get("bytesReceived", 0)
        # Also check if source is ready AND has received data
        source_ready = details.get("sourceReady", False)
        is_streaming = source_ready and bytes_received > 0

        # Debug logging to see what MediaMTX is actually returning
        camera_logger.info(
            f"Camera {camera_id} ({camera.name}) MediaMTX status: sourceReady={source_ready}, bytesReceived={bytes_received}, is_streaming={is_streaming}"
        )
        camera_logger.debug(f"Camera {camera_id} full details: {details}")

    return {
        "camera_id": camera_id,
        "camera_name": camera.name,
        "path_configured": path_status.get("status") == "ok",
        "path_active": is_streaming,  # Only true if actively receiving data
        "path_status": path_status,
        "active_path": active_path,
        "recording_status": recording_status,
    }


def _log_provision_start(user_id: int, camera_id: int, request: Request, **kwargs):
    camera_logger.log_action(
        "camera.manual_provision_start",
        user_id=user_id,
        camera_id=camera_id,
        message=f"Attempting to manually provision camera {camera_id} in MediaMTX",
        extra_data=kwargs,
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
    )


def _log_provision_result(
    user_id: int, camera_id: int, result: dict, enable_recording: bool
):
    if result.get("status") == "ok":
        camera_logger.log_action(
            "camera.manual_provision_success",
            user_id=user_id,
            camera_id=camera_id,
            message=f"Camera {camera_id} manually provisioned in MediaMTX",
            extra_data={"result": result, "recording_enabled": enable_recording},
        )
    else:
        camera_logger.log_action(
            "camera.manual_provision_failed",
            user_id=user_id,
            camera_id=camera_id,
            message=f"Failed to manually provision camera {camera_id} in MediaMTX",
            extra_data={"result": result},
        )


@router.post("/{camera_id}/provision-mediamtx")
async def provision_camera_mediamtx(
    camera_id: int,
    enable_recording: bool = False,
    rtsp_transport: str = "tcp",
    recording_segment_seconds: int = 300,
    recording_path: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    request: Request = None,
):
    """Manually provision camera in MediaMTX with optional recording configuration."""
    from services.mediamtx_admin_service import MediaMtxAdminService

    _log_provision_start(
        current_user.id,
        camera_id,
        request,
        enable_recording=enable_recording,
        rtsp_transport=rtsp_transport,
        recording_segment_seconds=recording_segment_seconds,
        recording_path=recording_path,
    )

    try:
        # Get camera and check permissions
        camera = CameraService.get_camera_by_id(
            db=db, camera_id=camera_id, user_id=current_user.id
        )
        if not camera:
            raise HTTPException(status_code=404, detail="Camera not found")

        if not camera.rtsp_url:
            raise HTTPException(
                status_code=400, detail="Camera has no RTSP URL configured"
            )

        # Provision in MediaMTX
        transport = "automatic" if rtsp_transport == "auto" else rtsp_transport

        result = await MediaMtxAdminService.push_rtsp_stream(
            camera_id=camera.id,
            camera_ip=camera.ip_address,
            rtsp_url=camera.rtsp_url,
            enable_recording=enable_recording,
            rtsp_transport=transport,
            recording_segment_seconds=recording_segment_seconds,
            recording_path=recording_path,
        )

        if result.get("status") == "ok":
            camera.status = "provisioned"
            db.commit()

        _log_provision_result(current_user.id, camera_id, result, enable_recording)

        return {
            "camera_id": camera_id,
            "mediamtx_result": result,
            "recording_enabled": enable_recording,
            "rtsp_transport": rtsp_transport,
            "recording_path": recording_path,
        }

    except HTTPException:
        raise
    except Exception as e:
        camera_logger.error(
            f"Exception during manual provisioning of camera {camera_id}",
            extra={
                "user_id": current_user.id,
                "camera_id": camera_id,
                "error_type": type(e).__name__,
                "action": "camera.manual_provision_exception",
            },
            exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail="Internal server error during provisioning"
        )


@router.post("/{camera_id}/test-connection")
def test_camera_connection(
    camera_id: int,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Test camera connection."""
    # Since we have the camera object via dependency, we could refactor the service to take camera obj
    # But for now, just delegate with confirmed permission.
    try:
        from services.camera_service import CameraService

        # Note: service call also checks perms again currently.
        # Ideally refactor service later.
        return CameraService.test_camera_connection(
            db=db, camera_id=camera_id, user_id=current_user.id
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _update_camera_recording_config(db: Session, camera_id: int, enable: bool):
    from sqlalchemy.sql import func

    from models import Camera, CameraConfig

    config = db.query(CameraConfig).filter(CameraConfig.camera_id == camera_id).first()
    if config:
        config.recording_enabled = enable
    else:
        # Create config if it doesn't exist
        cam = db.query(Camera).filter(Camera.id == camera_id).first()
        if cam and cam.rtsp_url:
            config = CameraConfig(
                camera_id=camera_id,
                stream_protocol="rtsp",
                source_url=cam.rtsp_url,
                recording_enabled=enable,
                rtsp_transport="tcp",
                recording_segment_seconds=300,
                last_provisioned_at=func.now(),
            )
            db.add(config)

    db.commit()


@router.post("/{camera_id}/toggle-recording")
async def toggle_camera_recording(
    camera_id: int,
    enable: bool,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    request: Request = None,
):
    """Toggle recording for a camera."""
    # Check permissions
    if not CameraService.user_has_permission(
        db, camera_id, current_user.id, require_manage=True
    ):
        raise HTTPException(
            status_code=403, detail="Not enough permissions to manage recording"
        )

    from services.mediamtx_admin_service import MediaMtxAdminService

    camera_logger.log_action(
        "camera.toggle_recording_start",
        user_id=current_user.id,
        camera_id=camera_id,
        message=f"Toggling recording for camera {camera_id} to {enable}",
        extra_data={"enable": enable},
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
    )

    try:
        if enable:
            result = await MediaMtxAdminService.enable_recording(
                camera_id, duration="300s", segment_duration="10s"
            )
        else:
            result = await MediaMtxAdminService.disable_recording(camera_id)

        is_success = result.get("status") == "ok" or (
            result.get("status") != "error" and "recording_enabled" in result
        )

        if is_success:
            _update_camera_recording_config(db, camera_id, enable)
            camera_logger.log_action(
                "camera.toggle_recording_success",
                user_id=current_user.id,
                camera_id=camera_id,
                message=f"Recording {'enabled' if enable else 'disabled'} successfully",
                extra_data=result,
            )
        else:
            camera_logger.log_action(
                "camera.toggle_recording_failed",
                user_id=current_user.id,
                camera_id=camera_id,
                message="Failed to toggle recording",
                extra_data=result,
            )

        return result
    except Exception as e:
        camera_logger.error(
            f"Exception toggling recording for camera {camera_id}: {e}", exc_info=True
        )
        raise HTTPException(status_code=500, detail=str(e))


# Stream URLs (WHEP/HLS) - no FFmpeg/RTSP proxy required
@router.get("/{camera_id}/stream/urls", response_model=dict[str, str])
def stream_urls(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    cam = CameraService.get_camera_by_id(db, camera_id, current_user.id)
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found")
    # Build secure WHEP
    whep = build_secure_whep_url_for_user(camera_id, cam.ip_address, current_user.id)
    # Build HLS URL based on stream name - use HLS-specific URL (port 8888)
    stream_name = _build_stream_name(
        settings.mediamtx_stream_prefix, camera_id, cam.ip_address
    )
    hls_base = settings.mediamtx_hls_url or "http://localhost:8888"
    hls = hls_base.rstrip("/") + f"/{stream_name}/index.m3u8"
    return {"whep": whep, "hls": hls}


# ============================================================================
# PTZ Control Endpoints - Uses camera credentials from database
# ============================================================================


@router.post("/{camera_id}/ptz/move")
async def ptz_move(
    camera_id: int,
    x: float = Query(0.0, ge=-1.0, le=1.0),
    y: float = Query(0.0, ge=-1.0, le=1.0),
    z: float = Query(0.0, ge=-1.0, le=1.0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    PTZ continuous move for a camera.

    Uses the camera's stored ONVIF credentials from the database.
    x: Pan speed (-1.0 to 1.0, negative=left, positive=right)
    y: Tilt speed (-1.0 to 1.0, negative=down, positive=up)
    z: Zoom speed (-1.0 to 1.0, negative=zoom out, positive=zoom in)
    """
    camera_logger.info(f"PTZ move request: camera_id={camera_id}, x={x}, y={y}, z={z}")

    cam = CameraService.get_camera_by_id(db, camera_id, current_user.id)
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found")

    if not cam.username or not cam.password:
        raise HTTPException(
            status_code=400, detail="Camera ONVIF credentials not configured"
        )

    camera_logger.info(f"PTZ move: camera IP={cam.ip_address}, user={cam.username}")

    # Clamp values
    x = max(-1.0, min(1.0, x))
    y = max(-1.0, min(1.0, y))
    z = max(-1.0, min(1.0, z))

    from services.ptz_service import PTZService

    return await PTZService.move(
        camera_id=cam.id,
        ip=cam.ip_address,
        username=cam.username,
        password=cam.password,
        camera_port=cam.port,
        x=x,
        y=y,
        z=z,
    )


@router.post("/{camera_id}/ptz/stop")
async def ptz_stop(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Stop PTZ movement for a camera.

    Uses the camera's stored ONVIF credentials from the database.
    """
    cam = CameraService.get_camera_by_id(db, camera_id, current_user.id)
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found")

    if not cam.username or not cam.password:
        raise HTTPException(
            status_code=400, detail="Camera ONVIF credentials not configured"
        )

    from services.ptz_service import PTZService

    return await PTZService.stop(
        camera_id=cam.id,
        ip=cam.ip_address,
        username=cam.username,
        password=cam.password,
        camera_port=cam.port,
    )


# Proxy restart/status and publish URL endpoints removed (FFmpeg proxy eliminated)
