# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Evidence from the recording: the main-stream frame of the SAME MOMENT.

The keyframe-only evidence source (evidence.py) hands the tracker the
nearest decoded keyframe, and a keyframe-only decode delivers each
keyframe about one GOP late — measured on the Ventuno Q: ffmpeg holds one
frame in its output path, and with keyframes only the frame that pushes it
out is the next keyframe. So every synchronous main crop was a GOP older
than its stamp, and the match guard refused a walking person 52 times out
of 98 (2026-11-04). The recorder has the right frame: MediaMTX writes the
main stream to disk as it arrives and its playback server serves a range
that ends about a second before now, as a decodable fMP4 that starts at a
keyframe and ends exactly at ``start + duration`` (probed 2026-11-04,
84 ms median per fetch).

So evidence is asked for, not looked up: when a track's best crop improves
the tracker files a request (the detect frame's stamp, its box, its bare
crop); a thread per camera waits until the recorder can serve that moment,
fetches ``[t - lead, t + lead]`` plus the keyframe before it, decodes the
clip once (at most one GOP), scores the frames nearest ``t`` against the
detect crop with the same guard the synchronous path uses, and hands the
best one back. The worker loop drains the results: a live track gets its
full-resolution crop; a visit whose track has already ended waits a few
seconds for it (service.py holds the post) and is then sent with the crop
patched in.

Budgets: one request in flight per camera, coalesced per track (the newest
best-crop moment wins; an improvement that arrives while the previous one
is still queued replaces it), a minimum interval between fetches per
camera, and a box-wide cap on concurrent decodes. A request the budget
cannot serve is dropped and counted; the visit keeps its detect crop.
Nothing here runs on the detect thread.
"""
from __future__ import annotations

import datetime as _dt
import logging
import subprocess
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable
from urllib import request as _urlreq

from .captime import capture_wall
from .regions import Box

log = logging.getLogger(__name__)

#: How long after the detect frame the clip is asked to end (so a frame or
#: two past the moment are candidates too), and how far around the moment
#: a decoded frame may be to count as a candidate.
DEFAULT_LEAD_S = 0.3
DEFAULT_WINDOW_S = 0.35
#: The recorder serves ranges ending about this long before now; a request
#: waits until the moment it wants is that old.
DEFAULT_FRESHNESS_S = 1.2
#: Minimum time between two fetches on one camera, and the box-wide cap on
#: concurrent decodes (each decodes up to a GOP of the main stream).
DEFAULT_MIN_INTERVAL_S = 2.0
DEFAULT_PARALLEL = 2
#: A request older than this when its turn comes is dropped: the recorder
#: still has the frame, but the visit has long been posted.
DEFAULT_MAX_AGE_S = 20.0
#: Fallback main-stream GOP (how far before the moment the clip starts so a
#: keyframe is inside it) when the camera's has not been measured.
DEFAULT_GOP_S = 2.0

_box_parallel = threading.BoundedSemaphore(DEFAULT_PARALLEL)


def set_box_parallel(n: int) -> None:
    """The box-wide decode cap (DETECT_EVIDENCE_FETCH_PARALLEL), set once at start."""
    global _box_parallel
    _box_parallel = threading.BoundedSemaphore(max(1, int(n)))


@dataclass
class EvidenceRequest:
    track_id: int
    mono_ts: float                  # the detect frame's monotonic stamp (Frame.ts)
    box: Box                        # on the detect frame, pixels
    frame_shape: tuple[int, int]    # (h, w) of the detect frame
    sub_crop: object                # bare-box BGR crop of the detect frame
    label: str = ""
    # Stamped by PlaybackEvidence.request() on the instance's clock (None
    # until then), so the age check and the tests' injected clock agree.
    enqueued_mono: float | None = None


@dataclass
class EvidenceResult:
    request: EvidenceRequest
    outcome: str                    # accepted | refused | no_frames | error | dropped
    crop: object = None             # BGR, with the context margin, when accepted
    frame: object = None            # the main frame the crop came from (for the scene)
    score: float | None = None
    skew_s: float | None = None     # |candidate frame time - detect frame time|
    reason: str = ""
    fetch_s: float = 0.0
    decode_s: float = 0.0
    candidates: int = 0

    @property
    def track_id(self) -> int:
        return self.request.track_id


def _iso(wall: float) -> str:
    return _dt.datetime.fromtimestamp(wall, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def http_fetch(url: str, timeout: float = 10.0) -> bytes:
    """The clip bytes from MediaMTX's playback server (stdlib only)."""
    with _urlreq.urlopen(url, timeout=timeout) as r:
        return r.read()


def ffprobe_clip(data: bytes) -> tuple[int, int, float] | None:
    """``(width, height, fps)`` of the clip's video stream, or None."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate", "-of", "csv=p=0", "pipe:0"],
        input=data, capture_output=True, timeout=30,
    )
    try:
        w, h, rate = r.stdout.decode().strip().split("\n")[0].split(",")[:3]
        num, den = rate.split("/")
        return int(w), int(h), (float(num) / float(den)) if float(den) else 0.0
    except (ValueError, IndexError):
        return None


def ffmpeg_decode_clip(data: bytes, width: int, height: int) -> list:
    """Every frame of the clip as a BGR array, in order (one ffmpeg run)."""
    import numpy as np

    # Four threads and the fast decode flags the sub-stream path uses: on
    # the Ventuno Q ~0.65 s for a short clip against ~0.8 s plain.
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-threads", "4",
         "-skip_loop_filter", "all", "-flags2", "fast", "-i", "pipe:0",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"],
        input=data, capture_output=True, timeout=60,
    )
    n = width * height * 3
    raw = r.stdout
    frames = []
    for i in range(len(raw) // n):
        frames.append(np.frombuffer(raw[i * n:(i + 1) * n], np.uint8).reshape(height, width, 3))
    return frames


class PlaybackEvidence:
    """One camera's evidence requests, served from the recorder on a thread."""

    def __init__(
        self, camera_id: str, url_provider: Callable[[], str | None], *,
        match_min: float = 0.5, crop_margin: float = 0.25,
        gop_s: Callable[[], float | None] | None = None,
        lead_s: float = DEFAULT_LEAD_S, window_s: float = DEFAULT_WINDOW_S,
        freshness_s: float = DEFAULT_FRESHNESS_S, min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        max_age_s: float = DEFAULT_MAX_AGE_S,
        fetch=http_fetch, probe=ffprobe_clip, decode=ffmpeg_decode_clip,
        _clock=time.monotonic, _wall=time.time, _capture_wall=capture_wall,
        _sleep=None,
    ) -> None:
        self.camera_id = camera_id
        self._url_provider = url_provider
        self.match_min = float(match_min)
        self.crop_margin = float(crop_margin)
        self._gop_s = gop_s or (lambda: None)      # kept for the cost cap to come
        self.lead_s, self.window_s = float(lead_s), float(window_s)
        self.freshness_s, self.min_interval_s = float(freshness_s), float(min_interval_s)
        self.max_age_s = float(max_age_s)
        self._fetch, self._probe, self._decode = fetch, probe, decode
        self._clock, self._wall, self._capture_wall = _clock, _wall, _capture_wall
        self._cond = threading.Condition()
        self._pending: "OrderedDict[int, EvidenceRequest]" = OrderedDict()
        self._results: list[EvidenceResult] = []
        self._stop = threading.Event()
        self._last_fetch_mono: float | None = None
        self._stream: tuple[int, int, float] | None = None     # (w, h, fps) of the main stream
        self._sleep = _sleep or (lambda s: self._cond.wait(timeout=s))
        # Counters the worker exports.
        self.requests = 0
        self.dropped = 0
        self.outcomes: dict[str, int] = {}
        self.fetch_seconds = 0.0
        self.decode_seconds = 0.0
        self._thread = threading.Thread(target=self._run, name=f"evidence-{camera_id}", daemon=True)
        self._thread.start()

    # ── the detect thread's side ──────────────────────────────────────

    def request(self, req: EvidenceRequest) -> None:
        """File (or replace) this track's request. Never blocks."""
        self.requests += 1
        if req.enqueued_mono is None:
            req.enqueued_mono = self._clock()
        with self._cond:
            if req.track_id in self._pending:
                self._pending.pop(req.track_id)          # the newer moment replaces the older
            self._pending[req.track_id] = req
            self._cond.notify()

    def pending_for(self, track_id: int) -> bool:
        with self._cond:
            return track_id in self._pending or track_id in self._in_flight

    def results(self) -> list[EvidenceResult]:
        with self._cond:
            out, self._results = self._results, []
        return out

    def close(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()

    # ── the serving thread ────────────────────────────────────────────

    _in_flight: set = frozenset()

    def _run(self) -> None:
        self._in_flight = set()
        while not self._stop.is_set():
            with self._cond:
                while not self._pending and not self._stop.is_set():
                    self._cond.wait(timeout=1.0)
                if self._stop.is_set():
                    return
                tid, req = next(iter(self._pending.items()))
                # Serve when the recorder can: the moment must be old enough,
                # and the per-camera interval must have passed.
                ready_at = req.mono_ts + self.lead_s + self.freshness_s
                if self._last_fetch_mono is not None:
                    ready_at = max(ready_at, self._last_fetch_mono + self.min_interval_s)
                wait = ready_at - self._clock()
                if wait > 0:
                    self._cond.wait(timeout=min(wait, 1.0))
                    continue
                self._pending.pop(tid)
                if self._clock() - (req.enqueued_mono or self._clock()) > self.max_age_s:
                    self.dropped += 1
                    self._results.append(EvidenceResult(req, "dropped", reason="too_old"))
                    continue
                self._in_flight.add(tid)
            try:
                res = self._serve(req)
            except Exception as exc:                      # never let the thread die
                log.debug("tier0 %s: playback evidence failed: %s", self.camera_id, exc, exc_info=True)
                res = EvidenceResult(req, "error", reason=type(exc).__name__)
            with self._cond:
                self._in_flight.discard(tid)
                self._results.append(res)
                self.outcomes[res.outcome] = self.outcomes.get(res.outcome, 0) + 1
                self.fetch_seconds += res.fetch_s
                self.decode_seconds += res.decode_s

    def _serve(self, req: EvidenceRequest) -> EvidenceResult:
        from .tracking import _crop_bgr, crop_similarity, crop_texture, FLAT_TEXTURE

        base = self._url_provider() if self._url_provider else None
        if not base:
            return EvidenceResult(req, "error", reason="no_playback_url")
        wall_t = self._capture_wall(req.mono_ts)
        end = wall_t + self.lead_s
        # MediaMTX serves from the keyframe before ``start`` whatever the span,
        # so a short span is enough: measured on the Ventuno Q, 0.8 s costs
        # ~0.65 s to decode against ~1.1 s for a GOP-sized span.
        span = self.lead_s + 0.5
        sep = "&" if "?" in base else "?"
        url = f"{base}{sep}start={_iso(end - span)}&duration={span:.3f}&format=fmp4"
        t0 = self._clock()
        self._last_fetch_mono = t0
        data = self._fetch(url)
        fetch_s = self._clock() - t0
        if not data:
            return EvidenceResult(req, "no_frames", reason="empty_clip", fetch_s=fetch_s)
        t1 = self._clock()
        with _box_parallel:
            if self._stream is None:
                self._stream = self._probe(data)
            if not self._stream or not self._stream[2]:
                return EvidenceResult(req, "error", reason="unprobeable_clip", fetch_s=fetch_s)
            w, h, fps = self._stream
            frames = self._decode(data, w, h)
        decode_s = self._clock() - t1
        if not frames:
            return EvidenceResult(req, "no_frames", reason="undecodable_clip", fetch_s=fetch_s, decode_s=decode_s)
        # The clip ends exactly at ``end``: frame k is at end - (n-1-k)/fps.
        n = len(frames)
        sh, sw = req.frame_shape
        sx, sy = w / float(sw), h / float(sh)
        scaled = (req.box[0] * sx, req.box[1] * sy, req.box[2] * sx, req.box[3] * sy)
        best = None
        candidates = 0
        for k, frame in enumerate(frames):
            t_k = end - (n - 1 - k) / fps
            skew = abs(t_k - wall_t)
            if skew > self.window_s:
                continue
            candidates += 1
            main_crop = _crop_bgr(frame, scaled, margin=0.0)
            sim = crop_similarity(main_crop, req.sub_crop)
            if sim is None:
                tex_sub, tex_main = crop_texture(req.sub_crop), crop_texture(main_crop)
                if (tex_sub is not None and tex_main is not None
                        and tex_sub >= FLAT_TEXTURE and tex_main < FLAT_TEXTURE):
                    continue                                  # a blank where the sub saw a subject
                sim_key = (0.0, -skew)                       # undecidable: accepted, least preferred
            else:
                sim_key = (sim, -skew)
            if best is None or sim_key > best[0]:
                best = (sim_key, k, frame, sim, skew)
        if best is None:
            return EvidenceResult(req, "no_frames", reason="no_frame_near_moment",
                                  fetch_s=fetch_s, decode_s=decode_s, candidates=0)
        _, k, frame, sim, skew = best
        if sim is not None and sim < self.match_min:
            return EvidenceResult(req, "refused", score=sim, skew_s=skew, reason="low_corr",
                                  fetch_s=fetch_s, decode_s=decode_s, candidates=candidates)
        crop = _crop_bgr(frame, scaled, margin=self.crop_margin)
        if crop is None:
            return EvidenceResult(req, "no_frames", reason="empty_crop", fetch_s=fetch_s,
                                  decode_s=decode_s, candidates=candidates)
        return EvidenceResult(req, "accepted", crop=crop, frame=frame, score=sim, skew_s=skew,
                              fetch_s=fetch_s, decode_s=decode_s, candidates=candidates)
