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
Storage Service

Provides file system utilities for recordings without FFmpeg recording functionality.
MediaMTX handles recording via webhooks - this service only provides file listing and storage configuration.
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy.orm import Session

from core.config import settings
from core.database import SessionLocal
from models import SecuritySetting

SETTINGS_KEY_STORAGE = "recordings_storage"

# Logger for storage operations
storage_logger = logging.getLogger(__name__)


@dataclass
class StorageConfig:
    recordings_base_path: str
    segment_seconds: int
    filename_template: str
    active_mount_path: str | None = None


def _load_storage_config(db: Session) -> StorageConfig:
    """Load storage configuration from database."""
    row: SecuritySetting | None = (
        db.query(SecuritySetting)
        .filter(SecuritySetting.key == SETTINGS_KEY_STORAGE)
        .first()
    )
    default = {
        "recordings_base_path": None,  # None means use settings.recordings_base_path
        "segment_seconds": 60,
        "filename_template": "%camera/%Y/%m/%d/%H-%M-%S.mp4",
        "devices": [],
        "active_device_id": None,
    }
    if not row or not row.json_value:
        data = default
    else:
        try:
            data = {**default, **json.loads(row.json_value)}
        except Exception:
            data = default

    # Use settings.recordings_base_path as fallback when recordings_base_path is None, empty, or "recordings" (old default)
    db_recordings_base_path = data.get("recordings_base_path") or data.get(
        "root_path"
    )  # Also check old field name
    if not db_recordings_base_path or db_recordings_base_path == "recordings":
        recordings_base_path = settings.recordings_base_path
    else:
        recordings_base_path = str(db_recordings_base_path)
    # If there is an active device, use its mount_path as the effective root
    devices = data.get("devices") or []
    active_id = data.get("active_device_id")
    active_mount: str | None = None
    for d in devices:
        if d.get("id") == active_id and d.get("enabled", True):
            active_mount = d.get("mount_path")
            break

    segment_seconds = int(data.get("segment_seconds") or 60)
    filename_template = str(
        data.get("filename_template") or "%camera/%Y/%m/%d/%H-%M-%S.mp4"
    )

    return StorageConfig(
        recordings_base_path=recordings_base_path,
        segment_seconds=segment_seconds,
        filename_template=filename_template,
        active_mount_path=active_mount,
    )


def _effective_root(cfg: StorageConfig) -> Path:
    """Get the effective root directory for recordings."""
    root = Path(cfg.active_mount_path or cfg.recordings_base_path).expanduser()
    return root


def is_recording_path_configured(db: Session = None) -> bool:
    """
    Check if recording path is configured (either in database or via default).
    
    Returns:
        True if path is available (from DB or default), False otherwise
    """
    path = get_effective_recordings_base_path(db)
    return path is not None and path != ""


def get_effective_recordings_base_path(db: Session = None) -> str:
    """
    Get the effective recordings base path with auto-creation.

    Priority:
    1. User-configured path from database (recordings_storage setting)
    2. settings.recordings_base_path (auto-detected default)

    Auto-creates the directory if it doesn't exist.

    Args:
        db: Database session (optional)
        
    Returns:
        Recording path (never None)
    """
    from pathlib import Path
    
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    try:
        # Try to get from database first
        row = (
            db.query(SecuritySetting)
            .filter(SecuritySetting.key == SETTINGS_KEY_STORAGE)
            .first()
        )
        if row and row.json_value:
            try:
                data = json.loads(row.json_value)
                db_path = data.get("recordings_base_path") or data.get("root_path")
                if db_path and db_path != "recordings":
                    path = str(db_path)
                    # Auto-create directory
                    try:
                        Path(path).mkdir(parents=True, exist_ok=True)
                    except Exception as e:
                        storage_logger.warning(f"Failed to create recording directory {path}: {e}")
                    return path
            except Exception:
                pass
        
        # Fallback to auto-detected default
        path = settings.recordings_base_path
        
        # Auto-create directory
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
            storage_logger.info(f"Using default recording path: {path}")
        except Exception as e:
            storage_logger.warning(f"Failed to create recording directory {path}: {e}")
        
        return path
    finally:
        if close_db:
            db.close()


class StorageService:
    """Service for managing recording file storage and listing."""

    def list_recordings(
        self,
        db: Session,
        camera_id: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List recorded files from the storage directory."""
        store = _load_storage_config(db)
        root = _effective_root(store)

        items: list[dict[str, Any]] = []

        def _match_time(p: Path) -> datetime | None:
            """Extract timestamp from file path pattern.

            Handles two formats:
            - Old: cam-XX/YYYY/MM/DD/HH-MM-SS-ffffff.mp4
            - New: cam-XX/YYYY/MM/DD/HH-MM-SS-ffffff/cam-XX.mp4
            """
            try:
                parts = p.parts
                if len(parts) < 4:
                    return None

                # Check if this is the new format (file inside timestamp folder)
                parent_name = parts[-2]
                filename = parts[-1].split(".")[0]

                # New format: parent folder is timestamp (HH-MM-SS-ffffff)
                if "-" in parent_name and len(parent_name.split("-")) >= 3:
                    time_parts = parent_name.split("-")
                    if len(time_parts) >= 3 and time_parts[0].isdigit():
                        # New format detected
                        yyyy = parts[-5]
                        mm = parts[-4]
                        dd = parts[-3]
                        hh, mi, ss = time_parts[0], time_parts[1], time_parts[2]
                        dt = datetime(
                            int(yyyy),
                            int(mm),
                            int(dd),
                            int(hh),
                            int(mi),
                            int(ss),
                            tzinfo=UTC,
                        )
                        return dt

                # Old format: filename is timestamp (HH-MM-SS-ffffff.mp4)
                yyyy = parts[-4]
                mm = parts[-3]
                dd = parts[-2]
                hms = filename

                if "-" in hms:
                    time_parts = hms.split("-")
                    if len(time_parts) >= 3:
                        hh, mi, ss = time_parts[0], time_parts[1], time_parts[2]
                        dt = datetime(
                            int(yyyy),
                            int(mm),
                            int(dd),
                            int(hh),
                            int(mi),
                            int(ss),
                            tzinfo=UTC,
                        )
                        return dt

                return None
            except Exception:
                return None

        cam_dirs: list[Path] = []
        if camera_id is not None:
            cam_dirs = [root / f"cam-{int(camera_id)}"]
        else:
            # List all cam-* dirs
            if root.exists():
                for d in root.iterdir():
                    if d.is_dir() and d.name.startswith("cam-"):
                        cam_dirs.append(d)

        for cdir in cam_dirs:
            if not cdir.exists():
                continue
            # Walk year/month/day structure
            for year in sorted([p for p in cdir.glob("*/") if p.is_dir()]):
                for month in sorted([p for p in year.glob("*/") if p.is_dir()]):
                    for day in sorted([p for p in month.glob("*/") if p.is_dir()]):
                        # Find MP4 files - check both direct files and files in timestamp subfolders
                        all_files = list(day.glob("*.mp4")) + list(day.glob("*/*.mp4"))
                        for f in sorted(all_files, key=lambda x: str(x)):
                            ts = _match_time(f)
                            if start and (not ts or ts < start):
                                continue
                            if end and (not ts or ts > end):
                                continue
                            try:
                                st = f.stat()
                                size = st.st_size
                                # Skip zero-byte files (failed/in-progress)
                                if not size:
                                    continue
                            except Exception:
                                size = None

                            rel = os.path.relpath(f, root)
                            rel_posix = rel.replace("\\", "/")
                            items.append(
                                {
                                    "camera": cdir.name,
                                    "relpath": rel_posix,
                                    "size": size,
                                    "start_time": ts.isoformat() if ts else None,
                                    "url": f"{settings.api_prefix}/recordings/raw?rel={quote(rel_posix)}",
                                }
                            )

        # Sort by time desc
        items.sort(key=lambda x: x.get("start_time") or "", reverse=True)
        total = len(items)
        items = items[offset : offset + limit]
        return {"items": items, "total": total}

    def get_storage_info(self, db: Session) -> dict[str, Any]:
        """Get storage configuration and disk usage information."""
        store = _load_storage_config(db)
        root = _effective_root(store)

        info = {
            "root_path": str(root),
            "exists": root.exists(),
            "segment_seconds": store.segment_seconds,
        }

        # Get disk usage if possible
        try:
            if root.exists():
                total, used, free = (
                    os.statvfs(str(root)) if hasattr(os, "statvfs") else (0, 0, 0)
                )
                if total == 0:  # Windows fallback
                    import shutil

                    total, used, free = shutil.disk_usage(str(root))
                info.update(
                    {
                        "disk_total": total,
                        "disk_used": used,
                        "disk_free": free,
                    }
                )
        except Exception as e:
            info["disk_error"] = str(e)

        return info


# Singleton service instance
storage_service = StorageService()
