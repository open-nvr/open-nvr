# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Evidence from the main stream while detection runs on the substream.

Detecting on a camera's low-res substream is the single biggest CPU saving
Tier-0 has (decode is the dominant cost, and a 640x360 sub costs a tenth of
a 1080p main). Its price was the evidence: every crop, scene, plate
candidate and Tier-1 escalation was cut from the same 640x360 frame the
detector saw, so the operator asking "who was that?" got a 90-pixel face
and the OCR adapter got a 40-pixel plate.

This module pays for the detection on the cheap stream and the EVIDENCE on
the expensive one, without decoding the expensive one all day:

* An ``EvidenceSource`` owns a second ffmpeg on the camera's MAIN stream.
  It is started on the first sign of activity (a motion box, a live track)
  and stopped after a quiet period, so an idle camera costs nothing extra
  and a busy one pays full-rate main decode only while something is in
  front of it. The same trigger as adaptive decode, for the same reason:
  the best frame of a visit comes when the subject is biggest, which is
  seconds after it first appears, and by then the main decode is running.
* It keeps a short ring of the newest main frames. When the tracker picks
  a frame as a track's best, it asks for the main frame nearest in CAPTURE
  time to the sub frame it detected on, scales the box by the resolution
  ratio and crops THERE. The two streams are the same sensor, so a box in
  sub coordinates lands on the same pixels in main coordinates — only the
  timing can be off, and a main frame further than ``max_skew_s`` from the
  sub frame is refused: a crop of where the subject WAS is worse than a
  low-res crop of where it is. The tracker then keeps the sub crop and
  says so (``Track.best_crop_source``).

Pure plumbing on the hot path: the ring append is a deque op per main
frame, the lookup a scan of at most ``ring`` entries per best-frame
update, and the main frame is converted to BGR lazily, once, only when a
crop is actually taken from it.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable

log = logging.getLogger("detect_pipeline.evidence")

#: Main frames kept for the nearest-in-time lookup. At the evidence fps
#: (5 by default) eight frames span 1.6 s — comfortably more than the gap
#: between a sub frame's capture and the moment the tracker gets to it.
DEFAULT_RING = 8
#: A main frame further than this from the sub frame is not the same
#: moment. 350 ms is a person's stride, well under a frame's worth of a
#: slow pan, and twice the inter-frame gap at 5 fps on top of the decode
#: skew between the two ffmpegs.
DEFAULT_MAX_SKEW_S = 0.35
#: Quiet seconds before the main decode is stopped again.
DEFAULT_IDLE_AFTER_S = 15.0


class EvidenceFrame:
    """One main-stream frame, converted to BGR at most once, on demand."""

    __slots__ = ("frame", "_bgr")

    def __init__(self, frame) -> None:
        self.frame = frame
        self._bgr = None

    @property
    def ts(self) -> float:
        return float(self.frame.ts)

    @property
    def width(self) -> int:
        return int(self.frame.width)

    @property
    def height(self) -> int:
        return int(self.frame.height)

    def bgr(self):
        """The frame as a BGR array (cached). None if conversion fails."""
        if self._bgr is None:
            try:
                from .detector import to_bgr

                self._bgr = to_bgr(self.frame.data, self.frame.width, self.frame.height)
            except Exception:
                log.debug("evidence frame conversion failed", exc_info=True)
                return None
        return self._bgr


def _activity(result) -> bool:
    """The adaptive-decode trigger: anything in front of the camera."""
    return bool(
        getattr(result, "tracks", None)
        or getattr(result, "motion_boxes", None)
        or getattr(result, "calibrating", False)
    )


class EvidenceSource:
    """A main-stream decode that runs only while the camera is busy, and
    the ring of recent frames the tracker crops evidence from.

    ``source_factory`` builds the thing to read — in production a
    ``FrameSource`` on the main tap; in tests anything with ``stream()``
    (an iterable of frames with ``ts``/``width``/``height``/``data``) and
    ``close()``. A fresh one is built on every start so a stopped ffmpeg
    is never resumed.
    """

    def __init__(
        self,
        camera_id: str,
        source_factory: Callable[[], object],
        *,
        idle_after: float = DEFAULT_IDLE_AFTER_S,
        max_skew_s: float = DEFAULT_MAX_SKEW_S,
        ring: int = DEFAULT_RING,
        _clock=time.monotonic,
    ) -> None:
        self.camera_id = camera_id
        self._factory = source_factory
        self.idle_after = max(1.0, float(idle_after))
        self.max_skew_s = max(0.0, float(max_skew_s))
        self._ring: deque = deque(maxlen=max(1, int(ring)))
        self._clock = _clock
        self._lock = threading.Lock()
        self._src = None
        self._thread: threading.Thread | None = None
        self._last_activity: float | None = None
        self._closed = False
        # Observability, read by the worker for metrics/logs.
        self.frames_total = 0
        self.starts = 0
        self.lookups = 0
        self.hits = 0

    # ── lifecycle ────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def observe(self, result, now: float | None = None) -> bool:
        """Feed one FrameResult; starts or stops the main decode.
        Returns True when the state flipped (for the worker's log line)."""
        if self._closed:
            return False
        if now is None:
            now = self._clock()
        if _activity(result):
            self._last_activity = now
            if not self.active:
                self._start()
                return True
            return False
        if self.active and self._last_activity is not None \
                and now - self._last_activity >= self.idle_after:
            self._stop()
            return True
        return False

    def _start(self) -> None:
        try:
            src = self._factory()
        except Exception:
            log.warning("tier0 %s: could not open the main stream for evidence",
                        self.camera_id, exc_info=True)
            return
        self._src = src
        self.starts += 1
        self._thread = threading.Thread(
            target=self._read, args=(src,),
            name=f"tier0-evidence-{self.camera_id}", daemon=True,
        )
        self._thread.start()

    def _read(self, src) -> None:
        try:
            for frame in src.stream():
                with self._lock:
                    self._ring.append(EvidenceFrame(frame))
                    self.frames_total += 1
        except Exception:
            log.debug("tier0 %s: evidence reader ended", self.camera_id, exc_info=True)

    def _stop(self) -> None:
        src, thread = self._src, self._thread
        self._src, self._thread = None, None
        if src is not None:
            close = getattr(src, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    log.debug("tier0 %s: error closing the evidence source",
                              self.camera_id, exc_info=True)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        with self._lock:
            self._ring.clear()

    def close(self) -> None:
        self._closed = True
        self._stop()

    # ── lookup ───────────────────────────────────────────────────────

    def nearest(self, ts: float | None) -> EvidenceFrame | None:
        """The main frame captured nearest to ``ts`` (a sub frame's capture
        stamp), or None when the nearest one is further than ``max_skew_s``
        away — or when nothing is running. ``ts`` None means "now"."""
        self.lookups += 1
        with self._lock:
            frames: Iterable[EvidenceFrame] = tuple(self._ring)
        if not frames:
            return None
        if ts is None:
            ts = self._clock()
        best = min(frames, key=lambda f: abs(f.ts - ts))
        if abs(best.ts - ts) > self.max_skew_s:
            return None
        self.hits += 1
        return best

    def skew_of(self, ev: EvidenceFrame, ts: float | None) -> float | None:
        if ts is None:
            return None
        return abs(ev.ts - float(ts))
