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

"""Notice the system clock jumping and repair what it breaks (issue #1023).

A board with no battery-backed RTC boots on the time it last saved, starts
the stack, and only later steps the clock when NTP answers; an admin can
also simply set the time. Streams connected before the step keep recording
segments dated by the old clock (MediaMTX measures elapsed time on the
monotonic clock), so footage lands at the wrong place on the timeline and
the camera reads "Recording stalled" while it records.

The wall clock minus the monotonic clock is constant while nothing steps
the clock (NTP slewing moves it by milliseconds); a change of more than
``JUMP_THRESHOLD_S`` between two checks is a step. On one, every recording
camera stream is reconnected so new segments carry the corrected time, and
the step is written to the audit log: footage recorded between the step and
the reconnect keeps its wrong time, and an operator reviewing evidence
needs to know that window exists.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import logging
import platform
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

CHECK_INTERVAL_S = 5.0
#: Smaller than any step that matters for evidence, larger than anything
#: routine NTP discipline does to a running clock.
JUMP_THRESHOLD_S = 10.0
#: A step often comes in a burst (``date`` set twice, NTP's follow-up
#: correction): wait for it to finish and reconnect streams once.
SETTLE_S = 5.0


def _clock_offset() -> float:
    return time.time() - time.monotonic()


#: The most recent step this process saw, for the dashboard; the audit log
#: keeps every one.
_last_jump: dict[str, object] | None = None


class _Timex(ctypes.Structure):
    # The head of Linux's struct timex; the rest is padding the kernel may
    # write into. Same layout on x86_64 and arm64 (both LP64).
    _fields_ = [
        ("modes", ctypes.c_uint),
        ("offset", ctypes.c_long),
        ("freq", ctypes.c_long),
        ("maxerror", ctypes.c_long),
        ("esterror", ctypes.c_long),
        ("status", ctypes.c_int),
        ("_rest", ctypes.c_byte * 256),
    ]


_STA_UNSYNC = 0x0040


def system_clock_synchronized() -> bool | None:
    """Whether the kernel's clock is disciplined by a time server.

    The same flag ``timedatectl`` reports as "System clock synchronized",
    read with a modes=0 ``adjtimex`` that changes nothing and needs no
    capability, so it works from inside the container (the kernel is the
    host's). None when it cannot be known: not Linux, the call fails, or a
    Docker Desktop / WSL2 VM whose clock the host OS sets without NTP, so
    the flag would read "unsynchronised" forever.
    """
    if sys.platform != "linux":
        return None
    release = platform.release().lower()
    if "linuxkit" in release or "microsoft" in release:
        return None
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        tx = _Timex()
        if libc.adjtimex(ctypes.byref(tx)) < 0:
            return None
        return not (tx.status & _STA_UNSYNC)
    except Exception:  # no libc, blocked syscall: unknown
        return None


def clock_status() -> dict[str, object]:
    """What the dashboard's Clock chip shows."""
    return {"synchronized": system_clock_synchronized(), "last_jump": _last_jump}


class ClockStepDetector:
    """Report how far the wall clock moved relative to the monotonic one."""

    def __init__(self, offset: Callable[[], float] = _clock_offset) -> None:
        self._offset = offset
        self._last = offset()

    def step(self) -> float:
        """Seconds the wall clock was stepped since the previous call
        (positive = moved forward). Slow drift is absorbed call by call."""
        now = self._offset()
        delta, self._last = now - self._last, now
        return delta


def _audit(delta_s: float, result: dict[str, list[str]]) -> None:
    from core.database import SessionLocal
    from services.audit_service import write_audit_log

    db = SessionLocal()
    try:
        write_audit_log(
            db,
            action="system.clock_jump",
            entity_type="system",
            details={
                "delta_seconds": round(delta_s, 1),
                "detected_at": datetime.now(UTC).isoformat(),
                "streams_restarted": result.get("restarted", []),
                "streams_failed": result.get("failed", []),
            },
        )
    finally:
        db.close()


async def handle_clock_jump(delta_s: float) -> dict[str, list[str]]:
    from services.mediamtx_admin_service import MediaMtxAdminService

    logger.warning(
        "System clock stepped by %+.1f s; reconnecting recording streams so "
        "new footage carries the corrected time", delta_s)
    try:
        result = await MediaMtxAdminService.restart_recording_paths()
    except Exception as exc:
        logger.error("clock jump: stream restart failed: %s", exc, exc_info=True)
        result = {"restarted": [], "failed": ["<all: path list unavailable>"]}
    logger.warning("clock jump: restarted %d stream(s), %d failed",
                   len(result["restarted"]), len(result["failed"]))
    global _last_jump
    _last_jump = {
        "delta_seconds": round(delta_s, 1),
        "at": datetime.now(UTC).isoformat(),
        "streams_restarted": len(result["restarted"]),
        "streams_failed": len(result["failed"]),
    }
    try:
        await asyncio.to_thread(_audit, delta_s, result)
    except Exception as exc:
        logger.error("clock jump: audit write failed: %s", exc)
    return result


async def watch_clock_jumps(
    *,
    detector: ClockStepDetector | None = None,
    interval_s: float = CHECK_INTERVAL_S,
    threshold_s: float = JUMP_THRESHOLD_S,
    settle_s: float = SETTLE_S,
    on_jump: Callable[[float], object] = handle_clock_jump,
) -> None:
    """Run forever: check the clock every ``interval_s`` seconds."""
    detector = detector or ClockStepDetector()
    while True:
        await asyncio.sleep(interval_s)
        delta = detector.step()
        if abs(delta) < threshold_s:
            continue
        await asyncio.sleep(settle_s)
        delta += detector.step()
        if abs(delta) < threshold_s:
            continue  # stepped and stepped back: nothing recorded wrong for long
        outcome = on_jump(delta)
        if asyncio.iscoroutine(outcome):
            await outcome
