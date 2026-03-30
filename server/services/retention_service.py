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
Recording Retention Service

Manages automatic deletion of old recordings based on retention policy.
This service is the single source of truth for recording lifecycle.
MediaMTX never deletes recordings (recordDeleteAfter: 0s).
"""

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from core.database import SessionLocal
from core.logging_config import recording_logger
from models import Recording, SecuritySetting
from services.storage_service import get_effective_recordings_base_path

RETENTION_SETTINGS_KEY = "recordings_retention"


class RetentionService:
    """Service for managing recording retention and cleanup."""

    @staticmethod
    def _get_retention_settings(db: Session) -> dict[str, Any]:
        """Load retention settings from database."""
        row = (
            db.query(SecuritySetting)
            .filter(SecuritySetting.key == RETENTION_SETTINGS_KEY)
            .first()
        )
        default = {
            "retention_days": 30,
            "protect_flagged": True,
            "min_free_space_gb": None,
        }
        if not row or not row.json_value:
            return default

        try:
            data = json.loads(row.json_value)
            return {**default, **data}
        except Exception:
            return default

    @staticmethod
    def _get_disk_free_space_gb(path: str) -> float:
        """Get free disk space in GB for the given path."""
        try:
            stat = shutil.disk_usage(path)
            return stat.free / (1024**3)  # Convert bytes to GB
        except Exception as e:
            recording_logger.error(f"Failed to get disk space for {path}: {e}")
            return float("inf")  # Return large number to avoid cleanup on error

    @staticmethod
    def _delete_file_safely(file_path: Path) -> bool:
        """Delete a file safely, logging any errors."""
        try:
            if file_path.exists():
                file_path.unlink()
                recording_logger.info(f"Deleted recording file: {file_path}")
                return True
        except Exception as e:
            recording_logger.error(f"Failed to delete file {file_path}: {e}")
        return False

    @staticmethod
    def _delete_empty_directories(base_path: Path, file_path: Path):
        """Delete empty parent directories up to base_path."""
        try:
            parent = file_path.parent
            while parent != base_path and parent.exists():
                if not any(parent.iterdir()):
                    parent.rmdir()
                    recording_logger.debug(f"Removed empty directory: {parent}")
                    parent = parent.parent
                else:
                    break
        except Exception as e:
            recording_logger.error(f"Error cleaning up empty directories: {e}")

    @staticmethod
    def cleanup_old_recordings(db: Session = None) -> dict[str, Any]:
        """
        Clean up old recordings based on retention policy.

        Returns:
            Dict with cleanup statistics
        """
        close_db = False
        if db is None:
            db = SessionLocal()
            close_db = True

        try:
            # Load retention settings
            settings_dict = RetentionService._get_retention_settings(db)
            retention_days = settings_dict.get("retention_days", 30)
            protect_flagged = settings_dict.get("protect_flagged", True)
            min_free_space_gb = settings_dict.get("min_free_space_gb")

            recording_logger.info(
                f"Starting retention cleanup: retention_days={retention_days}, "
                f"protect_flagged={protect_flagged}, min_free_space_gb={min_free_space_gb}"
            )

            # If retention_days is 0, keep all recordings (unless min_free_space is triggered)
            if retention_days == 0 and not min_free_space_gb:
                recording_logger.info(
                    "Retention policy: Keep all recordings indefinitely"
                )
                return {"deleted_files": 0, "deleted_records": 0, "freed_space_mb": 0}

            # Get recordings base path
            base_path = Path(get_effective_recordings_base_path(db))
            if not base_path.exists():
                recording_logger.warning(
                    f"Recordings base path does not exist: {base_path}"
                )
                return {"deleted_files": 0, "deleted_records": 0, "freed_space_mb": 0}

            stats = {
                "deleted_files": 0,
                "deleted_records": 0,
                "freed_space_mb": 0,
                "errors": [],
            }

            # Calculate cutoff time for age-based retention
            cutoff_time = None
            if retention_days > 0:
                cutoff_time = datetime.now(UTC) - timedelta(days=retention_days)

            # Phase 1: Age-based cleanup
            if cutoff_time:
                recording_logger.info(
                    f"Phase 1: Deleting recordings older than {cutoff_time}"
                )
                stats_age = RetentionService._cleanup_by_age(
                    db, base_path, cutoff_time, protect_flagged
                )
                stats["deleted_files"] += stats_age["deleted_files"]
                stats["deleted_records"] += stats_age["deleted_records"]
                stats["freed_space_mb"] += stats_age["freed_space_mb"]

            # Phase 2: Free space cleanup (if needed)
            if min_free_space_gb:
                free_space_gb = RetentionService._get_disk_free_space_gb(str(base_path))
                recording_logger.info(
                    f"Current free space: {free_space_gb:.2f} GB, "
                    f"minimum required: {min_free_space_gb} GB"
                )

                if free_space_gb < min_free_space_gb:
                    recording_logger.info(
                        "Phase 2: Free space below threshold, deleting oldest recordings"
                    )
                    stats_space = RetentionService._cleanup_by_space(
                        db, base_path, min_free_space_gb, protect_flagged
                    )
                    stats["deleted_files"] += stats_space["deleted_files"]
                    stats["deleted_records"] += stats_space["deleted_records"]
                    stats["freed_space_mb"] += stats_space["freed_space_mb"]

            recording_logger.info(f"Retention cleanup completed: {stats}")
            return stats

        except Exception as e:
            recording_logger.error(
                f"Error during retention cleanup: {e}", exc_info=True
            )
            return {
                "error": str(e),
                "deleted_files": 0,
                "deleted_records": 0,
                "freed_space_mb": 0,
            }
        finally:
            if close_db:
                db.close()

    @staticmethod
    def _cleanup_by_age(
        db: Session, base_path: Path, cutoff_time: datetime, protect_flagged: bool
    ) -> dict[str, Any]:
        """Delete recordings older than cutoff_time."""
        stats = {"deleted_files": 0, "deleted_records": 0, "freed_space_mb": 0}

        # Walk through all recording files
        for file_path in base_path.rglob("*.mp4"):
            try:
                # Get file modification time
                mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=UTC)

                if mtime < cutoff_time:
                    file_size_mb = file_path.stat().st_size / (1024**2)

                    # Check if file should be protected
                    # Try to find corresponding DB record
                    rel_path = str(file_path.relative_to(base_path))
                    db_record = (
                        db.query(Recording)
                        .filter(
                            (Recording.file_path == str(file_path))
                            | (Recording.file_path.like(f"%{file_path.name}%"))
                        )
                        .first()
                    )

                    # Skip if flagged and protect_flagged is enabled
                    if protect_flagged and db_record:
                        # Note: Recording model doesn't have a flagged field yet
                        # If you add one later, check it here:
                        # if db_record.is_flagged:
                        #     continue
                        pass

                    # Delete the file
                    if RetentionService._delete_file_safely(file_path):
                        stats["deleted_files"] += 1
                        stats["freed_space_mb"] += file_size_mb

                        # Delete DB record if exists
                        if db_record:
                            db.delete(db_record)
                            stats["deleted_records"] += 1

                        # Clean up empty directories
                        RetentionService._delete_empty_directories(base_path, file_path)

            except Exception as e:
                recording_logger.error(f"Error processing file {file_path}: {e}")

        # Commit DB changes
        db.commit()
        return stats

    @staticmethod
    def _cleanup_by_space(
        db: Session, base_path: Path, min_free_space_gb: float, protect_flagged: bool
    ) -> dict[str, Any]:
        """Delete oldest recordings until free space threshold is met."""
        stats = {"deleted_files": 0, "deleted_records": 0, "freed_space_mb": 0}

        # Get all recording files sorted by modification time (oldest first)
        files: list[tuple[Path, float, float]] = []  # (path, mtime, size_mb)
        for file_path in base_path.rglob("*.mp4"):
            try:
                stat = file_path.stat()
                mtime = stat.st_mtime
                size_mb = stat.st_size / (1024**2)
                files.append((file_path, mtime, size_mb))
            except Exception as e:
                recording_logger.error(f"Error stat'ing file {file_path}: {e}")

        # Sort by modification time (oldest first)
        files.sort(key=lambda x: x[1])

        # Delete oldest files until we have enough free space
        for file_path, _, size_mb in files:
            try:
                # Check current free space
                free_space_gb = RetentionService._get_disk_free_space_gb(str(base_path))
                if free_space_gb >= min_free_space_gb:
                    break  # We have enough free space now

                # Check if file should be protected
                rel_path = str(file_path.relative_to(base_path))
                db_record = (
                    db.query(Recording)
                    .filter(
                        (Recording.file_path == str(file_path))
                        | (Recording.file_path.like(f"%{file_path.name}%"))
                    )
                    .first()
                )

                # Skip if flagged and protect_flagged is enabled
                if protect_flagged and db_record:
                    # Note: Add flagged check here if field is added
                    pass

                # Delete the file
                if RetentionService._delete_file_safely(file_path):
                    stats["deleted_files"] += 1
                    stats["freed_space_mb"] += size_mb

                    # Delete DB record if exists
                    if db_record:
                        db.delete(db_record)
                        stats["deleted_records"] += 1

                    # Clean up empty directories
                    RetentionService._delete_empty_directories(base_path, file_path)

            except Exception as e:
                recording_logger.error(f"Error deleting file {file_path}: {e}")

        # Commit DB changes
        db.commit()
        return stats


# Singleton instance
retention_service = RetentionService()
