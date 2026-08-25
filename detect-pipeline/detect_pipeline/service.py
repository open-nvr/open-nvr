# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
The Tier-0 service: one worker per camera, reconciled against OpenNVR's camera
list, publishing detections to the existing inference event bus.

This is an **additive consumer** — it subscribes to streams MediaMTX already
republishes and emits to the NATS subjects adapters already use. It changes
nothing about how cameras are ingested, recorded, or served.

Everything external is an injected interface so the core logic is unit-testable
without OpenNVR, NATS, or ffmpeg:

* :class:`CameraProvider` — where the camera list comes from (HTTP endpoint).
* :class:`ResultSink` — where detections go (NATS).
* the worker factory — so tests use fakes instead of spawning ffmpeg threads.

Enablement: **on by default**, globally disable-able via ``enabled=False``
(wired to ``DETECT_PIPELINE_ENABLED``); a camera with ``analyze=False`` is
skipped, so per-camera opt-out works without touching the rest.
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Protocol

from .detector import DetectorAdapter, DetectorPool, StubDetector
from .frame_source import FrameSource, probe_stream
from .dispatch import dispatch_escalations
from .gate import Gate
from .ffmpeg_presets import DEFAULT_RTSP_TIMEOUT_S, HwAccel, resolve_hwaccel
from .metrics import (
    metrics as _metrics,
    record_frame,
    record_gate,
    record_processing_fps,
    record_mainstream_fallback,
    record_published,
    record_sink_error,
    record_worker_restart,
    record_worker_state,
    record_worker_straggler,
    forget_camera,
)
from .motion import MotionConfig, MotionDetector
from .pipeline import DetectPipeline, FrameResult, RegionBudgetController
from .tracking import TrackConfig, Tracker

log = logging.getLogger("detect_pipeline.service")

# Labels Tier-0 TRACKS by default (#track-explosion). yolov8n predicts all 80
# COCO classes, but an NVR only cares about a handful — and every tracked
# phantom ("kite" on a wall of wires) is a standing per-frame re-verify cost.
# Frigate ships `person`-only for the same reason; we default a little wider.
# Override with DETECT_LABELS (comma-separated); "all" tracks everything.
DEFAULT_TRACK_LABELS = "person,car,truck,bus,motorcycle,bicycle,cat,dog"


def _env_int(name: str, default: int) -> int:
    import os as _os
    raw = (_os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    import os as _os
    raw = (_os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


def _env_labels(name: str, default_csv: str) -> frozenset[str] | None:
    """Parse a comma-separated label allowlist; "all"/"*" → None (no filter)."""
    import os as _os
    raw = (_os.environ.get(name) or "").strip() or default_csv
    if raw.lower() in ("all", "*"):
        return None
    labels = frozenset(p.strip().lower() for p in raw.split(",") if p.strip())
    return labels or None


@dataclass(frozen=True)
class CameraSpec:
    """One camera to analyze, as returned by the provider."""

    camera_id: str
    name: str
    substream_url: str            # rtsp(s):// (creds inline) or MediaMTX republish
    analyze: bool = True
    width: int | None = None      # if the provider knows it; else we ffprobe
    height: int | None = None
    fps: int = 5
    # Per-camera decode backend, if the discovery endpoint ever declares one.
    # None = not declared → the service-wide DETECT_HWACCEL applies. It
    # defaulted to "cpu", which was indistinguishable from "core said cpu"
    # and silently pinned every camera to software decode.
    hwaccel: str | None = None
    # Per-camera label set from the camera's ``object_detection`` assignment
    # ("camera 4 wants person + truck"). None = no per-camera declaration →
    # the global DETECT_LABELS applies, exactly as before assignments
    # existed. Set by the provider from the internal endpoint's
    # ``assignments`` field; a change restarts the worker on the next
    # reconcile tick (see WorkerManager.reconcile).
    labels: frozenset[str] | None = None
    # Core's numeric Camera.id (from the endpoint's ``open_nvr_camera_id``).
    # camera_id above is the string handle ("cam1") used in topics/metrics —
    # core's events store keys on this numeric id instead. Appended LAST so
    # existing positional constructions keep their meaning.
    nvr_camera_id: int | None = None


def hwaccel_for(spec: "CameraSpec", default: str) -> str:
    """The decode backend THIS camera should use.

    Same precedence as ``allowed_labels_for``: a per-camera declaration wins
    when present, otherwise the service-wide setting. Hardware decode is a
    property of the HOST, so the service-wide value is the one that normally
    applies — it just never used to reach here.
    """
    return spec.hwaccel or default


def allowed_labels_for(spec: "CameraSpec") -> frozenset[str] | None:
    """The label allowlist THIS camera's pipeline should run with.

    The camera's assignment wins when declared; else the global env
    (DETECT_LABELS, defaulting to the curated track set). Factored out of
    the worker so the precedence is testable without opening a stream."""
    if spec.labels is not None:
        return spec.labels
    return _env_labels("DETECT_LABELS", DEFAULT_TRACK_LABELS)


class CameraProvider(Protocol):
    def list_cameras(self) -> list[CameraSpec] | None:
        """The desired camera set; None = discovery failed (keep current)."""
        ...


class ResultSink(Protocol):
    def publish(self, camera_id: str, result: FrameResult, frame) -> bool:
        """Return True iff an event was actually published (not a no-op frame)."""
        ...


class Worker(Protocol):
    """A per-camera worker handle (thread-backed in production, fake in tests).

    ``request_stop``/``join`` are optional: when a worker provides them the
    manager stops the fleet in two phases (signal everyone, then wait once).
    A worker with only the blocking ``stop`` still works — it is just stopped
    serially, which is fine for fakes and single cameras.
    """

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def is_alive(self) -> bool: ...


# How long the manager waits for the whole fleet to wind down. This is a
# budget for ALL workers together, not per worker: paying it serially is what
# turned a gate-mode toggle on 50 cameras into minutes of downtime.
STOP_TIMEOUT_S = 5.0


def _signal_stop(worker) -> None:
    """Phase 1 — tell a worker to stop without waiting for it."""
    request = getattr(worker, "request_stop", None)
    if request is not None:
        request()
    else:                       # legacy/fake worker: blocking stop is all it has
        worker.stop()


def _join_workers(workers: dict[str, Worker], timeout: float) -> list[str]:
    """Phase 2 — wait for signalled workers against ONE shared deadline.

    Returns the ids that did not exit in time.
    """
    deadline = time.monotonic() + timeout
    stragglers: list[str] = []
    for cid, worker in workers.items():
        join = getattr(worker, "join", None)
        if join is None:        # already stopped synchronously in phase 1
            continue
        if not join(max(0.0, deadline - time.monotonic())):
            stragglers.append(cid)
    return stragglers


# ── adaptive decode (Blue Iris-style "limit decoding unless required") ──

class AdaptiveDecode:
    """Per-camera decode-depth state machine.

    IDLE decodes with ``idle_skip`` (normally ``nokey`` — keyframes only, ~one
    frame per GOP) so a quiet scene costs almost nothing while motion is still
    watched at the keyframe rate. The FIRST motion box, live track, or
    calibration phase promotes to ACTIVE (the configured normal decode) by
    restarting the ffmpeg source; after ``idle_after`` seconds with no
    activity it demotes back. The restart is a reconnect to the LOCAL
    MediaMTX republish — sub-second — but promotion still costs one GOP of
    latency in the worst case (the event lands just after a keyframe), which
    is why this is opt-in rather than default.

    Pure logic — the worker owns the actual source restarts — so it unit-tests
    with a fake clock and hand-built results.
    """

    ACTIVE = "active"
    IDLE = "idle"

    def __init__(
        self,
        idle_skip: str,
        active_skip: str,
        idle_after: float = 60.0,
        *,
        _clock=time.monotonic,
    ) -> None:
        self.idle_skip = idle_skip
        self.active_skip = active_skip
        self.idle_after = max(1.0, float(idle_after))
        self._clock = _clock
        # Start ACTIVE: prove the full chain at boot and catch whatever is
        # already in front of the camera before the first quiet period.
        self.mode = self.ACTIVE
        self._last_activity = _clock()

    @property
    def skip(self) -> str:
        return self.idle_skip if self.mode == self.IDLE else self.active_skip

    def observe(self, result, now: float | None = None) -> bool:
        """Feed one FrameResult; returns True when the mode flipped (the
        caller must then rebuild its source with the new ``skip``)."""
        if now is None:
            now = self._clock()
        active = bool(
            getattr(result, "tracks", None)
            or getattr(result, "motion_boxes", None)
            or getattr(result, "calibrating", False)
        )
        if active:
            self._last_activity = now
            if self.mode == self.IDLE:
                self.mode = self.ACTIVE
                return True
            return False
        if self.mode == self.ACTIVE and now - self._last_activity >= self.idle_after:
            self.mode = self.IDLE
            return True
        return False


# ── the real per-camera worker ──────────────────────────────────────

class CameraWorker:
    """Runs the Tier-0 pipeline for one camera on a background thread."""

    def __init__(
        self,
        spec: CameraSpec,
        sink: ResultSink,
        *,
        detector: DetectorAdapter | None = None,
        model_size: int = 320,
        model_id: str | None = None,             # detector identity for benchmarking labels
        best_frames=None,                        # shared BestFrameStore (on-demand best frame)
        device: str = "/dev/dri/renderD128",
        hwaccel: str = "cpu",             # service-wide DETECT_HWACCEL default
        decode_skip: str = "none",               # ffmpeg -skip_frame (decode-side CPU dial)
        decode_threads: int = 2,                 # ffmpeg decoder thread cap (0 = auto)
        fast_decode: bool = False,               # skip h264 loop filter (opt-in)
        rtsp_timeout_s: float = DEFAULT_RTSP_TIMEOUT_S,   # socket-I/O timeout
        url_provider=None,                       # freshest tap URL (its JWT rotates)
        start_delay: float = 0.0,                # stagger the first RTSP dial
        decode_idle: str = "",                   # idle skip mode ("" = adaptive off)
        decode_idle_after: float = 60.0,         # quiet seconds before idling
        frame_source=None,                       # injectable for tests
        gate: Gate | None = None,                # per-camera Tier-1 gate (PR B)
        gate_sink=None,                          # publishes gate decisions (audit)
        dispatcher=None,                         # Tier-1 dispatch (#10); shared, thread-safe
        router=None,                             # escalation → adapter routing
        visit_poster=None,                       # events store: post finished visits (RFC-0001 C1)
    ) -> None:
        self.spec = spec
        self.sink = sink
        self.detector = detector or StubDetector()
        self.model_size = model_size
        self.model_id = model_id
        self.best_frames = best_frames
        self.device = device
        self.hwaccel = hwaccel
        self.decode_skip = decode_skip
        self.decode_threads = decode_threads
        self.fast_decode = fast_decode
        self.rtsp_timeout_s = rtsp_timeout_s
        self.url_provider = url_provider
        self.start_delay = max(0.0, start_delay)
        self.decode_idle = decode_idle
        self.decode_idle_after = decode_idle_after
        self._frame_source = frame_source
        self.gate = gate
        self.gate_sink = gate_sink
        self.dispatcher = dispatcher
        self.router = router
        self.visit_poster = visit_poster
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # The live source, so request_stop() can unblock the reader.
        self._src = None
        self._superseded = False

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name=f"tier0-{self.spec.camera_id}", daemon=True
        )
        self._thread.start()

    def request_stop(self) -> None:
        """Signal the worker to stop and unblock it — never waits.

        Split from the join so a manager stopping N cameras can signal them
        all first and then wait ONCE, instead of paying the timeout serially
        per camera. Closing the source is what makes the wait short: the
        thread is otherwise parked in a blocking read or the restart backoff
        and only notices the flag between frames.
        """
        self._stop.set()
        src = self._src
        if src is not None:
            close = getattr(src, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # pragma: no cover - defensive
                    log.debug("tier0 %s: error closing source",
                              self.spec.camera_id, exc_info=True)

    def join(self, timeout: float) -> bool:
        """Wait up to ``timeout`` for the thread to exit; True if it did."""
        if self._thread is None:
            return True
        self._thread.join(timeout=max(0.0, timeout))
        return not self._thread.is_alive()

    def mark_superseded(self) -> None:
        """A replacement worker for this camera now owns the metrics.

        Without this, a worker that outlived its join would later write
        tier0_worker_up=0 and zero the gauge of the healthy replacement.
        """
        self._superseded = True

    def stop(self, timeout: float = 5.0) -> bool:
        self.request_stop()
        return self.join(timeout)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _effective_hwaccel(self) -> HwAccel:
        """Resolve this camera's decode backend, degrading loudly if unusable.

        Announced per camera because the consequence is a ~6x CPU difference
        and it is otherwise invisible: core hands the pipeline the MAIN
        stream whenever hwaccel is configured (it assumes a GPU will absorb
        it), so silently falling back to software decode would leave the
        camera decoding full-resolution video on the CPU.
        """
        requested = hwaccel_for(self.spec, self.hwaccel)
        accel, downgrade = resolve_hwaccel(requested, device=self.device)
        if downgrade:
            log.warning(
                "tier0 %s: hwaccel %r unusable — falling back to CPU decode: %s",
                self.spec.camera_id, requested, downgrade,
            )
        elif accel is not HwAccel.CPU:
            log.info(
                "tier0 %s: hardware decode via %s (%s)",
                self.spec.camera_id, accel.value, self.device,
            )
        return accel

    def _make_source(self, decode_skip: str | None = None):
        if self._frame_source is not None:
            return self._frame_source, self._frame_source.width, self._frame_source.height
        w, h = self.spec.width, self.spec.height
        if not (w and h):
            probed = probe_stream(self.spec.substream_url)
            if probed is None:
                raise RuntimeError(f"could not probe {self.spec.camera_id} stream")
            w, h, _fps = probed
        src = FrameSource(
            self.spec.substream_url, width=w, height=h, fps=self.spec.fps,
            hwaccel=self._effective_hwaccel(), device=self.device,
            decode_skip=self.decode_skip if decode_skip is None else decode_skip,
            decode_threads=self.decode_threads,
            fast_decode=self.fast_decode,
            rtsp_timeout_s=self.rtsp_timeout_s,
            url_provider=self.url_provider,
        )
        return src, w, h

    def _run(self) -> None:
        # Stagger the opening dial. A reconcile tick starts every new worker
        # at once, and each one ffprobes then spawns ffmpeg — so a fleet
        # coming up (or recovering together after a MediaMTX restart) hits it
        # with N simultaneous RTSP session setups. Interruptible: a stop
        # during the stagger returns immediately rather than waiting it out.
        if self.start_delay > 0 and self._stop.wait(self.start_delay):
            # Same guard the teardown path uses: a worker stopped during its
            # stagger may already have been replaced (the fleet stop shares
            # ONE deadline, so a late waker is marked superseded), and writing
            # DOWN here would zero the gauge its replacement just set — the
            # camera would then read down for good, since UP is only written
            # once at start.
            if not self._superseded:
                record_worker_state(
                    self.spec.camera_id, False, target_fps=self.spec.fps
                )
            return
        try:
            src, w, h = self._make_source()
            self._src = src
            # request_stop() reads self._src to close the source. Between the
            # stagger returning and this assignment it is still None, so a stop
            # arriving in that window closed NOTHING and left _closing False:
            # the worker went on to dial RTSP and, on a dead camera, kept
            # respawning ffmpeg for a minute or more AFTER being told to stop —
            # while the manager had already timed out and started its
            # replacement. Re-check now that the source is reachable; the flag
            # is set before _src is read, so one of the two orderings always
            # catches it.
            if self._stop.is_set():
                src.close()
                if not self._superseded:
                    record_worker_state(
                        self.spec.camera_id, False, target_fps=self.spec.fps
                    )
                return
        except Exception as exc:
            # Emit the DOWN gauge before bailing. Returning here used to skip
            # record_worker_state entirely, so a camera that never opens had
            # no tier0_worker_up series at all — not even 0 — and reconcile
            # silently re-attempted it every tick with nothing but a log line
            # to show for it. An absent series can't alert; a 0 can.
            record_worker_state(self.spec.camera_id, False, target_fps=self.spec.fps)
            # A camera that is simply unreachable — powered off, unplugged,
            # rebooting — is normal operation, not a code fault. ffprobe has
            # already logged WHY at warning level, so a full traceback every
            # reconcile tick just buries that reason and reads like a crash.
            # Anything unexpected still gets its stack.
            if isinstance(exc, RuntimeError) and "could not probe" in str(exc):
                log.warning(
                    "tier0 %s: source unavailable (%s) — retrying next tick",
                    self.spec.camera_id, exc,
                )
            else:
                log.exception("tier0 %s: could not open source", self.spec.camera_id)
            return
        # Substream guard: Tier-0 is designed to decode a LOW-RES substream.
        # Decoding a high-res main stream (no substream configured for this
        # camera) is the expensive path — the difference between ~0.3 and ~2
        # CPU cores. Say so loudly and expose it as a gauge so the panel and
        # the README's "why is my CPU high" section can point at it.
        mainstream = (w or 0) * (h or 0) > 1280 * 720
        record_mainstream_fallback(self.spec.camera_id, mainstream)
        if mainstream:
            log.warning(
                "tier0 %s: decoding a %dx%d stream — this looks like a MAIN "
                "stream (no substream configured). Configure the camera's "
                "substream to cut Tier-0's CPU cost by ~5x "
                "(see detect-pipeline/README.md).",
                self.spec.camera_id, w, h,
            )
        from .events_poster import VisitLifecycle
        lifecycle = VisitLifecycle(
            self.spec.camera_id, nvr_camera_id=self.spec.nvr_camera_id
        )
        motion = MotionDetector((h, w), MotionConfig())
        tracker = Tracker((h, w), TrackConfig(
            fps=self.spec.fps,
            max_tracks=_env_int("DETECT_MAX_TRACKS", 50),
            coast_ttl_seconds=float(_env_int("DETECT_TRACK_TTL", 300)),
            min_spawn_score=_env_float("DETECT_MIN_SPAWN_SCORE", 0.5),
        ))
        pipe = DetectPipeline(
            None, motion, self.detector, tracker,
            model_size=(self.model_size, self.model_size),
            stationary_interval=_env_int("DETECT_STATIONARY_INTERVAL", 10),
            max_regions=_env_int("DETECT_MAX_REGIONS", 8),
            allowed_labels=allowed_labels_for(self.spec),
        )
        if self.spec.labels is not None:
            log.info(
                "tier0 %s: per-camera assignment labels active: %s",
                self.spec.camera_id, ", ".join(sorted(self.spec.labels)) or "-",
            )
        log.info("tier0 %s: started (%dx%d)", self.spec.camera_id, w, h)
        adaptive: AdaptiveDecode | None = None
        if self.decode_idle and self._frame_source is None:
            adaptive = AdaptiveDecode(
                self.decode_idle, self.decode_skip, self.decode_idle_after
            )
            _metrics.gauge(
                "tier0_decode_idle", 0.0, {"camera": self.spec.camera_id}
            )
            log.info(
                "tier0 %s: adaptive decode on (idle=%s after %.0fs quiet)",
                self.spec.camera_id, self.decode_idle, self.decode_idle_after,
            )
        record_worker_state(self.spec.camera_id, True, target_fps=self.spec.fps)
        budget = RegionBudgetController(pipe.max_regions, self.spec.fps)
        _metrics.gauge(
            "tier0_regions_budget", float(budget.current),
            {"camera": self.spec.camera_id},
        )
        prev_seq: int | None = None
        win_t0 = time.monotonic()
        win_n = 0
        try:
            for frame in src.stream():
                if self._stop.is_set():
                    break
                # Frame.seq resets to 0 when the source (ffmpeg) restarts — a truthful
                # restart signal without reaching into the source's internals.
                seq = getattr(frame, "seq", None)
                if seq == 0 and prev_seq is not None:
                    # Adaptive decode (on by default) respawns ffmpeg on every
                    # idle<->active flip, which also resets seq. Counting those
                    # as feed restarts made a healthy camera with intermittent
                    # activity look like a flapping one — on the very metric
                    # the docs tell operators to alert on.
                    if getattr(frame, "deliberate_restart", False):
                        _metrics.inc(
                            "tier0_decode_mode_changes_total",
                            {"camera": self.spec.camera_id},
                        )
                    else:
                        record_worker_restart(self.spec.camera_id)
                prev_seq = seq
                t0 = time.monotonic()
                result = pipe.process_frame(frame)
                frame_latency = time.monotonic() - t0
                # Spend fewer detector crops when we cannot finish a frame in
                # its budget. Falling behind does not just delay detections —
                # the worker stops draining ffmpeg's stdout, ffmpeg blocks on
                # the full pipe, and MediaMTX drops the session, which surfaces
                # as a "flaky camera" that is really us.
                delta = budget.observe(frame_latency, len(result.regions))
                if delta:
                    pipe.max_regions = budget.current
                    _metrics.gauge(
                        "tier0_regions_budget", float(budget.current),
                        {"camera": self.spec.camera_id},
                    )
                    # Shedding is a capability loss and deserves a warning;
                    # getting capacity back is routine. The direction comes
                    # from the DELTA — "shedding" (below configured) is still
                    # true while recovering, so keying off it reported a step
                    # UP as a cut.
                    log.log(
                        logging.WARNING if delta < 0 else logging.INFO,
                        "tier0 %s: frame latency %.2fs against a %.2fs budget — "
                        "detector regions %s %d (of %d configured)",
                        self.spec.camera_id, frame_latency, budget.budget_s,
                        "cut to" if delta < 0 else "restored to",
                        budget.current, budget.configured,
                    )
                record_frame(
                    self.spec.camera_id, result,
                    latency_s=frame_latency,
                    detector_latency_s=getattr(result, "detect_latency_s", None),
                    stage_latency_s=getattr(result, "stage_latency_s", None),
                    model=self.model_id,
                )
                # Bounded-load observability: spawns refused by the track
                # cap / spawn-score floor (monotonic; exported as a gauge).
                _metrics.gauge(
                    "tier0_track_spawns_dropped", float(tracker.spawns_dropped),
                    {"camera": self.spec.camera_id},
                )
                # Retain each track's best crop for on-demand fetch (best-frame
                # store) — cheap: a reference, encoded lazily only if requested.
                if self.best_frames is not None:
                    ts = getattr(frame, "ts", None)
                    for tr in result.tracks:
                        crop = getattr(tr, "best_crop", None)
                        if crop is not None:
                            self.best_frames.put(self.spec.camera_id, tr.id, crop, ts)
                # Visit lifecycle: when a track id vanishes, that visit is over
                # and its best frame is final — exactly the moment it becomes
                # history in the canonical event store. Non-blocking: finished
                # visits go to the poster's bounded queue.
                if self.visit_poster is not None:
                    for visit in lifecycle.observe(result.tracks, time.time()):
                        self.visit_poster.submit(visit)
                # Sustained fps over a ~1s window — compared to target_fps, this is
                # the "is the box keeping up with this camera" signal.
                win_n += 1
                now = time.monotonic()
                if now - win_t0 >= 1.0:
                    record_processing_fps(self.spec.camera_id, win_n / (now - win_t0))
                    win_t0, win_n = now, 0
                try:
                    if self.sink.publish(self.spec.camera_id, result, frame):
                        record_published(self.spec.camera_id)   # count real publishes only
                except Exception:
                    # Counted, not just debug-logged: at the default INFO level
                    # a sink raising on EVERY frame produced no output at all,
                    # so a bus outage looked identical to a quiet scene —
                    # tier0_events_published_total simply stopped climbing with
                    # nothing to say why.
                    record_sink_error(self.spec.camera_id)
                    log.debug("tier0 %s: sink error", self.spec.camera_id, exc_info=True)
                if self.gate is not None:
                    self._run_gate(result, frame)
                # Adaptive decode: promote on the first sign of activity,
                # demote after the quiet period. The flip terminates the
                # current ffmpeg; the source's restart loop respawns it with
                # the new -skip_frame immediately (no backoff).
                if adaptive is not None and adaptive.observe(result):
                    idle = adaptive.mode == AdaptiveDecode.IDLE
                    _metrics.gauge(
                        "tier0_decode_idle", 1.0 if idle else 0.0,
                        {"camera": self.spec.camera_id},
                    )
                    log.info(
                        "tier0 %s: decode %s (skip=%s)",
                        self.spec.camera_id, adaptive.mode, adaptive.skip,
                    )
                    src.set_decode_skip(adaptive.skip)
        except Exception:
            log.exception("tier0 %s: worker loop crashed", self.spec.camera_id)
        finally:
            # Worker stopping: whatever is still live is a finished visit too.
            if self.visit_poster is not None:
                for visit in lifecycle.flush():
                    self.visit_poster.submit(visit)
            if not self._superseded:
                record_worker_state(self.spec.camera_id, False)
            log.info("tier0 %s: stopped", self.spec.camera_id)

    def _run_gate(self, result, frame) -> None:
        """Gate the tracks (shadow by default) and publish the decisions (audit)."""
        try:
            now = getattr(frame, "ts", None)
            if now is None:
                now = time.monotonic()
            gres = self.gate.evaluate(result.tracks, now)
            record_gate(self.spec.camera_id, gres)
            if self.gate_sink is not None:
                self.gate_sink.publish(self.spec.camera_id, gres, frame)
            # Tier-1 dispatch (#10) — enforce-only; shadow/off dispatch nothing.
            if self.dispatcher is not None and self.router is not None:
                dispatch_escalations(
                    self.spec.camera_id, result.tracks, gres, self.router, self.dispatcher
                )
        except Exception:
            log.debug("tier0 %s: gate error", self.spec.camera_id, exc_info=True)


WorkerFactory = Callable[[CameraSpec, ResultSink], Worker]


# ── the manager that reconciles workers to the camera list ──────────

class WorkerManager:
    """Keeps exactly one worker running per analyze-enabled camera."""

    def __init__(
        self,
        provider: CameraProvider,
        sink: ResultSink,
        *,
        enabled: bool = True,
        worker_factory: WorkerFactory | None = None,
        detector_factory: Callable[[], DetectorAdapter] | None = None,
        detector_pool: int = 0,                           # 0 = one detector per worker
        start_spread_s: float = 10.0,                     # spread simultaneous dials
        cv_threads_pinned: bool = False,                  # operator set DETECT_CV_THREADS

        model_size: int = 320,
        model_id: str | None = None,                      # detector identity (benchmark labels)
        best_frames=None,                                 # shared BestFrameStore (thread-safe)
        device: str = "/dev/dri/renderD128",
        hwaccel: str = "cpu",                             # service-wide DETECT_HWACCEL
        decode_skip: str = "none",                        # ffmpeg -skip_frame (decode-side CPU dial)
        decode_threads: int = 2,                          # ffmpeg decoder thread cap (0 = auto)
        fast_decode: bool = False,                        # skip h264 loop filter (opt-in)
        rtsp_timeout_s: float = DEFAULT_RTSP_TIMEOUT_S,   # socket-I/O timeout
        decode_idle: str = "",                            # idle skip mode ("" = adaptive off)
        decode_idle_after: float = 60.0,                  # quiet seconds before idling
        gate_factory: Callable[[], Gate] | None = None,   # fresh gate per camera (stateful)
        gate_sink=None,
        dispatcher=None,                                  # Tier-1 dispatch (#10), shared
        router=None,
        visit_poster=None,                                # events store (RFC-0001 C1), shared
    ) -> None:
        self.provider = provider
        self.sink = sink
        self.enabled = enabled
        # cv2 detectors are not safe to call concurrently on one instance, so
        # a worker can never SHARE a detector — but it does not need a private
        # one either. A pool borrows an instance for the duration of a single
        # detect() call, which keeps the exclusivity while capping how many
        # models are resident: one per camera was this service's memory wall
        # (each holds its own weights + activation arenas), and it grew with
        # the fleet rather than with the hardware.
        #
        # detector_pool=0/None keeps the old behaviour (one per worker) so a
        # caller can opt out; the pool grows lazily, so below the cap nothing
        # changes anyway.
        # NOT `self._factory is self._default_factory`: attribute access
        # builds a NEW bound method each time, so that identity check is
        # always False and the stagger would never apply.
        self._own_factory = worker_factory is None
        factory = detector_factory or (lambda: StubDetector())
        self._detector_factory = factory
        self._shared_detector = (
            DetectorPool(factory, detector_pool) if detector_pool else None
        )
        self._model_size = model_size
        self._model_id = model_id
        self._best_frames = best_frames
        self._device = device
        self._hwaccel = hwaccel
        # Max window over which a batch of new workers opens its streams.
        self._start_spread_s = max(0.0, start_spread_s)
        self._rand = random.random
        self._decode_skip = decode_skip
        self._decode_threads = decode_threads
        self._fast_decode = fast_decode
        self._rtsp_timeout_s = rtsp_timeout_s
        self._decode_idle = decode_idle
        self._decode_idle_after = decode_idle_after
        # The gate is stateful per camera, so each worker gets its own instance.
        self._gate_factory = gate_factory
        self._gate_sink = gate_sink
        # The dispatcher is thread-safe (semaphore + pool) → shared across workers.
        self._dispatcher = dispatcher
        self._router = router
        self._visit_poster = visit_poster
        self._factory = worker_factory or self._default_factory
        self._workers: dict[str, Worker] = {}
        # The spec each running worker was built with — reconcile restarts
        # on any change to the fields a worker bakes in at start (see
        # _baked: everything except the volatile substream_url, whose JWT
        # is re-minted on every fetch, and nvr_camera_id, compared
        # asymmetrically) so a settings-page change takes effect within
        # one tick.
        self._specs: dict[str, CameraSpec] = {}
        # Freshest tap URL seen per camera, refreshed every reconcile. The URL
        # embeds a 60-minute JWT; a long-running worker's baked-in copy WILL
        # expire, so the frame source consults this before each respawn
        # instead of 401-ing until the worker dies. Kept out of _baked (a
        # re-minted JWT must not bounce a healthy worker) precisely because
        # this is the cheaper way to deliver it.
        self._latest_url: dict[str, str] = {}
        # Inference width, retuned as the fleet changes size. Pinned means
        # the operator set DETECT_CV_THREADS and we leave it alone.
        self._cv_threads = 0
        self._cv_threads_pinned = cv_threads_pinned

    def current_url(self, camera_id: str) -> str | None:
        """The freshest known stream URL for a camera (see _latest_url)."""
        return self._latest_url.get(camera_id)

    def _make_worker(self, spec: CameraSpec, spread: float) -> Worker:
        """Build a worker, giving it a random slot inside the start window.

        Only the built-in factory is offered a stagger: an injected factory
        (tests, fakes) has the two-argument shape and opens no stream, so
        there is nothing to spread.
        """
        if not self._own_factory:
            return self._factory(spec, self.sink)
        delay = self._rand() * spread if spread > 0 else 0.0
        return self._default_factory(spec, self.sink, delay)

    def _default_factory(self, spec: CameraSpec, sink: ResultSink,
                         start_delay: float = 0.0) -> Worker:
        return CameraWorker(
            spec, sink,
            detector=self._shared_detector or self._detector_factory(),
            model_size=self._model_size, model_id=self._model_id,
            best_frames=self._best_frames, device=self._device,
            hwaccel=self._hwaccel,
            decode_skip=self._decode_skip,
            decode_threads=self._decode_threads,
            fast_decode=self._fast_decode,
            rtsp_timeout_s=self._rtsp_timeout_s,
            url_provider=lambda cid=spec.camera_id: self.current_url(cid),
            start_delay=start_delay,
            decode_idle=self._decode_idle,
            decode_idle_after=self._decode_idle_after,
            gate=self._gate_factory() if self._gate_factory else None,
            gate_sink=self._gate_sink,
            dispatcher=self._dispatcher, router=self._router,
            visit_poster=self._visit_poster,
        )

    def running_ids(self) -> set[str]:
        return set(self._workers)

    def apply_gate_change(self, gate_factory, *, dispatcher=None, router=None) -> None:
        """Swap the gate (and optionally Tier-1 dispatch) for ALL workers.

        Used by guided promotion: the admin flips shadow->enforce in the UI
        and the pipeline applies it live. Gates are stateful per camera, so
        the only correct swap is stop-everything — the next reconcile tick
        rebuilds every worker with the new factory. A few seconds of gap on
        an explicit admin action is fine; a redeploy is not.

        "A few seconds" is now true at fleet scale: the wind-down signals
        every worker first and then waits ONCE (see _retire). Stopping them
        serially made this cost STOP_TIMEOUT_S per camera — minutes of total
        blackout on a large install, from one click in the UI."""
        self._gate_factory = gate_factory
        # Retire the outgoing dispatcher. _poll_gate_override builds a FRESH
        # KaicDispatcher on every transition into enforce and passes None on
        # the way out, and close() was never called anywhere — so an admin
        # A/B-ing shadow vs enforce from the promotion panel leaked a 4-thread
        # pool per round trip, and on the way out the old one stayed wired up
        # (disarmed only accidentally, by the gate returning nothing to
        # dispatch in shadow).
        if dispatcher is not self._dispatcher:
            self._close_dispatcher(self._dispatcher)
            self._dispatcher = dispatcher
        if router is not None:
            self._router = router
        self._stop_all()

    @staticmethod
    def _close_dispatcher(dispatcher) -> None:
        close = getattr(dispatcher, "close", None)
        if close is None:
            return
        try:
            close()
        except Exception:  # pragma: no cover - defensive
            log.debug("error closing the outgoing dispatcher", exc_info=True)

    @staticmethod
    def _baked(spec: CameraSpec) -> CameraSpec:
        """The spec with volatile fields masked — what a restart-worthy
        change means. substream_url carries a freshly minted JWT on every
        fetch; nvr_camera_id is compared separately (asymmetric: a fetch
        that degrades it to None must not bounce the worker)."""
        return replace(spec, substream_url="", nvr_camera_id=None)

    def reconcile(self) -> None:
        """Start workers for new analyze-enabled cameras, stop the rest."""
        if not self.enabled:
            self._stop_all()
            return
        cams = self.provider.list_cameras()
        if cams is None:
            # Discovery failed (core briefly down / timeout) — an empty
            # answer would read as "no cameras" and tear down every worker.
            # Keep what's running; the next tick retries.
            return
        desired = {c.camera_id: c for c in cams if c.analyze}
        # Every camera we just heard about, analyzed or not — a running
        # worker's source reads this to pick up a re-minted JWT.
        for c in cams:
            if c.substream_url:
                self._latest_url[c.camera_id] = c.substream_url

        doomed: dict[str, Worker] = {}
        for cid in list(self._workers):
            worker = self._workers[cid]
            spec_changed = (
                cid in desired
                and self._specs.get(cid) is not None
                and (
                    self._baked(desired[cid]) != self._baked(self._specs[cid])
                    or (
                        desired[cid].nvr_camera_id is not None
                        and desired[cid].nvr_camera_id
                        != self._specs[cid].nvr_camera_id
                    )
                )
            )
            if cid not in desired or not worker.is_alive() or spec_changed:
                doomed[cid] = worker
                del self._workers[cid]
                self._specs.pop(cid, None)
                if cid not in desired:
                    # Gone from core entirely — drop its frame-age entry so
                    # it stops being reported stale forever.
                    self._latest_url.pop(cid, None)
                    forget_camera(cid)
                if spec_changed:
                    log.info(
                        "tier0: restarting %s — baked-in spec changed "
                        "(labels=%s, nvr_camera_id=%s)",
                        cid,
                        sorted(desired[cid].labels) if desired[cid].labels is not None
                        else "(global default)",
                        desired[cid].nvr_camera_id,
                    )

        # Stop them together. Signalling every doomed worker BEFORE waiting on
        # any of them is what keeps the wind-down bounded by one timeout
        # instead of one-per-camera; the old serial loop turned a spec change
        # across a fleet into minutes of blocked reconcile.
        # Cameras still in `desired` are being REPLACED this tick, not removed.
        self._retire(doomed, replaced={c for c in doomed if c in desired})

        starting = [c for c in desired if c not in self._workers]
        # Spread this batch's opening dials. One tick starting N cameras means
        # N ffprobes and N RTSP session setups in the same instant — the shape
        # of a cold start AND of a fleet-wide recovery. Proportional so a
        # couple of cameras still come up immediately.
        spread = min(self._start_spread_s, 0.25 * max(0, len(starting) - 1))
        for cid, spec in desired.items():
            if cid not in self._workers:
                worker = self._make_worker(spec, spread)
                worker.start()
                self._workers[cid] = worker
                self._specs[cid] = spec
                log.info("tier0: started worker for camera %s", cid)
        self._tune_inference_threads()

    def _tune_inference_threads(self) -> None:
        """Give each concurrent inference the cores it can actually use.

        A FIXED per-inference thread cap is right for a fleet and wrong for a
        small one. Measured on an 8-core box, one camera, yolov8n at 640:

            1 thread   874 ms      4 threads  241 ms
            2 threads  449 ms      8 threads  176 ms

        Near-linear — so the shipped cap of 2 left six cores idle and made
        every frame 2.5x more expensive than the hardware required. That is
        what pushed this deployment six times over its frame budget.

        At most min(pool, cameras) inferences run at once, so that is what the
        cores divide between. Process-global (cv2's cap is), recomputed only
        when the camera count changes, and never overridden when the operator
        pinned DETECT_CV_THREADS themselves.
        """
        if self._cv_threads_pinned or not self._workers:
            return
        concurrent = len(self._workers)
        if self._shared_detector is not None:
            concurrent = min(concurrent, self._shared_detector.max_size)
        cores = os.cpu_count() or 2
        want = max(1, cores // max(1, concurrent))
        if want == self._cv_threads:
            return
        try:
            import cv2

            cv2.setNumThreads(want)
        except Exception:  # pragma: no cover - cv2 optional (stub/hog)
            return
        log.info(
            "tier0: %d camera(s) on %d cores — %d inference thread(s) each "
            "(was %d)", len(self._workers), cores, want, self._cv_threads,
        )
        self._cv_threads = want

    def _retire(self, workers: dict[str, Worker], replaced: set[str] | None = None) -> None:
        """Wind down a set of workers: signal all, then wait once.

        A straggler is reported rather than silently orphaned. Its source has
        already been closed by the signal, so it is no longer decoding or
        publishing — but it is marked superseded so that when it finally
        exits it cannot zero the health gauge of its replacement.
        """
        if not workers:
            return
        # Mark the ones a replacement is already queued for BEFORE signalling.
        # Doing it only for post-timeout stragglers left a race: a worker that
        # exits microseconds after the deadline runs its teardown concurrently
        # with the manager's loop, reads _superseded as False, and writes
        # tier0_worker_up=0. If that lands after the replacement's UP — likely,
        # since the replacement must clear a stagger, an ffprobe and an ffmpeg
        # spawn while the straggler only has to flush — the gauge stays 0
        # forever, because UP is written exactly once per worker at start.
        for cid in (replaced or set()):
            supersede = getattr(workers.get(cid), "mark_superseded", None)
            if supersede is not None:
                supersede()
        for worker in workers.values():
            _signal_stop(worker)
        stragglers = _join_workers(workers, STOP_TIMEOUT_S)
        for cid in stragglers:
            supersede = getattr(workers[cid], "mark_superseded", None)
            if supersede is not None:
                supersede()
            record_worker_straggler(cid)
        if stragglers:
            log.warning(
                "tier0: %d worker(s) did not exit within %.0fs: %s — their "
                "sources are closed; they are superseded and will not report "
                "state for the cameras that replace them",
                len(stragglers), STOP_TIMEOUT_S, ", ".join(sorted(stragglers)),
            )

    def _stop_all(self) -> None:
        self._retire(dict(self._workers))
        self._workers.clear()
        self._specs.clear()

    def stop(self) -> None:
        self._stop_all()
        self._close_dispatcher(self._dispatcher)
