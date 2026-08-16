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

"""Admin API for quarantined ("orphaned") recording trees.

After a DB wipe + camera re-add, footage that cannot be positively attributed
to a current camera is moved (never deleted) to ``<root>/orphaned/`` by the
identity layer (services/camera_identity.py). This router lets a superuser:

* list the quarantined trees with the old camera's identity (from the marker
  that traveled with the tree), size and date range;
* attach a tree to an existing camera (merge + reindex — never overwrites;
  a partial merge keeps the remainder listed);
* delete a tree (one of only two sanctioned deletion paths for orphaned
  footage, the other being retention_days aging);
* trigger an on-demand rescan.

The ``orphaned/`` directory itself is the registry — deliberately no DB rows,
so orphan state survives the DB wipes this feature exists for.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from core.auth import get_current_superuser
from core.database import get_db
from core.logging_config import camera_logger, recording_logger
from models import Camera
from services.camera_identity import (
    CONFLICT,
    MARKER_FILENAME,
    ORPHAN_INFO_FILENAME,
    ORPHANED_DIR_NAME,
    classify_dir,
    path_name_for_camera,
    read_marker,
    resolve_conflict,
    stamp_marker,
)
from services.recording_paths import iter_recording_files, parse_recording_time
from services.storage_service import get_effective_recordings_base_path

router = APIRouter(prefix="/recordings/orphans", tags=["recordings", "orphans"])

_ORPHAN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _root(db: Session) -> Path:
    return Path(get_effective_recordings_base_path(db))


def _orphan_dir(db: Session, orphan_id: str) -> Path:
    """Resolve a request-supplied orphan id to a directory strictly inside
    ``<root>/orphaned/`` (no traversal, direct child only)."""
    if not _ORPHAN_ID_RE.match(orphan_id) or orphan_id in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid orphan id")
    tree = _root(db) / ORPHANED_DIR_NAME / orphan_id
    if not tree.is_dir() or tree.parent.name != ORPHANED_DIR_NAME:
        raise HTTPException(status_code=404, detail="Orphaned tree not found")
    return tree


def _read_orphan_info(tree: Path) -> dict | None:
    try:
        with open(tree / ORPHAN_INFO_FILENAME, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _summarize_tree(db: Session, tree: Path) -> dict:
    """One orphan listing entry: identity, size, date range, suggestion."""
    info = _read_orphan_info(tree) or {}
    identity = info.get("old_marker")
    if identity is None:
        marker, _corrupt = read_marker(tree)
        identity = marker

    file_count = 0
    total_bytes = 0
    earliest: datetime | None = None
    latest: datetime | None = None
    for f in iter_recording_files(tree):
        try:
            st = f.stat()
        except OSError:
            continue
        file_count += 1
        total_bytes += st.st_size
        ts = parse_recording_time(f, mtime=st.st_mtime)
        if ts is None:
            continue
        if earliest is None or ts < earliest:
            earliest = ts
        if latest is None or ts > latest:
            latest = ts

    # Suggestion only — NEVER auto-attach (a DHCP-reassigned IP would
    # re-open silent mis-attribution). Match the old identity's ip first,
    # then name.
    suggested_camera_id = None
    if identity:
        ip = identity.get("ip_address")
        name = identity.get("camera_name")
        cam = None
        if ip:
            cam = db.query(Camera).filter(Camera.ip_address == ip).first()
        if cam is None and name:
            cam = db.query(Camera).filter(Camera.name == name).first()
        if cam is not None:
            suggested_camera_id = cam.id

    def _dt(v: datetime | None) -> str | None:
        return v.astimezone(UTC).isoformat() if v else None

    return {
        "id": tree.name,
        "original_dir": info.get("original_dir"),
        "reason": info.get("reason"),
        "quarantined_at": info.get("quarantined_at"),
        "identity": (
            {
                "camera_uuid": identity.get("camera_uuid"),
                "camera_id": identity.get("camera_id"),
                "camera_name": identity.get("camera_name"),
                "ip_address": identity.get("ip_address"),
            }
            if identity
            else None
        ),
        "file_count": file_count,
        "total_bytes": total_bytes,
        "earliest": _dt(earliest),
        "latest": _dt(latest),
        "suggested_camera_id": suggested_camera_id,
    }


@router.get("")
async def list_orphans(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """List quarantined recording trees (newest first)."""
    orphan_root = _root(db) / ORPHANED_DIR_NAME

    def _scan() -> list[dict]:
        if not orphan_root.is_dir():
            return []
        out = []
        for tree in sorted(orphan_root.iterdir(), reverse=True):
            if tree.is_dir():
                out.append(_summarize_tree(db, tree))
        return out

    items = await asyncio.to_thread(_scan)
    return {"items": items, "count": len(items)}


@router.post("/rescan")
async def rescan_orphans(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Run the identity/ownerless scan now instead of waiting for the next
    reconciler pass. Returns how many trees were quarantined."""
    from services.recording_reconciler import quarantine_ownerless_dirs

    root = _root(db)

    def _scan() -> int:
        quarantined = 0
        for cam in db.query(Camera).all():
            cam_dir = root / path_name_for_camera(cam)
            classification, reason = classify_dir(cam_dir, cam)
            if classification == CONFLICT and resolve_conflict(
                cam_dir, root, cam, reason, "rescan"
            ):
                quarantined += 1
        quarantined += quarantine_ownerless_dirs(db, root)
        return quarantined

    quarantined = await asyncio.to_thread(_scan)
    return {"status": "ok", "quarantined": quarantined}


def _merge_tree(src: Path, dest: Path, skipped: list[str], rel: str = "") -> None:
    """Move src's contents into dest without ever overwriting anything.

    Entries whose destination doesn't exist are renamed whole (day dirs move
    in one op); on collision, directories recurse to per-file granularity and
    files are SKIPPED and reported — never replaced.
    """
    dest.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        if entry.name in (ORPHAN_INFO_FILENAME, MARKER_FILENAME):
            continue
        entry_rel = f"{rel}/{entry.name}" if rel else entry.name
        target = dest / entry.name
        if not target.exists():
            entry.rename(target)
        elif entry.is_dir() and target.is_dir():
            _merge_tree(entry, target, skipped, entry_rel)
            with contextlib.suppress(OSError):
                entry.rmdir()  # only succeeds when emptied
        else:
            skipped.append(entry_rel)


@router.post("/{orphan_id}/attach")
async def attach_orphan(
    orphan_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Merge a quarantined tree into an existing camera's directory and
    reindex it. Never overwrites; a partial merge (name collisions) keeps
    the remainder quarantined and reports the skipped files."""
    camera_id = payload.get("camera_id")
    if not isinstance(camera_id, int):
        raise HTTPException(status_code=400, detail="camera_id (int) is required")
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")

    tree = _orphan_dir(db, orphan_id)
    root = _root(db)
    dest = root / path_name_for_camera(camera)

    def _attach() -> dict:
        skipped: list[str] = []
        _merge_tree(tree, dest, skipped)
        fully_merged = next(iter(tree.rglob("*.mp4")), None) is None
        if fully_merged:
            # Only dotfiles / emptied dirs remain — safe to drop the tree.
            shutil.rmtree(tree, ignore_errors=True)
        stamp_marker(dest, camera, "attach")

        from services.recording_reconciler import reconcile_camera

        inserted, deleted = reconcile_camera(db, camera.id, root, None)
        return {
            "status": "ok" if fully_merged else "partial",
            "fully_merged": fully_merged,
            "skipped_files": skipped,
            "indexed_rows": inserted,
            "removed_stale_rows": deleted,
        }

    result = await asyncio.to_thread(_attach)

    camera_logger.log_action(
        "recordings.orphan_attached",
        message=(
            f"Orphaned recordings '{orphan_id}' attached to camera "
            f"{camera.id} ({camera.name}) — {result['status']}"
        ),
        user_id=getattr(current_user, "id", None),
        camera_id=camera.id,
        extra_data={"orphan_id": orphan_id, **result},
    )
    return result


@router.delete("/{orphan_id}")
async def delete_orphan(
    orphan_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    """Permanently delete a quarantined tree (explicit admin action — one of
    the only two sanctioned deletion paths for orphaned footage)."""
    tree = _orphan_dir(db, orphan_id)

    def _delete() -> None:
        shutil.rmtree(tree)

    try:
        await asyncio.to_thread(_delete)
    except OSError as e:
        recording_logger.error(f"Orphan delete failed for {orphan_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}") from e

    camera_logger.log_action(
        "recordings.orphan_deleted",
        message=f"Orphaned recordings '{orphan_id}' permanently deleted",
        user_id=getattr(current_user, "id", None),
        extra_data={"orphan_id": orphan_id},
    )
    return {"status": "ok", "deleted": orphan_id}
