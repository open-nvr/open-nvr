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

"""Segment health: a recorder that keeps closing segments can still be
writing garbage.

The recording watchdog answers "did a segment arrive?". It cannot answer
"is the segment video?". On the Ventuno Q (2026-11-09) a camera's RTP clock
wrapped and the relay in front of it clamped every timestamp to "previous
+ 1 tick": MediaMTX kept recording, but a 60 s segment could never close on
time, so each file closed only on the 50 MB part cap and carried 0.07 s of
playable video. Segments arrived every seven minutes, the watchdog stayed
quiet, and the camera recorded nothing usable for hours.

The tell is in numbers the segment-complete webhook already has: bytes and
duration. Real camera video is a few Mbit/s, the heaviest 4K streams a few
tens. A segment whose implied bitrate is hundreds of Mbit/s — or one far
shorter than the configured segment length yet still fat — is not video
with a short duration, it is a clock that stopped. Each such segment keeps
one ``recording_degraded`` CameraEvent active; the first healthy segment
afterwards closes it. Same store and same fan-out as ``recording_stalled``,
so it shows wherever that does.
"""

from __future__ import annotations

from datetime import UTC, datetime

from core.config import settings
from core.logging_config import recording_logger
from models import CameraEvent

EVENT_TYPE = "recording_degraded"

#: Below this a segment is too small to judge: a tail written at stop or
#: reconnect is short AND small, and must never raise an alarm.
MIN_JUDGED_BYTES = 8 * 1024 * 1024
#: No camera stream carries this much per second of video. Camera 23's
#: broken segments implied ~6 Gbit/s (50 MB for 0.07 s).
DEGENERATE_MBPS = 200.0
#: A segment much shorter than configured that is still heavy for its length
#: (the first segment after the clock stops: a normal stretch, then the
#: clamp). 4K CCTV tops out around 32 Mbit/s.
SHORT_SHARE = 0.10
SHORT_SEGMENT_MBPS = 50.0
#: Recovery needs a segment that both lasted and carried a sane bitrate.
RECOVERED_SHARE = 0.50


def segment_seconds_for(camera) -> int:
    cfg = getattr(camera, "config", None)
    value = getattr(cfg, "recording_segment_seconds", None)
    if not value:
        value = getattr(settings, "recording_segment_seconds", 60) or 60
    return int(value)


def implied_mbps(duration_sec: float, file_size: int) -> float:
    return (file_size * 8) / max(duration_sec, 1e-3) / 1_000_000


def classify_segment(
    duration_sec: float | None, file_size: int | None, segment_seconds: int
) -> str | None:
    """``"degenerate"``, ``"normal"`` or ``None`` (no verdict).

    None covers everything we cannot honestly judge: a missing duration or
    size, a segment under MIN_JUDGED_BYTES (a tail), or a mid-length
    segment that is neither clearly broken nor clearly healthy.
    """
    if duration_sec is None or file_size is None or segment_seconds <= 0:
        return None
    mbps = implied_mbps(duration_sec, file_size)
    # Healthy first, whatever the size: a quiet camera's full-length segment
    # can be well under the judged floor (camera 23 wrote 7.5 MB a minute)
    # and it must still be able to clear the alarm.
    if duration_sec >= RECOVERED_SHARE * segment_seconds and mbps <= SHORT_SEGMENT_MBPS:
        return "normal"
    if file_size < MIN_JUDGED_BYTES:
        return None
    if mbps > DEGENERATE_MBPS:
        return "degenerate"
    if duration_sec < SHORT_SHARE * segment_seconds and mbps > SHORT_SEGMENT_MBPS:
        return "degenerate"
    return None


def _alarm_active(db, camera_id: int) -> bool:
    last = (
        db.query(CameraEvent)
        .filter(
            CameraEvent.camera_id == camera_id,
            CameraEvent.event_type == EVENT_TYPE,
        )
        .order_by(CameraEvent.id.desc())
        .first()
    )
    return last is not None and last.event_state == "active"


def note_segment(
    db,
    camera,
    duration_sec: float | None,
    file_size: int | None,
    *,
    now: datetime | None = None,
) -> dict | None:
    """Record what this completed segment says about the camera's recording.

    Returns the state transition it caused (a dict the caller publishes on
    the live bus, shaped like the recording watchdog's), or None when the
    alarm state did not change. Commits its own CameraEvent row.
    """
    verdict = classify_segment(duration_sec, file_size, segment_seconds_for(camera))
    if verdict is None:
        return None
    active = _alarm_active(db, camera.id)
    if verdict == "degenerate" and not active:
        state = "active"
        description = (
            f"Recording unplayable: {file_size / 1048576:.0f} MB written for "
            f"{duration_sec:.2f} s of video — the stream's timestamps are not "
            "advancing (camera clock wrap or relay fault); restart the camera stream"
        )[:200]
        recording_logger.warning(
            f"Recording degraded on camera {camera.id} ({camera.name or 'unnamed'}): "
            f"{file_size} bytes for {duration_sec:.3f}s"
        )
    elif verdict == "normal" and active:
        state = "inactive"
        description = "Recording timestamps recovered"
        recording_logger.info(
            f"Recording recovered on camera {camera.id} ({camera.name or 'unnamed'}): "
            f"a {duration_sec:.0f}s segment of {file_size} bytes"
        )
    else:
        return None
    when = now or datetime.now(UTC)
    db.add(
        CameraEvent(
            camera_id=camera.id,
            event_type=EVENT_TYPE,
            event_state=state,
            description=description,
            occurred_at=when,
        )
    )
    db.commit()
    return {
        "camera_id": camera.id,
        "camera_name": getattr(camera, "name", None),
        "event_type": EVENT_TYPE,
        "event_state": state,
        "description": description,
    }
