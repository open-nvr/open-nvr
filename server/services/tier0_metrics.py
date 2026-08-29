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

import math
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
            continue
        if not math.isfinite(value):
            continue  # +Inf/NaN — float() accepts these; drop so int() can't blow up
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


def _by_camera(samples: list[Sample], name: str) -> dict[str, float]:
    """Latest value of a per-camera family, keyed by camera label."""
    out: dict[str, float] = {}
    for s in samples:
        if s.name == name and "camera" in s.labels:
            out[s.labels["camera"]] = s.value
    return out


def _labels_by_camera(samples: list[Sample], name: str) -> dict[str, dict]:
    """Label sets of an info-metric family, keyed by camera.

    Info-metrics (value always 1) carry their payload in the LABELS, so
    ``_by_camera`` — which keeps the value — reads nothing useful from them.
    """
    out: dict[str, dict] = {}
    for s in samples:
        if s.name == name and "camera" in s.labels:
            out[s.labels["camera"]] = dict(s.labels)
    return out


def _decode_row(labels: dict | None) -> dict | None:
    """Shape one camera's decode config for the API, or None if unreported."""
    if not labels:
        return None
    try:
        threads = int(labels.get("threads", "0"))
    except ValueError:                      # a malformed scrape is not a crash
        threads = 0
    return {
        "skip": labels.get("skip") or "none",
        "threads": threads,
        "fast": labels.get("fast") == "true",
        "hwaccel": labels.get("hwaccel") or "cpu",
        "idle": labels.get("idle") or "off",
    }


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
    # cumulative counts per le-bucket, summed over label sets. Match `le`
    # numerically (not as a string) so "1" / "1.0" / "1e0" all bucket correctly.
    def _le_is(le_raw: str | None, boundary: float) -> bool:
        try:
            return le_raw is not None and float(le_raw) == boundary
        except ValueError:
            return False

    cum: dict[float, float] = {}
    for b in _BUCKETS:
        cum[b] = sum(
            s.value for s in samples
            if s.name == f"{name}_bucket" and _le_is(s.labels.get("le"), b)
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
    skipped_stationary = _sum(samples, "tier0_stationary_skipped_total")
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

    # Per-stage average latency (ms) — where the frame's time actually goes.
    stage_latency_ms: dict[str, float] = {}
    stages = {s.labels["stage"] for s in samples
              if s.name.startswith("tier0_stage_latency_seconds") and "stage" in s.labels}
    for st in stages:
        s_sum = sum(s.value for s in samples
                    if s.name == "tier0_stage_latency_seconds_sum" and s.labels.get("stage") == st)
        s_cnt = sum(s.value for s in samples
                    if s.name == "tier0_stage_latency_seconds_count" and s.labels.get("stage") == st)
        if s_cnt > 0:
            stage_latency_ms[st] = round((s_sum / s_cnt) * 1000.0, 3)

    # Operator health: are the workers up, and are they keeping up with the cameras?
    worker_cams = {s.labels.get("camera") for s in samples if s.name == "tier0_worker_up"}
    workers_up = int(sum(1 for s in samples if s.name == "tier0_worker_up" and s.value >= 1.0))
    proc = {s.labels["camera"]: s.value for s in samples
            if s.name == "tier0_processing_fps" and "camera" in s.labels}
    tgt = {s.labels["camera"]: s.value for s in samples
           if s.name == "tier0_target_fps" and "camera" in s.labels}
    ratios = {c: proc[c] / tgt[c] for c in proc if tgt.get(c, 0) > 0}
    worst_cam = min(ratios, key=ratios.get) if ratios else None
    # Per-camera detail — the fleet aggregate hides exactly the signals that
    # matter when ONE camera is struggling. Every one of these earned its
    # place in a live debugging session: the region budget pinned below its
    # ceiling is the load-shedding signature (the pipeline is protecting the
    # stream by detecting less); frame age says whether frames are fresh;
    # visits dropped and a main-stream fallback are misconfigurations the
    # operator can actually fix.
    frame_age = _by_camera(samples, "tier0_frame_age_seconds")
    budget_now = _by_camera(samples, "tier0_regions_budget")
    budget_cfg = _by_camera(samples, "tier0_regions_configured")
    tracks_act = _by_camera(samples, "tier0_tracks_active")
    mainstream = _by_camera(samples, "tier0_mainstream_fallback")
    visits_ok = _by_camera(samples, "tier0_visits_posted_total")
    visits_drop = _by_camera(samples, "tier0_visits_dropped_total")
    capped = _by_camera(samples, "tier0_regions_capped_total")
    up_by_cam = _by_camera(samples, "tier0_worker_up")
    # The decode dials this camera actually opened with. Sits next to the
    # struggling-camera signals on purpose: "cam3 is shedding" and "cam3 is
    # decoding on the CPU with skip=none" is one answer, not two lookups.
    decode_cfg = _labels_by_camera(samples, "tier0_decode_config")
    all_cams = sorted(set(proc) | set(tgt) | set(up_by_cam) | set(frame_age))
    cameras = []
    for cam in all_cams:
        b = budget_now.get(cam)
        c = budget_cfg.get(cam)
        cameras.append({
            "camera": cam,
            "up": bool(up_by_cam.get(cam, 0) >= 1.0),
            "fps": round(proc[cam], 2) if cam in proc else None,
            "target_fps": round(tgt[cam], 2) if cam in tgt else None,
            "frame_age_s": round(frame_age[cam], 2) if cam in frame_age else None,
            "regions_budget": None if b is None else int(b),
            "regions_configured": None if c is None else int(c),
            # Shedding = the budget is currently held below its ceiling.
            # Only claimable when BOTH numbers exist; absence is not evidence.
            "shedding": (b is not None and c is not None and b < c),
            "regions_capped_total": int(capped.get(cam, 0)),
            "tracks_active": int(tracks_act.get(cam, 0)),
            "visits_posted": int(visits_ok.get(cam, 0)),
            "visits_dropped": int(visits_drop.get(cam, 0)),
            "mainstream_fallback": bool(mainstream.get(cam, 0) >= 1.0),
            # None when the pipeline predates the metric — an older
            # detect-pipeline image against a newer core must render as
            # "unknown", never as a fabricated default config.
            "decode": _decode_row(decode_cfg.get(cam)),
        })

    health = {
        "workers_up": workers_up,
        "workers_total": len([c for c in worker_cams if c is not None]),
        "min_fps_ratio": round(ratios[worst_cam], 3) if worst_cam else None,
        "worst_camera": worst_cam,
        "restarts_total": int(_sum(samples, "tier0_worker_restarts_total")),
    }

    # Coarse mode inference (best-effort UI label, not a control signal):
    #   no frames -> not running; frames but no gate metrics -> gate off;
    #   shadow-suppress counter present -> shadow; else acting -> enforce.
    if frames <= 0 and not samples:
        mode = "not_running"
    elif not gate_present:
        mode = "off"
    elif shadow_suppress > 0:
        # Only the shadow-would-suppress counter reliably signals shadow. (A quiet
        # *enforce* run can also have suppressions>0 and no escalations yet, so we
        # must NOT infer shadow from "suppress but no dispatch".)
        mode = "shadow"
    else:
        mode = "enforce"

    return {
        "available": True,
        "mode": mode,
        "model": model,
        "health": health,
        "detector": {
            "latency_avg_ms": det_avg_ms,
            "latency_p95_ms": det_p95_ms,
            "detections_total": int(sum(detections_by_class.values())),
            "detections_by_class": detections_by_class,
            "stage_latency_ms": stage_latency_ms,
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
            "skipped_stationary": int(skipped_stationary),
            "motion_gate_ratio": motion_gate_ratio,
        },
        "cameras": cameras,
        # The pipeline's outputs actually LEAVING it: detections become NATS
        # events (what alarms and apps consume) and finished visits become
        # events-store rows (what history answers from). Zero here while
        # detections climb is the "detected but nobody was told" failure —
        # observed live, and invisible until these were surfaced.
        "events_flow": {
            "bus_events_published": int(_sum(samples, "tier0_events_published_total")),
            "visits_posted": int(_sum(samples, "tier0_visits_posted_total")),
            "visits_dropped": int(_sum(samples, "tier0_visits_dropped_total")),
            "sink_errors": int(_sum(samples, "tier0_sink_errors_total")),
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
    # ``:0`` is DETECT_METRICS_PORT=0 flowing through the compose URL
    # (docker-compose.yml builds it as http://detect-pipeline:${PORT}):
    # the operator disabled exposition on the pipeline, so report
    # "disabled" — probing port 0 would misreport it as "unreachable".
    if not base or base.endswith(":0"):
        return {"available": False, "reason": "disabled"}
    url = f"{base}/metrics"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            text = resp.text
    except Exception as e:  # HTTPError, InvalidURL, connect/timeout — all "not reachable"
        main_logger.debug("Tier-0 metrics unreachable at %s: %s", url, e)
        return {"available": False, "reason": "unreachable"}
    try:
        return reduce_metrics(parse_prometheus_text(text))
    except Exception:
        # A malformed metrics payload must degrade, never 500 the route.
        main_logger.debug("Tier-0 metrics parse failed for %s", url, exc_info=True)
        return {"available": False, "reason": "parse_error"}


def promotion_evidence(
    data: dict[str, Any],
    *,
    override: str | None,
    shadow_since_iso: str | None,
    now=None,
    min_days: float = 7.0,
    min_ratio: float = 0.2,
) -> dict[str, Any]:
    """Pure rollup -> promotion recommendation (guided promotion card).

    ``ready`` iff the pipeline is in shadow, shadow has run >= ``min_days``,
    and the would-save ratio is >= ``min_ratio``. All inputs explicit so this
    is trivially testable; the route supplies the DB-stored override and
    shadow-since timestamp.
    """
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)
    shadow_days = None
    if shadow_since_iso:
        try:
            shadow_days = (now - datetime.fromisoformat(shadow_since_iso)).total_seconds() / 86400.0
        except ValueError:
            shadow_days = None
    gate = data.get("gate") or {}
    esc = gate.get("escalations") or 0
    would = gate.get("shadow_would_suppress") or 0
    ratio = (would / (esc + would)) if (esc + would) > 0 else None
    return {
        "override": override,
        "shadow_since": shadow_since_iso or None,
        "shadow_days": None if shadow_days is None else round(shadow_days, 2),
        "would_save_ratio": None if ratio is None else round(ratio, 3),
        "ready": bool(
            data.get("mode") == "shadow"
            and shadow_days is not None and shadow_days >= min_days
            and ratio is not None and ratio >= min_ratio
        ),
    }
