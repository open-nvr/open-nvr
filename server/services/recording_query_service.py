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

"""Recording listings and timelines served from the recordings DB index.

Replaces the per-request MediaMTX ``/list`` fan-outs and filesystem walks
with indexed SQL over the ``recordings`` table (fed by the segment-complete
webhook, converged by the reconciler).

Day buckets are LOCAL days: the ``tz`` the browser sends (IANA name) or the
deployment's recording timezone — never bare UTC dates (user requirement:
selecting a date must show local midnight → local midnight).

Contiguous one-minute clips are merged into continuous segments before they
leave the API, mirroring what MediaMTX's ``/list`` did for contiguous files —
so the timeline draws a handful of blocks per day, not 1,440.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import Recording
from services.recording_paths import get_recording_tz

# Clips whose gap is at most this are one continuous recording. MediaMTX
# closes one file and opens the next within well under a second; 2s absorbs
# duration rounding without swallowing real gaps.
MERGE_TOLERANCE_SECONDS = 2.0


def resolve_query_tz(tz_name: str | None) -> tzinfo:
    """The timezone for day bucketing: caller's IANA name, else deployment's."""
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            pass
    return get_recording_tz()


def local_day_bounds(date_str: str, tz: tzinfo) -> tuple[datetime, datetime]:
    """[local midnight, next local midnight) of ``date_str`` as UTC instants."""
    y, m, d = (int(x) for x in date_str.split("-"))
    start_local = datetime(y, m, d, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def camera_has_rows(db: Session, camera_id: int) -> bool:
    return (
        db.query(Recording.id)
        .filter(Recording.camera_id == camera_id)
        .first()
        is not None
    )


def merge_contiguous(
    rows: list[tuple],
    tolerance: float = MERGE_TOLERANCE_SECONDS,
) -> list[dict[str, Any]]:
    """Merge sorted clip rows into continuous WALL-CLOCK segments.

    Each row is ``(start_time, duration, end_time)``. ``end_time`` is the
    wall-clock end of the clip (see
    ``routers.mediamtx_hooks.compute_segment_end_time``): a camera whose
    delivered fps trails its nominal fps produces a "60s" media clip that
    actually covers ~72 real seconds, so contiguity/gap detection MUST use the
    wall-clock end — using ``start + media_duration`` fabricated the phantom
    10-15s gaps between back-to-back clips reported in #229. ``duration`` (the
    media/playable length) is only a fallback for rows written before
    ``end_time`` existed (e.g. legacy or reconciler rows with NULL end_time).

    Returns ``[{"start": datetime, "duration": float}, ...]`` where each
    ``duration`` is the wall-clock span of the merged run, so adjacent clips
    touch on the timeline instead of leaving a gap.
    """
    merged: list[dict[str, Any]] = []
    for row in rows:
        start = row[0]
        dur = float(row[1] or 0.0)
        end = row[2] if len(row) > 2 else None
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end is not None and end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        # Wall-clock end: the stored end_time, else the media-duration fallback.
        clip_end = end if end is not None else start + timedelta(seconds=dur)
        if clip_end < start:
            clip_end = start
        if merged:
            prev = merged[-1]
            prev_end = prev["start"] + timedelta(seconds=prev["duration"])
            if (start - prev_end).total_seconds() <= tolerance:
                new_end = max(prev_end, clip_end)
                prev["duration"] = (new_end - prev["start"]).total_seconds()
                continue
        merged.append(
            {"start": start, "duration": (clip_end - start).total_seconds()}
        )
    return merged


def day_segments(
    db: Session,
    camera_id: int,
    start_utc: datetime,
    end_utc: datetime,
) -> list[dict[str, Any]]:
    """Merged continuous segments for one camera in [start_utc, end_utc).

    One indexed range query on (camera_id, start_time); merging is O(rows).
    """
    rows = (
        db.query(Recording.start_time, Recording.duration, Recording.end_time)
        .filter(
            Recording.camera_id == camera_id,
            Recording.start_time >= start_utc,
            Recording.start_time < end_utc,
        )
        .order_by(Recording.start_time.asc())
        .all()
    )
    return merge_contiguous(list(rows))


def day_summaries(
    db: Session,
    camera_ids: list[int],
    tz: tzinfo,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
) -> dict[int, list[dict[str, Any]]]:
    """Per-camera, per-LOCAL-day aggregates for the recordings overview.

    Returns ``{camera_id: [{date, total_duration, segment_count, first_start},
    ...]}`` with days sorted descending — the shape ``/recordings/list``
    always exposed.

    Uses SQL grouping on Postgres (timezone-shifted date_trunc); the SQLite
    fallback (tests) groups in Python.
    """
    if not camera_ids:
        return {}

    q = db.query(
        Recording.camera_id, Recording.start_time, Recording.duration
    ).filter(Recording.camera_id.in_(camera_ids))
    if start_utc is not None:
        q = q.filter(Recording.start_time >= start_utc)
    if end_utc is not None:
        q = q.filter(Recording.start_time < end_utc)

    dialect = db.get_bind().dialect.name
    result: dict[int, dict[str, dict[str, Any]]] = {}

    if dialect == "postgresql":
        tz_key = getattr(tz, "key", None) or "UTC"
        local_ts = func.timezone(tz_key, Recording.start_time)
        day_expr = func.to_char(local_ts, "YYYY-MM-DD")
        agg_q = (
            db.query(
                Recording.camera_id,
                day_expr.label("day"),
                func.count().label("clips"),
                func.coalesce(func.sum(Recording.duration), 0.0).label("dur"),
                func.min(Recording.start_time).label("first_start"),
            )
            .filter(Recording.camera_id.in_(camera_ids))
        )
        if start_utc is not None:
            agg_q = agg_q.filter(Recording.start_time >= start_utc)
        if end_utc is not None:
            agg_q = agg_q.filter(Recording.start_time < end_utc)
        for cam_id, day, clips, dur, first_start in agg_q.group_by(
            Recording.camera_id, day_expr
        ):
            if first_start.tzinfo is None:
                first_start = first_start.replace(tzinfo=UTC)
            result.setdefault(cam_id, {})[day] = {
                "date": day,
                "total_duration": float(dur or 0.0),
                "segment_count": int(clips),
                "first_start": first_start,
            }
    else:
        # Portable fallback: bucket in Python. Fine for test-sized data.
        for cam_id, start_time, duration in q.all():
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=UTC)
            day = start_time.astimezone(tz).strftime("%Y-%m-%d")
            bucket = result.setdefault(cam_id, {}).setdefault(
                day,
                {
                    "date": day,
                    "total_duration": 0.0,
                    "segment_count": 0,
                    "first_start": start_time,
                },
            )
            bucket["total_duration"] += float(duration or 0.0)
            bucket["segment_count"] += 1
            if start_time < bucket["first_start"]:
                bucket["first_start"] = start_time
    return {
        cam_id: sorted(days.values(), key=lambda d: d["date"], reverse=True)
        for cam_id, days in result.items()
    }
