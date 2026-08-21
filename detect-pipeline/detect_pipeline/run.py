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
  NATS_TOKEN                bus auth token; defaults to INTERNAL_API_KEY (the
                            compose broker runs with --auth $INTERNAL_API_KEY)
  DETECT_CV_THREADS         cv2 intra-op thread cap (default 2; 0 = uncapped)
  DETECT_DETECTOR           onnx | hog | blob | stub (default onnx)
  DETECT_ONNX_BACKEND       cvdnn (default, zero-dep CPU) | ort (ONNX Runtime)
  DETECT_ONNX_PROVIDERS     ort execution providers, comma-separated, e.g.
                            "OpenVINOExecutionProvider" (Intel N100) or
                            "TensorrtExecutionProvider,CUDAExecutionProvider"
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

from .bus import EventSink, GateEventSink
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
    onnx_backend: str
    onnx_providers: str
    hwaccel: str
    device: str
    model_size: int
    model_id: str            # detector identity for benchmarking labels (see below)
    refresh_seconds: float
    # PR B — the gate (off by default; shadow measures; enforce acts). Note:
    # `always_analyze` is deliberately NOT a global env — it is per-camera by
    # design (a global "analyze everything" would silently disable the gate).
    gate_mode: str
    gate_heartbeat_s: float
    gate_critical_classes: str
    gate_cooldown_s: float
    metrics_port: int
    # #10 Tier-1 dispatch — off unless a KAI-C URL is set; only fires in enforce.
    dispatch_kaic_url: str
    dispatch_task: str
    # Bounded-load guards (#track-explosion): detector confidence floor.
    # yolov8n at 0.25 on a cluttered scene (wires, boards) hallucinates
    # kites/bananas endlessly; each becomes a standing track. 0.4 keeps real
    # people/vehicles (typically >0.6) while starving the phantom supply.
    detect_conf: float = 0.4
    visits_enabled: bool = True


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _derive_model_id(env: dict) -> str:
    """Identity of the active detector, for benchmarking labels.

    Explicit ``DETECT_MODEL_ID`` wins (set it when A/B-ing two builds of the same
    family). Otherwise, for the onnx detector, use the model file's basename
    (e.g. ``/app/model_weights/yolov8n.onnx`` → ``yolov8n``); else the detector
    type. This is what labels ``tier0_detector_*`` so models are comparable.
    """
    explicit = (env.get("DETECT_MODEL_ID") or "").strip()
    if explicit:
        return explicit
    detector = env.get("DETECT_DETECTOR", "onnx")
    if detector == "onnx":
        model_path = env.get("DETECT_ONNX_MODEL", "/app/model_weights/yolov8n.onnx")
        base = os.path.basename(model_path)
        stem = base.rsplit(".", 1)[0] if "." in base else base
        return stem or "onnx"
    return detector


def config_from_env(env: dict) -> ServiceConfig:
    return ServiceConfig(
        enabled=_truthy(env.get("DETECT_PIPELINE_ENABLED", "true")),
        core_url=env.get("OPENNVR_INTERNAL_URL", "http://opennvr-core:8000"),
        api_key=env.get("INTERNAL_API_KEY") or None,
        nats_url=env.get("NATS_URL") or None,
        detector=env.get("DETECT_DETECTOR", "onnx"),
        onnx_model=env.get("DETECT_ONNX_MODEL", "/app/model_weights/yolov8n.onnx"),
        onnx_input=int(env.get("DETECT_ONNX_INPUT", "640")),
        onnx_backend=env.get("DETECT_ONNX_BACKEND", "cvdnn"),
        onnx_providers=env.get("DETECT_ONNX_PROVIDERS", ""),
        hwaccel=env.get("DETECT_HWACCEL", "cpu"),
        device=env.get("DETECT_HWACCEL_DEVICE", "/dev/dri/renderD128"),
        model_size=int(env.get("DETECT_MODEL_SIZE", "320")),
        model_id=_derive_model_id(env),
        refresh_seconds=float(env.get("DETECT_REFRESH_SECONDS", "30")),
        gate_mode=env.get("DETECT_GATE_MODE", "shadow").strip().lower(),
        visits_enabled=_truthy(env.get("DETECT_VISITS_ENABLED", "true")),
        gate_heartbeat_s=float(env.get("DETECT_GATE_HEARTBEAT_S", "0")),
        gate_critical_classes=env.get("DETECT_GATE_CRITICAL_CLASSES", ""),
        gate_cooldown_s=float(env.get("DETECT_GATE_COOLDOWN_S", "30")),
        metrics_port=int(env.get("DETECT_METRICS_PORT", "9109")),
        dispatch_kaic_url=env.get("DETECT_DISPATCH_KAIC_URL", ""),
        dispatch_task=env.get("DETECT_DISPATCH_TASK", "caption"),
        detect_conf=_env_float(env, "DETECT_CONF", 0.4),
    )


def _env_float(env: dict, name: str, default: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


def _gate_factory(cfg: ServiceConfig):
    """Return a fresh-Gate factory (gate is stateful per camera), or None when off.

    Default is off → PR A behavior is byte-for-byte unchanged. `shadow` computes +
    audits decisions but never enforces; `enforce` actually gates the expensive tier.
    """
    mode = (cfg.gate_mode or "off").lower()
    if mode not in ("shadow", "enforce"):
        if mode != "off":
            log.warning("unknown DETECT_GATE_MODE=%r; gate disabled", cfg.gate_mode)
        return None
    from .gate import Gate, GateConfig

    crit = frozenset(c.strip() for c in cfg.gate_critical_classes.split(",") if c.strip())
    gcfg = GateConfig(
        shadow=(mode == "shadow"),           # always_analyze stays per-camera (GateConfig default)
        critical_classes=crit,
        heartbeat_s=cfg.gate_heartbeat_s,
        escalate_cooldown_s=cfg.gate_cooldown_s,
    )
    return lambda: Gate(gcfg)


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

        providers = [p.strip() for p in cfg.onnx_providers.split(",") if p.strip()] or None
        backend = (cfg.onnx_backend or "cvdnn").lower()
        if backend not in ("cvdnn", "ort"):
            log.warning("unknown DETECT_ONNX_BACKEND=%r; falling back to cvdnn", cfg.onnx_backend)
            backend = "cvdnn"
        if providers and backend != "ort":
            log.warning(
                "DETECT_ONNX_PROVIDERS is set but backend=%s ignores it "
                "(set DETECT_ONNX_BACKEND=ort to use execution providers)", backend,
            )

        def make_onnx():
            try:
                return OnnxYoloDetector(
                    model_path=cfg.onnx_model, input_size=cfg.onnx_input,
                    backend=backend, providers=providers,
                    conf_threshold=cfg.detect_conf,
                )
            except Exception:
                log.warning(
                    "failed to load ONNX model %s (backend=%s); using stub",
                    cfg.onnx_model, cfg.onnx_backend, exc_info=True,
                )
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


def build_manager(cfg: ServiceConfig, sink, *, gate_sink=None) -> WorkerManager:
    provider = HttpCameraProvider(cfg.core_url, api_key=cfg.api_key)
    # Region crops match the detector input so the model sees full-detail crops.
    model_size = cfg.onnx_input if cfg.detector == "onnx" else cfg.model_size
    # #10 Tier-1 dispatch: built only when a KAI-C URL is configured (off by
    # default). It still only fires on enforce escalations (shadow/off = nothing).
    dispatcher = router = None
    if cfg.dispatch_kaic_url and (cfg.gate_mode or "").lower() == "enforce":
        from .dispatch import DispatchRouter, KaicDispatcher
        dispatcher = KaicDispatcher(cfg.dispatch_kaic_url, api_key=cfg.api_key, task=cfg.dispatch_task)
        router = DispatchRouter()
        log.info("tier1 dispatch enabled -> %s (task=%s)", cfg.dispatch_kaic_url, cfg.dispatch_task)
    elif cfg.dispatch_kaic_url:
        # URL set but gate isn't enforcing -> dispatch would never fire; don't spin
        # up an idle pool. (shadow measures; only enforce dispatches.)
        log.info("tier1 dispatch URL set but gate mode=%s (not enforce); dispatch inactive",
                 cfg.gate_mode)
    # Best-frame store: retains each track's best crop for on-demand fetch (served
    # on the metrics port at /best_frame). Only built when there's a metrics port to
    # serve it on — no point doing per-frame store work nothing can ever read.
    best_frames = None
    if cfg.metrics_port:
        from .bestframe import BestFrameStore
        best_frames = BestFrameStore()
    # Events store (RFC-0001 C1): post finished visits + best-frame evidence
    # to core. Best-effort by design — core down loses history, not detection.
    visit_poster = None
    if cfg.visits_enabled and cfg.core_url:
        from .events_poster import VisitPoster
        visit_poster = VisitPoster(cfg.core_url, cfg.api_key)
        visit_poster.start()
    manager = WorkerManager(
        provider, sink,
        enabled=cfg.enabled,
        detector_factory=_detector_factory(cfg),
        model_size=model_size,
        model_id=cfg.model_id,
        best_frames=best_frames,
        device=cfg.device,
        gate_factory=_gate_factory(cfg),
        gate_sink=gate_sink,
        dispatcher=dispatcher,
        router=router,
        visit_poster=visit_poster,
    )
    manager.best_frames = best_frames            # expose so main() can serve it
    return manager


def _nats_connect_options(nats_url: str, token: str | None) -> dict:
    """kwargs for ``nats.connect`` against the compose broker.

    The bus runs with token auth (``--auth $INTERNAL_API_KEY`` in
    docker-compose) — connecting without the token is an Authorization
    Violation and the reconnect loop spams the log while every publish is
    silently dropped. Reuse the same INTERNAL_API_KEY the service already
    holds for opennvr-core. Bounded reconnects keep a genuinely
    misconfigured broker from error-looping forever.
    """
    opts: dict = {
        "servers": [nats_url],
        "name": "opennvr-tier0",
        "max_reconnect_attempts": 10,
    }
    if token:
        opts["token"] = token
    return opts


def _make_publisher(nats_url: str | None, token: str | None = None):  # pragma: no cover - needs a broker
    """Return (publish_fn, close_fn, connected_fn).

    Best-effort: NATS down never breaks a worker — but ``connected_fn`` feeds
    /health so "down" is VISIBLE instead of silently dropping events."""
    if not nats_url:
        return (lambda subject, data: None), (lambda: None), (lambda: False)
    import asyncio

    import nats

    loop = asyncio.new_event_loop()
    box: dict = {}

    def _serve():
        asyncio.set_event_loop(loop)
        try:
            box["nc"] = loop.run_until_complete(
                nats.connect(**_nats_connect_options(nats_url, token))
            )
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

    def connected() -> bool:
        nc = box.get("nc")
        return bool(nc is not None and not getattr(nc, "is_closed", False))

    return publish, close, connected


def main() -> int:  # pragma: no cover - integration entrypoint
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = config_from_env(os.environ)
    # Cap OpenCV's intra-op threads. cv2.dnn defaults to every core, which
    # starves CPU-bound co-tenants (llama.cpp/whisper.cpp in the lite stack)
    # far more than it helps a 320px detector. 0 disables the cap.
    threads = int(os.environ.get("DETECT_CV_THREADS", "2") or 0)
    if threads > 0:
        try:
            import cv2

            cv2.setNumThreads(threads)
        except Exception:  # cv2 optional (stub/hog detectors)
            pass
    log.info("tier0 service starting (enabled=%s, detector=%s, hwaccel=%s, cv_threads=%s)",
             cfg.enabled, cfg.detector, cfg.hwaccel, threads or "uncapped")

    publish, close, nats_connected = _make_publisher(
        cfg.nats_url, os.environ.get("NATS_TOKEN") or cfg.api_key
    )
    manager = build_manager(cfg, EventSink(publish), gate_sink=GateEventSink(publish))

    # Probe the detector factory ONCE so /health (and the log) can tell the
    # truth about degradation: requested onnx but got the stub means the model
    # is missing/broken — the container would otherwise sit "healthy" while
    # detecting nothing (the exact zombie QA found).
    detector_actual = type(_detector_factory(cfg)()).__name__
    from .health import HealthState, evaluate as health_evaluate
    from .metrics import newest_frame_age_s
    hstate = HealthState(
        enabled=cfg.enabled,
        detector_requested=cfg.detector,
        detector_actual=detector_actual,
        nats_configured=bool(cfg.nats_url),
        nats_connected=nats_connected,
        workers_running=lambda: len(manager.running_ids()),
        newest_frame_age_s=newest_frame_age_s,
    )

    # Effective config — one truthful block. Half of every support/QA thread
    # was "what state is it in"; this answers it at startup and at /health.
    log.info(
        "tier0 effective config:\n"
        "  enabled            = %s\n"
        "  detector           = %s (loaded: %s)\n"
        "  onnx backend       = %s  providers = %s\n"
        "  hwaccel            = %s\n"
        "  gate mode          = %s\n"
        "  tier1 dispatch     = %s\n"
        "  event bus          = %s\n"
        "  metrics/health     = %s\n"
        "  visit persistence  = %s\n"
        "  cv threads         = %s",
        cfg.enabled,
        cfg.detector, detector_actual,
        cfg.onnx_backend, cfg.onnx_providers or "(default)",
        cfg.hwaccel,
        cfg.gate_mode,
        cfg.dispatch_kaic_url or "off",
        cfg.nats_url or "unconfigured",
        f":{cfg.metrics_port}/metrics,/health,/best_frame" if cfg.metrics_port else "off",
        "on (events store)" if cfg.visits_enabled else "off",
        threads or "uncapped",
    )

    if cfg.metrics_port:
        from .metrics import serve_metrics
        serve_metrics(
            cfg.metrics_port,
            best_frames=getattr(manager, "best_frames", None),
            health_fn=lambda: health_evaluate(hstate),
        )
        log.info("tier0 metrics on :%d/metrics (+ /health, /best_frame)", cfg.metrics_port)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    # Guided promotion: the admin's gate-mode choice lives in core's DB
    # (set from the Compute-gated panel). Poll it on the same tick as camera
    # reconcile and apply changes live — no redeploy. Env stays the bootstrap
    # default; a DB override wins once set.
    effective_mode = cfg.gate_mode

    def _poll_gate_override() -> None:
        nonlocal effective_mode, cfg
        from dataclasses import replace

        from .providers import fetch_detect_config

        conf = fetch_detect_config(cfg.core_url, cfg.api_key)
        if not conf:
            return
        override = (conf.get("gate_mode") or "").strip().lower()
        if not override or override == effective_mode:
            return
        log.info("tier0 gate mode change (managed): %s -> %s", effective_mode, override)
        cfg = replace(cfg, gate_mode=override)
        dispatcher = router = None
        if override == "enforce" and cfg.dispatch_kaic_url:
            from .dispatch import DispatchRouter, KaicDispatcher

            dispatcher = KaicDispatcher(
                cfg.dispatch_kaic_url, api_key=cfg.api_key, task=cfg.dispatch_task
            )
            router = DispatchRouter()
            log.info("tier1 dispatch enabled -> %s (task=%s)",
                     cfg.dispatch_kaic_url, cfg.dispatch_task)
        manager.apply_gate_change(_gate_factory(cfg), dispatcher=dispatcher, router=router)
        effective_mode = override

    try:
        while not stop.is_set():
            try:
                _poll_gate_override()
            except Exception:
                log.exception("gate-override poll failed; keeping current mode")
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
