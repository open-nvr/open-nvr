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
    for det in getattr(result, "detections", None) or []:
        metrics.inc("tier0_detections_total", {"camera": camera_id, "label": det.label})
    if latency_s is not None:
        metrics.observe("tier0_frame_latency_seconds", latency_s, cam)
    if detector_latency_s is not None:
        metrics.observe("tier0_detector_latency_seconds", detector_latency_s, mcam)


def record_published(camera_id: str) -> None:
    metrics.inc("tier0_events_published_total", {"camera": camera_id})


def record_gate(camera_id: str, gate_result) -> None:
    """Emit gate metrics from a GateResult (escalations/suppressions by reason)."""
    for d in gate_result.decisions:
        if d.escalate:
            metrics.inc("gate_escalations_total", {"camera": camera_id, "reason": d.reason})
        else:
            metrics.inc("gate_suppressions_total", {"camera": camera_id, "reason": d.reason})
            if gate_result.shadow:
                metrics.inc("gate_shadow_would_suppress_total", {"camera": camera_id})


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


def serve_metrics(port: int = 9109, *, registry: Metrics | None = None):  # pragma: no cover
    """Start a background HTTP server exposing ``/metrics``. Best-effort."""
    import http.server
    import threading as _t

    reg = registry or metrics

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.rstrip("/") not in ("/metrics", ""):
                self.send_response(404); self.end_headers(); return
            sample_process_metrics(reg)          # refresh CPU%/RSS on each scrape
            body = reg.render().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # silence access log
            pass

    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    _t.Thread(target=httpd.serve_forever, name="tier0-metrics", daemon=True).start()
    return httpd
