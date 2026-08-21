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

"""Filesystem ↔ recordings-index reconciler.

The segment-complete webhook is the primary feed of the recordings table, but
it misses segments whenever the backend is down while MediaMTX keeps
recording, and nothing ever removed rows for files retention (or an operator)
deleted. This service converges the two:

* files on disk with no DB row are INSERTED (``source='reconciler'``,
  timestamps parsed from the filename by the shared layout-aware parser);
* DB rows whose file is gone are DELETED;
* rows that already exist are NEVER updated — the webhook's richer data
  (exact duration, codec) wins, and old rows keep the timestamps they were
  written with even if parsing conventions evolve.

Runs at startup as a full-history backfill (this is also the one-time import
of pre-existing recordings), then periodically over a recent window. All
filesystem work happens in a worker thread via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.database import SessionLocal
from core.logging_config import recording_logger
from models import Camera, Recording
from services.camera_identity import (
    ADOPTABLE,
    CONFLICT,
    REASON_NO_OWNER,
    camera_for_path_name,
    classify_dir,
    has_recording_files,
    quarantine_dir,
    read_marker,
    resolve_conflict,
    stamp_marker,
)
from services.recording_paths import (
    iter_recording_files,
    iter_recording_files_between,
    parse_recording_time,
)
from services.storage_service import get_effective_recordings_base_path
from services.stream_service import SUBSTREAM_SUFFIX

# A file whose mtime is this recent may still be written by MediaMTX — skip
# it; the webhook (or the next pass) will pick it up once it's closed.
_IN_PROGRESS_MTIME_SECONDS = 120

# Periodic pass window: generously covers any webhook outage between passes.
_PERIODIC_WINDOW_HOURS = 48
_PERIODIC_INTERVAL_SECONDS = 15 * 60


def _relative_posix(f: Path, root: Path) -> str:
    return os.path.relpath(f, root).replace("\\", "/")


def reconcile_camera(
    db, camera_id: int, root: Path, window_start: datetime | None
) -> tuple[int, int]:
    """Converge one camera's directory with its DB rows.

    Returns ``(inserted, deleted)``. ``window_start=None`` means full history.
    """
    cam_dir = root / f"cam-{int(camera_id)}"
    now = time.time()

    # Identity gate: never index a directory this camera does not positively
    # own. A DB wipe restarts the id sequence, so a re-added camera would
    # otherwise silently inherit (and index) another camera's archive.
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if camera is not None:
        classification, reason = classify_dir(cam_dir, camera)
        if classification == CONFLICT:
            if not resolve_conflict(cam_dir, root, camera, reason, "reconciler"):
                # Quarantine deferred (e.g. open file handle on Windows) —
                # skip indexing entirely this pass; retried next pass.
                return 0, 0
        elif classification == ADOPTABLE:
            stamp_marker(cam_dir, camera, "reconciler")

    # -- Disk side ----------------------------------------------------------
    # Full history on the startup backfill; only the window's date dirs on
    # periodic passes — a whole-tree rglob every 15 min is needless I/O on a
    # large archive.
    if window_start is None:
        disk_iter = iter_recording_files(cam_dir)
    else:
        disk_iter = iter_recording_files_between(
            camera_id, root, window_start, datetime.now(UTC)
        )
    on_disk: dict[str, tuple[Path, datetime, int]] = {}
    for f in disk_iter:
        try:
            st = f.stat()
        except OSError:
            continue
        if not st.st_size:
            continue
        if now - st.st_mtime < _IN_PROGRESS_MTIME_SECONDS:
            continue  # still being written
        ts = parse_recording_time(f, mtime=st.st_mtime)
        if ts is None:
            continue
        if window_start is not None and ts < window_start:
            continue
        on_disk[_relative_posix(f, root)] = (f, ts, st.st_size)

    # -- DB side ------------------------------------------------------------
    q = db.query(Recording).filter(Recording.camera_id == camera_id)
    if window_start is not None:
        q = q.filter(Recording.start_time >= window_start)
    db_rows = {r.file_path: r for r in q.all()}

    inserted = 0
    deleted = 0

    # Files with no row → insert. Sorted so end_time can be estimated from
    # the next file's start when durations are unknown.
    missing = sorted(
        (rel for rel in on_disk if rel not in db_rows),
        key=lambda rel: on_disk[rel][1],
    )
    for i, rel in enumerate(missing):
        f, ts, size = on_disk[rel]
        # End estimate: next segment's start (same camera, contiguous write)
        # capped at the file's mtime, which is when the last byte landed.
        try:
            mtime_dt = datetime.fromtimestamp(f.stat().st_mtime, tz=UTC)
        except OSError:
            continue  # deleted between scan and now
        if i + 1 < len(missing):
            next_ts = on_disk[missing[i + 1]][1]
            end_time = min(next_ts, mtime_dt)
        else:
            end_time = mtime_dt
        if end_time < ts:
            end_time = ts
        duration = (end_time - ts).total_seconds()

        db.add(
            Recording(
                camera_id=camera_id,
                filename=f.name,
                file_path=rel,
                file_size=size,
                duration=duration,
                recording_type="continuous",
                start_time=ts,
                end_time=end_time,
                is_processed=True,
                source="reconciler",
                created_by_id=None,
            )
        )
        inserted += 1
        if inserted % 500 == 0:
            db.commit()  # bound transaction size on big backfills

    # Rows whose file is gone → delete. Only within the scanned window, where
    # the disk picture is authoritative.
    for rel, row in db_rows.items():
        if rel not in on_disk:
            full = root / rel
            if not full.exists():
                db.delete(row)
                deleted += 1

    db.commit()
    return inserted, deleted


def quarantine_ownerless_dirs(db, root: Path) -> int:
    """Move recording dirs that belong to NO camera into ``orphaned/``.

    Without this, directories whose old numeric id was never reused after a
    DB wipe stay invisible forever (never indexed, never listed, space only
    reclaimable by age). Quarantining makes them visible in the orphans UI.

    Guards:
    * EMPTY cameras table -> skip entirely. A boot against a misconfigured
      or freshly-created DATABASE_URL must not mass-quarantine a healthy
      tree (the later reboot against the real DB would then let stale-row
      deletion purge the whole recordings index).
    * A dir whose marker uuid matches an EXISTING camera is not ownerless —
      its expected path name merely changed (e.g. ip edit in ip mode).
    * Substream dirs and dirs with no recording files are left alone.
    """
    if db.query(Camera.id).first() is None:
        recording_logger.warning(
            "Ownerless-dir scan skipped: cameras table is empty. If this "
            "database is fresh but recordings exist on disk, re-add cameras "
            "and the scan will resume protecting/quarantining on the next "
            "reconciler pass."
        )
        return 0

    quarantined = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.is_dir() or not entry.name.startswith("cam-"):
            continue
        if entry.name.endswith(SUBSTREAM_SUFFIX):
            continue
        try:
            if camera_for_path_name(db, entry.name) is not None:
                continue  # owned; identity handled in reconcile_camera
            marker, _corrupt = read_marker(entry)
            if marker and marker.get("camera_uuid"):
                match = (
                    db.query(Camera)
                    .filter(Camera.uuid == marker["camera_uuid"])
                    .first()
                )
                if match is not None:
                    recording_logger.warning(
                        f"Dir {entry.name} resolves to no camera by name but "
                        f"its marker belongs to camera {match.id} "
                        f"({match.name}); leaving it in place"
                    )
                    continue
            if not has_recording_files(entry):
                continue
            if quarantine_dir(entry, root, REASON_NO_OWNER, marker, "reconciler"):
                quarantined += 1
        except Exception as e:
            recording_logger.error(
                f"Ownerless scan failed for {entry.name}: {e}", exc_info=True
            )
    return quarantined


def reconcile_all(window_start: datetime | None) -> tuple[int, int]:
    """Reconcile every camera. Runs in a worker thread (own DB session)."""
    total_ins = 0
    total_del = 0
    db = SessionLocal()
    try:
        root = get_effective_recordings_base_path(db)
        camera_ids = [c.id for c in db.query(Camera.id).all()]
        for cid in camera_ids:
            try:
                ins, dele = reconcile_camera(db, cid, Path(root), window_start)
                total_ins += ins
                total_del += dele
            except Exception as e:
                db.rollback()
                recording_logger.error(
                    f"Reconciler failed for camera {cid}: {e}", exc_info=True
                )
        try:
            n = quarantine_ownerless_dirs(db, Path(root))
            if n:
                recording_logger.warning(f"Quarantined {n} ownerless recording dir(s)")
        except Exception as e:
            recording_logger.error(f"Ownerless-dir scan failed: {e}", exc_info=True)
    finally:
        db.close()
    return total_ins, total_del


async def run_reconciler_loop() -> None:
    """Startup full backfill, then a periodic recent-window pass.

    Scheduled as an asyncio task from the app lifespan; every filesystem/DB
    pass runs in a worker thread so the event loop stays free.
    """
    # Give the app a moment to finish booting before the (possibly large)
    # first backfill.
    await asyncio.sleep(20)
    try:
        ins, dele = await asyncio.to_thread(reconcile_all, None)
        recording_logger.info(
            f"Recording reconciler backfill: +{ins} rows, -{dele} stale rows"
        )
    except Exception as e:
        recording_logger.error(f"Reconciler backfill failed: {e}", exc_info=True)

    while True:
        await asyncio.sleep(_PERIODIC_INTERVAL_SECONDS)
        try:
            window = datetime.now(UTC) - timedelta(hours=_PERIODIC_WINDOW_HOURS)
            ins, dele = await asyncio.to_thread(reconcile_all, window)
            if ins or dele:
                recording_logger.info(
                    f"Recording reconciler pass: +{ins} rows, -{dele} stale rows"
                )
        except Exception as e:
            recording_logger.error(f"Reconciler pass failed: {e}", exc_info=True)
