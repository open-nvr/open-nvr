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

#: The free-space floor when the operator has not set one: 2 GB, or 2 % of
#: the recordings volume if that is more (80 GB on a 4 TB disk). Below it
#: core ROTATES: the oldest recordings go, oldest first, and recording never
#: stops. The old default was "off", and a shipped box with a small disk
#: sat at 10 GB free from an installer value it could never reach (Ventuno
#: Q, 2026-11-04): the purge deleted every recording and every evidence
#: photo, forever, and Search showed "no photo". 0 in the setting = off.
DEFAULT_MIN_FREE_GB = 2.0
DEFAULT_MIN_FREE_FRACTION = 0.02
#: A rotation deletes past the floor by this much so the pressure loop does
#: not thrash at the boundary: a quarter of the floor, at least 0.5 GB. It
#: used to be a flat 5 GB, which with a 2 GB floor meant the first rotation
#: deleted down to 7 GB free, nearly every recording on a small disk.
HYSTERESIS_FRACTION = 0.25
HYSTERESIS_MIN_GB = 0.5
#: Edge-triggered system event while rotation is on (data carries the floor,
#: the effective retention in hours, and whether the floor is reachable).
ROTATION_EVENT = "disk_rotation"


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
            "purge_orphaned_under_pressure": False,
        }
        if not row or not row.json_value:
            return default

        try:
            data = json.loads(row.json_value)
            return {**default, **data}
        except Exception:
            return default

    @staticmethod
    def _get_disk_free_space_gb(path: str) -> float | None:
        """Get free disk space in GB for the given path.

        Returns None when the disk can't be statted. Callers must treat None
        as "do nothing" (fail-safe) — never purge on bad data. The old
        float("inf") fail-open silently disabled space purging forever on a
        mount/permission error. The system monitor surfaces the failure as a
        ``disk_stat_error`` alert, so retention only needs to skip.
        """
        try:
            stat = shutil.disk_usage(path)
            return stat.free / (1024**3)  # Convert bytes to GB
        except Exception as e:
            recording_logger.error(f"Failed to get disk space for {path}: {e}")
            return None

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

            # If retention_days is 0 and the floor is explicitly off, keep all
            # recordings. (An unset floor is the automatic one, not off.)
            if retention_days == 0 and min_free_space_gb is not None and float(min_free_space_gb) <= 0:
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
            min_free_space_gb = RetentionService.effective_min_free_gb(settings_dict, base_path)

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
                if free_space_gb is None:
                    recording_logger.warning(
                        "Phase 2 skipped: disk usage unavailable (fail-safe)"
                    )
                else:
                    recording_logger.info(
                        f"Current free space: {free_space_gb:.2f} GB, "
                        f"minimum required: {min_free_space_gb} GB"
                    )

                    if free_space_gb < min_free_space_gb:
                        recording_logger.info(
                            "Phase 2: Free space below threshold, deleting oldest recordings"
                        )
                        stats_space = RetentionService._cleanup_by_space(
                            db,
                            base_path,
                            min_free_space_gb,
                            protect_flagged,
                            purge_orphaned=settings_dict.get(
                                "purge_orphaned_under_pressure", False
                            ),
                        )
                        stats["deleted_files"] += stats_space["deleted_files"]
                        stats["deleted_records"] += stats_space["deleted_records"]
                        stats["freed_space_mb"] += stats_space["freed_space_mb"]
                        RetentionService._note_rotation(db, base_path, min_free_space_gb, stats_space)
                    else:
                        RetentionService._clear_rotation_if_clear(
                            base_path, min_free_space_gb, free_space_gb)

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
    SYSTEM_EVENTS_MAX_ROWS = 50_000
    PENDING_DEVICE_MAX_AGE_DAYS = 30
    # The events store is deleted in batches so a first sweep over a year
    # of rows neither holds one giant transaction nor starves the ingest
    # path; the inbox gets the same age-then-cap treatment as camera_events.
    EVENTS_DELETE_BATCH = 5_000
    APP_ALERTS_MAX_ROWS = 100_000

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
            "deleted_system_events": 0,
            "deleted_pending_devices": 0,
            "hevc_cache_removed": 0,
            "deleted_events": 0,
            "deleted_app_alerts": 0,
        }
        from models import CameraEvent, DeviceStatus, SystemEvent, TrustedDevice

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

        # system_events: same age-then-cap pattern as camera_events, smaller
        # cap — host alerts are far lower volume.
        try:
            days = retention_days if retention_days > 0 else (
                RetentionService.CAMERA_EVENTS_FALLBACK_DAYS
            )
            cutoff = datetime.now(UTC) - timedelta(days=days)
            n = (
                db.query(SystemEvent)
                .filter(SystemEvent.created_at < cutoff)
                .delete(synchronize_session=False)
            )
            total = db.query(SystemEvent).count()
            over = total - RetentionService.SYSTEM_EVENTS_MAX_ROWS
            if over > 0:
                cap_id = (
                    db.query(SystemEvent.id)
                    .order_by(SystemEvent.id.desc())
                    .offset(RetentionService.SYSTEM_EVENTS_MAX_ROWS)
                    .limit(1)
                    .scalar()
                )
                if cap_id is not None:
                    n += (
                        db.query(SystemEvent)
                        .filter(SystemEvent.id <= cap_id)
                        .delete(synchronize_session=False)
                    )
            db.commit()
            stats["deleted_system_events"] = n
        except Exception as e:
            db.rollback()
            recording_logger.warning(f"system_events prune failed: {e}")

        # events store (visits + their sidecars). RFC-0001 promised "one
        # retention policy, tied to recordings"; cleanup_evidence delivered
        # half of it — the JPEGs aged out, the rows that pointed at them
        # never did, so `evidence_url` dangled and the table grew without
        # bound (a demo box wrote 31k rows in a day). Rows now age out on
        # the same window as their evidence, EVENTS_RETENTION_DAYS
        # overriding when the operator wants history and video to differ.
        # Sidecars cascade on Postgres; deleted explicitly here so SQLite
        # (tests, laptops) agrees.
        try:
            from core.config import settings
            from models import (
                EventEmbedding, EventText, TimelineEvent, VisitDescriptor,
            )
            ev_days = int(getattr(settings, "events_retention_days", 0) or 0) or retention_days
            n = 0
            if ev_days > 0:
                cutoff = datetime.now(UTC) - timedelta(days=ev_days)
                while True:
                    ids = [
                        r[0] for r in db.query(TimelineEvent.id)
                        .filter(TimelineEvent.started_at < cutoff)
                        .order_by(TimelineEvent.id)
                        .limit(RetentionService.EVENTS_DELETE_BATCH)
                        .all()
                    ]
                    if not ids:
                        break
                    for side in (EventText, EventEmbedding, VisitDescriptor):
                        db.query(side).filter(side.event_id.in_(ids)).delete(
                            synchronize_session=False)
                    n += db.query(TimelineEvent).filter(
                        TimelineEvent.id.in_(ids)).delete(synchronize_session=False)
                    db.commit()
            stats["deleted_events"] = n
        except Exception as e:
            db.rollback()
            recording_logger.warning(f"events prune failed: {e}")
        # app_alerts (the operator inbox): age, then a hard cap.
        try:
            from core.config import settings
            from models import AppAlert
            days = int(getattr(settings, "alerts_inbox_retention_days", 90) or 0)
            n = 0
            if days > 0:
                cutoff = datetime.now(UTC) - timedelta(days=days)
                n = (
                    db.query(AppAlert)
                    .filter(AppAlert.fired_at < cutoff)
                    .delete(synchronize_session=False)
                )
            total = db.query(AppAlert).count()
            if total > RetentionService.APP_ALERTS_MAX_ROWS:
                cap_id = (
                    db.query(AppAlert.id)
                    .order_by(AppAlert.id.desc())
                    .offset(RetentionService.APP_ALERTS_MAX_ROWS)
                    .limit(1)
                    .scalar()
                )
                if cap_id is not None:
                    n += (
                        db.query(AppAlert)
                        .filter(AppAlert.id <= cap_id)
                        .delete(synchronize_session=False)
                    )
            db.commit()
            stats["deleted_app_alerts"] = n
        except Exception as e:
            db.rollback()
            recording_logger.warning(f"app_alerts prune failed: {e}")
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
    @staticmethod
    def effective_min_free_gb(settings_dict: dict[str, Any], base_path: Path | str) -> float | None:
        """The floor in force: the operator's value, the automatic one when
        unset, None when explicitly 0 (off)."""
        raw = settings_dict.get("min_free_space_gb")
        if raw is not None:
            try:
                val = float(raw)
            except (TypeError, ValueError):
                val = 0.0
            return val if val > 0 else None
        total_gb = RetentionService._get_disk_total_gb(str(base_path))
        if total_gb is None:
            return DEFAULT_MIN_FREE_GB
        return max(DEFAULT_MIN_FREE_GB, round(total_gb * DEFAULT_MIN_FREE_FRACTION, 1))

    @staticmethod
    def hysteresis_gb(min_free_gb: float) -> float:
        return max(HYSTERESIS_MIN_GB, float(min_free_gb) * HYSTERESIS_FRACTION)

    @staticmethod
    def _get_disk_total_gb(path: str) -> float | None:
        try:
            return shutil.disk_usage(path).total / (1024 ** 3)
        except OSError:
            return None

    @staticmethod
    def _delete_rows_batch(
        db: Session, base_path: Path, rows: list, stats: dict[str, Any],
        stop_when=None,
    ) -> None:
        """Delete a batch of Recording rows and their files (+ empty dirs).

        ``stop_when``, when given, is asked after every file whether the
        goal is met (a ``disk_usage`` call, microseconds): a space rotation
        then stops at its target inside the batch instead of taking the
        whole batch, which with 500 rows is hours of footage."""
        for row in rows:
            if stop_when is not None and stop_when():
                break
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
            for tree in list(orphan_root.iterdir()):
                if not tree.is_dir():
                    continue
                # Materialize BEFORE deleting: iter_recording_files is a
                # lazy walk, and _delete_empty_directories removes the
                # emptied parents mid-loop — resuming the generator on a
                # deleted directory raises FileNotFoundError, aborting the
                # whole pass with the tree still on disk (the bug this
                # method's own test kept catching intermittently).
                for f in list(iter_recording_files(tree)):
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

    # Backstop against a purge invocation running unbounded (e.g. constant
    # ingest faster than deletion). 200 batches x 500 rows is far above any
    # single pass's legitimate work.
    MAX_SPACE_BATCHES = 200

    @staticmethod
    def _cleanup_by_space(
        db: Session,
        base_path: Path,
        min_free_space_gb: float,
        protect_flagged: bool,
        purge_orphaned: bool = False,
    ) -> dict[str, Any]:
        """Delete oldest recordings until free space clears the threshold.

        DB-driven oldest-first batches; ``shutil.disk_usage`` runs once per
        BATCH (the old version called it per file after two full ``rglob``
        passes). Deletes until threshold + hysteresis (a quarter of the
        floor, at least 0.5 GB) so the pressure loop doesn't thrash at the
        boundary without wiping the archive in one pass.

        When indexed recordings run out, falls back to non-indexed consumers
        oldest-first: filesystem-orphan clips, then (only with
        ``purge_orphaned``, an explicit operator opt-in) the quarantined
        ``orphaned/`` tree. Evidence photos are never deleted for space.
        Still below target after all that -> a critical
        ``disk_purge_exhausted`` system event that says whether the floor is
        reachable at all: the disk WILL fill and MediaMTX will stop writing.
        """
        stats = {"deleted_files": 0, "deleted_records": 0, "freed_space_mb": 0}
        target_gb = min_free_space_gb + RetentionService.hysteresis_gb(min_free_space_gb)
        exhausted_reason: str | None = None

        for _ in range(RetentionService.MAX_SPACE_BATCHES):
            free_gb = RetentionService._get_disk_free_space_gb(str(base_path))
            if free_gb is None:
                # Fail-safe: no reading, no deleting.
                return stats
            if free_gb >= target_gb:
                RetentionService._set_purge_exhausted(False, free_gb, target_gb)
                return stats

            q = db.query(Recording)
            if protect_flagged:
                q = q.filter(Recording.is_flagged == False)  # noqa: E712
            rows = (
                q.order_by(Recording.start_time.asc())
                .limit(RetentionService.DELETE_BATCH_SIZE)
                .all()
            )
            if not rows:
                exhausted_reason = "no more deletable recordings"
                break
            files_before = stats["deleted_files"]

            def _cleared() -> bool:
                f = RetentionService._get_disk_free_space_gb(str(base_path))
                return f is not None and f >= target_gb

            RetentionService._delete_rows_batch(db, base_path, rows, stats, stop_when=_cleared)
            if stats["deleted_files"] == files_before:
                # Rows deleted but zero files freed: pure DB/FS drift. Keeping
                # going would spin-delete the whole recordings index without
                # freeing a byte — reconciling drifted rows is the
                # reconciler's job, not the purge's.
                exhausted_reason = (
                    f"batch of {len(rows)} rows freed no files (index/disk drift)"
                )
                break
        else:
            exhausted_reason = (
                f"batch cap reached ({RetentionService.MAX_SPACE_BATCHES})"
            )

        # Indexed recordings couldn't clear the target — try the non-indexed
        # consumers the age path knows about, oldest-first.
        free_gb = RetentionService._get_disk_free_space_gb(str(base_path))
        if free_gb is not None and free_gb < target_gb:
            RetentionService._purge_unindexed_by_space(
                db, base_path, target_gb, purge_orphaned, stats
            )
            free_gb = RetentionService._get_disk_free_space_gb(str(base_path))

        if free_gb is not None and free_gb < target_gb:
            orphaned_gb = RetentionService._orphaned_size_gb(base_path)
            quarantine_note = (
                f"; {orphaned_gb:.1f} GB held in orphaned/ quarantine"
                f" (purge_orphaned_under_pressure is off)"
                if orphaned_gb and not purge_orphaned
                else ""
            )
            # Is the floor reachable at all? With every indexed recording
            # gone the disk would have free + recordings. When even that is
            # under the floor, the space is taken by something else on the
            # volume and no amount of rotation fixes it: say so, and say
            # how much, instead of grinding on every minute.
            recordings_gb = RetentionService._indexed_recordings_gb(db)
            unreachable = (free_gb + recordings_gb) < min_free_space_gb
            cause = (
                f"; even with every recording deleted the disk would have "
                f"{free_gb + recordings_gb:.1f} GB free: the space is taken by other "
                f"data on this volume, not by recordings"
                if unreachable else ""
            )
            desc = (
                f"Disk purge exhausted: {free_gb:.1f} GB free < target "
                f"{target_gb:.1f} GB ({exhausted_reason or 'nothing left to delete'})"
                f"{quarantine_note}{cause}"
            )
            recording_logger.warning(desc)
            RetentionService._set_purge_exhausted(
                True, free_gb, target_gb, description=desc
            )
            stats["exhausted"] = True
            stats["unreachable"] = unreachable
        elif free_gb is not None:
            RetentionService._set_purge_exhausted(False, free_gb, target_gb)

        return stats

    @staticmethod
    def _indexed_recordings_gb(db: Session) -> float:
        """Size of every indexed recording, from the index (file_size)."""
        try:
            from sqlalchemy import func as _f

            total = db.query(_f.coalesce(_f.sum(Recording.file_size), 0.0)).scalar() or 0.0
            return float(total) / (1024 ** 3)
        except Exception:  # noqa: BLE001 - a size estimate must never fail the purge
            return 0.0

    @staticmethod
    def rotation_state(db: Session, base_path: Path, min_free_gb: float) -> dict[str, Any]:
        """What rotation means right now: the floor, the oldest recording
        still held (the effective retention), and whether the floor can be
        reached at all."""
        oldest = (
            db.query(Recording.start_time)
            .order_by(Recording.start_time.asc())
            .first()
        )
        oldest_at = oldest[0] if oldest else None
        if oldest_at is not None and oldest_at.tzinfo is None:
            oldest_at = oldest_at.replace(tzinfo=UTC)
        hours = (
            round((datetime.now(UTC) - oldest_at).total_seconds() / 3600, 1)
            if oldest_at else None
        )
        free_gb = RetentionService._get_disk_free_space_gb(str(base_path))
        recordings_gb = RetentionService._indexed_recordings_gb(db)
        return {
            "min_free_gb": round(float(min_free_gb), 2),
            "free_gb": round(free_gb, 2) if free_gb is not None else None,
            "recordings_gb": round(recordings_gb, 2),
            "oldest_recording_at": oldest_at.isoformat() if oldest_at else None,
            "effective_retention_hours": hours,
            "unreachable": bool(free_gb is not None and (free_gb + recordings_gb) < float(min_free_gb)),
        }

    @staticmethod
    def _note_rotation(db: Session, base_path: Path, min_free_gb: float, stats: dict[str, Any]) -> None:
        """Rotation is on: raise (or refresh) the edge-triggered event the
        dashboard reads. Best-effort."""
        try:
            from services.system_events import record_system_event_edge

            state = RetentionService.rotation_state(db, base_path, min_free_gb)
            hours = state["effective_retention_hours"]
            if state["unreachable"]:
                desc = (
                    f"Disk full: the {state['min_free_gb']:g} GB headroom cannot be kept even with "
                    f"every recording deleted ({state['free_gb']} GB free, {state['recordings_gb']} GB "
                    f"of recordings); the space is taken by other data on this volume"
                )
            else:
                desc = (
                    f"Disk full: rotating recordings to keep {state['min_free_gb']:g} GB free; "
                    f"footage is kept for about {hours:g} hours instead of the configured retention"
                    if hours is not None else
                    f"Disk full: rotating recordings to keep {state['min_free_gb']:g} GB free"
                )
            record_system_event_edge(
                event_type=ROTATION_EVENT, active=True, severity="warning",
                description=desc, data={**state, "deleted_files": stats.get("deleted_files", 0)},
            )
        except Exception as e:  # noqa: BLE001
            recording_logger.warning(f"{ROTATION_EVENT} event failed: {e}")

    @staticmethod
    def _clear_rotation_if_clear(base_path: Path, min_free_gb: float, free_gb: float) -> None:
        """Free space is back above the floor plus its margin: rotation is
        over. Edge-triggered, so this is a no-op while nothing changes."""
        if free_gb < float(min_free_gb) + RetentionService.hysteresis_gb(min_free_gb):
            return
        try:
            from services.system_events import record_system_event_edge

            record_system_event_edge(
                event_type=ROTATION_EVENT, active=False, severity="warning",
                description=f"Disk rotation over: {free_gb:.1f} GB free",
                data={"min_free_gb": round(float(min_free_gb), 2), "free_gb": round(free_gb, 2)},
            )
        except Exception as e:  # noqa: BLE001
            recording_logger.warning(f"{ROTATION_EVENT} clear failed: {e}")

    @staticmethod
    def _set_purge_exhausted(
        active: bool,
        free_gb: float,
        target_gb: float,
        description: str | None = None,
    ) -> None:
        """Edge-triggered disk_purge_exhausted alert (critical: recording WILL
        stop once the disk fills). Best-effort — retention never fails on it."""
        try:
            from services.system_events import record_system_event_edge

            record_system_event_edge(
                event_type="disk_purge_exhausted",
                active=active,
                severity="critical",
                description=description
                or f"Disk purge recovered: {free_gb:.1f} GB free",
                data={"free_gb": round(free_gb, 1), "target_gb": target_gb},
            )
        except Exception as e:
            recording_logger.warning(f"disk_purge_exhausted event failed: {e}")

    @staticmethod
    def _purge_unindexed_by_space(
        db: Session,
        base_path: Path,
        target_gb: float,
        purge_orphaned: bool,
        stats: dict[str, Any],
    ) -> None:
        """Free space from files the recordings index doesn't cover.

        Order: filesystem-orphan clips in camera dirs (no DB row), then —
        opt-in only — quarantined orphaned/ clips. Each group is deleted
        oldest-first until the target clears.

        Evidence JPEGs are NOT a group. They are about 50 KB each, so
        deleting thousands frees nothing that matters, and each one is the
        photo of an event: on the Ventuno Q (2026-11-04) a floor the disk
        could never reach purged every photo and Search showed "no photo"
        for every visit. Evidence ages out with the events, never for space.
        """
        try:
            from services.camera_identity import ORPHANED_DIR_NAME
            from services.recording_paths import iter_recording_files

            known = {r[0] for r in db.query(Recording.file_path).all()}
            groups: list[list[Path]] = []

            fs_orphans: list[Path] = []
            for cam_dir in base_path.iterdir():
                if not cam_dir.is_dir() or not cam_dir.name.startswith("cam-"):
                    continue
                for f in iter_recording_files(cam_dir):
                    rel = str(f.relative_to(base_path)).replace("\\", "/")
                    if rel not in known:
                        fs_orphans.append(f)
            groups.append(fs_orphans)

            if purge_orphaned:
                orphan_root = base_path / ORPHANED_DIR_NAME
                if orphan_root.is_dir():
                    groups.append(list(iter_recording_files(orphan_root)))

            for files in groups:
                if not files:
                    continue

                def mtime(p: Path) -> float:
                    try:
                        return p.stat().st_mtime
                    except OSError:
                        return 0.0

                files.sort(key=mtime)
                for i, f in enumerate(files):
                    # Re-check the target once per pseudo-batch, not per file.
                    if i % RetentionService.DELETE_BATCH_SIZE == 0:
                        free = RetentionService._get_disk_free_space_gb(
                            str(base_path)
                        )
                        if free is None or free >= target_gb:
                            return
                    try:
                        size_mb = f.stat().st_size / (1024**2)
                    except OSError:
                        size_mb = 0.0
                    if RetentionService._delete_file_safely(f):
                        stats["deleted_files"] += 1
                        stats["freed_space_mb"] += size_mb
                        RetentionService._delete_empty_directories(base_path, f)
        except Exception as e:
            recording_logger.warning(f"Unindexed space purge failed: {e}")

    @staticmethod
    def _orphaned_size_gb(base_path: Path) -> float:
        """Total size of the orphaned/ quarantine tree (best-effort)."""
        try:
            from services.camera_identity import ORPHANED_DIR_NAME

            orphan_root = base_path / ORPHANED_DIR_NAME
            if not orphan_root.is_dir():
                return 0.0
            total = 0
            for f in orphan_root.rglob("*"):
                try:
                    if f.is_file():
                        total += f.stat().st_size
                except OSError:
                    continue
            return total / (1024**3)
        except Exception:
            return 0.0

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
            base_path = Path(get_effective_recordings_base_path(db))
            if not base_path.exists():
                return None
            min_free = RetentionService.effective_min_free_gb(settings_dict, base_path)
            if not min_free:
                return None
            free_gb = RetentionService._get_disk_free_space_gb(str(base_path))
            if free_gb is None:
                # Fail-safe: unknown disk state, don't purge. The system
                # monitor raises disk_stat_error for visibility.
                return None
            if free_gb >= min_free:
                RetentionService._clear_rotation_if_clear(base_path, min_free, free_gb)
                return None
            recording_logger.warning(
                f"Disk pressure: {free_gb:.1f}GB free < {min_free}GB threshold — "
                "purging oldest recordings"
            )
            stats = RetentionService._cleanup_by_space(
                db,
                base_path,
                min_free,
                settings_dict.get("protect_flagged", True),
                purge_orphaned=settings_dict.get(
                    "purge_orphaned_under_pressure", False
                ),
            )
            RetentionService._note_rotation(db, base_path, min_free, stats)
            # History row for the purge itself (discrete occurrence, not an
            # active/inactive pair). The async loop in main.py publishes the
            # live bus copy — this thread only persists.
            try:
                from services.system_events import record_system_event

                freed_gb = stats.get("freed_space_mb", 0) / 1024
                record_system_event(
                    db,
                    event_type="disk_pressure_purge",
                    state=None,
                    severity="warning",
                    description=(
                        f"Disk pressure purge: deleted "
                        f"{stats.get('deleted_files', 0)} files "
                        f"({freed_gb:.1f} GB) at {free_gb:.1f} GB free"
                    ),
                    data={"free_gb_before": round(free_gb, 1), **stats},
                )
            except Exception as e:
                recording_logger.warning(f"disk_pressure_purge event failed: {e}")
            return stats
        except Exception as e:
            recording_logger.error(f"Disk pressure check failed: {e}", exc_info=True)
            return None
        finally:
            if close_db:
                db.close()


# Singleton instance
retention_service = RetentionService()
