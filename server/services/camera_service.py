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

from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from core.camera_source import is_publisher
from core.config import settings
import logging

from core.logging_config import camera_logger
from models import Camera, CameraPermission, User
from schemas import CameraCreate, CameraUpdate
from services.mediamtx_admin_service import MediaMtxAdminService
from utils.url_redaction import redact_url_credentials


def camera_search_filter(q: str | None):
    """Case-insensitive substring match on a camera's name, IP address or
    location.

    None for an empty or missing query, so callers can apply it
    unconditionally and an unfiltered list stays exactly what it was. LIKE
    wildcards in the query are escaped: searching for "cam_1" must not also
    match "camX1".
    """
    term = (q or "").strip()
    if not term:
        return None
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    return or_(
        Camera.name.ilike(pattern, escape="\\"),
        Camera.ip_address.ilike(pattern, escape="\\"),
        Camera.location.ilike(pattern, escape="\\"),
    )


def camera_location_filter(location: str | None, no_location: bool = False):
    """Cameras at one location (trimmed, any case), or with none at all.

    None when neither is asked for, so the list stays unfiltered. A blank
    ``location`` counts as not asked for, like a blank search.
    """
    if no_location:
        return or_(Camera.location.is_(None), func.trim(Camera.location) == "")
    term = (location or "").strip()
    if not term:
        return None
    return func.lower(func.trim(Camera.location)) == term.lower()


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
                    substream_url=camera_create.substream_url,
                    display_aspect_ratio=camera_create.display_aspect_ratio,
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
                    source_type=camera_create.source_type,
                    source_device=camera_create.source_device or None,
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
                        # Redact user:pass@ before it reaches the log file.
                        "rtsp_url": redact_url_credentials(db_camera.rtsp_url),
                    },
                )

            # Commit the transaction to persist the camera
            db.commit()
            db.refresh(db_camera)
            # Every app that runs on all cameras picks the new one, so its
            # skills are on the camera from its first frame.
            try:
                from services.skill_assignments import adopt_new_camera
                if adopt_new_camera(db, db_camera.id):
                    db.commit()
                    db.refresh(db_camera)
            except Exception:  # noqa: BLE001 — never fail onboarding on this
                db.rollback()
                logging.getLogger(__name__).warning(
                    "all-cameras apps could not adopt camera %s", db_camera.id, exc_info=True)

            # Identity-protect the recordings directory BEFORE provisioning
            # (i.e. before MediaMTX can write the first segment): if this
            # camera's numeric id inherits a pre-existing directory that
            # belonged to a different camera (DB wipe + re-add), the old tree
            # is quarantined to orphaned/ and a clean, stamped dir takes its
            # place. Best-effort: a failure here must never block camera
            # creation (the reconciler repeats the same protection).
            try:
                from services.camera_identity import protect_camera_dir

                classification = protect_camera_dir(db, db_camera, "provision")
                if classification == "conflict":
                    camera_logger.log_action(
                        "camera.recordings_dir_quarantined",
                        message=(
                            f"Pre-existing recordings for camera id {db_camera.id} "
                            "belonged to a different camera and were moved to "
                            "orphaned/ (recoverable in Settings -> Recording)"
                        ),
                        user_id=owner_id,
                        camera_id=db_camera.id,
                    )
            except Exception:
                camera_logger.error(
                    f"Recording-dir identity protection failed for camera {db_camera.id}",
                    exc_info=True,
                )

            if is_publisher(db_camera):
                await CameraService._provision_publisher(db, db_camera, owner_id)
            # Auto-provision if RTSP URL is present
            elif db_camera.rtsp_url:
                try:
                    # Default settings for auto-provisioning.
                    # This is a Network Video RECORDER: recording is always on by
                    # default the moment a camera is configured — it is not an
                    # operator opt-in. TCP transport, configured segment length.
                    provision_result = await MediaMtxAdminService.push_rtsp_stream(
                        camera_id=db_camera.id,
                        camera_ip=db_camera.ip_address,
                        rtsp_url=db_camera.rtsp_url,
                        enable_recording=True,
                        rtsp_transport="tcp",
                        recording_segment_seconds=settings.recording_segment_seconds,
                    )

                    if provision_result.get("status") == "ok":
                        db_camera.status = "provisioned"

                        # Create CameraConfig to persist settings
                        from sqlalchemy.sql import func

                        from models import CameraConfig

                        # Probe the camera for RTSPS support at create time and
                        # default transport_security accordingly. Best-effort:
                        # unreachable → INCONCLUSIVE, policy stays rtsps_preferred
                        # (operator can re-probe later). See V-003.
                        from datetime import UTC, datetime

                        from services.transport_probe_service import (
                            ProbeOutcome,
                            TransportProbeService,
                            policy_for_outcome,
                        )

                        probe_outcome = await TransportProbeService.probe(
                            db_camera.rtsp_url
                        )
                        transport_security = policy_for_outcome(probe_outcome)
                        probed_at = datetime.now(UTC)

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
                                recording_enabled=True,
                                rtsp_transport="tcp",
                                recording_segment_seconds=settings.recording_segment_seconds,
                                last_provisioned_at=func.now(),
                                transport_security=transport_security,
                                # Probe-driven, not operator-set, so a later
                                # re-probe can update it without reset_policy.
                                transport_security_operator_set=False,
                                transport_security_probe_result=probe_outcome.value,
                                transport_security_probed_at=probed_at,
                            )
                            db.add(new_config)
                        else:
                            # Only overwrite the policy if it isn't an
                            # operator override — same semantics as the
                            # re-probe endpoint. If create_camera is
                            # called against a row that an operator
                            # already touched, preserve their choice.
                            if not existing_config.transport_security_operator_set:
                                existing_config.transport_security = transport_security
                            existing_config.transport_security_probe_result = (
                                probe_outcome.value
                            )
                            existing_config.transport_security_probed_at = probed_at

                        db.commit()
                        db.refresh(db_camera)

                        camera_logger.log_action(
                            "camera.auto_provision_success",
                            message=f"Camera {db_camera.id} auto-provisioned successfully",
                            user_id=owner_id,
                            camera_id=db_camera.id,
                            extra_data={
                                **provision_result,
                                "transport_security": transport_security,
                                "transport_security_probe_result": probe_outcome.value,
                            },
                        )
                        if probe_outcome == ProbeOutcome.NOT_SUPPORTED:
                            camera_logger.log_action(
                                "camera.transport_security_plaintext",
                                message=(
                                    f"Camera {db_camera.id} does not support RTSPS; "
                                    f"transport_security set to plaintext_allowed"
                                ),
                                user_id=owner_id,
                                camera_id=db_camera.id,
                                extra_data={"rtsp_url_host": db_camera.ip_address},
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
    async def _provision_publisher(db: Session, db_camera: Camera, owner_id: int) -> None:
        """Provision a new webcam camera's path and its CameraConfig.

        Like the RTSP branch, minus everything that needs a source URL (the
        RTSPS probe, transport policy, substream). Recording is on, as for
        every camera on an NVR. A failure leaves the camera row in place: the
        startup reconcile and ``provision-mediamtx`` retry it.
        """
        from models import CameraConfig

        from services.local_webcams import provisioning_for, substream_marker
        extra = provisioning_for(db_camera)
        if extra.get("substream"):
            # The row says it has a sub stream (a Linux USB webcam, or one
            # the host agent streams); the detect tap reads the small path
            # and evidence the large one, as for an IP camera.
            from services.stream_service import _build_stream_name, substream_name
            name = _build_stream_name(settings.mediamtx_stream_prefix, db_camera.id, db_camera.ip_address)
            db_camera.substream_url = substream_marker(substream_name(name))
        try:
            result = await MediaMtxAdminService.push_publisher_path(
                camera_id=db_camera.id,
                camera_ip=db_camera.ip_address,
                enable_recording=True,
                recording_segment_seconds=settings.recording_segment_seconds,
                run_on_init=extra.get("run_on_init"),
                substream=bool(extra.get("substream")),
            )
            if result.get("status") != "ok":
                camera_logger.log_action(
                    "camera.auto_provision_failed",
                    message=f"Auto-provisioning failed for webcam camera {db_camera.id}",
                    user_id=owner_id,
                    camera_id=db_camera.id,
                    extra_data=result,
                )
                return
            db_camera.status = "provisioned"
            if not (
                db.query(CameraConfig)
                .filter(CameraConfig.camera_id == db_camera.id)
                .first()
            ):
                db.add(
                    CameraConfig(
                        camera_id=db_camera.id,
                        source_url=None,
                        recording_enabled=True,
                        recording_segment_seconds=settings.recording_segment_seconds,
                        last_provisioned_at=func.now(),
                    )
                )
            db.commit()
            db.refresh(db_camera)
            camera_logger.log_action(
                "camera.auto_provision_success",
                message=f"Webcam camera {db_camera.id} provisioned; waiting for a publisher",
                user_id=owner_id,
                camera_id=db_camera.id,
                extra_data=result,
            )
        except Exception as e:
            db.rollback()
            camera_logger.error(
                f"Exception during auto-provisioning for webcam camera {db_camera.id}",
                extra={
                    "user_id": owner_id,
                    "camera_id": db_camera.id,
                    "error_type": type(e).__name__,
                    "action": "camera.auto_provision_exception",
                },
                exc_info=True,
            )

    @staticmethod
    def get_camera_by_id(db: Session, camera_id: int, user_id: int) -> Camera | None:
        """Get camera by ID (only if user owns it or is superuser)."""
        camera = (
            db.query(Camera)
            .filter(Camera.id == camera_id, Camera.deleted_at.is_(None))
            .first()
        )
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
        """Delete a camera (irreversible soft delete: tombstone + deactivate)."""
        db_camera = (
            db.query(Camera)
            .filter(Camera.id == camera_id, Camera.deleted_at.is_(None))
            .first()
        )
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
        db_camera.deleted_at = datetime.now(UTC)
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
