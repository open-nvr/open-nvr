# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Visit persistence — post finished tracks (+ best-frame crop) to core.

The moment a track ends is the moment its best frame is FINAL — so that is
when the visit becomes history: one POST to core's canonical event store
(RFC-0001 C1) with the lifecycle summary and the crop as JPEG.

Design constraints honoured here:
* the worker loop must never block ON CORE → the POST goes through a small
  bounded queue drained by one daemon thread; when the queue is full the
  oldest visit is dropped (and counted) rather than stalling detection.
  Note the best-frame JPEG encode is NOT off-thread: _finish() encodes on
  the worker's frame loop, so a track ending costs one imencode there;
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
import re
import threading
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

from .metrics import record_visit_dropped, record_visit_posted

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
    # Core's numeric Camera.id — what the events endpoint keys on. The
    # pipeline-facing camera_id is the provider's string handle ("cam1"),
    # which is NOT the DB id; core sends the real one separately as
    # ``open_nvr_camera_id``. None → derived from camera_id at post time.
    nvr_camera_id: int | None = None
    # Multi-frame OCR: up to K plate-candidate crops (JPEG), most
    # promising first. Core's enrichment sweeps these in order until a
    # read clears the confidence floor — several diverse lottery
    # tickets per car instead of one. Empty for non-vehicle visits and
    # non-LPR cameras. Appended LAST (after nvr_camera_id, same rule
    # that field followed) so existing POSITIONAL constructions keep
    # their meaning.
    candidate_jpegs: tuple = ()


def _core_camera_id(v: Visit) -> int:
    """The numeric camera id core's events endpoint expects.

    Prefers the explicit ``nvr_camera_id`` threaded from the provider;
    falls back to parsing the string handle ("cam1"/"cam-1"/"1" → 1) so
    visits from older specs still land instead of failing on int('cam1').
    The fallback is a guess about a format core owns — a handle whose
    digits are not the DB id would file history under the wrong camera —
    but the provider already WARNed once per camera when the spec lost its
    id, so per-visit noise stays at debug.
    """
    if v.nvr_camera_id is not None:
        return v.nvr_camera_id
    m = re.fullmatch(r"(?:cam[-_]?)?(\d+)", str(v.camera_id).strip())
    if m is None:
        raise ValueError(f"no numeric core camera id for {v.camera_id!r}")
    log.debug(
        "visit for %s posted with handle-parsed camera id %s — spec carried "
        "no open_nvr_camera_id", v.camera_id, m.group(1),
    )
    return int(m.group(1))


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
        self._drop_lock = threading.Lock()
        self._evict_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # -- worker-side (never blocks) ---------------------------------

    def submit(self, visit: Visit) -> bool:
        """Queue a visit. Never blocks the worker; drops the OLDEST on overflow."""
        try:
            self._q.put_nowait(visit)
            return True
        except queue.Full:
            pass
        # Make room by discarding the oldest, which is what the module has
        # always promised. put_nowait alone dropped the NEWEST, so a backlog
        # meant keeping a queue full of stale visits and throwing away
        # everything currently happening — the opposite of useful history.
        #
        # Serialised: submit is called from every worker thread and this path
        # makes it a queue CONSUMER as well as a producer. Two workers
        # overflowing at once could each pull one off and only one win the put
        # back, silently losing the other thread's evicted visit while counting
        # a single drop.
        evicted: Visit | None = None
        with self._evict_lock:
            try:
                evicted = self._q.get_nowait()
            except queue.Empty:              # drained between the two calls
                pass
            try:
                self._q.put_nowait(visit)
                queued = True
            except queue.Full:               # refilled underneath us; give up
                queued = False
                if evicted is not None:      # put the old one back, not the new
                    try:
                        self._q.put_nowait(evicted)
                        evicted = None
                    except queue.Full:       # pragma: no cover - drained again
                        pass

        lost = evicted if queued else visit
        with self._drop_lock:                # plain += raced across N workers
            self._dropped += 1
            total = self._dropped
        record_visit_dropped(lost.camera_id, "queue_full")
        log.warning(
            "visit queue full — dropped %s/%s (total dropped: %d)",
            lost.camera_id, lost.track_id, total,
        )
        return queued

    # -- drain thread -----------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._drain, name="tier0-visits", daemon=True
        )
        self._thread.start()

    def is_alive(self) -> bool:
        """Whether the drain thread is still running (fed to /health)."""
        return self._thread is not None and self._thread.is_alive()

    def _drain(self) -> None:
        # Outer guard: ONE drain thread serves every camera, so an exception
        # escaping the inner handler — including from the handler itself —
        # used to kill visit persistence fleet-wide, silently and for the
        # life of the process, while the queue filled behind it.
        while True:
            try:
                self._drain_one()
            except Exception:  # pragma: no cover - the last line of defence
                log.exception("visit drain iteration failed; continuing")

    def _drain_one(self) -> None:
        visit = self._q.get()
        try:
            self._post(visit)
            record_visit_posted(visit.camera_id)
        except Exception as e:
            # A post failure is PERMANENT history loss — there is no retry
            # queue — so it is counted, not just logged. The reason label
            # separates "core is down" from "this visit can never be posted"
            # (an unresolvable camera id), which look identical in the log.
            reason = "unresolved_camera" if isinstance(e, ValueError) else "post_failed"
            record_visit_dropped(visit.camera_id, reason)
            log.warning(
                "visit post failed for %s/%s: %s",
                visit.camera_id, visit.track_id, e,
            )

    def _post(self, v: Visit) -> None:
        body = {
            "camera_id": _core_camera_id(v),
            "label": v.label,
            "score": v.score,
            "track_id": str(v.track_id),
            "started_at": v.started_at.astimezone(timezone.utc).isoformat(),
            "ended_at": v.ended_at.astimezone(timezone.utc).isoformat(),
            "stationary": v.stationary,
        }
        if v.jpeg:
            body["evidence_jpeg_b64"] = base64.b64encode(v.jpeg).decode("ascii")
        if v.candidate_jpegs:
            body["candidate_jpegs_b64"] = [
                base64.b64encode(j).decode("ascii") for j in v.candidate_jpegs
            ]
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

    def __init__(
        self,
        camera_id: str,
        *,
        nvr_camera_id: int | None = None,
        min_duration_s: float = 1.0,
    ) -> None:
        self.camera_id = camera_id
        self.nvr_camera_id = nvr_camera_id
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
                    "ring": None,
                }
            v["end"] = now_wall
            v["label"] = tr.label
            ring = getattr(tr, "plate_ring", None)
            if ring is not None:
                v["ring"] = ring
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
        if not v["confirmed"]:
            # Belt-and-braces only: observe() is fed Tracker.tracks, which is
            # confirmed-only, so this cannot fire through the production path.
            # Kept for callers that feed unfiltered tracks (bench, replay);
            # it is NOT the junk-suppression rail — min_duration_s below is.
            record_visit_dropped(self.camera_id, "unconfirmed")
            return None
        if (end - v["start"]) < self.min_duration_s:
            # Junk suppression is deliberate, but it was previously invisible:
            # there was no way to tell "nothing happened" from "the floor is
            # eating every real visit" — which is what a raised DETECT_FPS
            # does, since confirmation then needs more consecutive frames.
            record_visit_dropped(self.camera_id, "too_short")
            return None
        jpeg = None
        crop = v.get("crop")
        if crop is not None:
            try:
                from .bestframe import _encode_jpeg

                jpeg = _encode_jpeg(crop)
            except Exception:
                jpeg = None  # a visit without a photo still beats no history
        candidate_jpegs: list[bytes] = []
        ring = v.get("ring")
        if ring is not None:
            try:
                from .bestframe import _encode_jpeg

                for cand in ring.ranked():
                    encoded = _encode_jpeg(cand.crop)
                    if encoded:
                        candidate_jpegs.append(encoded)
            except Exception:
                # Candidates are an enhancement; the visit itself (and its
                # single-crop enrichment fallback) must never be lost to a
                # bad encode.
                candidate_jpegs = candidate_jpegs or []
        return Visit(
            camera_id=self.camera_id,
            nvr_camera_id=self.nvr_camera_id,
            label=str(v["label"]),
            score=v["score"],
            track_id=str(tid),
            started_at=datetime.fromtimestamp(v["start"], tz=timezone.utc),
            ended_at=datetime.fromtimestamp(end, tz=timezone.utc),
            stationary=v["stationary"],
            jpeg=jpeg,
            candidate_jpegs=tuple(candidate_jpegs),
        )
