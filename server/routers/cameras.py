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

import shutil
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from core.auth import get_current_active_user, get_current_superuser
from core.config import settings
from core.database import get_db
from core.logging_config import camera_logger
from core.permissions import get_camera_or_403
from models import Camera, CameraEvent, CameraPermission, Recording, TimelineEvent, User
from schemas import (
    CameraCreate,
    CameraHardDeleteRequest,
    CameraList,
    CameraPermissionAssign,
    CameraPermissionResponse,
    CameraResponse,
    CameraUpdate,
    DeletedCameraInfo,
    DeletedCameraList,
    TransportSecurityUpdate,
)
from services.audit_service import write_audit_log
from services.camera_identity import path_name_for_camera, read_marker
from services.camera_service import CameraService
from services.mediamtx_admin_service import MediaMtxAdminService
from services.storage_service import get_effective_recordings_base_path
from services.stream_service import _build_stream_name, build_secure_whep_url_for_user
from utils.mfa_guard import require_mfa_code
from utils.url_redaction import redact_url_credentials

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


def _find_duplicate_cameras(
    db: Session, user_id: int, ip_address: str, rtsp_url: str | None
) -> list[Camera]:
    """Existing non-deleted cameras of this owner matching the new camera's
    IP address or exact RTSP URL. Inactive (paused) cameras count — they can
    be reactivated; binned ones don't."""
    conditions = [Camera.ip_address == ip_address]
    if rtsp_url:
        conditions.append(Camera.rtsp_url == rtsp_url)
    return (
        db.query(Camera)
        .filter(
            Camera.owner_id == user_id,
            Camera.deleted_at.is_(None),
            or_(*conditions),
        )
        .all()
    )


def _record_forced_duplicate_audit(
    db: Session,
    user_id: int,
    cam: Camera,
    duplicates: list[Camera],
    request: Request,
):
    try:
        write_audit_log(
            db,
            action="camera.create_forced_duplicate",
            user_id=user_id,
            entity_type="camera",
            entity_id=cam.id,
            details={
                "name": cam.name,
                "ip": cam.ip_address,
                "duplicate_of": [d.id for d in duplicates],
            },
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
                Camera.deleted_at.is_(None),
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
    force: bool = Query(
        False,
        description="Add the camera even when its IP or RTSP URL matches an existing one.",
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    request: Request = None,
):
    """Create a new camera.

    When no RTSP URL is supplied but credentials are, the server derives it
    from the IP + credentials (ONVIF direct-connect, then vendor RTSP
    templates) and back-fills the camera's identity — so operators normally
    only need IP + username + password.
    """
    _log_camera_creation_start(current_user.id, camera_create, request)

    # Referenced after creation regardless of the credentials branch below —
    # without these initializers a credential-less create crashed with
    # UnboundLocalError before ever returning.
    onvif_port = None
    control_scheme = None

    # On connect, when credentials are supplied: derive the RTSP URL (if none was
    # given), always back-fill device identity (manufacturer/model/firmware/serial),
    # and sync the camera's clock to correct time.
    if camera_create.username and camera_create.password:
        from services.camera_source_resolver import (
            fetch_identity,
            inject_credentials,
            resolve_source,
            sync_camera_time,
        )

        onvif_port = None
        control_scheme = None
        # Any identity fields the resolver returns; back-filled below without
        # ever overwriting values the caller (e.g. ONVIF discovery) already set.
        identity: dict | None = None

        if not camera_create.rtsp_url:
            # No URL provided — derive it (ONVIF, then vendor RTSP templates).
            try:
                derived = await resolve_source(
                    camera_create.ip_address,
                    camera_create.username,
                    camera_create.password,
                    camera_create.port or 554,
                )
            except Exception:
                derived = None
            if not (derived and derived.get("rtsp_url")):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Couldn't automatically determine the RTSP stream for this "
                        "camera. Check the IP address and credentials, and that the "
                        "camera is reachable on the network."
                    ),
                )
            onvif_port = derived.get("onvif_port")
            control_scheme = derived.get("control_scheme")
            identity = derived
            camera_create = camera_create.model_copy(update={"rtsp_url": derived["rtsp_url"]})
        else:
            # URL supplied — embed the credentials into it when they aren't already
            # part of the URL. MediaMTX authenticates using the userinfo in the RTSP
            # URL, so a bare "rtsp://host/path" plus separate user/pass would fail to
            # stream. inject_credentials is a no-op if the URL already has userinfo,
            # and URL-encodes special characters (e.g. "@") in the password.
            url_with_creds = inject_credentials(
                camera_create.rtsp_url,
                camera_create.username,
                camera_create.password,
            )
            if url_with_creds and url_with_creds != camera_create.rtsp_url:
                camera_create = camera_create.model_copy(
                    update={"rtsp_url": url_with_creds}
                )
            # Still enrich identity + locate the ONVIF port (for time-sync).
            identity = await fetch_identity(
                camera_create.ip_address,
                camera_create.username,
                camera_create.password,
            )
            if identity:
                onvif_port = identity.get("onvif_port")
                control_scheme = identity.get("control_scheme")

        if identity:
            camera_create = camera_create.model_copy(
                update={
                    "manufacturer": camera_create.manufacturer or identity.get("manufacturer"),
                    "model": camera_create.model or identity.get("model"),
                    "firmware_version": camera_create.firmware_version
                    or identity.get("firmware_version"),
                    "serial_number": camera_create.serial_number
                    or identity.get("serial_number"),
                    "hardware_id": camera_create.hardware_id or identity.get("hardware_id"),
                }
            )

        # Correct the camera clock (fixes the timestamp burned into the video).
        # sync_camera_time is self-guarding — it returns False rather than raising.
        await sync_camera_time(
            camera_create.ip_address,
            camera_create.username,
            camera_create.password,
            onvif_port,
            control_scheme,
        )

    # Duplicate guard — compared after RTSP derivation/credential injection so
    # the final URL is what's matched. 409 carries the matches so the client
    # can show "already added — add anyway?" and retry with ?force=true.
    duplicates = _find_duplicate_cameras(
        db, current_user.id, camera_create.ip_address, camera_create.rtsp_url
    )
    if duplicates and not force:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "duplicate_camera",
                "message": (
                    "A camera with this IP address or stream URL is already added."
                ),
                "duplicates": [
                    {
                        "id": d.id,
                        "name": d.name,
                        "ip_address": d.ip_address,
                        "rtsp_url": redact_url_credentials(d.rtsp_url)
                        if d.rtsp_url
                        else None,
                        "is_active": d.is_active,
                    }
                    for d in duplicates
                ],
            },
        )

    try:
        cam = await CameraService.create_camera(
            db=db, camera_create=camera_create, owner_id=current_user.id
        )

        if duplicates:
            # User explicitly confirmed past the duplicate warning.
            _record_forced_duplicate_audit(db, current_user.id, cam, duplicates, request)

        # Persist the ONVIF control endpoint discovered above (Hikvision http:80,
        # Secureye/Tiandy http:8088, TLS-only devices https:…) so the driver
        # layer never re-guesses it. Any port/scheme on any camera works because
        # we remember what actually answered.
        if onvif_port or control_scheme:
            if onvif_port:
                cam.onvif_port = onvif_port
            if control_scheme:
                cam.control_scheme = control_scheme
            db.commit()
            db.refresh(cam)

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
        # Binned cameras never count; active_only additionally hides paused ones.
        total_query = db.query(Camera).filter(Camera.deleted_at.is_(None))
        if active_only:
            total_query = total_query.filter(Camera.is_active == True)
        total = total_query.count()
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
        query = db.query(Camera).filter(
            Camera.ip_address == ip_address, Camera.deleted_at.is_(None)
        )
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
            and_(Camera.ip_address == ip_address, Camera.owner_id == current_user.id),
            Camera.deleted_at.is_(None),
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
            and_(Camera.ip_address == ip_address, Camera.id.in_(permitted_subq)),
            Camera.deleted_at.is_(None),
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


# NOTE: must be registered before GET /{camera_id} — otherwise "deleted"
# would be parsed as the int camera_id and 422.
@router.get("/deleted", response_model=DeletedCameraList)
def get_deleted_cameras(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """List binned (soft-deleted) cameras — the Deleted Cameras page.

    Superusers see every binned camera, other users only their own. These
    cameras can never be edited or reactivated; their recordings remain
    playable (via the returned ``path``) until retention removes them, and a
    superuser can purge them permanently via the hard-delete endpoint.
    """
    query = db.query(Camera).filter(Camera.deleted_at.isnot(None))
    if not current_user.is_superuser:
        query = query.filter(Camera.owner_id == current_user.id)
    cameras = query.order_by(Camera.deleted_at.desc()).all()

    items = [
        DeletedCameraInfo(
            id=c.id,
            name=c.name,
            ip_address=c.ip_address,
            location=c.location,
            owner_id=c.owner_id,
            deleted_at=c.deleted_at,
            path=path_name_for_camera(c),
        )
        for c in cameras
    ]
    return DeletedCameraList(cameras=items, total=len(items))


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
        if camera is None:
            raise HTTPException(status_code=404, detail="Camera not found")

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
            camera = (
                db.query(Camera)
                .filter(Camera.id == camera_id, Camera.deleted_at.is_(None))
                .first()
            )
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
async def update_camera(
    camera_id: int,
    camera_update: CameraUpdate,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    request: Request = None,
):
    """Update camera information (owner or superuser).

    Toggling ``is_active`` is the reversible pause: deactivating immediately
    tears down the camera's MediaMTX path (stops live view and recording),
    reactivating re-provisions it. The flag is the source of truth — a
    MediaMTX hiccup never blocks the state change.
    """
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
        was_active = bool(camera.is_active)
        for field, value in camera_update.model_dump(exclude_unset=True).items():
            setattr(camera, field, value)

        db.commit()
        db.refresh(camera)

        now_active = bool(camera.is_active)
        stream_action = None
        if was_active and not now_active:
            # Pause: stop the stream (and with it, recording) right now, not
            # at the next restart. Best-effort — see docstring.
            stream_action = "deactivate"
            try:
                res = await MediaMtxAdminService.unprovision_path(
                    camera.id, camera.ip_address
                )
                camera_logger.log_action(
                    "camera.deactivate_unprovision",
                    user_id=current_user.id,
                    camera_id=camera.id,
                    message=f"Camera {camera.id} deactivated; MediaMTX path removed",
                    extra_data={"unprovision_result": res},
                )
            except Exception as e:
                camera_logger.error(
                    f"Camera {camera.id} deactivated but MediaMTX teardown failed: {e}",
                    extra={"camera_id": camera.id, "action": "camera.deactivate_unprovision_failed"},
                    exc_info=True,
                )
        elif not was_active and now_active:
            # Resume: re-provision. provision_camera_by_id re-reads the camera
            # (post-commit, so the new is_active passes its filter) and is
            # lenient about a missing CameraConfig row.
            stream_action = "activate"
            from services.mediamtx_startup_service import MediaMtxStartupService

            try:
                res = await MediaMtxStartupService.provision_camera_by_id(camera.id)
                camera_logger.log_action(
                    "camera.activate_provision",
                    user_id=current_user.id,
                    camera_id=camera.id,
                    message=f"Camera {camera.id} reactivated; MediaMTX provisioning: {res.get('status')}",
                    extra_data={"provision_result": res},
                )
            except Exception as e:
                camera_logger.error(
                    f"Camera {camera.id} reactivated but MediaMTX provisioning failed: {e}",
                    extra={"camera_id": camera.id, "action": "camera.activate_provision_failed"},
                    exc_info=True,
                )

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
                    ],
                    **({"stream_action": stream_action} if stream_action else {}),
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
async def delete_camera(
    camera_id: int,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    request: Request = None,
):
    """Delete a camera (irreversible soft delete).

    Tears down the MediaMTX path immediately (stops live view and recording),
    stamps ``deleted_at`` and moves the camera to the bin: it disappears from
    every normal list, can never be edited or reactivated, and its recordings
    stay viewable only through the bin until retention ages them out. The
    already-deleted case 404s via get_camera_or_403.
    """
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
        # Stop the stream first — best-effort, idempotent, no-op when the
        # MediaMTX admin API isn't configured. The tombstone below is what
        # keeps every (re)provisioning path from resurrecting it.
        teardown = None
        try:
            teardown = await MediaMtxAdminService.unprovision_path(
                camera.id, camera.ip_address
            )
        except Exception as e:
            camera_logger.error(
                f"MediaMTX teardown failed while deleting camera {camera_id}: {e}",
                extra={"camera_id": camera_id, "action": "camera.delete_unprovision_failed"},
                exc_info=True,
            )

        # We already have the camera via dependency and ownership is confirmed.
        # Irreversible soft delete: tombstone + deactivate.
        camera.is_active = False
        camera.deleted_at = datetime.now(UTC)
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
                details={
                    "camera_name": camera_name,
                    "deleted_at": camera.deleted_at.isoformat(),
                    "stream_teardown": (teardown or {}).get("status", "failed"),
                },
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


@router.post("/{camera_id}/hard-delete")
async def hard_delete_camera(
    camera_id: int,
    payload: CameraHardDeleteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
    request: Request = None,
    mfa_code: str | None = Header(None, alias="X-MFA-Code"),
):
    """Permanently delete a binned camera: its DB rows and its recordings.

    Superuser only, and deliberately hard to trigger by accident:
    - the camera must already be in the bin (soft-deleted) — 409 otherwise;
    - the caller's current TOTP code must be in the X-MFA-Code header;
    - the body must carry the exact phrase
      ``hard delete <camera name> and it's recording``.

    Files are purged before rows so a failed purge stays retryable. A
    recordings dir whose identity marker belongs to a *different* camera uuid
    is skipped, never deleted (it is that camera's archive).
    """
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    if camera.deleted_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Camera is not in the bin. Delete it first, then purge it from the bin.",
        )

    require_mfa_code(current_user, mfa_code)

    expected_phrase = f"hard delete {camera.name} and it's recording"
    if payload.confirmation_phrase != expected_phrase:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Confirmation phrase mismatch. Type exactly: "{expected_phrase}"',
        )

    # Defensive teardown — delete already unprovisioned it, but the purge must
    # never race a still-writing publisher.
    try:
        await MediaMtxAdminService.unprovision_path(camera.id, camera.ip_address)
    except Exception:
        camera_logger.error(
            f"MediaMTX teardown failed during hard delete of camera {camera_id}",
            extra={"camera_id": camera_id},
            exc_info=True,
        )

    # Purge recording files first; DB rows only fall once the disk is clean,
    # so an rmtree failure leaves everything retryable.
    files_result = "no-directory"
    cam_dir = Path(get_effective_recordings_base_path(db)) / path_name_for_camera(camera)
    if cam_dir.is_dir():
        marker, _corrupt = read_marker(cam_dir)
        marker_uuid = (marker or {}).get("camera_uuid")
        if marker_uuid and camera.uuid and marker_uuid != camera.uuid:
            # Different camera's archive living under this reused path name.
            files_result = "skipped-identity-mismatch"
            camera_logger.log_action(
                "camera.hard_delete_dir_skipped",
                user_id=current_user.id,
                camera_id=camera_id,
                message=f"Recordings dir {cam_dir.name} owned by another camera uuid; not deleted",
                extra_data={"dir": str(cam_dir), "marker_uuid": marker_uuid},
            )
        else:
            try:
                shutil.rmtree(cam_dir)
                files_result = "deleted"
            except Exception as e:
                camera_logger.error(
                    f"Failed to purge recordings dir for camera {camera_id}: {e}",
                    extra={"camera_id": camera_id, "dir": str(cam_dir)},
                    exc_info=True,
                )
                raise HTTPException(
                    status_code=500,
                    detail="Failed to delete the camera's recordings from disk. "
                    "Nothing was removed from the database; try again.",
                )

    # Rows without an ORM cascade from Camera go first, then the camera row
    # (config/permissions/capability cascade via the relationships).
    camera_name = camera.name
    recording_rows = (
        db.query(Recording).filter(Recording.camera_id == camera_id).delete()
    )
    event_rows = (
        db.query(TimelineEvent).filter(TimelineEvent.camera_id == camera_id).delete()
    )
    camera_event_rows = (
        db.query(CameraEvent).filter(CameraEvent.camera_id == camera_id).delete()
    )
    db.delete(camera)
    db.commit()

    summary = {
        "message": "Camera permanently deleted",
        "camera_id": camera_id,
        "camera_name": camera_name,
        "files": files_result,
        "recording_rows_deleted": recording_rows,
        "event_rows_deleted": event_rows + camera_event_rows,
    }

    camera_logger.log_action(
        "camera.hard_delete",
        user_id=current_user.id,
        camera_id=camera_id,
        message=f"Camera permanently deleted: {camera_name}",
        extra_data=summary,
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
    )
    try:
        write_audit_log(
            db,
            action="camera.hard_delete",
            user_id=current_user.id,
            entity_type="camera",
            entity_id=camera_id,
            details=summary,
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception as e:
        camera_logger.error(f"Failed to write audit log: {e}", exc_info=True)

    return summary


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
    recording_segment_seconds: int = settings.recording_segment_seconds,
    recording_path: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    request: Request = None,
):
    """Manually provision a camera in MediaMTX.

    NVR policy: recording is mandatory, so ``enable_recording`` is forced on here
    regardless of the query param — a `curl` with ``?enable_recording=false``
    still provisions with recording enabled. Enforcement also lives at the
    service choke point (push_rtsp_stream); this keeps the response/logs honest.
    """
    from services.mediamtx_admin_service import MediaMtxAdminService

    # Recording cannot be opted out of on an NVR. See docstring.
    enable_recording = True

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

        # Thread the transport policy through to push_rtsp_stream so an
        # rtsps_required camera refuses a plaintext URL. No CameraConfig yet
        # (never provisioned) → pass None, gate skips. See V-003.
        from models import CameraConfig
        from services.transport_probe_service import TransportPolicyViolation

        cfg = (
            db.query(CameraConfig)
            .filter(CameraConfig.camera_id == camera.id)
            .first()
        )
        policy = cfg.transport_security if cfg is not None else None

        # Provision in MediaMTX
        transport = "automatic" if rtsp_transport == "auto" else rtsp_transport

        try:
            result = await MediaMtxAdminService.push_rtsp_stream(
                camera_id=camera.id,
                camera_ip=camera.ip_address,
                rtsp_url=camera.rtsp_url,
                enable_recording=enable_recording,
                rtsp_transport=transport,
                recording_segment_seconds=recording_segment_seconds,
                recording_path=recording_path,
                transport_security=policy,
            )
        except TransportPolicyViolation as exc:
            camera_logger.log_action(
                "camera.transport_policy_refused",
                message=(
                    f"Refused to provision camera {camera.id}: "
                    f"policy={exc.policy} url_scheme={exc.scheme}"
                ),
                user_id=current_user.id,
                camera_id=camera.id,
                extra_data={
                    "policy": exc.policy,
                    "url_scheme": exc.scheme,
                },
            )
            # 409 Conflict: the request is well-formed but conflicts
            # with the stored policy for this resource. The client
            # needs to either update the URL or change the policy.
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
                recording_segment_seconds=settings.recording_segment_seconds,
                last_provisioned_at=func.now(),
            )
            db.add(config)

    db.commit()


# DISABLED — this is a Network Video RECORDER: recording is automatic and must
# not be switchable off by anyone, including via the API. The route is
# intentionally commented out so a direct `curl` cannot enable/disable
# recording. Recording is turned on once at camera-configure time (see
# CameraService, enable_recording=True). The handler is kept (not deleted) so
# the behaviour can be restored by re-enabling the decorator if the product
# decision ever changes.
# @router.post("/{camera_id}/toggle-recording")
async def toggle_camera_recording(
    camera_id: int,
    enable: bool,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    request: Request = None,
):
    """Enable/disable recording for a camera. (Route disabled — see note above.)"""
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
                camera_id,
                duration=f"{settings.recording_segment_seconds}s",
                part_duration="1s",
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


# Re-probe a camera's RTSPS support and update its transport policy. See V-003.
@router.post("/{camera_id}/probe-transport")
async def probe_camera_transport(
    camera_id: int,
    port: int | None = Query(
        None,
        ge=1,
        le=65535,
        description=(
            "Override the RTSPS port for this probe. Useful when the "
            "camera multiplexes RTSPS onto a non-default port (e.g. 443). "
            "Omit to use the default port-selection rules."
        ),
    ),
    reset_policy: bool = Query(
        False,
        description=(
            "If True, overwrite any operator-set transport_security policy "
            "with the probe-driven value. Default False preserves the "
            "operator's explicit choice."
        ),
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    request: Request = None,
):
    """V-003: re-run the RTSPS reachability probe against the camera
    and update its ``transport_security`` policy in CameraConfig.

    Useful when the operator has:

    * just connected a camera that wasn't reachable at create time
      (probe returned INCONCLUSIVE, policy stuck at ``rtsps_preferred``),
    * upgraded the camera's firmware to add RTSPS support,
    * swapped the underlying device while keeping the OpenNVR row.

    Honours the operator's existing explicit choice: if
    ``transport_security_operator_set`` is True (the operator previously
    set the policy via PUT ``/cameras/{id}/transport-security``), this
    endpoint refreshes the probe result but leaves the policy intact.
    Pass ``?reset_policy=true`` to clear the override AND let the probe
    drive the value.
    """
    # Imports inline to keep the import graph slim (matches other endpoints).
    from datetime import UTC, datetime

    from models import CameraConfig
    from services.transport_probe_service import (
        TransportProbeService,
        policy_for_outcome,
    )

    camera = CameraService.get_camera_by_id(
        db=db, camera_id=camera_id, user_id=current_user.id
    )
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    if not camera.rtsp_url:
        raise HTTPException(
            status_code=400,
            detail="Camera has no RTSP URL configured; cannot probe",
        )

    config = (
        db.query(CameraConfig).filter(CameraConfig.camera_id == camera.id).first()
    )
    if not config:
        raise HTTPException(
            status_code=400,
            detail=(
                "Camera has no CameraConfig row yet — provision it first via "
                "POST /cameras/{id}/provision-mediamtx"
            ),
        )

    # Honour the port override so a camera with RTSPS on a non-default port
    # (some Hikvision firmware uses 443) can be probed without editing it.
    outcome = await TransportProbeService.probe(
        camera.rtsp_url, rtsps_port=port
    )

    # Use the explicit operator_set flag, not value pattern-matching, so a
    # deliberate "rtsps_preferred" choice is protected the same as "required".
    operator_set_explicitly = bool(config.transport_security_operator_set)
    if reset_policy or not operator_set_explicitly:
        config.transport_security = policy_for_outcome(outcome)
        # Reset the flag too — the policy is now probe-driven again.
        config.transport_security_operator_set = False

    config.transport_security_probe_result = outcome.value
    config.transport_security_probed_at = datetime.now(UTC)
    db.commit()
    db.refresh(config)

    camera_logger.log_action(
        "camera.transport_probe",
        message=(
            f"Camera {camera.id} re-probed: outcome={outcome.value} "
            f"policy={config.transport_security} "
            f"port_override={port}"
        ),
        user_id=current_user.id,
        camera_id=camera.id,
        extra_data={
            "outcome": outcome.value,
            "transport_security": config.transport_security,
            "reset_policy": reset_policy,
            "port_override": port,
            "ip": request.client.host if request and request.client else None,
        },
    )

    return {
        "camera_id": camera.id,
        "transport_security": config.transport_security,
        "transport_security_operator_set": config.transport_security_operator_set,
        "transport_security_probe_result": config.transport_security_probe_result,
        "transport_security_probed_at": (
            config.transport_security_probed_at.isoformat()
            if config.transport_security_probed_at
            else None
        ),
        "operator_override_preserved": (
            operator_set_explicitly and not reset_policy
        ),
    }


# Explicit operator-override endpoint. Setting the policy here flips
# transport_security_operator_set=True so a later re-probe won't walk it back.
@router.put("/{camera_id}/transport-security")
async def set_camera_transport_security(
    camera_id: int,
    payload: TransportSecurityUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    request: Request = None,
):
    """V-003: explicitly set the camera's transport_security policy.

    Use to:

    * Lock a camera at ``rtsps_required`` so the stream service refuses
      plaintext fallback even if the camera temporarily fails the
      probe (V-003 strict mode).
    * Mark a legacy camera as ``plaintext_allowed`` so the UI stops
      flagging it as a posture warning.
    * Restore a custom-port camera to ``rtsps_preferred`` after a
      misleading probe.

    Sets ``transport_security_operator_set=True`` so subsequent calls
    to POST ``/probe-transport`` preserve the operator's choice.
    """
    from models import CameraConfig

    camera = CameraService.get_camera_by_id(
        db=db, camera_id=camera_id, user_id=current_user.id
    )
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    config = (
        db.query(CameraConfig).filter(CameraConfig.camera_id == camera.id).first()
    )
    if not config:
        raise HTTPException(
            status_code=400,
            detail=(
                "Camera has no CameraConfig row yet — provision it first via "
                "POST /cameras/{id}/provision-mediamtx"
            ),
        )

    previous = config.transport_security
    config.transport_security = payload.policy
    config.transport_security_operator_set = True
    db.commit()
    db.refresh(config)

    camera_logger.log_action(
        "camera.transport_security_set",
        message=(
            f"Camera {camera.id} transport_security: {previous} -> "
            f"{payload.policy} (operator override)"
        ),
        user_id=current_user.id,
        camera_id=camera.id,
        extra_data={
            "previous": previous,
            "new": payload.policy,
            "ip": request.client.host if request and request.client else None,
        },
    )

    return {
        "camera_id": camera.id,
        "transport_security": config.transport_security,
        "transport_security_operator_set": config.transport_security_operator_set,
        "previous": previous,
    }


@router.get("/{camera_id}/snapshot")
async def get_camera_snapshot(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """One JPEG still of the camera's CURRENT view.

    Backs the App Catalog's geometry editors: an operator draws a
    restricted zone / tripwire directly on what the camera actually
    sees, instead of hand-typing pixel coordinates. Served from the
    same persistent capture pool inference uses (in-memory JPEG, no
    disk round-trip), including the MediaMTX tap-URL resolution and
    stale-JWT self-heal.

    Ownership-checked like every camera read (owner or superuser);
    404 for unknown/unowned, 503 when no frame can be captured (camera
    offline / stream down) — the editor falls back to a plain grid.
    """
    from fastapi.responses import Response

    from services.kai_c_service import get_kai_c_service

    camera = CameraService.get_camera_by_id(db, camera_id, current_user.id)
    if not camera:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found"
        )
    if not camera.rtsp_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Camera has no stream URL configured",
        )

    jpeg = await get_kai_c_service().capture_frame_bytes(
        camera.rtsp_url, camera.id
    )
    if not jpeg:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not capture a frame (camera offline?)",
        )
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        # Always-fresh: the editor wants the current view, and browsers
        # aggressively cache image GETs otherwise.
        headers={"Cache-Control": "no-store"},
    )
