# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
ONNX object detector for Tier-0, with a **pluggable inference backend**.

The same YOLOv8/YOLO11-family ONNX model is run through one of two backends:

* ``cvdnn`` (default) — ``cv2.dnn`` from OpenCV, already a dependency, so it needs
  **nothing extra to install** and runs on any platform. The OpenCV 5 DNN engine
  is CPU-only but materially faster than 4.x. This is the zero-dependency floor.
* ``ort`` — ONNX Runtime with selectable **execution providers**. On the *same*
  ONNX file this is the on-ramp to accelerators: ``OpenVINOExecutionProvider``
  (Intel CPU / iGPU / NPU — the N100 win), ``TensorrtExecutionProvider`` /
  ``CUDAExecutionProvider`` (Nvidia / Jetson), ``CoreMLExecutionProvider`` (Mac).
  It's an optional dependency (``pip install 'detect-pipeline[onnxruntime]'``, or
  the matching accelerator wheel such as ``onnxruntime-openvino``).

Backends implement one method — ``infer(blob) -> output_tensor`` — so preprocessing
(``cv2.dnn.blobFromImage``) and the decode (``postprocess_yolo``) are shared and
backend-agnostic. Coral (EdgeTPU) and Hailo need their own model format + SDK, so
they are separate backends on this same seam, tracked as follow-ups / the KAI-C
accelerator adapter — not ORT execution providers.

The decode is a pure function tested against synthetic output tensors, and the
net/session is injectable, so nothing here requires a model file, a download, or
onnxruntime to be installed at test time — the real model only loads in deployment.

YOLOv8/YOLO11/YOLO26 detect output (exported with ``end2end`` off) is ``(1, 4+nc, N)`` (e.g. ``(1, 84, 8400)`` for COCO):
4 box params (cx, cy, w, h in input pixels) + ``nc`` class scores per anchor, no
objectness, no softmax. Transpose to ``(N, 4+nc)``, argmax the class scores,
threshold, then ``cv2.dnn.NMSBoxes``. (NMS-free families — YOLO26, RF-DETR — need
a different decode; that's a tracked follow-up, independent of the backend here.)
"""
from __future__ import annotations

import logging

import cv2
import numpy as np

from .detector import RawDetection

log = logging.getLogger("detect_pipeline.onnx_detector")

_CPU_EP = "CPUExecutionProvider"

# COCO-80 class names (the classes the default yolo26n.onnx predicts).
COCO_LABELS = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def postprocess_yolo(
    output: np.ndarray,
    *,
    input_size: int,
    conf_threshold: float = 0.25,
    iou_threshold: float = 0.45,
    labels: list[str] | None = None,
) -> list[RawDetection]:
    """Decode a YOLOv8/YOLO11 ONNX output tensor into normalized detections.

    ``output`` may be ``(1, 4+nc, N)``, ``(4+nc, N)``, or ``(N, 4+nc)``. Boxes are
    returned normalized to ``[0, 1]`` of the model input (which equals the crop).
    """
    labels = labels or COCO_LABELS
    arr = np.squeeze(np.asarray(output))
    if arr.ndim != 2:
        return []
    # Orient to (N, 4+nc): detections are the long axis.
    if arr.shape[0] < arr.shape[1]:
        arr = arr.T
    if arr.shape[1] < 5:
        return []

    boxes_xywh = arr[:, :4].astype(np.float32)
    class_scores = arr[:, 4:]
    class_ids = np.argmax(class_scores, axis=1)
    confidences = class_scores[np.arange(class_scores.shape[0]), class_ids]

    keep = confidences >= conf_threshold
    if not np.any(keep):
        return []
    boxes_xywh, class_ids, confidences = boxes_xywh[keep], class_ids[keep], confidences[keep]

    cx, cy, w, h = boxes_xywh[:, 0], boxes_xywh[:, 1], boxes_xywh[:, 2], boxes_xywh[:, 3]
    x1, y1 = cx - w / 2.0, cy - h / 2.0
    rects = np.stack([x1, y1, w, h], axis=1).tolist()  # NMSBoxes wants [x, y, w, h]

    idxs = cv2.dnn.NMSBoxes(rects, confidences.tolist(), conf_threshold, iou_threshold)
    if idxs is None or len(idxs) == 0:
        return []

    out: list[RawDetection] = []
    for i in np.asarray(idxs).reshape(-1):
        cid = int(class_ids[i])
        label = labels[cid] if 0 <= cid < len(labels) else str(cid)
        out.append(
            RawDetection(
                label,
                float(confidences[i]),
                (
                    _clamp01(x1[i] / input_size),
                    _clamp01(y1[i] / input_size),
                    _clamp01((x1[i] + w[i]) / input_size),
                    _clamp01((y1[i] + h[i]) / input_size),
                ),
            )
        )
    return out


# ─────────────────────────── inference backends ───────────────────────────
# A backend takes a preprocessed NCHW float32 blob and returns the raw model
# output tensor. Preprocessing + decode live in the detector, so a backend is
# tiny and the two are interchangeable.


class CvDnnBackend:
    """Run the ONNX model through ``cv2.dnn`` (default; zero extra dependency, CPU)."""

    name = "cvdnn"

    def __init__(self, model_path: str | None = None, *, net=None) -> None:
        # ``net`` is injectable for tests (an object with .setInput()/.forward()).
        if net is not None:
            self._net = net
        else:
            if not model_path:
                raise ValueError("CvDnnBackend needs a model_path or an injected net")
            self._net = cv2.dnn.readNetFromONNX(model_path)

    def infer(self, blob: np.ndarray) -> np.ndarray:
        self._net.setInput(blob)
        return self._net.forward()

    def infer_all(self, blob: np.ndarray) -> list[np.ndarray]:
        """All output tensors (multi-head models, e.g. DETR's dets+labels)."""
        self._net.setInput(blob)
        names = self._net.getUnconnectedOutLayersNames()
        outs = self._net.forward(names) if names else [self._net.forward()]
        return list(outs)


# Device option each execution provider takes, keyed by provider name. The
# ``Name:device`` shorthand in DETECT_ONNX_PROVIDERS maps onto it:
#   OpenVINOExecutionProvider:NPU             -> {"device_type": "NPU"}
#   OpenVINOExecutionProvider:AUTO:NPU,GPU    -> {"device_type": "AUTO:NPU,GPU"}
#   CUDAExecutionProvider:1                   -> {"device_id": "1"}
# Providers not listed here take no device and the shorthand is refused.
_DEVICE_OPTION = {
    "OpenVINOExecutionProvider": "device_type",
    "CUDAExecutionProvider": "device_id",
    "TensorrtExecutionProvider": "device_id",
    "ROCMExecutionProvider": "device_id",
    "MIGraphXExecutionProvider": "device_id",
    # Qualcomm AI Runtime: ``QNNExecutionProvider:htp`` is the Hexagon NPU
    # (``cpu``/``gpu`` are the other QNN backends; the shorthand is the
    # backend, there is one HTP per SoC).
    "QNNExecutionProvider": "backend_type",
}

# Plugin execution providers (ONNX Runtime >= 1.23): a separately installed
# wheel that must be REGISTERED with the runtime before a session can use it,
# and that ``get_available_providers()`` never lists — so the usual
# "requested ∩ available" filter would silently drop it. Provider name -> the
# package that ships the library (``get_library_path()``). Registration is
# process-wide and idempotent; see ``register_plugin_providers``.
_PLUGIN_EPS = {
    "QNNExecutionProvider": "onnxruntime_qnn",   # the -qualcomm flavour
}
_registered_plugin_eps: set[str] = set()
#: The Hexagon compute DSP, as the host lends it to a container
#: (docker-compose.accel-qualcomm.yml). The QNN plugin lists the HTP as a
#: device on any Qualcomm host and compiles the graph without opening it,
#: so this node is the only honest sign the NPU is reachable from here.
QNN_HTP_DEVICE = "/dev/fastrpc-cdsp"

# Options a plugin provider gets on top of the ``:device`` shorthand. QNN's
# HTP defaults are tuned for an always-on detector: sustained clocks rather
# than burst (burst throttles after a few seconds of steady load), fp16 for
# an fp32 model (the HTP has no fp32 path; without this every node falls
# back to CPU), and the slowest graph finalisation (done once per session,
# buys the fastest graph). An operator overrides any of them with
# DETECT_ONNX_PROVIDER_OPTIONS="key=value,key=value".
_PROVIDER_DEFAULTS = {
    "QNNExecutionProvider": {
        "backend_type": "htp",
        "htp_performance_mode": "sustained_high_performance",
        "enable_htp_fp16_precision": "1",
        "htp_graph_finalization_optimization_mode": "3",
    },
}


def plugin_device_missing(name: str, entry) -> str | None:
    """Why ``entry`` cannot run on its accelerator from THIS container, or None.

    Measured on a Ventuno Q, 2026-11-01: a detect-pipeline recreated without
    the Qualcomm overlay — no ``/dev/fastrpc-cdsp`` — still got an HTP
    device from ``get_ep_devices()``, built the session and ran every node
    on CPU, 18 cameras over capacity, while the backend metric could name
    QNN as effective. The runtime never says. The device node does.
    """
    import os as _os
    if name != "QNNExecutionProvider":
        return None
    opts = entry[1] if isinstance(entry, tuple) and isinstance(entry[1], dict) else {}
    backend = str(opts.get("backend_type", "htp")).strip().lower()
    if backend != "htp":
        return None   # QNN's own CPU (or GPU) backend needs no DSP
    dev = QNN_HTP_DEVICE
    if not _os.path.exists(dev):
        return (f"{name}(htp) requested but {dev} is not in this container — the "
                "Qualcomm overlay (docker-compose.accel-qualcomm.yml, applied by "
                "./start.sh up) lends it; running on CPU")
    if not _os.access(dev, _os.R_OK | _os.W_OK):
        return (f"{name}(htp) requested but {dev} is not accessible from this "
                "container — check FASTRPC_GID / DMAHEAP_GID in .env match the host's "
                "fastrpc and dmaheap groups; running on CPU")
    return None


def register_plugin_providers(ort, requested) -> set[str]:
    """Register the plugin EP libraries named in ``requested`` and return the
    provider names that are now usable (the already-registered ones included).

    A plugin whose package is not installed is reported once and skipped, so
    the request degrades to the next provider (CPU in the end) exactly like a
    built-in provider the wheel does not carry. ``requested`` takes whatever
    :func:`parse_provider_specs` takes.
    """
    usable: set[str] = set()
    for name, _device in parse_provider_specs(requested):
        if name not in _PLUGIN_EPS:
            continue
        if name in _registered_plugin_eps:
            usable.add(name)
            continue
        pkg = _PLUGIN_EPS[name]
        try:
            import importlib

            mod = importlib.import_module(pkg)
            ort.register_execution_provider_library(name, mod.get_library_path())
        except ImportError:
            log.warning(
                "ORT provider %s needs the %s package (the matching image flavour); "
                "not installed — skipping", name, pkg,
            )
            continue
        except Exception as exc:  # the runtime refused the library
            log.warning("ORT could not register plugin provider %s: %s", name, exc)
            continue
        _registered_plugin_eps.add(name)
        usable.add(name)
        log.info("ORT plugin provider %s registered from %s", name, pkg)
    return usable


def available_providers(ort, requested) -> list[str]:
    """``ort.get_available_providers()`` plus the plugin providers ``requested``
    names, registered on the way — what :func:`resolve_providers` filters by."""
    avail = list(ort.get_available_providers())
    for name in register_plugin_providers(ort, requested):
        if name not in avail:
            avail.append(name)
    return avail


def provider_options(entry, env=None) -> dict[str, str]:
    """The options dict a provider entry carries, with the provider's defaults
    underneath and DETECT_ONNX_PROVIDER_OPTIONS (``k=v,k=v``) on top."""
    import os as _os

    name = provider_name(entry)
    opts = dict(_PROVIDER_DEFAULTS.get(name, {}))
    if isinstance(entry, tuple) and isinstance(entry[1], dict):
        opts.update({k: str(v) for k, v in entry[1].items()})
    raw = (env if env is not None else _os.environ).get("DETECT_ONNX_PROVIDER_OPTIONS", "")
    for item in raw.split(","):
        k, sep, v = item.strip().partition("=")
        if sep and k:
            opts[k] = v
    if name == "QNNExecutionProvider" and "backend_path" not in opts:
        # ``backend_type`` is the documented knob, but on the plugin it
        # failed device creation (QNN_DEVICE_ERROR_INVALID_CONFIG) where the
        # library path worked — measured on a QCS8275. The package knows
        # where its backends are; the shorthand names one of them.
        path = _qnn_backend_path(opts.get("backend_type", "htp"))
        if path:
            opts["backend_path"] = path
            opts.pop("backend_type", None)
    return opts


def _qnn_backend_path(backend: str) -> str | None:
    """The onnxruntime_qnn library for a backend name (``htp``/``cpu``/``gpu``),
    or None when the package (or that backend) is not there."""
    try:
        import onnxruntime_qnn as q
    except ImportError:
        return None
    getter = getattr(q, f"get_qnn_{backend.strip().lower()}_path", None)
    return getter() if getter else None


def plugin_context_path(model_path: str, provider: str) -> str:
    """Where a plugin provider's compiled graph for ``model_path`` is cached.

    QNN (and the other plugin providers) compile the graph for the device at
    session build — 6 s for yolo26n on a Hexagon v73 — and can dump that as an
    "EP context" model, which later sessions load in a fraction of the time.
    In ``.opennvr-accel/`` inside the model's directory when that is writable
    (the weights volume), else under the temp dir; the file name carries the
    provider so flavours never share one.
    """
    import os as _os
    import tempfile

    base = f"{_os.path.basename(model_path)}.{provider}.ctx.onnx"
    d = _os.path.dirname(_os.path.abspath(model_path))
    # A hidden directory INSIDE the model's, never beside the model: the
    # weights volume is shared with the yolov8 adapter, and libraries that
    # glob *.onnx load every file they find as a model (seen with
    # InsightFace's pack in ai-adapter).
    d = _os.path.join(d, ".opennvr-accel") if _os.access(d, _os.W_OK) \
        else _os.path.join(tempfile.gettempdir(), "opennvr-accel")
    _os.makedirs(d, exist_ok=True)
    return _os.path.join(d, base)


def parse_provider_specs(requested) -> list[tuple[str, str | None]]:
    """Split a provider setting into ``(name, device)`` pairs.

    ``requested`` is a comma-separated string or a list. A token that is not
    a provider name (does not end in ``ExecutionProvider``) continues the
    previous token's device, so the OpenVINO multi-device forms survive the
    comma split: ``OpenVINOExecutionProvider:AUTO:NPU,GPU,CPUExecutionProvider``
    is ``[("OpenVINOExecutionProvider", "AUTO:NPU,GPU"), ("CPUExecutionProvider", None)]``.
    """
    if requested is None:
        return []
    if isinstance(requested, str):
        tokens = [t.strip() for t in requested.split(",") if t.strip()]
    else:
        tokens = []
        for item in requested:
            if isinstance(item, tuple):
                name, opts = item
                dev = (opts or {}).get(_DEVICE_OPTION.get(name, ""), None) if isinstance(opts, dict) else None
                tokens.append(f"{name}:{dev}" if dev else name)
            else:
                tokens.append(str(item).strip())
    specs: list[tuple[str, str | None]] = []
    for tok in tokens:
        if not tok:
            continue
        name, _, device = tok.partition(":")
        if name.endswith("ExecutionProvider") or not specs:
            specs.append((name, device or None))
        else:
            prev_name, prev_dev = specs[-1]
            specs[-1] = (prev_name, f"{prev_dev},{tok}" if prev_dev else tok)
    return specs


def provider_entry(name: str, device: str | None):
    """What ``onnxruntime.InferenceSession(providers=...)`` takes for one spec:
    the bare name, or ``(name, {option: device})`` when a device was asked for."""
    if not device:
        return name
    option = _DEVICE_OPTION.get(name)
    if option is None:
        log.warning("ORT provider %s takes no device; ignoring %r", name, device)
        return name
    return (name, {option: device})


def provider_name(entry) -> str:
    return entry[0] if isinstance(entry, tuple) else entry


def resolve_providers(requested, available) -> list:
    """Pick ORT execution providers: ``requested ∩ available``, CPU always last.

    Pure + testable. ``requested`` may be a list or a comma string; ``None`` means
    "let ORT use whatever it has, by its own priority order". CPU is always kept as
    a final fallback so a missing accelerator EP degrades instead of failing.

    Entries carry their device when one was requested (``Name:device``, see
    :func:`parse_provider_specs`), as the ``(name, options)`` tuples ORT takes.
    """
    available = list(available) if available else []
    specs = parse_provider_specs(requested)
    if not specs:
        provs: list = list(available) if available else [_CPU_EP]
    else:
        provs = []
        for name, device in specs:
            if available and name not in available:
                log.warning("ORT provider %s not available; skipping", name)
                continue
            provs.append(provider_entry(name, device))
    if _CPU_EP not in [provider_name(p) for p in provs]:
        provs.append(_CPU_EP)
    return provs


def describe_providers(entries) -> str:
    """``OpenVINOExecutionProvider(NPU), CPUExecutionProvider`` — for logs and the UI."""
    parts = []
    for e in entries:
        if isinstance(e, tuple):
            name, opts = e
            dev = next(iter((opts or {}).values()), None)
            parts.append(f"{name}({dev})" if dev else name)
        else:
            parts.append(str(e))
    return ", ".join(parts)


def _ort_session_options(ort):
    """Bound an ORT session's own thread pools to DETECT_CV_THREADS.

    ``cv2.setNumThreads`` caps OpenCV ONLY. An InferenceSession built with no
    options defaults ``intra_op_num_threads`` to the machine's core count and
    allocates its own pool, so the ``ort`` and ``rfdetr`` paths escaped the
    service's only CPU governor entirely — several sessions on one box then
    each believed they owned every core. The detector pool caps how many
    sessions exist; this caps how wide each one is.

    An EXPLICIT 0 means "let ORT decide". Unset/empty pins 2 instead —
    deliberately conservative, because this path has no equivalent of the
    OpenCV governor: WorkerManager._tune_inference_threads only ever calls
    cv2.setNumThreads, so an unset value here would leave every session
    claiming the whole box. That asymmetry is a real gap (the ort/rfdetr
    paths never widen on an idle host the way the cvdnn path does), but it
    is a safe default rather than an accidental one.
    """
    import os as _os

    opts = ort.SessionOptions()
    try:
        threads = int((_os.environ.get("DETECT_CV_THREADS") or "2").strip() or 0)
    except ValueError:
        threads = 2
    if threads > 0:
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1     # one session, one request at a time
    return opts


def _build_plugin_session(ort, opts, model_path: str, provider: str):
    """Build a session whose graph a plugin provider compiles, through the
    provider's compiled-graph cache. Returns ``(session, compiled)``.

    First build: ask the runtime to dump the EP-context model next to the
    model (``ep.context_enable``); the dump existing afterwards is the only
    reliable sign the provider took the graph (see ``_effective_providers``).
    Later builds load that file as the model — seconds instead of the full
    compile, for every session in the detector pool. A context file the
    runtime rejects (another QNN version, another SoC) is deleted and the
    model is compiled again. Concurrent first builds (the pool fills in
    parallel) each dump to their own temp name and the last one wins.
    """
    import os as _os
    import threading

    ctx = plugin_context_path(model_path, provider)
    if _os.path.exists(ctx):
        try:
            sess = ort.InferenceSession(ctx, sess_options=opts)
            log.info("%s: loaded the compiled graph %s", provider, ctx)
            return sess, True
        except Exception as exc:
            log.warning("%s: compiled graph %s rejected (%s); rebuilding from the model", provider, ctx, exc)
            try:
                _os.remove(ctx)
            except OSError:
                pass
    tmp = f"{ctx}.{_os.getpid()}.{threading.get_ident()}.tmp"
    opts.add_session_config_entry("ep.context_enable", "1")
    opts.add_session_config_entry("ep.context_file_path", tmp)
    opts.add_session_config_entry("ep.context_embed_mode", "1")
    sess = ort.InferenceSession(model_path, sess_options=opts)
    if _os.path.exists(tmp):
        try:
            _os.replace(tmp, ctx)
            log.info("%s: compiled the graph, cached at %s", provider, ctx)
        except OSError as exc:
            log.warning("%s: could not keep the compiled graph at %s: %s", provider, ctx, exc)
        return sess, True
    return sess, False


class OrtBackend:
    """Run the ONNX model through ONNX Runtime with selectable execution providers.

    This is the accelerator on-ramp: install the matching wheel
    (``onnxruntime``, ``onnxruntime-openvino``, ``onnxruntime-gpu``) and pass
    ``providers`` (e.g. ``["OpenVINOExecutionProvider"]`` on an Intel N100, or
    ``["TensorrtExecutionProvider"]`` on Nvidia). The *same* ONNX model is used.
    """

    name = "ort"
    #: Set when the accelerated session could not be built and the backend
    #: fell back to CPU (the error text); None otherwise.
    fallback_error: str | None = None

    def __init__(
        self,
        model_path: str | None = None,
        *,
        providers=None,
        session=None,  # injectable for tests (obj with .get_inputs()/.run())
    ) -> None:
        if session is not None:
            self._sess = session
        else:
            if not model_path:
                raise ValueError("OrtBackend needs a model_path or an injected session")
            import onnxruntime as ort  # lazy: optional dependency

            # What was ASKED for, kept before anything is dropped or reset: a
            # provider the wheel does not carry leaves ``resolve_providers``
            # already, and a session that fails to build is rebuilt on CPU
            # below. ``requested`` must survive both, or the metric that
            # exists to show the fallback (requested != effective) shows CPU
            # on both sides.
            asked = [provider_entry(n, d) for n, d in parse_provider_specs(providers)]
            provs = resolve_providers(providers, available_providers(ort, providers))
            log.info("ONNX Runtime backend requesting providers: %s", describe_providers(provs))
            try:
                opts = _ort_session_options(ort)
                plugins = [p for p in provs if provider_name(p) in _registered_plugin_eps]
                attached = 0
                for p in plugins:
                    # A plugin provider is attached through the session options
                    # by DEVICE (ORT's V2 provider API: the devices the registered
                    # library reported, filtered by provider name). It cannot be
                    # named in ``providers=`` and ``SessionOptions.add_provider``
                    # by name refuses it ("Provider configuration is not
                    # supported") — that one is for the built-in providers.
                    name = provider_name(p)
                    missing = plugin_device_missing(name, p)
                    if missing:
                        log.warning("%s", missing)
                        continue
                    devices = [d for d in ort.get_ep_devices() if d.ep_name == name]
                    if not devices:
                        log.warning("ORT plugin provider %s reports no device on this host; skipping", name)
                        continue
                    opts.add_provider_for_devices(devices, provider_options(p))
                    attached += 1
                if attached:
                    # CPU stays the implicit fallback for whatever the plugin
                    # declines, so the list is not passed at all — passing it
                    # alongside the session-level providers is refused.
                    self._sess, self._plugin_compiled = _build_plugin_session(
                        ort, opts, model_path, provider_name(plugins[0])
                    )
                else:
                    # The classic list: a plugin name in it would be refused by
                    # ORT, so a plugin that could not attach is left out here and
                    # the fallback is reported through ``requested`` vs effective.
                    classic = [p for p in provs if provider_name(p) not in _PLUGIN_EPS]
                    self._sess = ort.InferenceSession(
                        model_path, sess_options=opts, providers=classic
                    )
            except Exception as exc:  # noqa: BLE001 — any failure of the accelerated path
                if not provs or provider_name(provs[0]) == _CPU_EP:
                    raise
                # An accelerator that cannot even start (device not passed
                # through, driver missing, plugin failure) must not take
                # Tier-0 down with it: run on CPU, say so loudly, and let
                # the tier0_detector_backend metric show requested != effective.
                self.fallback_error = f"{type(exc).__name__}: {exc}"[:400]
                log.error(
                    "ONNX Runtime could not start on %s — %s. Falling back to CPU; "
                    "detection works but is not accelerated. Check the device is "
                    "passed through to the container and the image flavour matches "
                    "the host.",
                    describe_providers(provs[:1]), self.fallback_error,
                )
                provs = [_CPU_EP]
                self._plugin_compiled = None
                self._sess = ort.InferenceSession(
                    model_path, sess_options=_ort_session_options(ort), providers=provs
                )
            self.requested = asked or provs
        self._input = self._sess.get_inputs()[0].name
        self.effective = self._effective_providers()

    _plugin_compiled: bool | None = None   # None: no plugin in this session

    def _effective_providers(self) -> list[str]:
        """What the session actually runs on. ORT drops a provider it cannot
        initialise (no OpenVINO device, no CUDA libs) and carries on with the
        next one *silently* — the "declared an accelerator, running on CPU"
        failure. Surface it: log once, and expose it for metrics and the UI.

        A plugin provider is worse: ``get_providers()`` keeps listing it after
        its device failed (measured: QNN device creation refused, every node
        on CPU, the session still says QNN). So for a plugin session the
        answer comes from whether it compiled anything — the EP-context file
        it was asked to write, see :func:`_build_plugin_session`."""
        try:
            effective = list(self._sess.get_providers())
        except Exception:
            return []
        if self._plugin_compiled is False:
            effective = [p for p in effective if p not in _PLUGIN_EPS]
        requested = getattr(self, "requested", None)
        if requested and not self.fallback_error:   # the fallback path logged already
            wanted = provider_name(requested[0])
            if wanted != _CPU_EP and (not effective or effective[0] != wanted):
                log.warning(
                    "ONNX Runtime could not use %s — running on %s. Check the "
                    "accelerator's user-space driver is in the image and its "
                    "device is passed through to the container.",
                    describe_providers(requested[:1]), ", ".join(effective) or "nothing",
                )
            else:
                log.info("ONNX Runtime backend running on %s", describe_providers(requested[:1]))
        return effective

    def infer(self, blob: np.ndarray) -> np.ndarray:
        return self._sess.run(None, {self._input: blob})[0]

    def infer_all(self, blob: np.ndarray) -> list[np.ndarray]:
        """All output tensors (multi-head models, e.g. DETR's dets+labels)."""
        return list(self._sess.run(None, {self._input: blob}))


def build_backend(name: str, model_path: str | None = None, *, providers=None,
                  net=None, session=None):
    """Construct an inference backend by name: ``cvdnn`` (default) or ``ort``."""
    name = (name or "cvdnn").lower()
    if name in ("cvdnn", "opencv", "dnn"):
        return CvDnnBackend(model_path, net=net)
    if name in ("ort", "onnxruntime", "onnx-runtime"):
        return OrtBackend(model_path, providers=providers, session=session)
    raise ValueError(f"unknown ONNX backend {name!r} (use 'cvdnn' or 'ort')")


class OnnxYoloDetector:
    """YOLOv8/YOLO11 ONNX detector over a pluggable backend (``cvdnn`` or ``ort``)."""

    def __init__(
        self,
        model_path: str | None = None,
        *,
        input_size: int = 640,
        conf_threshold: float = 0.25,
        iou_threshold: float = 0.45,
        labels: list[str] | None = None,
        backend: str = "cvdnn",
        providers=None,
        net=None,      # injects a cv2.dnn net  -> cvdnn backend (also back-compat)
        session=None,  # injects an ORT session -> ort backend
        backend_impl=None,  # inject a fully-built backend
    ) -> None:
        self.input_size = input_size
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold
        self.labels = labels or COCO_LABELS
        if backend_impl is not None:
            self._backend = backend_impl
        elif net is not None:
            self._backend = CvDnnBackend(net=net)
        elif session is not None:
            self._backend = OrtBackend(session=session)
        elif model_path:
            self._backend = build_backend(backend, model_path, providers=providers)
        else:
            raise ValueError(
                "OnnxYoloDetector needs a model_path, or an injected net/session/backend_impl"
            )
        self.backend_name = getattr(self._backend, "name", "custom")
        # Settle the input size against the real graph ONCE, rather than
        # raising per region per frame (which killed the worker). Only the
        # cv2.dnn path can be probed cheaply; ORT reports its own shapes.
        if model_path:
            from .detector import resolve_input_size
            self.input_size = resolve_input_size(self, self.input_size)

    def detect(self, crop: np.ndarray) -> list[RawDetection]:
        blob = cv2.dnn.blobFromImage(
            crop, scalefactor=1 / 255.0,
            size=(self.input_size, self.input_size), swapRB=True, crop=False,
        )
        output = self._backend.infer(blob)
        return postprocess_yolo(
            output, input_size=self.input_size,
            conf_threshold=self.conf_threshold, iou_threshold=self.iou_threshold,
            labels=self.labels,
        )
