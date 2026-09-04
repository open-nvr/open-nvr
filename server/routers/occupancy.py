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
from models import (
    Camera, OccupancyFootfall, OccupancyHeatmap, OccupancySample, User,
)

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


def occupancy_heatmap(
    db: Session,
    *,
    camera_id: int,
    hours: int = 24,
    owner_id: int | None = None,
    now: datetime | None = None,
) -> dict:
    """The camera's heat grid summed over the last ``hours`` — the read
    side of ``occupancy.heatmap.v1``. Rows of a different grid shape (a
    producer reconfigured mid-window) are skipped rather than mixed.

    ``cells`` is the raw hit count per cell; ``max`` is the peak; the
    page normalises against it. ``frames`` is how many detection frames
    fed the window, so "hits per frame" is available to compare windows
    of different length."""
    now = now or datetime.now(timezone.utc)
    hours = max(1, min(int(hours), 24 * 30))
    start = (now - timedelta(hours=hours)).replace(minute=0, second=0,
                                                   microsecond=0)
    q = db.query(OccupancyHeatmap).filter(
        OccupancyHeatmap.camera_id == camera_id,
        OccupancyHeatmap.hour_start >= start,
    )
    if owner_id is not None:
        q = q.join(Camera, Camera.id == OccupancyHeatmap.camera_id).filter(
            Camera.owner_id == owner_id
        )
    cols = rows = 0
    cells: list[int] = []
    frames = 0
    hours_covered = 0
    latest: datetime | None = None
    for row in q.order_by(OccupancyHeatmap.hour_start.asc()).all():
        if not cols:
            cols, rows = int(row.cols), int(row.rows)
            cells = [0] * (cols * rows)
        elif (row.cols, row.rows) != (cols, rows):
            continue
        src = row.cells if isinstance(row.cells, list) else []
        for i, v in enumerate(src[:len(cells)]):
            if isinstance(v, int) and v > 0:
                cells[i] += v
        frames += int(row.frames or 0)
        hours_covered += 1
        upd = row.updated_at
        if upd is not None and (latest is None or upd > latest):
            latest = upd
    return {
        "camera_id": camera_id,
        "hours": hours,
        "cols": cols,
        "rows": rows,
        "cells": cells,
        "max": max(cells) if cells else 0,
        "frames": frames,
        "hours_covered": hours_covered,
        "updated_at": latest.isoformat() if latest else None,
    }


@router.get("/occupancy/heatmap")
async def get_occupancy_heatmap(
    camera_id: int,
    hours: int = 24,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Where watched entities stood on one camera over the window —
    a unit-space grid the Occupancy page paints over a camera still."""
    return occupancy_heatmap(
        db,
        camera_id=camera_id,
        hours=hours,
        owner_id=None if current_user.is_superuser else current_user.id,
    )


def occupancy_footfall(
    db: Session,
    *,
    hours: int = 24,
    owner_id: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Entries, exits and dwell per camera over the window, as hourly
    buckets plus totals — the read side of ``occupancy.footfall.v1``."""
    now = now or datetime.now(timezone.utc)
    hours = max(1, min(int(hours), 24 * 30))
    start = (now - timedelta(hours=hours)).replace(minute=0, second=0,
                                                   microsecond=0)
    q = db.query(OccupancyFootfall).filter(OccupancyFootfall.hour_start >= start)
    if owner_id is not None:
        q = q.join(Camera, Camera.id == OccupancyFootfall.camera_id).filter(
            Camera.owner_id == owner_id
        )
    per_camera: dict[int, dict] = {}
    for row in q.order_by(OccupancyFootfall.hour_start.asc()).all():
        cam = per_camera.setdefault(row.camera_id, {
            "camera_id": row.camera_id, "entries": 0, "exits": 0,
            "dwell_count": 0, "dwell_seconds": 0.0, "dwell_max_seconds": 0.0,
            "hours": [],
        })
        cam["entries"] += int(row.entries or 0)
        cam["exits"] += int(row.exits or 0)
        cam["dwell_count"] += int(row.dwell_count or 0)
        cam["dwell_seconds"] += float(row.dwell_seconds or 0.0)
        cam["dwell_max_seconds"] = max(cam["dwell_max_seconds"],
                                       float(row.dwell_max_seconds or 0.0))
        hs = row.hour_start if row.hour_start.tzinfo else \
            row.hour_start.replace(tzinfo=timezone.utc)
        cam["hours"].append({
            "t": hs.isoformat(),
            "entries": int(row.entries or 0),
            "exits": int(row.exits or 0),
            "dwell_avg_seconds": round(float(row.dwell_seconds or 0.0)
                                       / row.dwell_count, 1)
            if row.dwell_count else None,
        })
    cameras = []
    for cam in per_camera.values():
        cam["dwell_avg_seconds"] = (
            round(cam["dwell_seconds"] / cam["dwell_count"], 1)
            if cam["dwell_count"] else None)
        cam["dwell_max_seconds"] = round(cam["dwell_max_seconds"], 1)
        cam["dwell_seconds"] = round(cam["dwell_seconds"], 1)
        cameras.append(cam)
    cameras.sort(key=lambda c: c["camera_id"])
    total_dwell_count = sum(c["dwell_count"] for c in cameras)
    total_dwell_seconds = sum(c["dwell_seconds"] for c in cameras)
    return {
        "hours": hours,
        "cameras": cameras,
        "totals": {
            "entries": sum(c["entries"] for c in cameras),
            "exits": sum(c["exits"] for c in cameras),
            "dwell_count": total_dwell_count,
            "dwell_avg_seconds": (round(total_dwell_seconds / total_dwell_count, 1)
                                  if total_dwell_count else None),
            "dwell_max_seconds": round(max(
                [c["dwell_max_seconds"] for c in cameras], default=0.0), 1),
        },
    }


@router.get("/occupancy/footfall")
async def get_occupancy_footfall(
    hours: int = 24,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Entries / exits through each camera's entry line and dwell inside
    its zone, hourly and in total, over the window."""
    return occupancy_footfall(
        db,
        hours=hours,
        owner_id=None if current_user.is_superuser else current_user.id,
    )


def occupancy_report(
    db: Session,
    *,
    days: int = 7,
    owner_id: int | None = None,
    now: datetime | None = None,
    tz_offset_minutes: int = 0,
) -> dict:
    """The printable period report: per camera, peak occupancy (and
    when), average occupancy, the busiest hour of day, footfall totals,
    average and longest stay, plus fleet-wide totals and a daily
    footfall series. Built from the three history tables; nothing new
    is measured for it. Hour-of-day and day buckets are formed in the
    operator's LOCAL time (``tz_offset_minutes`` east of UTC, as the
    browser reports it) — a busiest hour of "03:00" is meaningless if it
    is UTC on an install in Delhi."""
    now = now or datetime.now(timezone.utc)
    days = max(1, min(int(days), 92))
    local = timezone(timedelta(minutes=max(-14 * 60, min(14 * 60,
                                                          int(tz_offset_minutes)))))
    end = now.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)

    # ── occupancy samples → peak / average / busiest hour ──────────
    sq = db.query(OccupancySample).filter(OccupancySample.ts >= start,
                                          OccupancySample.ts < end)
    if owner_id is not None:
        sq = sq.join(Camera, Camera.id == OccupancySample.camera_id).filter(
            Camera.owner_id == owner_id)
    per: dict[int, dict] = {}

    def _cam(camera_id: int) -> dict:
        return per.setdefault(camera_id, {
            "camera_id": camera_id,
            "samples": 0, "count_sum": 0, "peak": 0, "peak_at": None,
            "over_transitions": 0, "hour_of_day": {},
            "entries": 0, "exits": 0, "dwell_count": 0,
            "dwell_seconds": 0.0, "dwell_max_seconds": 0.0,
            "daily": {},
        })

    for row in sq.all():
        ts = row.ts if row.ts.tzinfo else row.ts.replace(tzinfo=timezone.utc)
        c = _cam(row.camera_id)
        c["samples"] += 1
        c["count_sum"] += int(row.count)
        if int(row.count) > c["peak"] or c["peak_at"] is None:
            c["peak"], c["peak_at"] = int(row.count), ts
        if (row.level or "") == "over":
            c["over_transitions"] += 1
        c["hour_of_day"].setdefault(ts.astimezone(local).hour, []).append(
            int(row.count))

    # ── footfall rows → entries / exits / dwell, per day ──────────
    fq = db.query(OccupancyFootfall).filter(OccupancyFootfall.hour_start >= start,
                                            OccupancyFootfall.hour_start < end)
    if owner_id is not None:
        fq = fq.join(Camera, Camera.id == OccupancyFootfall.camera_id).filter(
            Camera.owner_id == owner_id)
    for row in fq.all():
        hs = row.hour_start if row.hour_start.tzinfo else \
            row.hour_start.replace(tzinfo=timezone.utc)
        c = _cam(row.camera_id)
        c["entries"] += int(row.entries or 0)
        c["exits"] += int(row.exits or 0)
        c["dwell_count"] += int(row.dwell_count or 0)
        c["dwell_seconds"] += float(row.dwell_seconds or 0.0)
        c["dwell_max_seconds"] = max(c["dwell_max_seconds"],
                                     float(row.dwell_max_seconds or 0.0))
        day = hs.astimezone(local).date().isoformat()
        d = c["daily"].setdefault(day, {"entries": 0, "exits": 0})
        d["entries"] += int(row.entries or 0)
        d["exits"] += int(row.exits or 0)

    cameras = []
    fleet_daily: dict[str, dict] = {}
    for c in sorted(per.values(), key=lambda v: v["camera_id"]):
        busiest = None
        if c["hour_of_day"]:
            hour, counts = max(c["hour_of_day"].items(),
                               key=lambda kv: sum(kv[1]) / len(kv[1]))
            busiest = {"hour": hour, "avg": round(sum(counts) / len(counts), 1)}
        for day, d in c["daily"].items():
            fd = fleet_daily.setdefault(day, {"day": day, "entries": 0, "exits": 0})
            fd["entries"] += d["entries"]
            fd["exits"] += d["exits"]
        cameras.append({
            "camera_id": c["camera_id"],
            "samples": c["samples"],
            "avg_occupancy": (round(c["count_sum"] / c["samples"], 1)
                              if c["samples"] else None),
            "peak_occupancy": c["peak"],
            "peak_at": c["peak_at"].isoformat() if c["peak_at"] else None,
            "over_transitions": c["over_transitions"],
            "busiest_hour": busiest,
            "entries": c["entries"],
            "exits": c["exits"],
            "dwell_count": c["dwell_count"],
            "dwell_avg_seconds": (round(c["dwell_seconds"] / c["dwell_count"], 1)
                                  if c["dwell_count"] else None),
            "dwell_max_seconds": round(c["dwell_max_seconds"], 1),
            "daily": [dict(day=k, **v) for k, v in sorted(c["daily"].items())],
        })
    total_dwell_count = sum(c["dwell_count"] for c in cameras)
    total_dwell_seconds = sum(per[c["camera_id"]]["dwell_seconds"] for c in cameras)
    peak_cam = max(cameras, key=lambda c: c["peak_occupancy"], default=None)
    return {
        "days": days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "generated_at": now.isoformat(),
        "tz_offset_minutes": int(tz_offset_minutes),
        "cameras": cameras,
        "daily": [fleet_daily[k] for k in sorted(fleet_daily)],
        "totals": {
            "entries": sum(c["entries"] for c in cameras),
            "exits": sum(c["exits"] for c in cameras),
            "dwell_count": total_dwell_count,
            "dwell_avg_seconds": (round(total_dwell_seconds / total_dwell_count, 1)
                                  if total_dwell_count else None),
            "dwell_max_seconds": round(max(
                [c["dwell_max_seconds"] for c in cameras], default=0.0), 1),
            "peak_occupancy": peak_cam["peak_occupancy"] if peak_cam else 0,
            "peak_camera_id": peak_cam["camera_id"] if peak_cam else None,
            "peak_at": peak_cam["peak_at"] if peak_cam else None,
            "over_transitions": sum(c["over_transitions"] for c in cameras),
        },
    }


@router.get("/occupancy/report")
async def get_occupancy_report(
    days: int = 7,
    tz_offset_minutes: int = 0,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """The period occupancy report (printable from the Occupancy page):
    peaks, averages, busiest hours, footfall and stays per camera.
    ``tz_offset_minutes`` = the browser's offset east of UTC."""
    return occupancy_report(
        db,
        days=days,
        owner_id=None if current_user.is_superuser else current_user.id,
        tz_offset_minutes=tz_offset_minutes,
    )
