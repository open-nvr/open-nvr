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
import posixpath
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
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


class RecordingPathError(ValueError):
    """Raised when a recording-relative path escapes its configured base.

    V-005 (Zenodo 17261761 §3.4 data integrity / customer-controlled storage):
    any path operation that originates from user input (URL ?rel= parameter,
    DB-stored relpath, etc.) must resolve to a real file inside the configured
    recordings base. Symlinks that point outside the base are rejected.
    """


def _resolve_strict(path: Path) -> Path:
    """Resolve symlinks and ``..`` with strict semantics where the file may not
    yet exist (during write-back of a recording segment).

    We resolve the closest existing ancestor and then re-join the unresolved
    tail; this still catches symlinks because every ancestor in the chain is
    realpath-ed.
    """
    p = Path(path)
    try:
        return p.resolve(strict=True)
    except FileNotFoundError:
        # Walk up to the closest ancestor that does exist, resolve it, then
        # re-attach the unresolved tail.
        cur = p
        tail: list[str] = []
        while True:
            parent = cur.parent
            tail.insert(0, cur.name)
            if parent == cur:
                # Reached filesystem root without finding an existing ancestor.
                return p.resolve()
            try:
                resolved_parent = parent.resolve(strict=True)
                return resolved_parent.joinpath(*tail)
            except FileNotFoundError:
                cur = parent


def resolve_under_root(root: Path, rel: str | Path) -> Path:
    """V-005: validate that ``rel`` resolves to a file under ``root``.

    Use this everywhere a request-supplied or DB-supplied relative path is
    about to become a filesystem operation. Rejects:

    * absolute paths in ``rel`` (they would silently override ``root``);
    * paths containing ``..`` segments that escape the base after resolution;
    * symlinks whose target lies outside the base.

    Returns the canonical absolute path on success; raises
    :class:`RecordingPathError` on any rejection.
    """
    if rel is None or (isinstance(rel, str) and not rel.strip()):
        raise RecordingPathError("recording path is empty")
    rel_path = Path(rel)
    # An empty Path() stringifies to "." which would otherwise silently resolve
    # to the base itself — refuse that explicitly so callers never get a
    # directory handle back when they asked for a file.
    if not rel_path.parts or str(rel_path) in (".", ""):
        raise RecordingPathError("recording path is empty")
    if rel_path.is_absolute() or (
        # On Windows, drive-relative paths like "C:foo" are also absolute-ish.
        len(str(rel_path)) >= 2 and str(rel_path)[1] == ":"
    ):
        raise RecordingPathError(
            f"recording path must be relative to the recordings base, got "
            f"{str(rel_path)!r}"
        )
    # Reject "..", ".", and empty components defensively before we join, in
    # addition to the resolve-based containment check below.
    for part in rel_path.parts:
        if part in ("..", "."):
            raise RecordingPathError(
                f"recording path contains a forbidden component "
                f"({part!r}) in {str(rel_path)!r}"
            )

    base_resolved = _resolve_strict(Path(root).expanduser())
    candidate = _resolve_strict(base_resolved / rel_path)

    try:
        # Python 3.9+: Path.is_relative_to. We require the candidate to be at
        # or below the base after symlink resolution.
        if not candidate.is_relative_to(base_resolved):
            raise RecordingPathError(
                f"recording path resolves outside the configured base: "
                f"base={base_resolved} candidate={candidate}"
            )
    except AttributeError:  # pragma: no cover  (python < 3.9 fallback)
        try:
            candidate.relative_to(base_resolved)
        except ValueError as exc:
            raise RecordingPathError(
                f"recording path resolves outside the configured base: "
                f"base={base_resolved} candidate={candidate}"
            ) from exc
    return candidate


def safe_recording_path(rel: str | Path, db: Session | None = None) -> Path:
    """V-005 convenience wrapper: resolve ``rel`` against the effective
    recordings base configured for the current deployment.

    Caller is responsible for the DB session lifetime when one is passed in.
    """
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    try:
        cfg = _load_storage_config(db)
        root = _effective_root(cfg)
        return resolve_under_root(root, rel)
    finally:
        if close_db:
            db.close()


def is_recording_path_configured(db: Session = None) -> bool:
    """
    Check if recording path is configured (either in database or via default).
    
    Returns:
        True if path is available (from DB or default), False otherwise
    """
    path = get_effective_recordings_base_path(db)
    return path is not None and path != ""


def recordings_mount() -> dict[str, Any]:
    """Where recordings reach the host disk, when compose bind-mounts them.

    Under Docker, compose mounts the host's ``RECORDINGS_PATH`` at
    ``/app/recordings`` in core and MediaMTX and passes the host side in as
    ``RECORDINGS_HOST_BASE``. That mount is the only path inside the
    containers that lands on the host disk, so the host location is chosen
    in ``.env``, not in the settings page (#976).
    """
    host = settings.recordings_host_base
    return {
        "docker_managed": bool(host),
        "host_path": host or None,
        "container_path": settings.recordings_container_base if host else None,
    }


#: ``recordings_path_status`` results.
PATH_OK = "ok"  # host path, or inside the compose recordings mount
PATH_OTHER_MOUNT = "other_mount"  # on a volume the operator mounted themselves
PATH_CONTAINER_LAYER = "container_layer"  # never reaches the host disk


def _on_other_mount(p: PurePosixPath) -> bool:
    """Is ``p`` (or a folder above it, short of ``/``) a mount point here?"""
    for candidate in (p, *p.parents):
        if str(candidate) == "/":
            return False
        try:
            if os.path.ismount(str(candidate)):
                return True
        except OSError:
            pass
    return False


def recordings_path_status(path: str | None) -> str:
    """Whether recordings written to ``path`` reach the host disk.

    Outside Docker the configured path *is* a host path, so it is always ok.
    Under Docker it is ok inside the compose recordings mount
    (``/app/recordings``). A path on a volume the operator mounted
    themselves (a compose override) works too, as long as MediaMTX has the
    same mount, which core cannot see. Anything else (``/mnt/storage``
    typed as if it were a host path, a relative path) writes into the
    containers' own layers and is lost when they are recreated (#976).
    """
    if not path or path == "recordings" or not settings.recordings_host_base:
        return PATH_OK
    p = PurePosixPath(posixpath.normpath(str(path).replace("\\", "/")))
    base = PurePosixPath(posixpath.normpath(settings.recordings_container_base))
    if not p.is_absolute():
        return PATH_CONTAINER_LAYER
    if p == base or base in p.parents:
        return PATH_OK
    if _on_other_mount(p):
        return PATH_OTHER_MOUNT
    return PATH_CONTAINER_LAYER


#: Default recording paths already announced at INFO (see below).
_announced_default_paths: set[str] = set()


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
                    # Kept as configured (moving footage is the operator's
                    # call), but said once: these recordings never reach
                    # the host disk (#976).
                    warn_key = f"container_layer:{path}"
                    if (
                        warn_key not in _announced_default_paths
                        and recordings_path_status(path) == PATH_CONTAINER_LAYER
                    ):
                        _announced_default_paths.add(warn_key)
                        storage_logger.warning(
                            f"Recording path {path} is inside the container, "
                            "not on the host disk: recordings there are lost "
                            "when the container is recreated. Set "
                            "RECORDINGS_PATH in .env and reset the path in "
                            "Settings > Recordings."
                        )
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
            # Announced once per path: the system monitor asks every 15 s,
            # and 240 identical INFO lines an hour buried the log.
            if path not in _announced_default_paths:
                _announced_default_paths.add(path)
                storage_logger.info(f"Using default recording path: {path}")
            else:
                storage_logger.debug(f"Using default recording path: {path}")
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

        # Timestamp parsing is shared with the webhook/reconciler/playback so
        # every consumer agrees on layouts and timezone conventions.
        from services.recording_paths import iter_recording_files, parse_recording_time

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
            # All layouts (legacy year/month/day and new date/hour) via the
            # shared bounded recursive walk.
            for f in iter_recording_files(cdir):
                try:
                    st = f.stat()
                    size = st.st_size
                    # Skip zero-byte files (failed/in-progress)
                    if not size:
                        continue
                    mtime = st.st_mtime
                except Exception:
                    size = None
                    mtime = None

                ts = parse_recording_time(f, mtime=mtime)
                if start and (not ts or ts < start):
                    continue
                if end and (not ts or ts > end):
                    continue

                rel = os.path.relpath(f, root)
                rel_posix = rel.replace("\\", "/")
                # NOTE: no "url" field — the /recordings/raw route
                # it used to point at never existed (always 404).
                items.append(
                    {
                        "camera": cdir.name,
                        "relpath": rel_posix,
                        "size": size,
                        "start_time": ts.isoformat() if ts else None,
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

        # Get disk usage if possible. shutil.disk_usage works on every
        # platform; the old os.statvfs branch unpacked a 10-field struct into
        # 3 names, raised on Linux, and the error was swallowed — so disk
        # usage was silently never reported there.
        try:
            if root.exists():
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
