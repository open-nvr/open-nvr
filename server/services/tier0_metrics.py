"""
Copyright (c) 2026 OpenNVR
SPDX-License-Identifier: AGPL-3.0-or-later

Read-only scrape of the detect-pipeline's Tier-0 ``/metrics`` (compute-gated
inference) for the app's *Compute-gated* panel.

The detect-pipeline exposes dependency-free Prometheus text at
``DETECT_METRICS_PORT`` (default ``:9109``) — the SAME exposition format the AI
adapters already use, which is why the app can show its CPU/RAM beside them.
This module fetches that text, parses it, and reduces it to the handful of
signals the panel renders:

  * process CPU% / RSS               (``tier0_process_*``)
  * motion-gate ratio + frame counts (``tier0_frames_total`` / ``tier0_detector_*``)
  * gate escalations vs suppressions (``gate_*``)
  * expensive-model (Tier-1) calls   (``tier1_dispatch_*``)

It also infers a coarse ``mode`` (``not_running`` / ``off`` / ``shadow`` /
``enforce``) from which metric families are present — the pipeline doesn't
export its flag directly, so this is a best-effort label for the UI, never a
control. Unreachable pipeline is a normal, non-error state (gate disabled or not
deployed): the caller gets ``available=False`` and the panel simply says so.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from core.config import settings
from core.logging_config import main_logger

# name{label="v",...} value   — the only line shape we consume (comments skipped)
_SAMPLE_RE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?P<labels>\{[^}]*\})?\s+(?P<value>[^\s]+)\s*$")
_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')

# Prometheus histogram le-buckets the pipeline exports (see metrics.py _BUCKETS).
_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


class Sample:
    __slots__ = ("name", "labels", "value")

    def __init__(self, name: str, labels: dict[str, str], value: float) -> None:
        self.name = name
        self.labels = labels
        self.value = value


def _parse_labels(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    return {m.group(1): m.group(2) for m in _LABEL_RE.finditer(raw)}


def parse_prometheus_text(text: str) -> list[Sample]:
    """Parse Prometheus exposition text into flat samples (comments ignored)."""
    out: list[Sample] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE_RE.match(line)
        if not m:
            continue
        try:
            value = float(m.group("value"))
        except ValueError:
            continue  # +Inf/NaN in gauges we don't consume
        out.append(Sample(m.group("name"), _parse_labels(m.group("labels")), value))
    return out


def _sum(samples: list[Sample], name: str) -> float:
    """Sum a counter/gauge family across all label sets (e.g. per-camera)."""
    return sum(s.value for s in samples if s.name == name)


def _last_gauge(samples: list[Sample], name: str) -> float | None:
    """A process-wide gauge with no labels (CPU%, RSS, inflight)."""
    vals = [s.value for s in samples if s.name == name and not s.labels]
    return vals[-1] if vals else None


def _sum_by_label(samples: list[Sample], name: str, label: str, value: str) -> float:
    return sum(s.value for s in samples if s.name == name and s.labels.get(label) == value)


def _sums_grouped_by(samples: list[Sample], name: str, label: str) -> dict[str, float]:
    """Sum a counter family, grouped by one label value (e.g. detections per class)."""
    out: dict[str, float] = {}
    for s in samples:
        if s.name == name and label in s.labels:
            out[s.labels[label]] = out.get(s.labels[label], 0.0) + s.value
    return out


def _dominant_label(samples: list[Sample], name: str, label: str) -> str | None:
    """The label value carrying the most weight (e.g. the active detector model)."""
    grouped = _sums_grouped_by(samples, name, label)
    return max(grouped, key=grouped.get) if grouped else None


def _hist_avg_and_p95(samples: list[Sample], name: str) -> tuple[float | None, float | None]:
    """Avg (sum/count) and a bucket-interpolated p95, in **milliseconds**.

    Aggregates the histogram across label sets (all adapters) — the panel shows
    one fleet-wide expensive-call latency figure.
    """
    total_sum = _sum(samples, f"{name}_sum")
    total_count = _sum(samples, f"{name}_count")
    if total_count <= 0:
        return None, None
    avg_ms = (total_sum / total_count) * 1000.0
    # cumulative counts per le-bucket, summed over label sets
    cum: dict[float, float] = {}
    for b in _BUCKETS:
        cum[b] = sum(
            s.value for s in samples
            if s.name == f"{name}_bucket" and s.labels.get("le") == str(b)
        )
    target = 0.95 * total_count
    p95_ms: float | None = None
    for b in _BUCKETS:
        if cum.get(b, 0.0) >= target:
            p95_ms = b * 1000.0
            break
    if p95_ms is None:  # tail beyond the largest finite bucket
        p95_ms = _BUCKETS[-1] * 1000.0
    return avg_ms, p95_ms


def reduce_metrics(samples: list[Sample]) -> dict[str, Any]:
    """Reduce raw samples to the Compute-gated panel's rollup."""
    frames = _sum(samples, "tier0_frames_total")
    runs = _sum(samples, "tier0_detector_runs_total")
    skipped_motion = _sum_by_label(samples, "tier0_detector_skipped_total", "reason", "no_motion")
    skipped_calib = _sum_by_label(samples, "tier0_detector_skipped_total", "reason", "calibrating")
    denom = runs + skipped_motion  # calibrating frames aren't a gate decision
    motion_gate_ratio = (skipped_motion / denom) if denom > 0 else None

    escalations = _sum(samples, "gate_escalations_total")
    suppressions = _sum(samples, "gate_suppressions_total")
    shadow_suppress = _sum(samples, "gate_shadow_would_suppress_total")
    gate_present = any(
        s.name in ("gate_escalations_total", "gate_suppressions_total", "gate_shadow_would_suppress_total")
        for s in samples
    )

    dispatched = _sum(samples, "tier1_dispatch_total")
    lat_avg_ms, lat_p95_ms = _hist_avg_and_p95(samples, "tier1_dispatch_latency_seconds")

    # Model-benchmarking signals: which detector is active, its pure inference
    # latency, and per-class output volume — the aspects you compare two models
    # of the same kind on. `model` is labelled on tier0_detector_* by the pipeline.
    model = _dominant_label(samples, "tier0_detector_runs_total", "model") \
        or _dominant_label(samples, "tier0_detector_latency_seconds_count", "model")
    det_avg_ms, det_p95_ms = _hist_avg_and_p95(samples, "tier0_detector_latency_seconds")
    detections_by_class = {k: int(v) for k, v in _sums_grouped_by(samples, "tier0_detections_total", "label").items()}

    # Coarse mode inference (best-effort UI label, not a control signal):
    #   no frames -> not running; frames but no gate metrics -> gate off;
    #   shadow-suppress counter present -> shadow; else acting -> enforce.
    if frames <= 0 and not samples:
        mode = "not_running"
    elif not gate_present:
        mode = "off"
    elif shadow_suppress > 0 or (suppressions > 0 and escalations == 0 and dispatched == 0):
        mode = "shadow"
    else:
        mode = "enforce"

    return {
        "available": True,
        "mode": mode,
        "model": model,
        "detector": {
            "latency_avg_ms": det_avg_ms,
            "latency_p95_ms": det_p95_ms,
            "detections_total": int(sum(detections_by_class.values())),
            "detections_by_class": detections_by_class,
        },
        "process": {
            "cpu_percent": _last_gauge(samples, "tier0_process_cpu_percent"),
            "memory_bytes": _last_gauge(samples, "tier0_process_resident_memory_bytes"),
        },
        "frames": {
            "total": int(frames),
            "detector_runs": int(runs),
            "skipped_no_motion": int(skipped_motion),
            "skipped_calibrating": int(skipped_calib),
            "motion_gate_ratio": motion_gate_ratio,
        },
        "gate": {
            "escalations": int(escalations),
            "suppressions": int(suppressions),
            "shadow_would_suppress": int(shadow_suppress),
        },
        "tier1": {
            "dispatched": int(dispatched),
            "errors": int(_sum(samples, "tier1_dispatch_errors_total")),
            "dropped": int(_sum(samples, "tier1_dispatch_dropped_total")),
            "inflight": int(_last_gauge(samples, "tier1_dispatch_inflight") or 0),
            "latency_avg_ms": lat_avg_ms,
            "latency_p95_ms": lat_p95_ms,
        },
    }


async def get_tier0_metrics() -> dict[str, Any]:
    """Fetch + reduce the detect-pipeline's ``/metrics``.

    Returns ``{available: False, reason: ...}`` when the endpoint is disabled or
    unreachable — a normal state (gate off / pipeline not deployed), not an error.
    """
    base = (settings.detect_pipeline_metrics_url or "").rstrip("/")
    if not base:
        return {"available": False, "reason": "disabled"}
    url = f"{base}/metrics"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            text = resp.text
    except httpx.HTTPError as e:
        main_logger.debug("Tier-0 metrics unreachable at %s: %s", url, e)
        return {"available": False, "reason": "unreachable"}
    return reduce_metrics(parse_prometheus_text(text))
