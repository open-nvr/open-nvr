# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Early plate attempts — the latency half of multi-frame OCR.

Today the only OCR trigger is the visit ENDING, so a read's latency is
the track's lifetime — and on a busy road a track that should die in
seconds can live minutes (a lookalike car adopts it, or it coasts on
the TTL). The plate was readable in second two; the trigger came in
minute three.

This module fires the first OCR attempt the moment a vehicle track is
CONFIRMED, by shipping its best plate candidate to core's internal
attempt endpoint. Core OCRs it (through KAI-C, same as enrichment) and
parks an accepted read in a small cache; when the visit later ingests,
the read is applied instantly — the plate is known while the car is
still in frame. A second attempt fires only if a MUCH better candidate
appears (see ``EarlyAttemptPolicy``), and a hard per-track budget caps
the compute.

Same design constraints as the visit poster: the worker loop never
blocks on core (bounded queue, one daemon thread, drop-oldest), and
every failure is best-effort — a lost early attempt costs latency,
never the read itself (the ingest-time candidate sweep is the safety
net).
"""
from __future__ import annotations

import base64
import json
import logging
import queue
import threading
import time
import urllib.request
from dataclasses import dataclass

from .platecands import VEHICLE_LABELS, EarlyAttemptPolicy

log = logging.getLogger("detect_pipeline.plates")

ATTEMPT_PATH = "/api/v1/internal/camera-agent/plates/attempt"


@dataclass(frozen=True)
class Attempt:
    """One early OCR attempt, ready to post."""

    camera_id: str            # platform handle ("cam1") — for logs/metrics
    nvr_camera_id: int | None  # core's numeric Camera.id — what core keys on
    track_id: str
    ts: float                 # wall clock of submission (visit-window match)
    jpeg: bytes


class AttemptPoster:
    """Bounded-queue, single-thread poster to core's attempt endpoint."""

    def __init__(
        self,
        core_url: str,
        api_key: str | None,
        *,
        maxsize: int = 128,
        opener=None,
        timeout: float = 10.0,
    ) -> None:
        self.core_url = core_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen
        self._q: queue.Queue[Attempt] = queue.Queue(maxsize=maxsize)
        self._thread: threading.Thread | None = None
        self.dropped = 0
        self.posted = 0

    def submit(self, attempt: Attempt) -> bool:
        """Queue an attempt; never blocks the worker. On overflow the NEW
        attempt is dropped (unlike visits, an early attempt is a latency
        optimisation — the freshest one is no more valuable than the
        queued ones, and the ingest sweep covers everyone)."""
        try:
            self._q.put_nowait(attempt)
            return True
        except queue.Full:
            self.dropped += 1
            log.debug("plate attempt queue full — dropped %s/%s",
                      attempt.camera_id, attempt.track_id)
            return False

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._drain, name="tier0-plate-attempts", daemon=True
        )
        self._thread.start()

    def _drain(self) -> None:
        while True:
            try:
                self._drain_one()
            except Exception:  # pragma: no cover - last line of defence
                log.exception("plate attempt drain iteration failed; continuing")

    def _drain_one(self) -> None:
        attempt = self._q.get()
        try:
            self._post(attempt)
            self.posted += 1
        except Exception as e:
            # Best-effort by design: the ingest-time sweep still runs.
            log.debug("plate attempt post failed for %s/%s: %s",
                      attempt.camera_id, attempt.track_id, e)

    def _post(self, a: Attempt) -> None:
        if a.nvr_camera_id is None:
            raise ValueError(f"no numeric camera id for {a.camera_id!r}")
        body = {
            "camera_id": a.nvr_camera_id,
            "track_id": str(a.track_id),
            "ts": a.ts,
            "jpeg_b64": base64.b64encode(a.jpeg).decode("ascii"),
        }
        req = urllib.request.Request(
            f"{self.core_url}{ATTEMPT_PATH}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if self.api_key:
            req.add_header("X-Internal-Api-Key", self.api_key)
        with self._opener(req, timeout=self.timeout) as resp:
            if getattr(resp, "status", 202) not in (200, 201, 202):
                raise RuntimeError(f"core answered {resp.status}")


class EarlyPlateAttempts:
    """Per-camera orchestration: watch the live tracks each frame and
    fire early attempts per ``EarlyAttemptPolicy``.

    Pure apart from the poster hand-off and JPEG encode (both bounded
    by the per-track attempt budget). Policies are dropped when their
    track disappears, so state is bounded by the live-track cap.
    """

    def __init__(
        self,
        poster: AttemptPoster,
        camera_id: str,
        *,
        nvr_camera_id: int | None = None,
        max_attempts: int = 2,
        clock=None,
    ) -> None:
        self.poster = poster
        self.camera_id = camera_id
        self.nvr_camera_id = nvr_camera_id
        self.max_attempts = max_attempts
        self._clock = clock or time.monotonic
        self._policies: dict = {}
        self.attempts_submitted = 0

    def observe(self, tracks) -> int:
        """Feed one frame's confirmed tracks; returns attempts fired."""
        now = self._clock()
        fired = 0
        current = set()
        for tr in tracks:
            current.add(tr.id)
            if tr.label not in VEHICLE_LABELS:
                continue
            ring = getattr(tr, "plate_ring", None)
            if ring is None:
                continue
            best = ring.best()
            if best is None:
                continue
            policy = self._policies.get(tr.id)
            if policy is None:
                policy = self._policies[tr.id] = EarlyAttemptPolicy(
                    max_attempts=self.max_attempts,
                )
            if not policy.should_attempt(
                confirmed=bool(getattr(tr, "confirmed", False)),
                now=now, best_score=best.score,
            ):
                continue
            jpeg = self._encode(best.crop)
            if jpeg is None:
                continue
            queued = self.poster.submit(Attempt(
                camera_id=self.camera_id,
                nvr_camera_id=self.nvr_camera_id,
                track_id=str(tr.id),
                ts=time.time(),
                jpeg=jpeg,
            ))
            # Budget is consumed even when the queue was full — an
            # overloaded box must not amplify its own load by retrying.
            policy.note_attempt(now=now, score=best.score)
            if queued:
                fired += 1
                self.attempts_submitted += 1
        # Bounded state: forget tracks that vanished.
        for tid in [t for t in self._policies if t not in current]:
            self._policies.pop(tid, None)
        return fired

    @staticmethod
    def _encode(crop) -> bytes | None:
        try:
            from .bestframe import _encode_jpeg

            return _encode_jpeg(crop)
        except Exception:
            return None
