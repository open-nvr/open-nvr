# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lightweight, dependency-free metrics for the Tier-0 service (Prometheus text).

Rather than pull in ``prometheus_client``, this is a tiny thread-safe registry
that renders the Prometheus text exposition format — the same convention KAI-C's
``/metrics`` already uses, so one Prometheus can scrape the whole stack.

Metrics are emitted from the **service layer** (the worker), so the pure
pipeline/gate core stays metric-free and unit-testable. Names follow
``FOLLOWUPS.md #9``:

    tier0_frames_total{camera}                         counter
    tier0_detector_runs_total{camera}                  counter
    tier0_detector_skipped_total{camera,reason}        counter  reason=calibrating|no_motion
    tier0_motion_forced_exits_total{camera}            counter
    tier0_motion_latched_open{camera}                  gauge
    tier0_tracks_active{camera}                        gauge
    tier0_events_published_total{camera}               counter
    gate_escalations_total{camera,reason}              counter
    gate_suppressions_total{camera,reason}             counter
    gate_shadow_would_suppress_total{camera}           counter
    tier0_frame_latency_seconds{camera}                histogram
    tier0_decode_config{camera,skip,threads,fast,hwaccel,idle}  info (always 1)
    tier0_detector_backend{backend,requested,effective}  info (always 1)

Derived KPIs (compute in Prometheus/Grafana or the benchmark harness):
motion-gate ratio = skipped / (runs + skipped); expensive-call reduction =
baseline / gated escalations.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict

log = logging.getLogger("detect_pipeline.metrics")

_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _labels_str(labels: dict | None) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{_escape(str(v))}"' for k, v in sorted(labels.items()))
    return "{" + inner + "}"


def _fmt(v: float) -> str:
    # integers render without a trailing .0 for readability; else plain float
    return str(int(v)) if float(v).is_integer() else repr(float(v))


class Metrics:
    """Thread-safe counter/gauge/histogram registry rendering Prometheus text."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple, float] = defaultdict(float)
        self._gauges: dict[tuple, float] = defaultdict(float)
        self._hist: dict[tuple, dict] = {}

    @staticmethod
    def _key(name: str, labels: dict | None) -> tuple:
        return (name, tuple(sorted((labels or {}).items())))

    def inc(self, name: str, labels: dict | None = None, value: float = 1.0) -> None:
        with self._lock:
            self._counters[self._key(name, labels)] += value

    def gauge(self, name: str, value: float, labels: dict | None = None) -> None:
        with self._lock:
            self._gauges[self._key(name, labels)] = value

    def observe(self, name: str, value: float, labels: dict | None = None) -> None:
        k = self._key(name, labels)
        with self._lock:
            h = self._hist.get(k)
            if h is None:
                h = {"buckets": {b: 0.0 for b in _BUCKETS}, "sum": 0.0, "count": 0.0}
                self._hist[k] = h
            h["sum"] += value
            h["count"] += 1.0
            for b in _BUCKETS:                    # cumulative le-buckets
                if value <= b:
                    h["buckets"][b] += 1.0

    def value(self, name: str, labels: dict | None = None) -> float:
        """Read one counter/gauge value (used by tests)."""
        k = self._key(name, labels)
        with self._lock:
            if k in self._counters:
                return self._counters[k]
            return self._gauges.get(k, 0.0)

    def drop_label(self, label: str, value: str) -> int:
        """Remove every series carrying ``label=value``. Returns how many.

        Prometheus has no notion of "this series is over", so a camera deleted
        from core kept reporting: tier0_worker_up{camera=X} stayed at 0 and
        alerted forever, tier0_frame_age_seconds froze at its last scrape, and
        only a container restart cleared them. forget_camera() cleaned the
        /health input but nothing removed the metric an operator actually
        alerts on.
        """
        pair = (label, value)
        removed = 0
        with self._lock:
            for store in (self._counters, self._gauges, self._hist):
                for key in [k for k in store if pair in k[1]]:
                    store.pop(key, None)
                    removed += 1
        return removed

    def drop_series(self, name: str, label: str, value: str) -> int:
        """Remove every series of ONE metric carrying ``label=value``.

        Narrower than ``drop_label``, and it exists for info-metrics: their
        payload lives in the LABELS, so re-emitting after a config change
        mints a second series instead of updating the first. Both would then
        render — one describing a decode config that no longer exists —
        which is worse than not exporting the config at all. Callers
        drop-then-set to keep exactly one series per camera.
        """
        pair = (label, value)
        removed = 0
        with self._lock:
            for store in (self._counters, self._gauges, self._hist):
                for key in [k for k in store if k[0] == name and pair in k[1]]:
                    store.pop(key, None)
                    removed += 1
        return removed

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._hist.clear()

    def render(self) -> str:
        with self._lock:
            counters = sorted(self._counters.items())
            gauges = sorted(self._gauges.items())
            # snapshot histogram values *under the lock* — copying by reference
            # would risk a torn read against a concurrent observe().
            hists = sorted(
                (k, {"buckets": dict(v["buckets"]), "sum": v["sum"], "count": v["count"]})
                for k, v in self._hist.items()
            )
        lines: list[str] = []
        seen: set[str] = set()

        def typ(name: str, kind: str) -> None:
            if name not in seen:
                lines.append(f"# TYPE {name} {kind}")
                seen.add(name)

        for (name, labels), v in counters:
            typ(name, "counter")
            lines.append(f"{name}{_labels_str(dict(labels))} {_fmt(v)}")
        for (name, labels), v in gauges:
            typ(name, "gauge")
            lines.append(f"{name}{_labels_str(dict(labels))} {_fmt(v)}")
        for (name, labels), h in hists:
            typ(name, "histogram")
            base = dict(labels)
            for b in _BUCKETS:
                bl = dict(base, le=str(b))
                lines.append(f"{name}_bucket{_labels_str(bl)} {_fmt(h['buckets'][b])}")
            lines.append(f"{name}_bucket{_labels_str(dict(base, le='+Inf'))} {_fmt(h['count'])}")
            lines.append(f"{name}_sum{_labels_str(base)} {_fmt(h['sum'])}")
            lines.append(f"{name}_count{_labels_str(base)} {_fmt(h['count'])}")
        return "\n".join(lines) + "\n"


# Module-global default registry (Prometheus clients are process-global too).
metrics = Metrics()


def record_frame(
    camera_id: str,
    result,
    *,
    latency_s: float | None = None,
    detector_latency_s: float | None = None,
    stage_latency_s: dict[str, float] | None = None,
    model: str | None = None,
) -> None:
    """Emit Tier-0 per-frame metrics from a FrameResult (service layer).

    ``model`` (the active detector's identity, e.g. ``yolov8n``) labels the
    model-attributable series so two detectors of the same kind can be compared
    on the same dashboard: ``tier0_detector_runs_total`` and the pure
    ``tier0_detector_latency_seconds`` (region-loop time only, excluding
    decode/motion/track). ``tier0_detections_total{label}`` is per-class output
    volume — another axis for a model-vs-model comparison.
    """
    with _frame_wall_lock:
        _last_frame_wall[camera_id] = time.time()
    cam = {"camera": camera_id}
    # model-attributable label set (falls back to camera-only when unknown)
    mcam = {"camera": camera_id, "model": model} if model else cam
    metrics.inc("tier0_frames_total", cam)
    if getattr(result, "calibrating", False):
        metrics.inc("tier0_detector_skipped_total", {"camera": camera_id, "reason": "calibrating"})
    elif result.regions:
        metrics.inc("tier0_detector_runs_total", mcam)
    else:
        metrics.inc("tier0_detector_skipped_total", {"camera": camera_id, "reason": "no_motion"})
    metrics.gauge("tier0_tracks_active", float(len(result.tracks)), cam)
    # ALL internal tracks, tentative included — this is the population
    # DETECT_MAX_TRACKS caps. tracks_active counts confirmed ones only, so a
    # camera could sit at the cap refusing new spawns while the gauge meant
    # to explain that looked perfectly healthy.
    population = getattr(result, "track_population", None)
    if population is not None:
        metrics.gauge("tier0_tracks_population", float(population), cam)
    # Bounded-load guards (#track-explosion) — never cap silently:
    if getattr(result, "regions_capped", False):
        metrics.inc("tier0_regions_capped_total", cam)
    # Object masks (masks.py): detections dropped for standing inside one.
    # Counted so a mask that swallows real objects is visible, not silent.
    masked = int(getattr(result, "masked_detections", 0) or 0)
    if masked:
        metrics.inc("tier0_object_masked_total", cam, value=float(masked))
    # Per-label size/shape/score filters (tuning.py): same rule, never silent.
    filtered = int(getattr(result, "filtered_detections", 0) or 0)
    if filtered:
        metrics.inc("tier0_object_filtered_total", cam, value=float(filtered))
    # Motion gate: the #373 deadline firing is the signal that the gate
    # cannot model this scene. It used to be invisible — the repeat WARN
    # is demoted to debug, so a gate feeding the detector 1.5% of frames
    # looked exactly like a quiet camera. The counter is incremented on
    # the per-frame EDGE; the gauge says the gate has latched open.
    if getattr(result, "motion_forced_exit", False):
        metrics.inc("tier0_motion_forced_exits_total", cam)
    metrics.gauge(
        "tier0_motion_latched_open",
        1.0 if getattr(result, "motion_latched_open", False) else 0.0,
        cam,
    )
    skipped = int(getattr(result, "skipped_stationary", 0) or 0)
    if skipped:
        metrics.inc("tier0_stationary_skipped_total", cam, value=float(skipped))
    for det in getattr(result, "detections", None) or []:
        metrics.inc("tier0_detections_total", {"camera": camera_id, "label": det.label})
    if latency_s is not None:
        metrics.observe("tier0_frame_latency_seconds", latency_s, cam)
    if detector_latency_s is not None:
        metrics.observe("tier0_detector_latency_seconds", detector_latency_s, mcam)
    for stage, secs in (stage_latency_s or {}).items():
        metrics.observe("tier0_stage_latency_seconds", secs, {"camera": camera_id, "stage": stage})


# Wall-clock time of the most recent processed frame per camera — the "are
# frames actually flowing" signal /health uses. Module-level like ``metrics``.
#
# Guarded: N worker threads INSERT into this (a camera's first frame adds a
# key) while the scrape thread iterates it. An unguarded insert during
# iteration raises "dictionary changed size during iteration", which would
# take out /metrics and /health together — and it fires precisely during
# worker churn, when those endpoints are what you are looking at.
_last_frame_wall: dict[str, float] = {}
_frame_wall_lock = threading.Lock()


def _frame_wall_snapshot() -> dict[str, float]:
    with _frame_wall_lock:
        return dict(_last_frame_wall)


def forget_camera(camera_id: str) -> None:
    """Drop a camera's frame-age entry (it left the desired set).

    Without this the key lives forever and the camera is reported stale
    indefinitely after it is deleted from core.
    """
    with _frame_wall_lock:
        _last_frame_wall.pop(camera_id, None)
    # ...and drop its metric series, or the camera keeps alerting after it is
    # gone: tier0_worker_up stays 0 and tier0_frame_age_seconds freezes.
    dropped = metrics.drop_label("camera", camera_id)
    if dropped:
        log.info("dropped %d metric series for removed camera %s", dropped, camera_id)


def newest_frame_age_s(now: float | None = None) -> float | None:
    """Age in seconds of the newest frame across all cameras (None = none yet)."""
    snap = _frame_wall_snapshot()
    if not snap:
        return None
    now = time.time() if now is None else now
    return max(0.0, now - max(snap.values()))


def frame_ages_s(now: float | None = None) -> dict[str, float]:
    """Per-camera age of the last processed frame.

    The fleet-wide ``newest_frame_age_s`` takes the MAX across cameras, so a
    single healthy camera keeps it near zero while every other feed is dead —
    it cannot answer "is camera 2 still being analyzed?". This can.
    """
    now = time.time() if now is None else now
    return {cam: max(0.0, now - t) for cam, t in _frame_wall_snapshot().items()}


def stale_cameras(threshold_s: float, now: float | None = None) -> list[str]:
    """Cameras whose last frame is older than ``threshold_s``, sorted."""
    return sorted(
        cam for cam, age in frame_ages_s(now).items() if age > threshold_s
    )


def sample_frame_age_metrics(now: float | None = None) -> None:
    """Refresh the per-camera frame-age gauge (call at scrape time).

    Emitted on scrape rather than in the frame loop on purpose: a gauge only
    written while frames flow FREEZES at its last good value when the feed
    stalls, which is the exact moment it needs to move.
    """
    for cam, age in frame_ages_s(now).items():
        metrics.gauge("tier0_frame_age_seconds", age, {"camera": cam})


def record_evidence(camera_id: str, active: bool, stats: dict, *,
                    frames: int = 0, starts: int = 0, mismatches: int = 0,
                    gop_s: float | None = None,
                    keyframes_only: bool | None = None) -> None:
    """Main-stream evidence (evidence.py): whether the camera's main decode
    is running right now, how many best-crop updates were cut from which
    stream, and how many main frames it has read. The split by source is
    the number a quality study reads: evidence from "main" is full-res,
    evidence from "detect" is the sub-sized fallback (no main frame close
    enough in time, the main decode still starting — or, counted on its
    own as ``mismatch``, a main frame that did not LOOK like the detect
    frame inside the box: a keyframe too old for a moving subject, or a
    sub-stream with a different field of view). ``gop_s`` is the keyframe
    interval keyframe-only mode measured; ``keyframes_only`` whether the
    camera is still on keyframes (0 = full decode, by dial or because the
    GOP was too long)."""
    metrics.gauge("tier0_evidence_active", 1.0 if active else 0.0, {"camera": camera_id})
    for source in ("main", "detect"):
        metrics.gauge("tier0_evidence_crops_total", float(stats.get(source, 0)),
                      {"camera": camera_id, "source": source})
    metrics.gauge("tier0_evidence_mismatch_total", float(mismatches), {"camera": camera_id})
    metrics.gauge("tier0_evidence_frames_total", float(frames), {"camera": camera_id})
    metrics.gauge("tier0_evidence_starts_total", float(starts), {"camera": camera_id})
    if gop_s is not None:
        metrics.gauge("tier0_evidence_gop_seconds", float(gop_s), {"camera": camera_id})
    if keyframes_only is not None:
        metrics.gauge("tier0_evidence_keyframes_only", 1.0 if keyframes_only else 0.0,
                      {"camera": camera_id})


#: Why a camera detects on its MAIN stream, as the value of
#: ``tier0_detect_on_main{camera}`` (0 = detecting on the sub stream). A
#: value rather than a label so a camera whose reason changes on restart
#: leaves no stale series behind.
DETECT_ON_MAIN_REASONS = {"": 0, "policy": 1, "no_substream": 2, "sub_unreachable": 3, "sub_too_small": 4}


def record_detect_stream(camera_id: str, stream: str, reason: str = "") -> None:
    """Which stream this camera's worker detects on, and why when it is the main."""
    code = DETECT_ON_MAIN_REASONS.get(reason, 1) if stream == "main" else 0
    metrics.gauge("tier0_detect_on_main", float(code), {"camera": camera_id})


def record_mainstream_fallback(camera_id: str, active: bool) -> None:
    """Gauge: 1 while Tier-0 decodes a high-resolution main stream for this
    camera (no usable substream) — the expensive path the README warns about."""
    metrics.gauge("tier0_mainstream_fallback", 1.0 if active else 0.0,
                  {"camera": camera_id})


def record_published(camera_id: str) -> None:
    metrics.inc("tier0_events_published_total", {"camera": camera_id})


def record_worker_straggler(camera_id: str) -> None:
    """A worker did not exit within the fleet stop budget.

    Previously invisible: the manager moved on regardless, so a thread that
    outlived its join kept its ffmpeg alive and its replacement quietly
    duplicated it."""
    metrics.inc("tier0_worker_stop_timeouts_total", {"camera": camera_id})


def record_sink_error(camera_id: str) -> None:
    """A publish to the event bus raised. Without a counter, a bus outage
    is indistinguishable from a quiet scene: both just stop incrementing
    tier0_events_published_total."""
    metrics.inc("tier0_sink_errors_total", {"camera": camera_id})


def record_visit_dropped(camera_id: str, reason: str) -> None:
    """A finished visit never reached the events store.

    The poster module has always promised this counter in its docstring
    ("failures are visible via tier0_visits_dropped_total") while nothing
    emitted it — so silent, permanent history loss (a queue that stays
    full, a post that always fails) showed up nowhere at all."""
    metrics.inc("tier0_visits_dropped_total",
                {"camera": camera_id, "reason": reason})


def record_visit_posted(camera_id: str) -> None:
    metrics.inc("tier0_visits_posted_total", {"camera": camera_id})


def record_gate(camera_id: str, gate_result) -> None:
    """Emit gate metrics from a GateResult (escalations/suppressions by reason)."""
    for d in gate_result.decisions:
        if d.escalate:
            metrics.inc("gate_escalations_total", {"camera": camera_id, "reason": d.reason})
        else:
            metrics.inc("gate_suppressions_total", {"camera": camera_id, "reason": d.reason})
            if gate_result.shadow:
                metrics.inc("gate_shadow_would_suppress_total", {"camera": camera_id})


def record_worker_state(camera_id: str, up: bool, *, target_fps: float | None = None) -> None:
    """Worker liveness + its configured target fps (the operator 'is it running' signal)."""
    metrics.gauge("tier0_worker_up", 1.0 if up else 0.0, {"camera": camera_id})
    if target_fps is not None:
        metrics.gauge("tier0_target_fps", float(target_fps), {"camera": camera_id})


def record_decode_config(
    camera_id: str,
    *,
    skip: str,
    threads: int,
    fast: bool,
    hwaccel: str,
    idle: str | None,
) -> None:
    """Export the decode configuration this camera is ACTUALLY running under.

    Every other decode saving is measurable — ``tier0_decode_idle`` says
    whether a camera is in cheap keyframe-only mode right now,
    ``tier0_regions_budget`` says where the shedder settled — but the static
    dials that shape the dominant cost (decode) were only ever logged at
    worker start. From a dashboard there was no way to answer "is this camera
    really running noref on 2 threads, or did a typo fall back?", and the
    fallbacks are deliberately silent: an invalid DECODE_SKIP degrades to a
    safe default (see run.py), and hwaccel downgrades to CPU when the device
    is unusable. That is exactly the state worth seeing — a camera whose
    config says ``vaapi`` while its decode says ``cpu`` is the difference
    between ~0.3 and ~2 cores, and #306 shipped a decode token that no
    dashboard could have contradicted.

    Prometheus info-metric convention: the value is always 1 and the payload
    is the label set, so it joins onto the numeric series in a query —
    e.g. ``tier0_process_cpu_percent * on(camera) group_left(skip,hwaccel)
    tier0_decode_config``.

    ``hwaccel`` must be the EFFECTIVE backend (after any downgrade), not the
    requested one — reporting the request would re-hide the fallback this
    metric exists to reveal.
    """
    labels = {
        "camera": camera_id,
        "skip": str(skip or "none"),
        "threads": str(int(threads)),
        "fast": "true" if fast else "false",
        "hwaccel": str(hwaccel or "cpu"),
        # "off" rather than an empty label: an absent value reads as "unknown"
        # in a query, and adaptive decode being OFF is a definite answer.
        "idle": str(idle or "off"),
    }
    # Drop-then-set: a worker restarting under changed config (reconcile, an
    # edited .env, an hwaccel downgrade on the second attempt) must REPLACE
    # its series, never accumulate a second one describing the old world.
    metrics.drop_series("tier0_decode_config", "camera", camera_id)
    metrics.gauge("tier0_decode_config", 1.0, labels)


def record_frames_dropped(camera_id: str, n: int) -> None:
    """Decoded frames the detector never saw: the drain thread overwrote
    them because detection was still busy with an older one (#507). Exact,
    from the gaps in Frame.seq. A sustained rate near the camera's fps is
    the capacity signal — the box cannot serve this camera at its
    configured detector budget — and the worker WARNs on it; before the
    drain was decoupled, the same condition dropped the RTSP session and
    read as a flaky camera."""
    if n > 0:
        metrics.inc("tier0_frames_dropped_total", {"camera": camera_id}, float(n))


def record_worker_restart(camera_id: str) -> None:
    """A source (ffmpeg) restart for this camera — repeated restarts = an unhealthy feed."""
    metrics.inc("tier0_worker_restarts_total", {"camera": camera_id})


def record_processing_fps(camera_id: str, fps: float) -> None:
    """Frames/s the worker is actually sustaining. Compare to target_fps: a ratio
    well under 1 means the box can't keep up with this camera (the capacity signal)."""
    metrics.gauge("tier0_processing_fps", float(fps), {"camera": camera_id})


_proc_prev: dict[str, float | None] = {"cpu_s": None, "t": None}


def sample_process_metrics(registry: Metrics | None = None) -> None:
    """Sample this process's CPU% + RSS from /proc and set gauges (Linux).

    Emitted so the OpenNVR app can show detect-pipeline CPU/RAM next to the AI
    adapters (which already export the same shape) and compare before/after
    enabling the gate. No dependency — reads /proc directly; a no-op off Linux.
    """
    reg = registry or metrics
    try:
        with open("/proc/self/stat") as f:
            parts = f.read().split()
        cpu_s = (int(parts[13]) + int(parts[14])) / os.sysconf("SC_CLK_TCK")  # utime+stime
        with open("/proc/self/statm") as f:
            rss = int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")        # resident pages
    except Exception:                                                          # not Linux / no /proc
        return
    now = time.monotonic()
    reg.gauge("tier0_process_resident_memory_bytes", float(rss))
    if _proc_prev["cpu_s"] is not None and now > _proc_prev["t"]:
        pct = (cpu_s - _proc_prev["cpu_s"]) / (now - _proc_prev["t"]) * 100.0
        reg.gauge("tier0_process_cpu_percent", max(0.0, pct))
    _proc_prev["cpu_s"], _proc_prev["t"] = cpu_s, now


def serve_metrics(port: int = 9109, *, registry: Metrics | None = None,
                  best_frames=None, health_fn=None):  # pragma: no cover
    """Start a background HTTP server exposing ``/metrics`` (and, when a
    ``BestFrameStore`` is given, ``/best_frame?camera=&track=``). Best-effort."""
    import http.server
    import threading as _t
    from urllib.parse import parse_qs, urlparse

    reg = registry or metrics

    class _Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code, body=b"", ctype="text/plain"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")
            if path in ("/metrics", ""):
                sample_process_metrics(reg)      # refresh CPU%/RSS on each scrape
                sample_frame_age_metrics()       # ...and per-camera staleness
                self._send(200, reg.render().encode("utf-8"),
                           "text/plain; version=0.0.4")
                return
            if path == "/health":
                import json as _json
                if health_fn is None:
                    self._send(200, b'{"status":"ok"}', "application/json")
                    return
                ok, detail = health_fn()
                body = _json.dumps(detail, indent=1).encode("utf-8")
                self._send(200 if ok else 503, body, "application/json")
                return
            if path == "/best_frame" and best_frames is not None:
                q = parse_qs(parsed.query)
                cam = (q.get("camera") or [""])[0]
                track = (q.get("track") or [None])[0]
                jpeg = (best_frames.get_jpeg(cam, track) if track is not None
                        else best_frames.latest_jpeg(cam))
                if jpeg is None:
                    self._send(404, b"no best frame")
                    return
                self._send(200, jpeg, "image/jpeg")
                return
            self._send(404)

        def log_message(self, *a):  # silence access log
            pass

    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    _t.Thread(target=httpd.serve_forever, name="tier0-metrics", daemon=True).start()
    return httpd


def record_detector_backend(backend: str, requested: str, effective: str) -> None:
    """Export what the Tier-0 detector ACTUALLY runs on.

    The decode twin of this is ``tier0_decode_config``; this one answers "is
    the detector on the NPU, or did OpenVINO quietly fall back to CPU?". ORT
    drops a provider it cannot initialise and carries on silently, so
    ``effective`` (from the live session) is the value that matters;
    ``requested`` is kept beside it so the fallback is visible in one query:
    ``tier0_detector_backend{requested!="",effective="CPUExecutionProvider"}``.
    Human form, e.g. ``OpenVINOExecutionProvider(AUTO:NPU,GPU,CPU)``.
    """
    labels = {
        "backend": str(backend or "unknown"),
        "requested": str(requested or ""),
        "effective": str(effective or ""),
    }
    metrics.drop_series("tier0_detector_backend", "backend", labels["backend"])
    metrics.gauge("tier0_detector_backend", 1.0, labels)
    last_detector_backend.update(labels)


#: The labels the last ``record_detector_backend`` call exported, for a log
#: line that must say what the detector runs on — the capacity warning: "18
#: cameras over capacity" reads very differently with "on CPUExecutionProvider,
#: QNNExecutionProvider(htp) requested" after it.
last_detector_backend: dict[str, str] = {"backend": "", "requested": "", "effective": ""}


def detector_backend_phrase() -> str:
    """``on <effective>`` plus ``, <requested> requested`` when they differ —
    empty before the first detector is built."""
    eff, req = last_detector_backend.get("effective", ""), last_detector_backend.get("requested", "")
    if not eff:
        return ""
    phrase = f"detector on {eff}"
    wanted = req.split(",")[0].strip().split("(")[0]     # "QNNExecutionProvider(htp)" → the provider
    if wanted and wanted != eff.split(",")[0].strip():
        phrase += f" ({req} requested — not accelerated)"
    return phrase
