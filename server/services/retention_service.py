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

            # Bounded-growth housekeeping (camera events, device registry,
            # remux cache) runs regardless of the recording retention policy.
            aux = RetentionService.cleanup_auxiliary(db, retention_days)

            # If retention_days is 0, keep all recordings (unless min_free_space is triggered)
            if retention_days == 0 and not min_free_space_gb:
                recording_logger.info(
                    "Retention policy: Keep all recordings indefinitely"
                )
                return {
                    "deleted_files": 0,
                    "deleted_records": 0,
                    "freed_space_mb": 0,
                    **aux,
                }

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

                # Quarantined footage ages out under the same policy.
                stats_orphans = RetentionService._cleanup_orphans_by_age(
                    base_path, cutoff_time
                )
                stats["deleted_files"] += stats_orphans["deleted_files"]
                stats["freed_space_mb"] += stats_orphans["freed_space_mb"]

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

            # Evidence JPEGs age out with the recordings.
            stats["deleted_evidence"] = RetentionService.cleanup_evidence(
                db, retention_days
            )

            stats.update(aux)
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

    # Growth caps for auxiliary stores. Age applies first; the row caps are a
    # backstop against event/scanner storms inside the retention window.
    CAMERA_EVENTS_MAX_ROWS = 200_000
    CAMERA_EVENTS_FALLBACK_DAYS = 90  # used when recordings are kept forever
    PENDING_DEVICE_MAX_AGE_DAYS = 30

    @staticmethod
    def cleanup_auxiliary(db: Session, retention_days: int) -> dict[str, Any]:
        """Prune the unbounded-growth side stores (best-effort, never raises).

        - ``camera_events``: drop rows older than the recording retention window
          (or ``CAMERA_EVENTS_FALLBACK_DAYS`` when recordings are kept forever),
          then enforce a hard row cap.
        - ``trusted_devices``: drop *pending* rows not seen for
          ``PENDING_DEVICE_MAX_AGE_DAYS`` (approved/blocked rows are kept —
          they encode admin decisions).
        - legacy ``.hevc_cache`` dir: removed entirely (H.265 remuxes are now
          served virtually from an in-RAM index — nothing is cached on disk).
        """
        stats: dict[str, Any] = {
            "deleted_camera_events": 0,
            "deleted_pending_devices": 0,
            "hevc_cache_removed": 0,
        }
        from models import CameraEvent, DeviceStatus, TrustedDevice

        try:
            days = retention_days if retention_days > 0 else (
                RetentionService.CAMERA_EVENTS_FALLBACK_DAYS
            )
            cutoff = datetime.now(UTC) - timedelta(days=days)
            n = (
                db.query(CameraEvent)
                .filter(CameraEvent.created_at < cutoff)
                .delete(synchronize_session=False)
            )
            # Hard cap: keep only the newest CAMERA_EVENTS_MAX_ROWS rows.
            total = db.query(CameraEvent).count()
            over = total - RetentionService.CAMERA_EVENTS_MAX_ROWS
            if over > 0:
                cap_id = (
                    db.query(CameraEvent.id)
                    .order_by(CameraEvent.id.desc())
                    .offset(RetentionService.CAMERA_EVENTS_MAX_ROWS)
                    .limit(1)
                    .scalar()
                )
                if cap_id is not None:
                    n += (
                        db.query(CameraEvent)
                        .filter(CameraEvent.id <= cap_id)
                        .delete(synchronize_session=False)
                    )
            db.commit()
            stats["deleted_camera_events"] = n
        except Exception as e:
            db.rollback()
            recording_logger.warning(f"camera_events prune failed: {e}")

        try:
            cutoff = datetime.now(UTC) - timedelta(
                days=RetentionService.PENDING_DEVICE_MAX_AGE_DAYS
            )
            n = (
                db.query(TrustedDevice)
                .filter(
                    TrustedDevice.status == DeviceStatus.pending,
                    TrustedDevice.last_seen < cutoff,
                )
                .delete(synchronize_session=False)
            )
            db.commit()
            stats["deleted_pending_devices"] = n
        except Exception as e:
            db.rollback()
            recording_logger.warning(f"trusted_devices prune failed: {e}")

        try:
            legacy = Path(get_effective_recordings_base_path(db)) / ".hevc_cache"
            if legacy.is_dir():
                shutil.rmtree(legacy, ignore_errors=True)
                stats["hevc_cache_removed"] = 1
                recording_logger.info(
                    "Removed legacy .hevc_cache dir (remux is now cache-free)"
                )
        except Exception as e:
            recording_logger.warning(f"legacy hevc cache removal failed: {e}")

        return stats

    # Batched deletion: bounds transaction size and lets the disk-usage check
    # run once per batch instead of once per file.
    DELETE_BATCH_SIZE = 500
    # Free-space hysteresis: once below the threshold, delete until we're this
    # far ABOVE it, so the pressure loop doesn't thrash at the boundary.
    FREE_SPACE_HYSTERESIS_GB = 5.0

    @staticmethod
    def _delete_rows_batch(
        db: Session, base_path: Path, rows: list, stats: dict[str, Any]
    ) -> None:
        """Delete a batch of Recording rows and their files (+ empty dirs)."""
        for row in rows:
            try:
                file_path = base_path / row.file_path
                size_mb = 0.0
                try:
                    size_mb = file_path.stat().st_size / (1024**2)
                except OSError:
                    pass
                if file_path.exists():
                    if RetentionService._delete_file_safely(file_path):
                        stats["deleted_files"] += 1
                        stats["freed_space_mb"] += size_mb
                        RetentionService._delete_empty_directories(
                            base_path, file_path
                        )
                db.delete(row)
                stats["deleted_records"] += 1
            except Exception as e:
                recording_logger.error(f"Error deleting recording {row.id}: {e}")
        db.commit()

    @staticmethod
    def _cleanup_by_age(
        db: Session, base_path: Path, cutoff_time: datetime, protect_flagged: bool
    ) -> dict[str, Any]:
        """Delete recordings older than cutoff_time.

        DB-driven: one indexed query per batch on ``start_time`` (the old
        version rglob'd the whole tree and ran an unindexed leading-wildcard
        LIKE per file). ``protect_flagged`` is enforced for real against the
        ``is_flagged`` column.
        """
        stats = {"deleted_files": 0, "deleted_records": 0, "freed_space_mb": 0}

        while True:
            q = db.query(Recording).filter(Recording.start_time < cutoff_time)
            if protect_flagged:
                q = q.filter(Recording.is_flagged == False)  # noqa: E712
            rows = (
                q.order_by(Recording.start_time.asc())
                .limit(RetentionService.DELETE_BATCH_SIZE)
                .all()
            )
            if not rows:
                break
            RetentionService._delete_rows_batch(db, base_path, rows, stats)
            if len(rows) < RetentionService.DELETE_BATCH_SIZE:
                break

        # Orphan sweep: on-disk files older than the cutoff with NO DB row
        # (pre-migration stragglers, files landed while the DB was down and
        # not yet reconciled). The filesystem walk only runs for this slow
        # path — never per-file DB queries.
        try:
            from services.recording_paths import (
                iter_recording_files,
                parse_recording_time,
            )

            known = {
                r[0]
                for r in db.query(Recording.file_path)
                .filter(Recording.start_time < cutoff_time + timedelta(days=1))
                .all()
            }
            from services.camera_identity import (
                camera_for_path_name,
                read_marker,
            )

            for cam_dir in base_path.iterdir():
                if not cam_dir.is_dir() or not cam_dir.name.startswith("cam-"):
                    continue
                # Only sweep dirs POSITIVELY owned by a live camera. An
                # ownerless dir (post-DB-wipe leftover) or a dir whose
                # identity marker belongs to a different camera must not be
                # aged away here — the reconciler quarantines those into
                # orphaned/, where they stay recoverable and age out under
                # the explicit orphan step instead.
                owner = camera_for_path_name(db, cam_dir.name)
                if owner is None:
                    continue
                marker, _corrupt = read_marker(cam_dir)
                if (
                    marker is not None
                    and owner.uuid
                    and marker.get("camera_uuid") != owner.uuid
                ):
                    continue
                for f in iter_recording_files(cam_dir):
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    ts = parse_recording_time(f, mtime=st.st_mtime)
                    if ts is None or ts >= cutoff_time:
                        continue
                    rel = str(f.relative_to(base_path)).replace("\\", "/")
                    if rel in known:
                        continue
                    if RetentionService._delete_file_safely(f):
                        stats["deleted_files"] += 1
                        stats["freed_space_mb"] += st.st_size / (1024**2)
                        RetentionService._delete_empty_directories(base_path, f)
        except Exception as e:
            recording_logger.warning(f"Orphan sweep failed: {e}")

        return stats

    @staticmethod
    def _cleanup_orphans_by_age(
        base_path: Path, cutoff_time: datetime
    ) -> dict[str, Any]:
        """Age out quarantined footage under ``orphaned/``.

        Same ``retention_days`` cutoff as live recordings (0 = keep forever —
        this method is only called with a real cutoff). This and the explicit
        admin DELETE endpoint are the ONLY code paths allowed to delete
        orphaned footage; the space-pressure pass deliberately never touches
        it. A fully-emptied quarantine tree (no .mp4 left) is removed
        entirely, dotfiles included.
        """
        stats = {"deleted_files": 0, "freed_space_mb": 0}
        try:
            from services.camera_identity import ORPHANED_DIR_NAME
            from services.recording_paths import (
                iter_recording_files,
                parse_recording_time,
            )

            orphan_root = base_path / ORPHANED_DIR_NAME
            if not orphan_root.is_dir():
                return stats
            for tree in orphan_root.iterdir():
                if not tree.is_dir():
                    continue
                for f in iter_recording_files(tree):
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    ts = parse_recording_time(f, mtime=st.st_mtime)
                    if ts is None or ts >= cutoff_time:
                        continue
                    if RetentionService._delete_file_safely(f):
                        stats["deleted_files"] += 1
                        stats["freed_space_mb"] += st.st_size / (1024**2)
                        RetentionService._delete_empty_directories(orphan_root, f)
                # Fully aged out -> drop the (now footage-free) tree.
                if next(iter(tree.rglob("*.mp4")), None) is None:
                    shutil.rmtree(tree, ignore_errors=True)
        except Exception as e:
            recording_logger.warning(f"Orphan aging failed: {e}")
        return stats

    @staticmethod
    def _cleanup_by_space(
        db: Session, base_path: Path, min_free_space_gb: float, protect_flagged: bool
    ) -> dict[str, Any]:
        """Delete oldest recordings until free space clears the threshold.

        DB-driven oldest-first batches; ``shutil.disk_usage`` runs once per
        BATCH (the old version called it per file after two full ``rglob``
        passes). Deletes until threshold + hysteresis so the 5-minute
        pressure loop doesn't thrash at the boundary.
        """
        stats = {"deleted_files": 0, "deleted_records": 0, "freed_space_mb": 0}
        target_gb = min_free_space_gb + RetentionService.FREE_SPACE_HYSTERESIS_GB

        while True:
            free_gb = RetentionService._get_disk_free_space_gb(str(base_path))
            if free_gb >= target_gb:
                break

            q = db.query(Recording)
            if protect_flagged:
                q = q.filter(Recording.is_flagged == False)  # noqa: E712
            rows = (
                q.order_by(Recording.start_time.asc())
                .limit(RetentionService.DELETE_BATCH_SIZE)
                .all()
            )
            if not rows:
                recording_logger.warning(
                    "Disk pressure: no more deletable recordings "
                    f"(free={free_gb:.1f}GB < target={target_gb:.1f}GB)"
                )
                break
            RetentionService._delete_rows_batch(db, base_path, rows, stats)

        return stats

    # Evidence JPEGs share the recordings volume under .evidence/ but the old
    # retention only globbed *.mp4 — they accumulated forever.
    @staticmethod
    def cleanup_evidence(db: Session, retention_days: int) -> int:
        """Delete .evidence/ JPEGs older than the retention window."""
        if retention_days <= 0:
            return 0
        deleted = 0
        try:
            evidence_dir = Path(get_effective_recordings_base_path(db)) / ".evidence"
            if not evidence_dir.is_dir():
                return 0
            cutoff = datetime.now(UTC).timestamp() - retention_days * 86400
            for f in evidence_dir.rglob("*.jpg"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                        deleted += 1
                except OSError:
                    continue
        except Exception as e:
            recording_logger.warning(f"Evidence purge failed: {e}")
        return deleted

    @staticmethod
    def check_disk_pressure(db: Session = None) -> dict[str, Any] | None:
        """Fast free-space check + oldest-first purge when below threshold.

        Called every few minutes (see main.py) — a near-full disk cannot wait
        for the daily sweep. Cheap when there's nothing to do: one settings
        read and one ``disk_usage`` call.
        """
        close_db = False
        if db is None:
            db = SessionLocal()
            close_db = True
        try:
            settings_dict = RetentionService._get_retention_settings(db)
            min_free = settings_dict.get("min_free_space_gb")
            if not min_free:
                return None
            base_path = Path(get_effective_recordings_base_path(db))
            if not base_path.exists():
                return None
            free_gb = RetentionService._get_disk_free_space_gb(str(base_path))
            if free_gb >= min_free:
                return None
            recording_logger.warning(
                f"Disk pressure: {free_gb:.1f}GB free < {min_free}GB threshold — "
                "purging oldest recordings"
            )
            return RetentionService._cleanup_by_space(
                db,
                base_path,
                min_free,
                settings_dict.get("protect_flagged", True),
            )
        except Exception as e:
            recording_logger.error(f"Disk pressure check failed: {e}", exc_info=True)
            return None
        finally:
            if close_db:
                db.close()


# Singleton instance
retention_service = RetentionService()
