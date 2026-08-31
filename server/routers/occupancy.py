# Copyright (c) 2026 OpenNVR
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Occupancy history — the read side of ``occupancy.changed.v1``.

The consumer writes samples; this router serves the Occupancy page's
charts: a bucketed series per camera over the requested window plus
busiest-hours-of-day over the last 7 days. Owner-scoped through the
cameras table like every other read surface."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.database import get_db
from core.auth import get_current_active_user
from models import Camera, OccupancySample, User

router = APIRouter(tags=["occupancy"])


def occupancy_history(
    db: Session,
    *,
    hours: int = 24,
    owner_id: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Bucketed occupancy series + busiest hours. Bucketing happens in
    Python (dialect portability; the feed is change-driven so windows
    stay small)."""
    now = now or datetime.now(timezone.utc)
    hours = max(1, min(int(hours), 24 * 7))
    bucket_minutes = 60 if hours > 24 else 15
    start = now - timedelta(hours=hours)
    week_start = now - timedelta(days=7)

    q = db.query(OccupancySample).filter(OccupancySample.ts >= week_start)
    if owner_id is not None:
        q = q.join(Camera, Camera.id == OccupancySample.camera_id).filter(
            Camera.owner_id == owner_id
        )

    series: dict[int, dict[datetime, list[int]]] = {}
    hour_of_day: dict[int, list[int]] = {}
    for row in q.all():
        ts = row.ts if row.ts.tzinfo else row.ts.replace(tzinfo=timezone.utc)
        hour_of_day.setdefault(ts.hour, []).append(row.count)
        if ts < start:
            continue
        bucket = ts.replace(
            minute=(ts.minute // bucket_minutes) * bucket_minutes
            if bucket_minutes < 60 else 0,
            second=0, microsecond=0,
        )
        series.setdefault(row.camera_id, {}).setdefault(bucket, []).append(
            row.count)

    cameras = [
        {
            "camera_id": camera_id,
            "samples": [
                {
                    "t": bucket.isoformat(),
                    "avg": round(sum(counts) / len(counts), 1),
                    "max": max(counts),
                }
                for bucket, counts in sorted(buckets.items())
            ],
        }
        for camera_id, buckets in sorted(series.items())
    ]
    busiest = [
        {"hour": hour, "avg": round(sum(counts) / len(counts), 1)}
        for hour, counts in sorted(hour_of_day.items())
    ]
    return {
        "hours": hours,
        "bucket_minutes": bucket_minutes,
        "cameras": cameras,
        "busiest_hours": busiest,
    }


@router.get("/occupancy/history")
async def get_occupancy_history(
    hours: int = 24,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Occupancy trends for the charts: bucketed avg/max per camera over
    the window, and busiest hours of day over the last 7 days."""
    return occupancy_history(
        db,
        hours=hours,
        owner_id=None if current_user.is_superuser else current_user.id,
    )
