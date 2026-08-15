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

"""Recording-health watchdog.

For an NVR, "a camera silently stopped recording" is the worst failure mode —
before this watchdog, nothing surfaced it and the gap was only discovered when
someone went looking for footage days later.

Every couple of minutes: one indexed query for the newest segment per active
recording-enabled camera. A camera whose latest segment is older than the
stall threshold (a few missed 1-minute segments) gets a ``recording_stalled``
CameraEvent (state ``active``); recovery writes state ``inactive``. Events
land in the existing camera_events store, so they show up wherever camera
events do and are pruned by the normal retention housekeeping.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func

from core.database import SessionLocal
from core.logging_config import recording_logger
from models import Camera, CameraEvent, Recording
from services.recording_paths import find_latest_recording_file
from services.storage_service import get_effective_recordings_base_path

EVENT_TYPE = "recording_stalled"

# 3 missed 1-minute segments + one segment length of slack for webhook lag.
STALL_THRESHOLD_SECONDS = 4 * 60
# Don't flag cameras that were only just created/enabled.
GRACE_PERIOD_SECONDS = 5 * 60

CHECK_INTERVAL_SECONDS = 2 * 60


def _still_stalled_on_disk(
    camera_id: int, root, threshold: datetime, now: datetime
) -> bool:
    """Filesystem cross-check for a suspected stall.

    The recordings table is webhook-fed; a webhook lag while recording
    continues would look like a stall. Return True only if the disk also has
    no clip newer than ``threshold`` (scans just the current/previous hour
    dir, so it is cheap). On any error, fall back to trusting the DB verdict.
    """
    if root is None:
        return True
    try:
        fs_latest = find_latest_recording_file(camera_id, root, now)
    except Exception:
        return True
    return fs_latest is None or fs_latest[1] < threshold


def check_recording_health() -> dict[str, int]:
    """One watchdog pass. Runs in a worker thread (own DB session)."""
    stalled = 0
    recovered = 0
    now = datetime.now(UTC)
    threshold = now - timedelta(seconds=STALL_THRESHOLD_SECONDS)

    db = SessionLocal()
    try:
        cameras = db.query(Camera).filter(Camera.is_active == True).all()  # noqa: E712

        try:
            root = Path(get_effective_recordings_base_path(db))
        except Exception:
            root = None

        # Newest segment per camera — one grouped, indexed query.
        latest_rows = dict(
            db.query(Recording.camera_id, func.max(Recording.start_time))
            .group_by(Recording.camera_id)
            .all()
        )

        for cam in cameras:
            cfg = getattr(cam, "config", None)
            if cfg is not None and not getattr(cfg, "recording_enabled", True):
                continue
            created = getattr(cam, "created_at", None)
            if created is not None:
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                if (now - created).total_seconds() < GRACE_PERIOD_SECONDS:
                    continue

            latest = latest_rows.get(cam.id)
            if latest is not None and latest.tzinfo is None:
                latest = latest.replace(tzinfo=UTC)
            is_stalled = latest is None or latest < threshold
            # Don't alarm on a webhook lag: confirm against the filesystem.
            if is_stalled:
                is_stalled = _still_stalled_on_disk(cam.id, root, threshold, now)

            # Current alarm state = the most recent stall event's state.
            last_event = (
                db.query(CameraEvent)
                .filter(
                    CameraEvent.camera_id == cam.id,
                    CameraEvent.event_type == EVENT_TYPE,
                )
                .order_by(CameraEvent.id.desc())
                .first()
            )
            alarm_active = last_event is not None and last_event.event_state == "active"

            if is_stalled and not alarm_active:
                last_str = latest.isoformat() if latest else "never"
                db.add(
                    CameraEvent(
                        camera_id=cam.id,
                        event_type=EVENT_TYPE,
                        event_state="active",
                        description=f"No new recording segment since {last_str}",
                        occurred_at=now,
                    )
                )
                stalled += 1
                recording_logger.warning(
                    f"Recording stalled on camera {cam.id} "
                    f"({cam.name or 'unnamed'}): last segment {last_str}"
                )
            elif not is_stalled and alarm_active:
                db.add(
                    CameraEvent(
                        camera_id=cam.id,
                        event_type=EVENT_TYPE,
                        event_state="inactive",
                        description="Recording resumed",
                        occurred_at=now,
                    )
                )
                recovered += 1
                recording_logger.info(
                    f"Recording recovered on camera {cam.id} ({cam.name or 'unnamed'})"
                )

        db.commit()
    except Exception as e:
        db.rollback()
        recording_logger.error(f"Recording watchdog pass failed: {e}", exc_info=True)
    finally:
        db.close()
    return {"stalled": stalled, "recovered": recovered}
