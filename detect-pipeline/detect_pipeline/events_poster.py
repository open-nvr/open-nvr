# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Visit persistence — post finished tracks (+ best-frame crop) to core.

The moment a track ends is the moment its best frame is FINAL — so that is
when the visit becomes history: one POST to core's canonical event store
(RFC-0001 C1) with the lifecycle summary and the crop as JPEG.

Design constraints honoured here:
* the worker loop must never block on core → posts go through a small
  bounded queue drained by one daemon thread; when the queue is full the
  oldest visit is dropped (and counted) rather than stalling detection;
* per-track grain, not per-frame — a busy camera posts a handful of visits
  per hour, so stdlib urllib + one thread is deliberately boring;
* best-effort: core being down loses history, never detection. Failures
  are visible via the tier0_visits_dropped_total counter and WARNs.
"""

from __future__ import annotations

import base64
import json
import logging
import queue
import threading
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger("detect_pipeline.events")

EVENTS_PATH = "/api/v1/internal/camera-agent/events"


@dataclass(frozen=True)
class Visit:
    """A finished track, wall-clock timed, ready to persist."""

    camera_id: str
    label: str
    score: float | None
    track_id: str
    started_at: datetime
    ended_at: datetime
    stationary: bool | None
    jpeg: bytes | None


class VisitPoster:
    """Bounded-queue, single-thread poster to core's event store."""

    def __init__(
        self,
        core_url: str,
        api_key: str | None,
        *,
        maxsize: int = 256,
        opener=None,
        timeout: float = 10.0,
    ) -> None:
        self.core_url = core_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen
        self._q: queue.Queue[Visit] = queue.Queue(maxsize=maxsize)
        self._dropped = 0
        self._thread: threading.Thread | None = None

    # -- worker-side (never blocks) ---------------------------------

    def submit(self, visit: Visit) -> bool:
        try:
            self._q.put_nowait(visit)
            return True
        except queue.Full:
            self._dropped += 1
            log.warning(
                "visit queue full — dropped %s/%s (total dropped: %d)",
                visit.camera_id, visit.track_id, self._dropped,
            )
            return False

    # -- drain thread -----------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._drain, name="tier0-visits", daemon=True
        )
        self._thread.start()

    def _drain(self) -> None:
        while True:
            visit = self._q.get()
            try:
                self._post(visit)
            except Exception as e:
                log.warning(
                    "visit post failed for %s/%s: %s",
                    visit.camera_id, visit.track_id, e,
                )

    def _post(self, v: Visit) -> None:
        body = {
            "camera_id": int(v.camera_id),
            "label": v.label,
            "score": v.score,
            "track_id": str(v.track_id),
            "started_at": v.started_at.astimezone(timezone.utc).isoformat(),
            "ended_at": v.ended_at.astimezone(timezone.utc).isoformat(),
            "stationary": v.stationary,
        }
        if v.jpeg:
            body["evidence_jpeg_b64"] = base64.b64encode(v.jpeg).decode("ascii")
        req = urllib.request.Request(
            f"{self.core_url}{EVENTS_PATH}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if self.api_key:
            req.add_header("X-Internal-Api-Key", self.api_key)
        with self._opener(req, timeout=self.timeout) as resp:
            if getattr(resp, "status", 201) not in (200, 201):
                raise RuntimeError(f"core answered {resp.status}")


class VisitLifecycle:
    """Pure track-lifecycle bookkeeping (no threads, no I/O — testable).

    Feed it each frame's tracks with a wall-clock timestamp; it returns the
    visits that just FINISHED (their id vanished). Junk suppression: a visit
    only counts if its track was confirmed at some point and lasted at least
    ``min_duration_s`` — flickers never become history rows.
    """

    def __init__(self, camera_id: str, *, min_duration_s: float = 1.0) -> None:
        self.camera_id = camera_id
        self.min_duration_s = min_duration_s
        self._live: dict = {}

    def observe(self, tracks, now_wall: float) -> list[Visit]:
        current = set()
        for tr in tracks:
            current.add(tr.id)
            v = self._live.get(tr.id)
            if v is None:
                v = self._live[tr.id] = {
                    "start": now_wall, "label": tr.label, "score": float(tr.score),
                    "stationary": False, "confirmed": False, "crop": None,
                }
            v["end"] = now_wall
            v["label"] = tr.label
            v["score"] = max(v["score"], float(tr.score))
            v["stationary"] = bool(getattr(tr, "stationary", False))
            v["confirmed"] = v["confirmed"] or bool(getattr(tr, "confirmed", False))
            crop = getattr(tr, "best_crop", None)
            if crop is not None:
                v["crop"] = crop
        finished = []
        for tid in [t for t in self._live if t not in current]:
            visit = self._finish(tid, self._live.pop(tid))
            if visit is not None:
                finished.append(visit)
        return finished

    def flush(self) -> list[Visit]:
        """Finish everything still live (worker stopping)."""
        out = []
        for tid in list(self._live):
            visit = self._finish(tid, self._live.pop(tid))
            if visit is not None:
                out.append(visit)
        return out

    def _finish(self, tid, v) -> Visit | None:
        end = v.get("end", v["start"])
        if not v["confirmed"] or (end - v["start"]) < self.min_duration_s:
            return None
        jpeg = None
        crop = v.get("crop")
        if crop is not None:
            try:
                from .bestframe import _encode_jpeg

                jpeg = _encode_jpeg(crop)
            except Exception:
                jpeg = None  # a visit without a photo still beats no history
        return Visit(
            camera_id=self.camera_id,
            label=str(v["label"]),
            score=v["score"],
            track_id=str(tid),
            started_at=datetime.fromtimestamp(v["start"], tz=timezone.utc),
            ended_at=datetime.fromtimestamp(end, tz=timezone.utc),
            stationary=v["stationary"],
            jpeg=jpeg,
        )
