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
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from .detector import DetectorAdapter, StubDetector
from .frame_source import FrameSource, probe_stream
from .ffmpeg_presets import HwAccel
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
    def publish(self, camera_id: str, result: FrameResult, frame) -> None:
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
        device: str = "/dev/dri/renderD128",
        frame_source=None,                       # injectable for tests
    ) -> None:
        self.spec = spec
        self.sink = sink
        self.detector = detector or StubDetector()
        self.model_size = model_size
        self.device = device
        self._frame_source = frame_source
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
        motion = MotionDetector((h, w), MotionConfig())
        tracker = Tracker((h, w), TrackConfig(fps=self.spec.fps))
        pipe = DetectPipeline(
            None, motion, self.detector, tracker,
            model_size=(self.model_size, self.model_size),
        )
        log.info("tier0 %s: started (%dx%d)", self.spec.camera_id, w, h)
        try:
            for frame in src.stream():
                if self._stop.is_set():
                    break
                result = pipe.process_frame(frame)
                try:
                    self.sink.publish(self.spec.camera_id, result, frame)
                except Exception:
                    log.debug("tier0 %s: sink error", self.spec.camera_id, exc_info=True)
        except Exception:
            log.exception("tier0 %s: worker loop crashed", self.spec.camera_id)
        finally:
            log.info("tier0 %s: stopped", self.spec.camera_id)


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
        detector: DetectorAdapter | None = None,
        model_size: int = 320,
        device: str = "/dev/dri/renderD128",
    ) -> None:
        self.provider = provider
        self.sink = sink
        self.enabled = enabled
        self._detector = detector
        self._model_size = model_size
        self._device = device
        self._factory = worker_factory or self._default_factory
        self._workers: dict[str, Worker] = {}

    def _default_factory(self, spec: CameraSpec, sink: ResultSink) -> Worker:
        return CameraWorker(
            spec, sink, detector=self._detector,
            model_size=self._model_size, device=self._device,
        )

    def running_ids(self) -> set[str]:
        return set(self._workers)

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
