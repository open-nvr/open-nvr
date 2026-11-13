# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Small cached JPEG stills of cameras, for pickers (the tour editor).

Not ``GET /cameras/{id}/snapshot``: that goes through the inference
capture pool, which keeps one OpenCV capture open per camera for good, so
a picker scrolling through 1000 cameras would leave 1000 RTSP readers
running inside core. A thumbnail here is one frame from a short-lived
``ffmpeg``, read from MediaMTX's existing session for the camera (the
inference tap URL), so the recorder sees no extra connection. A grab waits
for the camera's next keyframe, seconds, so results are cached for five
minutes (a picker needs no fresher still), failures for 15 s, at most four
grabs run at once, and concurrent requests for one camera share one grab.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

TTL_S = 300.0
FAIL_TTL_S = 15.0
CONCURRENCY = 4
TIMEOUT_S = 8.0
WIDTH = 320
MAX_ENTRIES = 2000

_cache: dict[int, tuple[float, bytes | None]] = {}
# Per event loop: asyncio primitives bind to the loop that first uses them.
_loop_state: dict[int, tuple[asyncio.Semaphore, dict[int, asyncio.Future]]] = {}


def _state() -> tuple[asyncio.Semaphore, dict[int, asyncio.Future]]:
    loop = asyncio.get_running_loop()
    st = _loop_state.get(id(loop))
    if st is None:
        _loop_state.clear()  # one live loop in production; drop stale ones
        st = (asyncio.Semaphore(CONCURRENCY), {})
        _loop_state[id(loop)] = st
    return st


async def grab_frame(url: str) -> bytes | None:
    """One JPEG frame from *url*, scaled to WIDTH px wide, or None."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-rtsp_transport", "tcp", "-i", url,
        "-frames:v", "1", "-vf", f"scale={WIDTH}:-2", "-q:v", "6",
        "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    return out if proc.returncode == 0 and out[:2] == b"\xff\xd8" else None


def _source_url(camera_id: int, rtsp_url: str) -> str:
    from services.kai_c_service import get_kai_c_service

    return get_kai_c_service()._resolve_inference_rtsp_url(camera_id, rtsp_url)


async def get_thumbnail(camera_id: int, rtsp_url: str) -> bytes | None:
    now = time.monotonic()
    hit = _cache.get(camera_id)
    if hit and now < hit[0]:
        return hit[1]
    sem, inflight = _state()
    pending = inflight.get(camera_id)
    if pending is not None:
        return await pending

    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    inflight[camera_id] = fut
    try:
        async with sem:
            try:
                jpeg = await grab_frame(_source_url(camera_id, rtsp_url))
            except Exception:
                logger.debug("thumbnail grab failed for camera %s", camera_id, exc_info=True)
                jpeg = None
        if len(_cache) >= MAX_ENTRIES:
            for key, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[: MAX_ENTRIES // 10]:
                _cache.pop(key, None)
        _cache[camera_id] = (time.monotonic() + (TTL_S if jpeg else FAIL_TTL_S), jpeg)
        fut.set_result(jpeg)
        return jpeg
    except BaseException as exc:
        if not fut.done():
            fut.set_exception(exc)
        raise
    finally:
        inflight.pop(camera_id, None)


def clear() -> None:
    """Forget every cached still (tests)."""
    _cache.clear()
