# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Container entrypoint for the Tier-0 service (``opennvr-tier0``).

Reads config from the environment, discovers cameras from opennvr-core, runs one
worker per camera, and publishes detections to NATS. Enabled by default; set
``DETECT_PIPELINE_ENABLED=false`` to disable (the container stays up but idle, so
it can be re-enabled without a redeploy).

Env:
  DETECT_PIPELINE_ENABLED   on/off (default true)
  OPENNVR_INTERNAL_URL      opennvr-core base (default http://opennvr-core:8000)
  INTERNAL_API_KEY          shared secret for opennvr-core's internal endpoint
                            (the same INTERNAL_API_KEY the deployment already uses)
  NATS_URL                  e.g. nats://nats:4222 (best-effort; down != fatal)
  DETECT_DETECTOR           hog | blob | stub (default hog)
  DETECT_HWACCEL            cpu | vaapi | nvidia | qsv | rpi | rkmpp | jetson
  DETECT_HWACCEL_DEVICE     e.g. /dev/dri/renderD128
  DETECT_MODEL_SIZE         detector input square (default 320)
  DETECT_REFRESH_SECONDS    camera-list reconcile interval (default 30)
"""
from __future__ import annotations

import logging
import os
import signal
import threading
from dataclasses import dataclass

from .bus import EventSink
from .providers import HttpCameraProvider
from .service import WorkerManager

log = logging.getLogger("detect_pipeline.run")


@dataclass
class ServiceConfig:
    enabled: bool
    core_url: str
    api_key: str | None
    nats_url: str | None
    detector: str
    onnx_model: str
    onnx_input: int
    hwaccel: str
    device: str
    model_size: int
    refresh_seconds: float


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def config_from_env(env: dict) -> ServiceConfig:
    return ServiceConfig(
        enabled=_truthy(env.get("DETECT_PIPELINE_ENABLED", "true")),
        core_url=env.get("OPENNVR_INTERNAL_URL", "http://opennvr-core:8000"),
        api_key=env.get("INTERNAL_API_KEY") or None,
        nats_url=env.get("NATS_URL") or None,
        detector=env.get("DETECT_DETECTOR", "onnx"),
        onnx_model=env.get("DETECT_ONNX_MODEL", "/app/model_weights/yolov8n.onnx"),
        onnx_input=int(env.get("DETECT_ONNX_INPUT", "640")),
        hwaccel=env.get("DETECT_HWACCEL", "cpu"),
        device=env.get("DETECT_HWACCEL_DEVICE", "/dev/dri/renderD128"),
        model_size=int(env.get("DETECT_MODEL_SIZE", "320")),
        refresh_seconds=float(env.get("DETECT_REFRESH_SECONDS", "30")),
    )


def _stub_factory():
    from .detector import StubDetector
    return StubDetector


def _detector_factory(cfg: ServiceConfig):
    """Return a zero-arg factory that builds one detector per worker.

    Every path degrades to the stub (worker runs, tracks motion regions, detects
    nothing) rather than crash-looping the container — so a missing/broken model,
    or an OpenCV build without a detector, is never fatal.
    """
    name = cfg.detector
    if name == "onnx":
        if not cfg.onnx_model or not os.path.exists(cfg.onnx_model):
            log.warning("ONNX model not found at %s; using stub detector", cfg.onnx_model)
            return _stub_factory()
        from .onnx_detector import OnnxYoloDetector

        def make_onnx():
            try:
                return OnnxYoloDetector(model_path=cfg.onnx_model, input_size=cfg.onnx_input)
            except Exception:
                log.warning("failed to load ONNX model %s; using stub", cfg.onnx_model, exc_info=True)
                from .detector import StubDetector
                return StubDetector()

        return make_onnx
    if name == "hog":
        from .detectors_local import HogPersonDetector, hog_available
        if hog_available():
            return HogPersonDetector
        log.warning("detector 'hog' unavailable in this OpenCV build; using stub")
        return _stub_factory()
    if name == "blob":
        from .detectors_local import BrightBlobDetector
        return BrightBlobDetector
    return _stub_factory()


def build_manager(cfg: ServiceConfig, sink) -> WorkerManager:
    provider = HttpCameraProvider(cfg.core_url, api_key=cfg.api_key)
    # Region crops match the detector input so the model sees full-detail crops.
    model_size = cfg.onnx_input if cfg.detector == "onnx" else cfg.model_size
    return WorkerManager(
        provider, sink,
        enabled=cfg.enabled,
        detector_factory=_detector_factory(cfg),
        model_size=model_size,
        device=cfg.device,
    )


def _make_publisher(nats_url: str | None):  # pragma: no cover - needs a broker
    """Return (publish_fn, close_fn). Best-effort: NATS down never breaks a worker."""
    if not nats_url:
        return (lambda subject, data: None), (lambda: None)
    import asyncio

    import nats

    loop = asyncio.new_event_loop()
    box: dict = {}

    def _serve():
        asyncio.set_event_loop(loop)
        try:
            box["nc"] = loop.run_until_complete(nats.connect(nats_url))
            log.info("connected to NATS at %s", nats_url)
        except Exception:
            log.warning("NATS connect failed at %s; publishing disabled", nats_url)
        loop.run_forever()

    threading.Thread(target=_serve, name="tier0-nats", daemon=True).start()

    def publish(subject: str, data: bytes) -> None:
        nc = box.get("nc")
        if nc is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(nc.publish(subject, data), loop)
        except Exception:
            pass

    def close() -> None:
        loop.call_soon_threadsafe(loop.stop)

    return publish, close


def main() -> int:  # pragma: no cover - integration entrypoint
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = config_from_env(os.environ)
    log.info("tier0 service starting (enabled=%s, detector=%s, hwaccel=%s)",
             cfg.enabled, cfg.detector, cfg.hwaccel)

    publish, close = _make_publisher(cfg.nats_url)
    manager = build_manager(cfg, EventSink(publish))

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    try:
        while not stop.is_set():
            try:
                manager.reconcile()
            except Exception:
                log.exception("reconcile failed; will retry")
            stop.wait(cfg.refresh_seconds)
    finally:
        manager.stop()
        close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
