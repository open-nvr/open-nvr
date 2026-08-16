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

"""On-disk camera identity: markers, adoption/conflict detection, quarantine.

The recordings tree names camera directories only by the numeric id
(``cam-<id>``) — a DB sequence that restarts at 1 on a fresh database. After
a DB wipe, the first re-added camera would silently inherit another camera's
entire archive. This module gives every directory a positive identity:

* ``.camera-identity.json`` inside each ``cam-<X>`` dir carries the owning
  camera's stable ``uuid`` (new ``cameras.uuid`` column). Informational
  fields (name, ip, created_at) travel along for humans and for re-attach
  suggestions after a wipe.
* :func:`classify_dir` decides MATCH / ADOPTABLE / CONFLICT / EMPTY before
  any indexing happens (reconciler, segment webhook, provisioning).
* Conflicted or ownerless trees are moved aside — never deleted — into
  ``<root>/orphaned/`` by :func:`quarantine_dir`, where the admin API lists
  them for re-attach or explicit deletion. The ``orphaned/`` directory itself
  is the registry (no DB rows), so orphan state survives the very DB wipes
  it exists for.

FILE-SAFETY INVARIANT: nothing in this module deletes or overwrites a
recording file. Quarantine is a single same-filesystem directory rename onto
a destination that does not exist. The only sanctioned deletion paths for
orphaned footage live elsewhere: the explicit admin DELETE endpoint and the
``retention_days`` aging policy.

Unmarked (pre-upgrade) directories are adopted via the created_at rule: if no
file in the directory predates the DB camera's ``created_at`` (minus a skew
grace), the footage could all have been produced during that camera's
lifetime and the dir is adopted and stamped — the normal upgrade path with an
intact DB stamps everything with zero admin action. Footage that predates the
camera row cannot be its history (the wipe-then-re-add case) and conflicts.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.config import settings
from core.logging_config import recording_logger
from models import Camera
from services.recording_paths import parse_recording_time
from services.stream_service import _build_stream_name

MARKER_FILENAME = ".camera-identity.json"
ORPHANED_DIR_NAME = "orphaned"
ORPHAN_INFO_FILENAME = ".orphan-info.json"
MARKER_VERSION = 1

# Clock-skew grace for the created_at rule: a file this much older than the
# camera row is still considered plausibly its own (NTP drift, container
# clock settle at first boot).
CREATED_AT_GRACE = timedelta(hours=1)

# Classification results.
MATCH = "match"
ADOPTABLE = "adoptable"
CONFLICT = "conflict"
EMPTY = "empty"

# Quarantine reasons (also recorded in .orphan-info.json).
REASON_UUID_MISMATCH = "uuid-mismatch"
REASON_PREDATES_CAMERA = "predates-camera"
REASON_NO_OWNER = "no-owner"

_NEW_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LEGACY_YEAR_RE = re.compile(r"^\d{4}$")

# Per-directory locks so the reconciler (worker thread), the webhook (event
# loop) and provisioning never interleave a stamp/quarantine on the same dir.
_locks_guard = threading.Lock()
_dir_locks: dict[str, threading.Lock] = {}


def _dir_lock(cam_dir: Path) -> threading.Lock:
    key = str(cam_dir)
    with _locks_guard:
        lock = _dir_locks.get(key)
        if lock is None:
            lock = _dir_locks[key] = threading.Lock()
        return lock


# ---------------------------------------------------------------------------
# Path-name <-> camera resolution (id mode and ip mode)
# ---------------------------------------------------------------------------


def path_name_for_camera(camera: Camera) -> str:
    """The MediaMTX path / on-disk directory name for a camera.

    Delegates to the same builder MediaMTX provisioning uses, so id mode
    (``cam-3``) and ip mode (``cam-192_168_1_9``) are covered identically.
    """
    prefix = getattr(settings, "mediamtx_stream_prefix", "cam-") or "cam-"
    return _build_stream_name(prefix, camera.id, camera.ip_address or "")


def camera_for_path_name(db, name: str) -> Camera | None:
    """Map a ``cam-<...>`` directory/path name back to a Camera row.

    Mirrors the webhook's resolution: numeric id form first, then one indexed
    ip lookup (ip mode replaces dots with underscores).
    """
    if not name.lower().startswith("cam-"):
        return None
    tag = name.split("-", 1)[1]
    try:
        return db.query(Camera).filter(Camera.id == int(tag)).first()
    except ValueError:
        return (
            db.query(Camera)
            .filter(Camera.ip_address == tag.replace("_", "."))
            .first()
        )


def ensure_camera_uuids(db) -> int:
    """Assign uuids to any camera rows missing one (create_all-bootstrapped
    databases skip the Alembic backfill). Returns the number filled."""
    import uuid as _uuid

    filled = 0
    for cam in db.query(Camera).filter(Camera.uuid.is_(None)).all():
        cam.uuid = str(_uuid.uuid4())
        filled += 1
    if filled:
        db.commit()
        recording_logger.info(f"Assigned uuids to {filled} camera(s) missing one")
    return filled


# ---------------------------------------------------------------------------
# Marker file I/O
# ---------------------------------------------------------------------------


def read_marker(cam_dir: Path) -> tuple[dict | None, bool]:
    """Read a directory's identity marker.

    Returns ``(marker, corrupt)``: ``(dict, False)`` when present and valid,
    ``(None, False)`` when absent, ``(None, True)`` when present but
    unreadable/invalid — treated by callers as absent (the created_at rule is
    the real guard) with a warning already logged here.
    """
    marker_path = cam_dir / MARKER_FILENAME
    try:
        if not marker_path.is_file():
            return None, False
        with open(marker_path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or not data.get("camera_uuid"):
            raise ValueError("marker missing camera_uuid")
        return data, False
    except Exception as e:
        recording_logger.warning(
            f"Corrupt camera identity marker at {marker_path}: {e} — "
            "treating as unmarked (created_at rule applies)"
        )
        return None, True


def _atomic_write_json(dest: Path, data: dict) -> None:
    tmp = dest.with_name(f"{dest.name}.tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, dest)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def stamp_marker(cam_dir: Path, camera: Camera, by: str) -> None:
    """Write/refresh the identity marker for ``camera`` in ``cam_dir``.

    Idempotent and atomic (tmp + os.replace). Only ``camera_uuid`` is
    authoritative; the rest is informational and refreshed on every stamp.
    Requires ``camera.uuid`` (ensure_camera_uuids runs at startup; new rows
    get one from the model default).
    """
    if not camera.uuid:
        recording_logger.warning(
            f"stamp_marker skipped for {cam_dir}: camera {camera.id} has no uuid"
        )
        return
    with _dir_lock(cam_dir):
        cam_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(
            cam_dir / MARKER_FILENAME,
            {
                "version": MARKER_VERSION,
                "camera_uuid": camera.uuid,
                "camera_id": camera.id,
                "path_name": cam_dir.name,
                "camera_name": camera.name,
                "ip_address": camera.ip_address,
                "camera_created_at": _iso(camera.created_at),
                "stamped_at": _iso(datetime.now(UTC)),
                "stamped_by": by,
            },
        )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def has_recording_files(cam_dir: Path) -> bool:
    try:
        return next(iter(cam_dir.rglob("*.mp4")), None) is not None
    except OSError:
        return False


def _oldest_recording_ts(cam_dir: Path) -> datetime | None:
    """Oldest recording timestamp in a camera dir, without a full-tree walk.

    Finds the lexicographically-smallest date directory per layout (new
    ``YYYY-MM-DD`` day dirs are local-named, legacy ``YYYY/MM/DD`` are UTC —
    both sort correctly as strings) and parses only that day's files.
    """
    candidates: list[Path] = []
    try:
        entries = [e for e in cam_dir.iterdir() if e.is_dir()]
    except OSError:
        return None

    new_days = sorted(e.name for e in entries if _NEW_DAY_RE.match(e.name))
    if new_days:
        day_dir = cam_dir / new_days[0]
        candidates.extend(day_dir.glob("*/*.mp4"))
        candidates.extend(day_dir.glob("*.mp4"))

    years = sorted(e.name for e in entries if _LEGACY_YEAR_RE.match(e.name))
    if years:
        year_dir = cam_dir / years[0]
        months = sorted(m.name for m in year_dir.iterdir() if m.is_dir())
        if months:
            month_dir = year_dir / months[0]
            days = sorted(d.name for d in month_dir.iterdir() if d.is_dir())
            if days:
                day_dir = month_dir / days[0]
                candidates.extend(day_dir.glob("*.mp4"))  # legacy flat
                candidates.extend(day_dir.glob("*/*.mp4"))  # legacy nested

    oldest: datetime | None = None
    for f in candidates:
        try:
            mtime = f.stat().st_mtime
        except OSError:
            mtime = None
        ts = parse_recording_time(f, mtime=mtime)
        if ts is not None and (oldest is None or ts < oldest):
            oldest = ts
    return oldest


def _created_at_utc(camera: Camera) -> datetime | None:
    dt = camera.created_at
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def classify_dir(cam_dir: Path, camera: Camera) -> tuple[str, str | None]:
    """Decide what ``cam_dir`` is to ``camera``.

    Returns ``(classification, conflict_reason)`` where classification is one
    of MATCH / ADOPTABLE / CONFLICT / EMPTY and conflict_reason is set only
    for CONFLICT.
    """
    if not cam_dir.is_dir():
        return EMPTY, None

    marker, _corrupt = read_marker(cam_dir)
    if marker is not None and camera.uuid:
        if marker.get("camera_uuid") == camera.uuid:
            return MATCH, None
        return CONFLICT, REASON_UUID_MISMATCH

    # Unmarked (or corrupt-marker, or camera missing a uuid — pre-migration):
    # the created_at rule decides.
    if not has_recording_files(cam_dir):
        return EMPTY, None

    created = _created_at_utc(camera)
    if created is None:
        # Cannot prove the footage predates the camera — adopt (quarantining
        # on missing evidence would punish normal upgrades).
        recording_logger.warning(
            f"{cam_dir.name}: camera {camera.id} has no created_at; adopting "
            "unmarked directory without the created_at check"
        )
        return ADOPTABLE, None

    oldest = _oldest_recording_ts(cam_dir)
    if oldest is None:
        # Files exist but none parse as recordings — nothing to compare;
        # adopt so the dir gets stamped (unparseable files are never indexed
        # or deleted by name anyway).
        return ADOPTABLE, None

    if oldest < created - CREATED_AT_GRACE:
        return CONFLICT, REASON_PREDATES_CAMERA
    return ADOPTABLE, None


# ---------------------------------------------------------------------------
# Quarantine (rename-only, never deletes)
# ---------------------------------------------------------------------------


def quarantine_dir(
    cam_dir: Path,
    root: Path,
    reason: str,
    old_marker: dict | None,
    by: str,
) -> Path | None:
    """Move a conflicted/ownerless tree aside to ``<root>/orphaned/``.

    Single same-filesystem directory rename onto a destination that does not
    exist — never a copy, never a delete, never an overwrite. Returns the
    destination on success, None on failure (e.g. Windows sharing violation
    with an open segment handle); callers must then skip indexing and retry
    on a later pass.
    """
    with _dir_lock(cam_dir):
        if not cam_dir.is_dir():
            return None  # lost a race with another quarantine — fine
        orphan_root = root / ORPHANED_DIR_NAME
        try:
            orphan_root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            recording_logger.error(f"Cannot create {orphan_root}: {e}")
            return None

        uuid_tag = "unmarked"
        if old_marker and old_marker.get("camera_uuid"):
            uuid_tag = str(old_marker["camera_uuid"])[:8]
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        base = f"{cam_dir.name}--{uuid_tag}--{stamp}"
        dest = orphan_root / base
        n = 1
        while dest.exists():
            n += 1
            dest = orphan_root / f"{base}-{n}"

        try:
            cam_dir.rename(dest)
        except OSError as e:
            recording_logger.warning(
                f"Quarantine of {cam_dir} deferred (rename failed: {e}); "
                "will retry on the next reconciler pass"
            )
            return None

        try:
            _atomic_write_json(
                dest / ORPHAN_INFO_FILENAME,
                {
                    "version": 1,
                    "original_dir": cam_dir.name,
                    "reason": reason,
                    "old_marker": old_marker,
                    "quarantined_at": _iso(datetime.now(UTC)),
                    "quarantined_by": by,
                },
            )
        except OSError as e:
            recording_logger.warning(f"Could not write orphan info in {dest}: {e}")

        recording_logger.warning(
            f"Quarantined recordings dir {cam_dir.name} -> "
            f"{ORPHANED_DIR_NAME}/{dest.name} (reason: {reason}). The footage "
            "is preserved and can be re-attached or deleted from Settings -> "
            "Recording -> Orphaned recordings."
        )
        return dest


def resolve_conflict(
    cam_dir: Path, root: Path, camera: Camera, reason: str, by: str
) -> bool:
    """Quarantine a conflicted dir, then recreate + stamp it for ``camera``.

    Returns True when the dir is safe to index afterwards.
    """
    old_marker, _ = read_marker(cam_dir)
    dest = quarantine_dir(cam_dir, root, reason, old_marker, by)
    if dest is None:
        return False
    stamp_marker(cam_dir, camera, by)
    _ingest_cache_invalidate(cam_dir.name)
    return True


def protect_camera_dir(db, camera: Camera, by: str) -> str:
    """Classify + stamp/quarantine a camera's directory at provisioning time.

    Called right after a camera row is committed (and from startup
    provisioning) so a post-wipe inheritance conflict is caught the moment
    the camera is (re-)added — before MediaMTX writes the first segment —
    instead of up to a reconciler pass later. Returns the classification.
    """
    from services.storage_service import get_effective_recordings_base_path

    root = Path(get_effective_recordings_base_path(db))
    cam_dir = root / path_name_for_camera(camera)
    classification, reason = classify_dir(cam_dir, camera)
    if classification == CONFLICT:
        resolve_conflict(cam_dir, root, camera, reason, by)
    else:
        stamp_marker(cam_dir, camera, by)
    return classification


# ---------------------------------------------------------------------------
# Webhook ingest gate (TTL-cached so it costs ~one marker read per camera
# per minute, not per segment)
# ---------------------------------------------------------------------------

_INGEST_CACHE_TTL = 60.0
_ingest_cache_guard = threading.Lock()
# path_name -> (camera_uuid, ok, expires_monotonic)
_ingest_cache: dict[str, tuple[str | None, bool, float]] = {}


def _ingest_cache_invalidate(path_name: str) -> None:
    with _ingest_cache_guard:
        _ingest_cache.pop(path_name, None)


def reset_ingest_cache() -> None:
    """Test hook."""
    with _ingest_cache_guard:
        _ingest_cache.clear()


def verify_segment_identity(camera: Camera, path_name: str, root: Path) -> bool:
    """May a completed segment under ``root/path_name`` be indexed (and
    mirrored) as ``camera``'s?

    MATCH -> True. EMPTY/ADOPTABLE -> stamp (this is the natural
    stamp-on-first-segment hook) and True. CONFLICT -> False; the reconciler
    performs the quarantine (the webhook must stay fast).
    """
    now = time.monotonic()
    with _ingest_cache_guard:
        cached = _ingest_cache.get(path_name)
        if cached and cached[0] == camera.uuid and cached[2] > now:
            return cached[1]

    cam_dir = root / path_name
    classification, reason = classify_dir(cam_dir, camera)
    ok = classification != CONFLICT
    if ok and classification in (EMPTY, ADOPTABLE):
        try:
            stamp_marker(cam_dir, camera, "webhook")
        except OSError as e:
            recording_logger.warning(f"Webhook stamp failed for {cam_dir}: {e}")
    if not ok:
        recording_logger.warning(
            f"Segment for path {path_name} NOT indexed: directory identity "
            f"conflict ({reason}); camera {camera.id} does not own this tree"
        )
    with _ingest_cache_guard:
        _ingest_cache[path_name] = (camera.uuid, ok, now + _INGEST_CACHE_TTL)
    return ok


# ---------------------------------------------------------------------------
# Playback guard (covers the window between conflict creation and quarantine)
# ---------------------------------------------------------------------------


def dir_conflicts_with_camera(camera: Camera, root: Path) -> bool:
    """True when the camera's directory carries a marker for a DIFFERENT
    camera — filesystem/proxy playback fallbacks must then return empty
    instead of serving another camera's footage."""
    if not camera or not camera.uuid:
        return False
    cam_dir = Path(root) / path_name_for_camera(camera)
    marker, _ = read_marker(cam_dir)
    return marker is not None and marker.get("camera_uuid") != camera.uuid
