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
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from .detector import DetectorAdapter, StubDetector
from .frame_source import FrameSource, probe_stream
from .dispatch import dispatch_escalations
from .gate import Gate
from .ffmpeg_presets import HwAccel
from .metrics import (
    record_frame,
    record_gate,
    record_processing_fps,
    record_mainstream_fallback,
    record_published,
    record_worker_restart,
    record_worker_state,
)
from .motion import MotionConfig, MotionDetector
from .pipeline import DetectPipeline, FrameResult
from .tracking import TrackConfig, Tracker

log = logging.getLogger("detect_pipeline.service")


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
    hwaccel: str = "cpu"


class CameraProvider(Protocol):
    def list_cameras(self) -> list[CameraSpec]:
        ...


class ResultSink(Protocol):
    def publish(self, camera_id: str, result: FrameResult, frame) -> bool:
        """Return True iff an event was actually published (not a no-op frame)."""
        ...


class Worker(Protocol):
    """A per-camera worker handle (thread-backed in production, fake in tests)."""

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def is_alive(self) -> bool: ...


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
        frame_source=None,                       # injectable for tests
        gate: Gate | None = None,                # per-camera Tier-1 gate (PR B)
        gate_sink=None,                          # publishes gate decisions (audit)
        dispatcher=None,                         # Tier-1 dispatch (#10); shared, thread-safe
        router=None,                             # escalation → adapter routing
    ) -> None:
        self.spec = spec
        self.sink = sink
        self.detector = detector or StubDetector()
        self.model_size = model_size
        self.model_id = model_id
        self.best_frames = best_frames
        self.device = device
        self._frame_source = frame_source
        self.gate = gate
        self.gate_sink = gate_sink
        self.dispatcher = dispatcher
        self.router = router
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name=f"tier0-{self.spec.camera_id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _make_source(self):
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
            hwaccel=HwAccel(self.spec.hwaccel), device=self.device,
        )
        return src, w, h

    def _run(self) -> None:
        try:
            src, w, h = self._make_source()
        except Exception:
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
        motion = MotionDetector((h, w), MotionConfig())
        tracker = Tracker((h, w), TrackConfig(fps=self.spec.fps))
        pipe = DetectPipeline(
            None, motion, self.detector, tracker,
            model_size=(self.model_size, self.model_size),
        )
        log.info("tier0 %s: started (%dx%d)", self.spec.camera_id, w, h)
        record_worker_state(self.spec.camera_id, True, target_fps=self.spec.fps)
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
                    record_worker_restart(self.spec.camera_id)
                prev_seq = seq
                t0 = time.monotonic()
                result = pipe.process_frame(frame)
                record_frame(
                    self.spec.camera_id, result,
                    latency_s=time.monotonic() - t0,
                    detector_latency_s=getattr(result, "detect_latency_s", None),
                    stage_latency_s=getattr(result, "stage_latency_s", None),
                    model=self.model_id,
                )
                # Retain each track's best crop for on-demand fetch (best-frame
                # store) — cheap: a reference, encoded lazily only if requested.
                if self.best_frames is not None:
                    ts = getattr(frame, "ts", None)
                    for tr in result.tracks:
                        crop = getattr(tr, "best_crop", None)
                        if crop is not None:
                            self.best_frames.put(self.spec.camera_id, tr.id, crop, ts)
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
                    log.debug("tier0 %s: sink error", self.spec.camera_id, exc_info=True)
                if self.gate is not None:
                    self._run_gate(result, frame)
        except Exception:
            log.exception("tier0 %s: worker loop crashed", self.spec.camera_id)
        finally:
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
        model_size: int = 320,
        model_id: str | None = None,                      # detector identity (benchmark labels)
        best_frames=None,                                 # shared BestFrameStore (thread-safe)
        device: str = "/dev/dri/renderD128",
        gate_factory: Callable[[], Gate] | None = None,   # fresh gate per camera (stateful)
        gate_sink=None,
        dispatcher=None,                                  # Tier-1 dispatch (#10), shared
        router=None,
    ) -> None:
        self.provider = provider
        self.sink = sink
        self.enabled = enabled
        # A factory (not a shared instance) so each worker thread gets its own
        # detector — cv2 detectors aren't guaranteed thread-safe to share.
        self._detector_factory = detector_factory or (lambda: StubDetector())
        self._model_size = model_size
        self._model_id = model_id
        self._best_frames = best_frames
        self._device = device
        # The gate is stateful per camera, so each worker gets its own instance.
        self._gate_factory = gate_factory
        self._gate_sink = gate_sink
        # The dispatcher is thread-safe (semaphore + pool) → shared across workers.
        self._dispatcher = dispatcher
        self._router = router
        self._factory = worker_factory or self._default_factory
        self._workers: dict[str, Worker] = {}

    def _default_factory(self, spec: CameraSpec, sink: ResultSink) -> Worker:
        return CameraWorker(
            spec, sink, detector=self._detector_factory(),
            model_size=self._model_size, model_id=self._model_id,
            best_frames=self._best_frames, device=self._device,
            gate=self._gate_factory() if self._gate_factory else None,
            gate_sink=self._gate_sink,
            dispatcher=self._dispatcher, router=self._router,
        )

    def running_ids(self) -> set[str]:
        return set(self._workers)

    def apply_gate_change(self, gate_factory, *, dispatcher=None, router=None) -> None:
        """Swap the gate (and optionally Tier-1 dispatch) for ALL workers.

        Used by guided promotion: the admin flips shadow->enforce in the UI
        and the pipeline applies it live. Gates are stateful per camera, so
        the only correct swap is stop-everything — the next reconcile tick
        rebuilds every worker with the new factory. A few seconds of gap on
        an explicit admin action is fine; a redeploy is not."""
        self._gate_factory = gate_factory
        if dispatcher is not None:
            self._dispatcher = dispatcher
        if router is not None:
            self._router = router
        self._stop_all()

    def reconcile(self) -> None:
        """Start workers for new analyze-enabled cameras, stop the rest."""
        if not self.enabled:
            self._stop_all()
            return
        desired = {c.camera_id: c for c in self.provider.list_cameras() if c.analyze}

        for cid in list(self._workers):
            worker = self._workers[cid]
            if cid not in desired or not worker.is_alive():
                worker.stop()
                del self._workers[cid]

        for cid, spec in desired.items():
            if cid not in self._workers:
                worker = self._factory(spec, self.sink)
                worker.start()
                self._workers[cid] = worker
                log.info("tier0: started worker for camera %s", cid)

    def _stop_all(self) -> None:
        for worker in self._workers.values():
            worker.stop()
        self._workers.clear()

    def stop(self) -> None:
        self._stop_all()
