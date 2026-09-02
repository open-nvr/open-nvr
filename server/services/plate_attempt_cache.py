# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Pending early plate reads — the hand-off between multi-frame OCR's
early attempts and the visit that hasn't been ingested yet.

The detect-pipeline fires an OCR attempt the moment a vehicle track is
confirmed — often minutes before the visit row exists (a track on a
busy road can coast for a long time before it closes). The accepted
read has nowhere to land yet, so it parks here, keyed by
``(camera_id, track_id)``, and ingest claims it when the visit arrives.

Deliberately modest: in-memory (a core restart loses pending reads and
the ingest-time candidate sweep re-reads them — best-effort by design),
TTL-bounded, size-bounded, thread-safe. Track ids are per-worker
counters that RESET when a worker restarts, so a bare (camera, track)
key could hand yesterday's plate to today's car — the visit's time
window must overlap the attempt's timestamp for a claim to succeed.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

#: Pending reads older than this are unclaimable and swept. Long enough
#: for a coasting track (DETECT_TRACK_TTL default 300 s) + slack.
TTL_SECONDS: float = 600.0
#: Hard cap on parked reads — a misbehaving producer cannot grow memory.
MAX_ENTRIES: int = 512
#: Slack around the visit window when matching the attempt timestamp.
WINDOW_SLACK_SECONDS: float = 10.0


@dataclass
class PendingRead:
    plate: str
    confidence: float
    attempt_ts: float          # wall-clock, from the producer
    stored_monotonic: float    # for TTL sweeping
    # Relative evidence path of the crop this read came from (#382), so
    # the claiming visit can show the image its plate was READ from
    # rather than the vehicle-best frame. A PATH, not the bytes: this
    # cache is deliberately modest, and the JPEG is already on disk.
    # None when the crop could not be stored — the visit then falls
    # back to its vehicle frame, exactly as before.
    plate_evidence_path: str | None = None


class PlateAttemptCache:
    """Thread-safe (camera_id, track_id) → best pending read."""

    def __init__(self, *, ttl_s: float = TTL_SECONDS,
                 max_entries: int = MAX_ENTRIES, clock=None) -> None:
        self._ttl = float(ttl_s)
        self._max = int(max_entries)
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._entries: dict[tuple[int, str], PendingRead] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def put(self, camera_id: int, track_id: str, *, plate: str,
            confidence: float, attempt_ts: float,
            plate_evidence_path: str | None = None) -> None:
        """Park an accepted read. A later read for the same track only
        replaces a parked one when its confidence is higher — attempts
        keep the best, never the latest."""
        key = (int(camera_id), str(track_id))
        with self._lock:
            self._sweep_locked()
            current = self._entries.get(key)
            if current is not None and current.confidence >= confidence:
                return
            if current is None and len(self._entries) >= self._max:
                # Evict the stalest entry — bounded beats complete.
                stalest = min(self._entries,
                              key=lambda k: self._entries[k].stored_monotonic)
                self._entries.pop(stalest, None)
            self._entries[key] = PendingRead(
                plate=plate, confidence=float(confidence),
                attempt_ts=float(attempt_ts),
                stored_monotonic=self._clock(),
                plate_evidence_path=plate_evidence_path,
            )

    def claim(self, camera_id: int, track_id: str, *,
              started_ts: float, ended_ts: float) -> PendingRead | None:
        """Take (and remove) the pending read for this visit — but ONLY
        if the attempt happened inside the visit's time window (with
        slack). A recycled track id from a restarted worker fails the
        window check and the stale read stays unclaimed until TTL."""
        key = (int(camera_id), str(track_id))
        with self._lock:
            self._sweep_locked()
            entry = self._entries.get(key)
            if entry is None:
                return None
            lo = float(started_ts) - WINDOW_SLACK_SECONDS
            hi = float(ended_ts) + WINDOW_SLACK_SECONDS
            if not (lo <= entry.attempt_ts <= hi):
                return None
            return self._entries.pop(key)

    def _sweep_locked(self) -> None:
        now = self._clock()
        dead = [k for k, v in self._entries.items()
                if (now - v.stored_monotonic) > self._ttl]
        for k in dead:
            self._entries.pop(k, None)


#: Process-wide instance — the attempt endpoint writes, ingest claims.
cache = PlateAttemptCache()
