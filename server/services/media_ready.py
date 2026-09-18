# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""``media_ready``: tell clients when an event's media can be fetched (HA-113).

A notification wants the picture now and the clip as soon as it plays. The
HA-007 spike showed MediaMTX serves the segment it is still writing, so a
clip window is playable about :data:`READY_LAG_S` after it ends; waiting
for the segment to close (up to a minute) is not needed.

* A visit (Tier-0 track) posts at track end: one ``media_ready`` after the
  clip window [start - pre, end + post] is playable, naming the images the
  row has and the clip range. Clients sign what they want (POST /media/sign).
* An app alert's images are stored with the alert, so the ``app_alert``
  push itself names them; ``media_ready`` follows for the clip around the
  alert when the alert has a camera.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

READY_LAG_S = float(os.environ.get("MEDIA_READY_LAG_S", "2") or 2)
CLIP_PRE_S = float(os.environ.get("MEDIA_CLIP_PRE_S", "5") or 5)
CLIP_POST_S = float(os.environ.get("MEDIA_CLIP_POST_S", "5") or 5)
#: A notification clip, not an export: long visits are trimmed to the end.
MAX_CLIP_S = 120.0


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def clip_window(start: datetime, end: datetime | None) -> dict[str, Any]:
    """The clip around ``[start, end]``: padded, at most MAX_CLIP_S long
    (keeping the END, where the event is), and when it becomes playable."""
    start, end = _aware(start), _aware(end or start)
    clip_start = start - timedelta(seconds=CLIP_PRE_S)
    clip_end = end + timedelta(seconds=CLIP_POST_S)
    if (clip_end - clip_start).total_seconds() > MAX_CLIP_S:
        clip_start = clip_end - timedelta(seconds=MAX_CLIP_S)
    return {
        "start": clip_start.isoformat(),
        "duration_s": round((clip_end - clip_start).total_seconds(), 3),
        "ready_at": clip_end + timedelta(seconds=READY_LAG_S),
    }


async def _publish_when_ready(camera_id: int, payload: dict[str, Any],
                              ready_at: datetime) -> None:
    delay = (ready_at - datetime.now(UTC)).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)
    from services.event_bus_service import publish_media_ready

    try:
        await publish_media_ready(camera_id=camera_id, payload=payload)
    except Exception:  # noqa: BLE001 - a missed nudge never costs the event
        logger.debug("media_ready publish failed", exc_info=True)


def _schedule(camera_id: int, payload: dict[str, Any], ready_at: datetime) -> None:
    from core.background_tasks import spawn_background

    try:
        spawn_background(_publish_when_ready(camera_id, payload, ready_at),
                         name=f"media-ready-{payload.get('source')}-{payload.get('id')}")
    except RuntimeError:
        # No running loop (sync caller outside the app): nothing to nudge.
        logger.debug("media_ready not scheduled: no event loop")


def schedule_for_visit(row) -> None:
    """After a Tier-0 visit was stored."""
    from services.media_signing import EVENT_IMAGES

    images = [name for name, col in EVENT_IMAGES.items() if getattr(row, col, None)]
    clip = clip_window(row.started_at, row.ended_at)
    ready_at = clip.pop("ready_at")
    _schedule(row.camera_id, {
        "source": "event", "id": row.id, "label": row.label,
        "images": images, "clip": clip, "zone_ids": getattr(row, "zone_ids", None),
    }, ready_at)


def schedule_for_alert(stored: dict[str, Any], camera_id: int | None,
                       at: datetime | None) -> None:
    """After an app alert was stored. No camera → no clip → nothing to say
    beyond the app_alert push itself."""
    if camera_id is None:
        return
    clip = clip_window(at or datetime.now(UTC), at or datetime.now(UTC))
    ready_at = clip.pop("ready_at")
    _schedule(camera_id, {
        "source": "alert", "id": stored.get("id"), "alert_id": stored.get("alert_id"),
        "images": stored.get("image_names") or [], "clip": clip,
    }, ready_at)
