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
MediaMTX Startup Service

This service handles automatic re-provisioning of camera streams when MediaMTX restarts.
It reads all active cameras from the database and provisions them in MediaMTX.

NOTE: All public methods are async — callers must await them.
"""

import asyncio
from typing import Any

from sqlalchemy.orm import Session

from core.database import SessionLocal
from core.logging_config import mediamtx_logger
from models import Camera, CameraConfig
from services.mediamtx_admin_service import MediaMtxAdminService
from services.storage_service import get_effective_recordings_base_path


class MediaMtxStartupService:
    """Service for handling MediaMTX startup and auto-provisioning."""

    @staticmethod
    async def push_path_defaults() -> dict[str, Any]:
        """
        Push the recording path defaults to MediaMTX.
        This ensures MediaMTX uses the user-configured recording path from the database.

        Returns:
            Dict with status and details of the operation
        """
        try:
            with SessionLocal() as db:
                base_path = get_effective_recordings_base_path(db)

            # Build path defaults payload with recording path
            # Use %path to include stream name in path
            record_path = f"{base_path}/%path/%Y/%m/%d/%H-%M-%S-%f"

            payload = {
                "recordPath": record_path,
                "record": True,  # Enable recording by default
            }

            mediamtx_logger.info(
                f"Pushing path defaults to MediaMTX: recordPath={record_path}",
                extra={
                    "action": "mediamtx.startup.push_path_defaults",
                    "record_path": record_path,
                },
            )

            result = await MediaMtxAdminService.pathdefaults_patch(payload)

            if result.get("status") == "ok":
                mediamtx_logger.info(
                    "Successfully pushed path defaults to MediaMTX",
                    extra={"action": "mediamtx.startup.path_defaults_success"},
                )
            else:
                mediamtx_logger.warning(
                    f"Failed to push path defaults: {result}",
                    extra={
                        "action": "mediamtx.startup.path_defaults_failed",
                        "result": result,
                    },
                )

            return result

        except Exception as e:
            mediamtx_logger.error(
                f"Error pushing path defaults: {e!s}",
                extra={"action": "mediamtx.startup.path_defaults_error"},
                exc_info=True,
            )
            return {"status": "error", "message": str(e)}

    @staticmethod
    async def auto_provision_all_cameras(
        delay_seconds: int = 5, max_retries: int = 3, retry_delay: int = 2
    ) -> dict[str, Any]:
        """
        Auto-provision all active cameras with valid configurations.

        Args:
            delay_seconds: Wait before starting provisioning (allows MediaMTX to fully start)
            max_retries: Number of retry attempts for each camera
            retry_delay: Delay between retry attempts
        """
        mediamtx_logger.info(
            f"MediaMTX startup auto-provisioning starting (delay: {delay_seconds}s)",
            extra={"action": "mediamtx.startup.auto_provision_start"},
        )

        # Wait for MediaMTX to be fully ready
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

        # First, push the global path defaults (recording path) from database
        path_defaults_result = await MediaMtxStartupService.push_path_defaults()

        results = {
            "status": "completed",
            "path_defaults": path_defaults_result,
            "total_cameras": 0,
            "provisioned": 0,
            "failed": 0,
            "skipped": 0,
            "cameras": [],
        }

        try:
            with SessionLocal() as db:
                # Get all active cameras that have configurations
                cameras = MediaMtxStartupService._get_provisionable_cameras(db)
                results["total_cameras"] = len(cameras)

                mediamtx_logger.info(
                    f"Found {len(cameras)} cameras to provision",
                    extra={
                        "action": "mediamtx.startup.cameras_found",
                        "camera_count": len(cameras),
                    },
                )

                for camera, config in cameras:
                    camera_result = (
                        await MediaMtxStartupService._provision_camera_with_retry(
                            db, camera, config, max_retries, retry_delay
                        )
                    )
                    results["cameras"].append(camera_result)

                    if camera_result["status"] == "success":
                        results["provisioned"] += 1
                    elif camera_result["status"] == "failed":
                        results["failed"] += 1
                    else:
                        results["skipped"] += 1

                mediamtx_logger.info(
                    f"Auto-provisioning completed: {results['provisioned']} success, "
                    f"{results['failed']} failed, {results['skipped']} skipped",
                    extra={
                        "action": "mediamtx.startup.auto_provision_complete",
                        "results": results,
                    },
                )

        except Exception as e:
            mediamtx_logger.error(
                f"Error during auto-provisioning: {e!s}",
                extra={"action": "mediamtx.startup.auto_provision_error"},
                exc_info=True,
            )
            results["status"] = "error"
            results["error"] = str(e)

        return results

    @staticmethod
    def _get_provisionable_cameras(
        db: Session,
    ) -> list[tuple[Camera, CameraConfig | None]]:
        """Get all active cameras that can be provisioned."""
        query = (
            db.query(Camera, CameraConfig)
            .outerjoin(CameraConfig, Camera.id == CameraConfig.camera_id)
            .filter(Camera.is_active == True)
            .filter(Camera.rtsp_url.isnot(None))
            .filter(Camera.rtsp_url != "")
        )

        cameras = []
        for camera, config in query.all():
            # Skip cameras without basic info
            if not camera.rtsp_url:
                continue

            cameras.append((camera, config))

        return cameras

    @staticmethod
    async def _provision_camera_with_retry(
        db: Session,
        camera: Camera,
        config: CameraConfig | None,
        max_retries: int,
        retry_delay: int,
    ) -> dict[str, Any]:
        """Provision a single camera with retry logic."""
        camera_result = {
            "camera_id": camera.id,
            "camera_name": camera.name,
            "status": "unknown",
            "attempts": 0,
            "error": None,
            "mediamtx_response": None,
        }

        for attempt in range(max_retries):
            camera_result["attempts"] = attempt + 1

            try:
                # Build configuration for provisioning (pass db for effective path lookup)
                provision_config = MediaMtxStartupService._build_provision_config(
                    camera, config, db
                )

                # Attempt provisioning
                result = await MediaMtxAdminService.provision_path(
                    camera.id, camera.ip_address, provision_config
                )

                camera_result["mediamtx_response"] = result

                if result.get("status") == "ok":
                    camera_result["status"] = "success"

                    # Update camera status and last provisioned time
                    camera.status = "provisioned"
                    if config:
                        config.last_provisioned_at = (
                            db.query(Camera)
                            .filter(Camera.id == camera.id)
                            .first()
                            .updated_at
                        )

                    db.commit()

                    mediamtx_logger.info(
                        f"Auto-provisioned camera {camera.id} ({camera.name})",
                        extra={
                            "action": "mediamtx.startup.camera_provisioned",
                            "camera_id": camera.id,
                            "camera_name": camera.name,
                            "attempt": attempt + 1,
                        },
                    )
                    break

                else:
                    # Check if it's already exists error - that's actually success for us
                    error_msg = result.get("details", {}).get("error", "").lower()
                    if (
                        "already exists" in error_msg
                        or "path already exists" in error_msg
                    ):
                        camera_result["status"] = "success"
                        camera_result["note"] = "Path already existed"
                        camera.status = "provisioned"
                        db.commit()

                        mediamtx_logger.info(
                            f"Camera {camera.id} ({camera.name}) already provisioned",
                            extra={
                                "action": "mediamtx.startup.camera_already_exists",
                                "camera_id": camera.id,
                                "camera_name": camera.name,
                            },
                        )
                        break
                    else:
                        camera_result["error"] = result.get("details", {}).get(
                            "error", "Unknown error"
                        )

                        if attempt < max_retries - 1:
                            mediamtx_logger.warning(
                                f"Camera {camera.id} provisioning failed, retrying (attempt {attempt + 1}/{max_retries})",
                                extra={
                                    "action": "mediamtx.startup.camera_retry",
                                    "camera_id": camera.id,
                                    "attempt": attempt + 1,
                                    "error": camera_result["error"],
                                },
                            )
                            await asyncio.sleep(retry_delay)
                        else:
                            camera_result["status"] = "failed"
                            camera.status = "provision_failed"
                            db.commit()

                            mediamtx_logger.error(
                                f"Camera {camera.id} provisioning failed after {max_retries} attempts",
                                extra={
                                    "action": "mediamtx.startup.camera_failed",
                                    "camera_id": camera.id,
                                    "camera_name": camera.name,
                                    "error": camera_result["error"],
                                },
                            )

            except Exception as e:
                camera_result["error"] = str(e)

                if attempt < max_retries - 1:
                    mediamtx_logger.warning(
                        f"Camera {camera.id} provisioning exception, retrying (attempt {attempt + 1}/{max_retries}): {e!s}",
                        extra={
                            "action": "mediamtx.startup.camera_exception_retry",
                            "camera_id": camera.id,
                            "attempt": attempt + 1,
                            "error": str(e),
                        },
                    )
                    await asyncio.sleep(retry_delay)
                else:
                    camera_result["status"] = "failed"
                    camera.status = "provision_failed"
                    db.commit()

                    mediamtx_logger.error(
                        f"Camera {camera.id} provisioning exception after {max_retries} attempts: {e!s}",
                        extra={
                            "action": "mediamtx.startup.camera_exception_failed",
                            "camera_id": camera.id,
                            "camera_name": camera.name,
                            "error": str(e),
                        },
                        exc_info=True,
                    )

        return camera_result

    @staticmethod
    def _build_provision_config(
        camera: Camera, config: CameraConfig | None, db: Session = None
    ) -> dict[str, Any]:
        """Build provisioning configuration for a camera."""
        provision_config = {
            "source_url": camera.rtsp_url,
            "rtsp_transport": "tcp",  # Default transport
        }

        if config:
            # Use configuration values if available
            if config.rtsp_transport:
                provision_config["rtsp_transport"] = config.rtsp_transport

            # Recording configuration
            if config.recording_enabled:
                # Use user's custom path or effective base path from database/settings
                if config.recording_path:
                    recording_path = config.recording_path
                else:
                    base_path = get_effective_recordings_base_path(db)
                    recording_path = f"{base_path}/cam-{camera.id}/%Y/%m/%d/%H-%M-%S-%f"
                provision_config["recording"] = {
                    "enabled": True,
                    "path": recording_path,
                    "segment_seconds": config.recording_segment_seconds or 300,
                }
            elif config.recording_enabled is False:
                # Explicitly disabled - set recording to False
                provision_config["recording"] = {"enabled": False}
            # If recording_enabled is None, don't set recording - inherit from pathDefaults
        # No config at all - don't set recording, inherit from pathDefaults (which has record: True)

        return provision_config

    @staticmethod
    def get_startup_status() -> dict[str, Any]:
        """Get status of cameras and their provisioning state."""
        with SessionLocal() as db:
            cameras = MediaMtxStartupService._get_provisionable_cameras(db)

            status = {"total_cameras": len(cameras), "by_status": {}, "cameras": []}

            for camera, config in cameras:
                camera_info = {
                    "id": camera.id,
                    "name": camera.name,
                    "ip_address": camera.ip_address,
                    "status": camera.status,
                    "rtsp_url": camera.rtsp_url,
                    "has_config": config is not None,
                    "last_provisioned": config.last_provisioned_at.isoformat()
                    if config and config.last_provisioned_at
                    else None,
                }

                status["cameras"].append(camera_info)

                # Count by status
                camera_status = camera.status or "unknown"
                status["by_status"][camera_status] = (
                    status["by_status"].get(camera_status, 0) + 1
                )

            return status

    @staticmethod
    async def provision_camera_by_id(
        camera_id: int, force: bool = False
    ) -> dict[str, Any]:
        """Provision a specific camera by ID."""
        with SessionLocal() as db:
            camera = (
                db.query(Camera)
                .filter(Camera.id == camera_id, Camera.is_active == True)
                .first()
            )

            if not camera:
                return {"status": "error", "error": "Camera not found or inactive"}

            if not camera.rtsp_url:
                return {"status": "error", "error": "Camera has no RTSP URL configured"}

            config = (
                db.query(CameraConfig)
                .filter(CameraConfig.camera_id == camera_id)
                .first()
            )

            return await MediaMtxStartupService._provision_camera_with_retry(
                db, camera, config, max_retries=3, retry_delay=1
            )
