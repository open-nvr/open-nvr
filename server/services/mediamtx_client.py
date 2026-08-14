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

"""Shared async client for MediaMTX's playback API.

Replaces the per-request synchronous ``requests`` calls that used to run
inside ``async def`` handlers and freeze the event loop (worst case
``cameras × 10s`` for a single ``/recordings/list`` request, stalling every
other request including in-flight video byte-ranges).

Provides:
- one process-wide ``httpx.AsyncClient`` (created lazily, closed at shutdown)
- a TTL-cached availability probe
- a TTL-cached, optionally date-bounded ``/list`` fetch
- a semaphore-capped parallel fan-out over many camera paths
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from core.config import settings
from core.logging_config import recording_logger

# Cap concurrent MediaMTX requests so a large camera fleet can't stampede it.
_FANOUT_LIMIT = 8

# Availability probe cache
_AVAILABILITY_TTL_SECONDS = 15.0
# /list responses for a closed historical range change only when retention
# deletes something — cache generously. Ranges touching "now" grow every
# second — cache just long enough to absorb bursts (several tiles polling).
_LIST_TTL_HISTORIC_SECONDS = 30.0
_LIST_TTL_LIVE_SECONDS = 5.0
_LIST_CACHE_MAX = 512

_client: httpx.AsyncClient | None = None
_semaphore: asyncio.Semaphore | None = None
_avail_cache: tuple[float, bool] | None = None
_list_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0))
    return _client


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_FANOUT_LIMIT)
    return _semaphore


async def aclose() -> None:
    """Close the shared client (lifespan shutdown)."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def invalidate_caches() -> None:
    global _avail_cache
    _avail_cache = None
    _list_cache.clear()


async def check_available() -> bool:
    """Whether the MediaMTX playback server answers HTTP. TTL-cached.

    Probes the server root, NOT ``/list?path=__health__``: listing a
    nonexistent path made MediaMTX log 'ERR path __health__ is not
    configured' on every probe (#218's log noise). Any HTTP answer
    (200/400/401/404/500) means the playback server is up — which is all
    this check ever asserted.
    """
    global _avail_cache
    if not settings.mediamtx_playback_url:  # url-internal-ok: guard check on backend-side config, never returned to browser
        return False

    now = time.monotonic()
    cached = _avail_cache
    if cached is not None and cached[0] > now:
        return cached[1]

    try:
        response = await get_client().get(
            f"{settings.mediamtx_playback_url}/", timeout=2.0  # url-internal-ok: server-side health probe to mediamtx playback server
        )
        available = response.status_code in (200, 400, 401, 404, 500)
    except Exception:
        available = False
    _avail_cache = (now + _AVAILABILITY_TTL_SECONDS, available)
    return available


def _fmt_rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def list_segments(
    path: str,
    start: datetime | None = None,
    end: datetime | None = None,
    *,
    timeout: float = 3.0,
    use_cache: bool = True,
) -> list[dict[str, Any]] | None:
    """Fetch MediaMTX's segment list for one camera path.

    ``start``/``end`` are passed to MediaMTX so the full recording history is
    never transferred for a single-day view. Returns the parsed JSON list, or
    ``None`` on any error / non-200 (callers treat that as "unavailable",
    matching the old behaviour).
    """
    if not settings.mediamtx_playback_url:  # url-internal-ok: guard check on backend-side config, never returned to browser
        return None

    now_wall = datetime.now(UTC)
    live_range = end is None or end >= now_wall
    ttl = _LIST_TTL_LIVE_SECONDS if live_range else _LIST_TTL_HISTORIC_SECONDS

    params: dict[str, str] = {"path": path}
    if start is not None:
        params["start"] = _fmt_rfc3339(start)
    if end is not None:
        params["end"] = _fmt_rfc3339(end)

    cache_key = f"{path}|{params.get('start', '')}|{params.get('end', '')}"
    now_mono = time.monotonic()
    if use_cache:
        cached = _list_cache.get(cache_key)
        if cached is not None and cached[0] > now_mono:
            # Per-call shallow copies: callers annotate segment dicts in
            # place, which must never leak into the shared cache entry.
            return [dict(s) for s in cached[1]]

    try:
        response = await get_client().get(
            f"{settings.mediamtx_playback_url}/list",  # url-internal-ok: server-side LIST call to mediamtx playback server
            params=params,
            timeout=timeout,
        )
        if response.status_code != 200:
            return None
        segments = response.json() or []
    except Exception as e:
        recording_logger.warning(f"MediaMTX list failed for path {path}: {e}")
        return None

    if use_cache:
        if len(_list_cache) >= _LIST_CACHE_MAX:
            _list_cache.clear()
        _list_cache[cache_key] = (now_mono + ttl, segments)
        return [dict(s) for s in segments]
    return segments


async def list_segments_many(
    paths: list[str],
    start: datetime | None = None,
    end: datetime | None = None,
    *,
    timeout: float = 3.0,
) -> dict[str, list[dict[str, Any]] | None]:
    """Fetch segment lists for many camera paths concurrently.

    Concurrency is capped at ``_FANOUT_LIMIT`` so a large fleet doesn't
    stampede MediaMTX; failures are per-path ``None``, never an exception.
    """
    sem = _get_semaphore()

    async def one(p: str) -> tuple[str, list[dict[str, Any]] | None]:
        async with sem:
            return p, await list_segments(p, start, end, timeout=timeout)

    results = await asyncio.gather(*(one(p) for p in paths))
    return dict(results)
