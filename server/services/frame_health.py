# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""A camera whose picture has stopped changing.

The enrichment tier already knows, per visit, whether the evidence
frame is byte-identical to an earlier visit's (``enrichment_reuse``:
the store is content-addressed). On a live camera that never happens —
two detections of the same parked car are different JPEGs. When it
happens visit after visit, the pipeline is being handed the same frame
again and again: a frozen stream (camera 23 fed the same frame for
hours on 2026-11-09 after an RTP clock wrap), a stuck relay, a camera
that froze on its last good frame, or a looping test file. None of
those is a camera watching anything, and until now nothing said so —
the stream was "up", recordings were written, visits were recorded, the
model described the same picture a thousand times.

This turns the reuse signal into the same kind of per-camera alarm the
recording watchdog raises (``segment_health``): after
``EVENTS_FROZEN_PICTURE_REPEATS`` consecutive visits from repeated
frames, a ``picture_frozen`` CameraEvent goes active with a plain
description; the first visit from a new frame clears it. The agent's
status read and the camera list can then say "the gate camera's picture
has not changed" instead of "online, recording".
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("frame_health")

EVENT_TYPE = "picture_frozen"
SETTING = "events_frozen_picture_repeats"

#: Consecutive repeated-frame visits per camera, in memory. A restart
#: forgets the streak and the alarm row stays; the next fresh frame or
#: the next run of repeats sets it right.
_streak: dict[int, int] = {}


def repeats_needed() -> int:
    from core.config import settings

    try:
        n = int(getattr(settings, SETTING, 5))
    except (TypeError, ValueError):
        n = 5
    return n


def _alarm_active(db, camera_id: int) -> bool:
    from models import CameraEvent

    row = (db.query(CameraEvent.event_state)
           .filter(CameraEvent.camera_id == camera_id, CameraEvent.event_type == EVENT_TYPE)
           .order_by(CameraEvent.id.desc()).first())
    return bool(row and row[0] == "active")


def note(db, row, twin) -> dict[str, Any] | None:
    """Count this visit toward (or against) the camera's frozen-picture
    streak. ``twin`` is the earlier visit with the same frame, or None
    for a new frame. Returns the alarm transition it caused (shaped like
    ``segment_health.note_segment``'s, for the caller to publish) or
    None. Commits its own CameraEvent row."""
    n = repeats_needed()
    if n <= 0:
        return None
    camera_id = int(row.camera_id)
    if twin is None:
        _streak[camera_id] = 0
        if not _alarm_active(db, camera_id):
            return None
        state, description = "inactive", "The picture is changing again: a new frame arrived"
        logger.info("frame health: camera %s picture changing again (visit %s)", camera_id, row.id)
    else:
        _streak[camera_id] = _streak.get(camera_id, 0) + 1
        if _streak[camera_id] < n or _alarm_active(db, camera_id):
            return None
        state = "active"
        description = (
            f"The picture is not changing: {_streak[camera_id]} visits in a row came from a "
            "frame already seen — a frozen stream, a stuck relay, or a looping file"
        )[:200]
        logger.warning("frame health: camera %s picture frozen (visit %s repeats visit %s, %d in a row)",
                       camera_id, row.id, twin.id, _streak[camera_id])
    from models import Camera, CameraEvent

    camera = db.get(Camera, camera_id)
    db.add(CameraEvent(camera_id=camera_id, event_type=EVENT_TYPE, event_state=state,
                       description=description, occurred_at=datetime.now(UTC)))
    db.commit()
    return {"camera_id": camera_id, "camera_name": getattr(camera, "name", None),
            "event_type": EVENT_TYPE, "event_state": state, "description": description}


async def publish(transition: dict[str, Any] | None) -> None:
    """Push a transition on the live bus (the camera list's badge, the
    agent's overlay). Never raises."""
    if not transition:
        return
    try:
        from services.event_bus_service import publish_camera_event

        await publish_camera_event(
            camera_id=int(transition["camera_id"]), event_type=transition["event_type"],
            payload={"state": transition["event_state"], "description": transition["description"],
                     "camera_name": transition.get("camera_name")})
    except Exception:                              # noqa: BLE001
        logger.debug("frame health: live push failed", exc_info=True)


def frozen_cameras(db, camera_ids) -> set[int]:
    """The cameras whose latest picture_frozen event is still active. One
    query for a page of cameras."""
    from sqlalchemy import func

    from models import CameraEvent

    ids = [int(c) for c in camera_ids]
    if not ids:
        return set()
    latest = (db.query(CameraEvent.camera_id, func.max(CameraEvent.id).label("mid"))
              .filter(CameraEvent.camera_id.in_(ids), CameraEvent.event_type == EVENT_TYPE)
              .group_by(CameraEvent.camera_id).subquery())
    rows = (db.query(CameraEvent.camera_id).join(latest, CameraEvent.id == latest.c.mid)
            .filter(CameraEvent.event_state == "active").all())
    return {int(r[0]) for r in rows}


def reset_for_tests() -> None:
    _streak.clear()
