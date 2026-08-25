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
    tier0_tracks_active{camera}                        gauge
    tier0_events_published_total{camera}               counter
    gate_escalations_total{camera,reason}              counter
    gate_suppressions_total{camera,reason}             counter
    gate_shadow_would_suppress_total{camera}           counter
    tier0_frame_latency_seconds{camera}                histogram

Derived KPIs (compute in Prometheus/Grafana or the benchmark harness):
motion-gate ratio = skipped / (runs + skipped); expensive-call reduction =
baseline / gated escalations.
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict

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
    # Bounded-load guards (#track-explosion) — never cap silently:
    if getattr(result, "regions_capped", False):
        metrics.inc("tier0_regions_capped_total", cam)
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


def record_mainstream_fallback(camera_id: str, active: bool) -> None:
    """Gauge: 1 while Tier-0 decodes a high-resolution main stream for this
    camera (no usable substream) — the expensive path the README warns about."""
    metrics.gauge("tier0_mainstream_fallback", 1.0 if active else 0.0,
                  {"camera": camera_id})


def record_published(camera_id: str) -> None:
    metrics.inc("tier0_events_published_total", {"camera": camera_id})


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
