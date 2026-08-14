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

"""Recording storage layout: path building and timestamp parsing.

One shared implementation for every consumer (webhook, reconciler, storage
listing, playback file resolution, retention) — the previous copies drifted.

Three on-disk layouts coexist:

1. NEW (current):    ``cam-<id>/YYYY-MM-DD/HH/MM-SS-ffffff.mp4``
   Named in the DEPLOYMENT'S LOCAL TIMEZONE (the TZ env var, shared with the
   MediaMTX container which expands the recordPath pattern in its process-
   local time). One-minute clips grouped in hour directories.
2. Legacy flat:      ``cam-<id>/YYYY/MM/DD/HH-MM-SS-ffffff.mp4``
3. Legacy nested:    ``cam-<id>/YYYY/MM/DD/HH-MM-SS-ffffff/cam-<id>.mp4``
   Both legacy layouts were written while every container ran UTC, so their
   names are always parsed as UTC.

The layout itself encodes the timezone convention — no cutover flag needed:
old files keep meaning what they always meant, new files are local-named.
Parsed timestamps are returned as tz-aware UTC; local time exists only at
the filesystem-naming and presentation boundaries.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from core.config import settings

# MediaMTX recordPath pattern for the new layout (relative to the recordings
# root). %path expands to the camera path (cam-<id>).
NEW_LAYOUT_RECORD_PATH = "%path/%Y-%m-%d/%H/%M-%S-%f"


def get_recording_tz() -> tzinfo:
    """Timezone that names NEW-layout files.

    ``settings.recording_timezone`` (IANA name) when set; otherwise the
    process-local timezone — which docker-compose sets identically (TZ env)
    on this container and the MediaMTX container that writes the files.
    """
    name = settings.recording_timezone
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    local = datetime.now().astimezone().tzinfo
    return local if local is not None else UTC


def _fold_disambiguate(naive: datetime, tz: tzinfo, mtime: float | None) -> datetime:
    """Resolve a DST fall-back ambiguity using the file's mtime.

    During the repeated hour a local name maps to two UTC instants (fold 0/1).
    The file's mtime is ground truth for roughly when it was written, so pick
    the fold closer to it. Zones without DST (e.g. IST) never take the second
    branch.
    """
    first = naive.replace(tzinfo=tz, fold=0)
    second = naive.replace(tzinfo=tz, fold=1)
    if first.utcoffset() == second.utcoffset() or mtime is None:
        return first
    return min(
        (first, second),
        key=lambda d: abs(d.astimezone(UTC).timestamp() - mtime),
    )


def parse_recording_time(
    path: Path | str, *, mtime: float | None = None
) -> datetime | None:
    """Timestamp (tz-aware UTC) encoded in a recording file's path.

    Accepts absolute or root-relative paths; only the trailing components are
    inspected. Returns None for paths that match no known layout. ``mtime``
    (file st_mtime) is optional and only used to disambiguate DST fall-back
    names in the new layout.
    """
    p = Path(path)
    parts = p.parts
    try:
        # NEW layout: .../cam-<id>/YYYY-MM-DD/HH/MM-SS-ffffff.mp4
        if len(parts) >= 3:
            date_dir, hour_dir, fname = parts[-3], parts[-2], parts[-1]
            stem = fname.split(".")[0]
            if (
                len(date_dir) == 10
                and date_dir[4] == "-"
                and date_dir[7] == "-"
                and hour_dir.isdigit()
                and len(hour_dir) <= 2
            ):
                t = stem.split("-")
                if len(t) >= 2 and t[0].isdigit() and t[1].isdigit():
                    y, mo, d = (int(x) for x in date_dir.split("-"))
                    naive = datetime(y, mo, d, int(hour_dir), int(t[0]), int(t[1]))
                    return _fold_disambiguate(naive, get_recording_tz(), mtime).astimezone(UTC)

        # Legacy nested: .../cam-<id>/YYYY/MM/DD/HH-MM-SS-ffffff/cam-<id>.mp4
        if len(parts) >= 5:
            parent = parts[-2]
            t = parent.split("-")
            if len(t) >= 3 and t[0].isdigit() and t[1].isdigit() and t[2].isdigit():
                yyyy, mm, dd = parts[-5], parts[-4], parts[-3]
                if yyyy.isdigit() and len(yyyy) == 4 and mm.isdigit() and dd.isdigit():
                    return datetime(
                        int(yyyy), int(mm), int(dd),
                        int(t[0]), int(t[1]), int(t[2]),
                        tzinfo=UTC,
                    )

        # Legacy flat: .../cam-<id>/YYYY/MM/DD/HH-MM-SS-ffffff.mp4
        if len(parts) >= 4:
            yyyy, mm, dd = parts[-4], parts[-3], parts[-2]
            stem = parts[-1].split(".")[0]
            t = stem.split("-")
            if (
                yyyy.isdigit() and len(yyyy) == 4
                and mm.isdigit() and dd.isdigit()
                and len(t) >= 3
                and t[0].isdigit() and t[1].isdigit() and t[2].isdigit()
            ):
                return datetime(
                    int(yyyy), int(mm), int(dd),
                    int(t[0]), int(t[1]), int(t[2]),
                    tzinfo=UTC,
                )
    except Exception:
        return None
    return None


def hour_dir_for(camera_id: int, instant_utc: datetime, root: Path) -> Path:
    """The NEW-layout hour directory that contains ``instant_utc``.

    Used by the live-edge probe: globbing one hour directory (≤ ~60 files)
    replaces the full-archive walk.
    """
    local = instant_utc.astimezone(get_recording_tz())
    return (
        root
        / f"cam-{int(camera_id)}"
        / local.strftime("%Y-%m-%d")
        / local.strftime("%H")
    )


def iter_recording_files(camera_dir: Path):
    """Yield every .mp4 under a camera directory, all layouts.

    Depth is bounded (≤4 levels) in every layout, so a recursive glob is
    equivalent to the old hand-rolled year/month/day walk while also covering
    the new date/hour layout.
    """
    if not camera_dir.exists():
        return
    yield from camera_dir.rglob("*.mp4")


def find_latest_recording_file(
    camera_id: int, root: Path, now_utc: datetime | None = None
) -> tuple[Path, datetime] | None:
    """The newest recording file for a camera, WITHOUT walking the archive.

    Only the directories that can contain the newest file are globbed: the
    current and previous NEW-layout hour dirs (≤ ~120 files) plus the legacy
    UTC-named day dirs for today/yesterday (for archives predating the layout
    change). Returns ``(path, start_time_utc)`` or None.

    This replaces the old full-archive ``list_recordings`` walk on the
    live-edge hot path (polled every 10–15s per camera by the timeline).
    """
    now_utc = now_utc or datetime.now(UTC)
    cam_dir = root / f"cam-{int(camera_id)}"
    candidates: list[Path] = []

    for delta_h in (0, 1):
        hd = hour_dir_for(camera_id, now_utc - timedelta(hours=delta_h), root)
        if hd.is_dir():
            candidates.extend(hd.glob("*.mp4"))

    for delta_d in (0, 1):
        day = now_utc - timedelta(days=delta_d)
        legacy_day = cam_dir / day.strftime("%Y") / day.strftime("%m") / day.strftime("%d")
        if legacy_day.is_dir():
            candidates.extend(legacy_day.glob("*.mp4"))
            candidates.extend(legacy_day.glob("*/*.mp4"))

    best: tuple[Path, datetime] | None = None
    for f in candidates:
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        ts = parse_recording_time(f, mtime=mtime)
        if ts is None:
            continue
        if best is None or ts > best[1]:
            best = (f, ts)
    return best
