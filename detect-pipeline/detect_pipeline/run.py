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
  DETECT_DETECTOR           onnx (YOLO) | rfdetr (DETR) | hog | blob | stub
  DETECT_ONNX_BACKEND       auto (default: cvdnn for onnx, ort for rfdetr)
                            | cvdnn | ort
  DETECT_ONNX_PROVIDERS     ort execution providers, comma-separated, e.g.
                            "OpenVINOExecutionProvider" (Intel N100) or
                            "TensorrtExecutionProvider,CUDAExecutionProvider"
  DETECT_HWACCEL            cpu | vaapi | nvidia | qsv | rpi | rkmpp | jetson
  DETECT_DECODE_SKIP        nonref (default) | bidir | nokey | none (decode CPU dial)
  DETECT_DECODE_THREADS     ffmpeg decoder thread cap (default 2; 0 = auto)
  DETECT_BESTFRAME_PER_CAMERA  best-frame crops retained per camera (default 16)
  DETECT_START_SPREAD_S     window over which a batch of workers opens its
                            streams (default 10; 0 = all at once)
  DETECT_DETECTOR_POOL      max resident detector instances (default: auto
                            from CPU count). One per camera was the memory
                            wall; 0 restores that.
  DETECT_RTSP_TIMEOUT_S     RTSP socket-I/O timeout in seconds (default 10;
                            0 disables). Without it a half-open TCP session
                            blocks the decode FOREVER and the camera goes
                            silently dead with no restart.
  DETECT_DECODE_FAST        true = skip h264 loop filter (CPU decode only; opt-in)
  DETECT_DECODE_IDLE        adaptive decode while quiet (default nokey; none = off)
  DETECT_DECODE_IDLE_AFTER  quiet seconds before idling (default 60)
  DETECT_HWACCEL_DEVICE     e.g. /dev/dri/renderD128
  DETECT_MODEL_SIZE         detector input square (default 320)
  DETECT_REFRESH_SECONDS    camera-list reconcile interval (default 30)
"""
from __future__ import annotations

import logging
import os
import random
import signal
import threading
from dataclasses import dataclass

from .bus import EventSink, GateEventSink
from .metrics import record_sink_error
from .ffmpeg_presets import DEFAULT_RTSP_TIMEOUT_S, resolve_hwaccel
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
    # ffmpeg -skip_frame: none | bidir | nonref | nokey. Moves the frame drop
    # from the fps filter (post-decode) into the DECODER — see ffmpeg_presets.
    decode_skip: str
    # ffmpeg decoder thread cap (0 = ffmpeg auto, up to 16/camera) and the
    # opt-in loop-filter skip — see ffmpeg_presets.build_decode_command.
    decode_threads: int
    fast_decode: bool
    # Adaptive decode (Blue Iris-style "limit decoding unless required"):
    # DETECT_DECODE_IDLE names the skip mode used while a camera is quiet
    # ("" = off). Promotion back to full decode happens on the first motion
    # box / track; demotion after DETECT_DECODE_IDLE_AFTER quiet seconds.
    decode_idle: str
    decode_idle_after: float
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
    rtsp_timeout_s: float = DEFAULT_RTSP_TIMEOUT_S
    bestframe_per_camera: int = 16
    detector_pool: int = 0
    start_spread_s: float = 10.0
    visits_enabled: bool = True


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _decode_skip_from_env(env: dict) -> str:
    """DETECT_DECODE_SKIP, degraded safely: a typo must not kill every worker
    at spawn time (build_decode_command raises on unknown modes), so an
    invalid value logs loudly and falls back to full decode."""
    from .ffmpeg_presets import DECODE_SKIP_MODES

    value = str(env.get("DETECT_DECODE_SKIP", "nonref")).strip().lower()
    if value == "noref":                 # ffmpeg's own spelling of nonref
        value = "nonref"
    if value not in DECODE_SKIP_MODES:
        log.warning(
            "DETECT_DECODE_SKIP=%r is not one of %s; using 'none' (full decode)",
            env.get("DETECT_DECODE_SKIP"), list(DECODE_SKIP_MODES),
        )
        return "none"
    return value


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


def _detector_pool_from_env(env: dict) -> int:
    """Max resident detectors. Auto = one per core-pair, clamped to [2, 8].

    Each detector holds its own model weights and activation arenas, so one
    per CAMERA made resident memory a function of the fleet instead of the
    hardware — the first hard wall this service hits. The pool grows lazily,
    so an install with fewer cameras than this is completely unaffected.

    Sized against cores rather than cameras because inference is CPU-bound
    and releases the GIL: more concurrent detectors than the box can run
    buys memory, not throughput. DETECT_CV_THREADS is each one's internal
    width, so cores/threads is the number that actually fit.
    """
    raw = (env.get("DETECT_DETECTOR_POOL") or "").strip()
    if raw:
        try:
            return max(0, int(raw))          # 0 = opt out, one per worker
        except ValueError:
            log.warning("DETECT_DETECTOR_POOL=%r is not an integer; using auto", raw)
    cores = os.cpu_count() or 2
    per_detector = max(1, _env_int(env, "DETECT_CV_THREADS", 2))
    return max(2, min(8, cores // per_detector))


def config_from_env(env: dict) -> ServiceConfig:
    return ServiceConfig(
        enabled=_truthy(env.get("DETECT_PIPELINE_ENABLED", "true")),
        core_url=env.get("OPENNVR_INTERNAL_URL", "http://opennvr-core:8000"),
        api_key=env.get("INTERNAL_API_KEY") or None,
        nats_url=env.get("NATS_URL") or None,
        detector=env.get("DETECT_DETECTOR", "onnx"),
        onnx_model=env.get("DETECT_ONNX_MODEL", "/app/model_weights/yolov8n.onnx"),
        onnx_input=int(env.get("DETECT_ONNX_INPUT", "640")),
        onnx_backend=env.get("DETECT_ONNX_BACKEND", "auto"),
        onnx_providers=env.get("DETECT_ONNX_PROVIDERS", ""),
        hwaccel=env.get("DETECT_HWACCEL", "cpu"),
        device=env.get("DETECT_HWACCEL_DEVICE", "/dev/dri/renderD128"),
        decode_skip=_decode_skip_from_env(env),
        decode_threads=_env_int(env, "DETECT_DECODE_THREADS", 2),
        rtsp_timeout_s=_env_float(env, "DETECT_RTSP_TIMEOUT_S", DEFAULT_RTSP_TIMEOUT_S),
        bestframe_per_camera=_env_int(env, "DETECT_BESTFRAME_PER_CAMERA", 16),
        detector_pool=_detector_pool_from_env(env),
        start_spread_s=_env_float(env, "DETECT_START_SPREAD_S", 10.0),
        fast_decode=_truthy(env.get("DETECT_DECODE_FAST", "false")),
        decode_idle=_decode_idle_from_env(env),
        decode_idle_after=_env_float(env, "DETECT_DECODE_IDLE_AFTER", 60.0),
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


def _decode_idle_from_env(env: dict) -> str:
    """DETECT_DECODE_IDLE: a skip mode used while the camera is quiet, or
    empty/none/off = adaptive decode disabled. Invalid values warn and
    disable rather than killing workers at spawn time."""
    from .ffmpeg_presets import DECODE_SKIP_MODES

    value = str(env.get("DETECT_DECODE_IDLE", "nokey")).strip().lower()
    if value == "noref":                 # ffmpeg's own spelling of nonref
        value = "nonref"
    if value in ("", "none", "off", "false"):
        return ""
    if value not in DECODE_SKIP_MODES:
        log.warning(
            "DETECT_DECODE_IDLE=%r is not one of %s; adaptive decode disabled",
            env.get("DETECT_DECODE_IDLE"), list(DECODE_SKIP_MODES),
        )
        return ""
    return value


def _env_int(env: dict, name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning("%s=%r is not an integer; using %s", name, raw, default)
        return default


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


def _resolve_onnx_backend(configured: str, family_default: str) -> str:
    """DETECT_ONNX_BACKEND: '' / 'auto' resolve per detector family — cvdnn
    for the YOLO head (runs everywhere, zero deps), ort for the DETR head
    (transformer exports exceed cv2.dnn's operator coverage). An explicit
    cvdnn/ort is honored; anything else warns and uses the family default."""
    value = (configured or "auto").strip().lower()
    if value in ("", "auto"):
        return family_default
    if value in ("cvdnn", "ort"):
        return value
    log.warning("unknown DETECT_ONNX_BACKEND=%r; using %s", configured, family_default)
    return family_default


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
        backend = _resolve_onnx_backend(cfg.onnx_backend, "cvdnn")
        if providers and backend != "ort":
            log.warning(
                "DETECT_ONNX_PROVIDERS is set but backend=%s ignores it "
                "(set DETECT_ONNX_BACKEND=ort to use execution providers)", backend,
            )

        def make_onnx():
            try:
                det = OnnxYoloDetector(
                    model_path=cfg.onnx_model, input_size=cfg.onnx_input,
                    backend=backend, providers=providers,
                    conf_threshold=cfg.detect_conf,
                )
                log.info(
                    "tier0 detector loaded: family=yolo model=%s backend=%s input=%d",
                    os.path.basename(cfg.onnx_model), det.backend_name, cfg.onnx_input,
                )
                return det
            except Exception:
                log.warning(
                    "failed to load ONNX model %s (backend=%s)",
                    cfg.onnx_model, cfg.onnx_backend, exc_info=True,
                )
                log.error(
                    "DETECTION IS OFF for this worker: running the STUB detector "
                    "(motion/tracking only, no objects). Fix the model/backend."
                )
                from .detector import StubDetector
                return StubDetector()

        return make_onnx
    if name == "rfdetr":
        # RF-DETR family (NMS-free DETR head). Reuses DETECT_ONNX_MODEL /
        # DETECT_ONNX_INPUT / DETECT_ONNX_BACKEND — point the model path at an
        # rf-detr ONNX export and set the input to the variant's resolution
        # (nano 384). Transformer exports usually exceed cv2.dnn's operator
        # coverage, so the backend default for this family is ort.
        if not cfg.onnx_model or not os.path.exists(cfg.onnx_model):
            log.warning("RF-DETR model not found at %s; using stub detector", cfg.onnx_model)
            return _stub_factory()
        from .detr_detector import OnnxDetrDetector

        providers = [p.strip() for p in cfg.onnx_providers.split(",") if p.strip()] or None
        backend = _resolve_onnx_backend(cfg.onnx_backend, "ort")
        if backend == "cvdnn":
            log.warning(
                "DETECT_DETECTOR=rfdetr with backend=cvdnn: cv2.dnn often lacks "
                "the transformer ops this export uses — will fall back to ort "
                "automatically if loading fails"
            )

        def make_rfdetr():
            for attempt in ([backend, "ort"] if backend == "cvdnn" else [backend]):
                try:
                    det = OnnxDetrDetector(
                        model_path=cfg.onnx_model, input_size=cfg.onnx_input,
                        backend=attempt, providers=providers,
                        conf_threshold=cfg.detect_conf,
                    )
                    log.info(
                        "tier0 detector loaded: family=detr model=%s backend=%s input=%d",
                        os.path.basename(cfg.onnx_model), det.backend_name, cfg.onnx_input,
                    )
                    return det
                except Exception:
                    log.warning(
                        "failed to load RF-DETR model %s (backend=%s)",
                        cfg.onnx_model, attempt, exc_info=True,
                    )
            log.error(
                "DETECTION IS OFF for this worker: RF-DETR could not load on any "
                "backend — running the STUB detector (motion/tracking only, no "
                "objects). Fix the model path/backend; do not mistake this for a "
                "working detector."
            )
            from .detector import StubDetector
            return StubDetector()

        return make_rfdetr
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
    # Resolve the decode backend ONCE here and tell core what we can actually
    # do. Core hands out the full-resolution main stream only when the reader
    # really can hardware-decode, so this must be the post-resolution value:
    # claiming vaapi without the render node is what used to buy us the main
    # stream AND software decode.
    effective_hwaccel, hw_downgrade = resolve_hwaccel(cfg.hwaccel, device=cfg.device)
    if hw_downgrade:
        log.warning(
            "hwaccel %r unusable — reporting 'cpu' to core so it keeps us on "
            "the substream: %s", cfg.hwaccel, hw_downgrade,
        )
    provider = HttpCameraProvider(
        cfg.core_url, api_key=cfg.api_key, hwaccel=effective_hwaccel.value,
    )
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
        # Per-camera quota keeps one busy camera from evicting the rest;
        # the global cap stays as the memory backstop.
        best_frames = BestFrameStore(
            max_per_camera=cfg.bestframe_per_camera,
        )
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
        hwaccel=cfg.hwaccel,
        decode_skip=cfg.decode_skip,
        detector_pool=cfg.detector_pool,
        start_spread_s=cfg.start_spread_s,
        decode_threads=cfg.decode_threads,
        rtsp_timeout_s=cfg.rtsp_timeout_s,
        fast_decode=cfg.fast_decode,
        decode_idle=cfg.decode_idle,
        decode_idle_after=cfg.decode_idle_after,
        gate_factory=_gate_factory(cfg),
        gate_sink=gate_sink,
        dispatcher=dispatcher,
        router=router,
        visit_poster=visit_poster,
    )
    manager.best_frames = best_frames            # expose so main() can serve it
    return manager


# Delay between reconnect attempts. This — not a retry cap — is what keeps a
# misconfigured broker from error-looping hot, while still recovering from an
# ordinary restart whenever it finishes.
_NATS_RECONNECT_WAIT_S = 2.0


def _nats_connect_options(nats_url: str, token: str | None) -> dict:
    """kwargs for ``nats.connect`` against the compose broker.

    The bus runs with token auth (``--auth $INTERNAL_API_KEY`` in
    docker-compose) — connecting without the token is an Authorization
    Violation and the reconnect loop spams the log while every publish is
    silently dropped. Reuse the same INTERNAL_API_KEY the service already
    holds for opennvr-core.

    Reconnects are UNBOUNDED. They used to stop after 10 attempts to keep a
    misconfigured broker from error-looping, but nothing rebuilt the client
    afterwards: an ordinary broker restart that outlasted ten tries left
    every camera's live events dead until the container was restarted. That
    is the wrong trade for a service meant to run for months. The retry WAIT
    is what stops the hot loop; giving up is not.
    """
    opts: dict = {
        "servers": [nats_url],
        "name": "opennvr-tier0",
        "max_reconnect_attempts": -1,     # never stop trying
        "reconnect_time_wait": _NATS_RECONNECT_WAIT_S,
    }
    if token:
        opts["token"] = token
    return opts


def _make_publisher(nats_url: str | None, token: str | None = None):  # pragma: no cover - needs a broker
    """Return (publish_fn, close_fn, connected_fn).

    Best-effort: NATS down never breaks a worker — but ``connected_fn`` feeds
    /health so "down" is VISIBLE instead of silently dropping events."""
    if not nats_url:
        # No bus configured: nothing is published, and saying so keeps
        # tier0_events_published_total honest rather than counting no-ops.
        return (lambda subject, data: False), (lambda: None), (lambda: False)
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

    def _note_async_failure(fut) -> None:
        """Count a publish that failed AFTER we handed it to the loop.

        The future is otherwise discarded, so a broker rejecting writes
        looked identical to success from the worker's side.
        """
        try:
            if fut.exception() is not None:
                record_sink_error("bus")
        except Exception:  # pragma: no cover - cancelled/loop torn down
            pass

    def publish(subject: str, data: bytes) -> bool:
        """True iff the payload reached a connected client.

        Returning None here (the old signature) is what let
        ``tier0_events_published_total`` count events that never left the
        process: the sink took "no exception" as "published", and a NATS
        client that had given up reconnecting raised nothing at all.

        Honest limit: True means accepted by the client, not acknowledged
        by the broker — awaiting that would block the frame loop. Failures
        after hand-off are counted asynchronously above instead.
        """
        nc = box.get("nc")
        if nc is None:
            # Counted, not merely absent: "published_total stopped climbing"
            # alone is indistinguishable from every camera going quiet.
            record_sink_error("bus")
            return False                     # never connected, or gave up
        try:
            fut = asyncio.run_coroutine_threadsafe(nc.publish(subject, data), loop)
        except Exception:
            log.debug("bus publish could not be scheduled", exc_info=True)
            return False
        fut.add_done_callback(_note_async_failure)
        return True

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
    # starves CPU-bound co-tenants (the camera-agent's adapters) far more
    # than it helps a 320px detector. 0 disables the cap.
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
    from .metrics import newest_frame_age_s, stale_cameras
    hstate = HealthState(
        enabled=cfg.enabled,
        detector_requested=cfg.detector,
        detector_actual=detector_actual,
        nats_configured=bool(cfg.nats_url),
        nats_connected=nats_connected,
        workers_running=lambda: len(manager.running_ids()),
        newest_frame_age_s=newest_frame_age_s,
        stale_cameras=stale_cameras,
        visits_running=(visit_poster.is_alive if visit_poster is not None else None),
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
            # Jitter the tick so this process does not poll core in lockstep
            # with anything else that restarted alongside it, and so a fleet
            # that gave up together does not all re-queue on the same instant.
            stop.wait(cfg.refresh_seconds * random.uniform(0.85, 1.15))
    finally:
        manager.stop()
        close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
