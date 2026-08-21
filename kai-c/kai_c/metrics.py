# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
KAI-C metrics collector — the thin collector from the capabilities &
observability spec §05.

The registry already polls every adapter every 60s for /health +
/capabilities; this module is the "/metrics on that same poll" half.
It has two parts:

* :func:`parse_adapter_metrics` — a small, tolerant Prometheus
  text-format parser. It extracts ONLY the contract metrics KAI-C
  cares about (``adapter_infer_latency_seconds`` histogram,
  ``adapter_infer_total{outcome}``, ``adapter_inflight_requests``,
  ``adapter_queue_depth``) and ignores everything else — unknown
  metrics, malformed lines, HELP/TYPE comments. No new dependencies.

* :class:`MetricsRollup` — a deliberately dumb per-adapter rollup
  store per the spec: a bounded ring buffer of the last
  ``MAX_SAMPLES`` scrape samples (60 samples × the 60s poll ≈ 1h) and
  a fixed window, not a mini-TSDB growing inside KAI-C. Long
  retention stays Prometheus's job — the /metrics format means an
  operator can bolt Prometheus + Grafana on later for free.

The rollup derives, over the window:

* p50/p95/p99 latency in ms — Prometheus-style linear interpolation
  over the histogram bucket deltas (newest − oldest sample; counter
  resets fall back to the newest cumulative values);
* per-outcome counts from ``adapter_infer_total``;
* the latest saturation gauges (``inflight``, ``queue_depth``);
* the fingerprint-change timeline (the registry already detects the
  drift; it records the timestamps here).
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# 60 samples on the 60s registry poll ≈ 1 hour of history.
MAX_SAMPLES: int = 60
WINDOW_SECONDS: int = 3600
MAX_FINGERPRINT_CHANGES: int = 60

_HISTOGRAM = "adapter_infer_latency_seconds"
_OUTCOMES = "adapter_infer_total"
_INFLIGHT = "adapter_inflight_requests"
_QUEUE_DEPTH = "adapter_queue_depth"
# OPTIONAL hardware gauges (observability spec §05 addendum): adapters
# MAY export these; absent families simply stay null in the rollup —
# no adapter is required to ship psutil/NVML to stay conformant.
_CPU_PERCENT = "adapter_process_cpu_percent"
_MEM_BYTES = "adapter_process_memory_bytes"
_GPU_UTIL = "adapter_gpu_utilization"
_GPU_MEM_BYTES = "adapter_gpu_memory_bytes"


# ── Prometheus text parsing ────────────────────────────────────────


def _parse_labels(raw: str) -> dict[str, str]:
    """Parse a Prometheus label body (``key="value",...``) tolerantly.

    Handles quoted values containing commas and escaped quotes. Any
    malformed remainder is skipped rather than raised.
    """
    labels: dict[str, str] = {}
    i = 0
    n = len(raw)
    while i < n:
        eq = raw.find("=", i)
        if eq < 0:
            break
        key = raw[i:eq].strip().strip(",").strip()
        j = eq + 1
        # Skip whitespace before the opening quote.
        while j < n and raw[j] in " \t":
            j += 1
        if j >= n or raw[j] != '"':
            # Unquoted value — not valid Prometheus, skip to next comma.
            nxt = raw.find(",", j)
            if nxt < 0:
                break
            i = nxt + 1
            continue
        j += 1
        value_chars: list[str] = []
        while j < n:
            ch = raw[j]
            if ch == "\\" and j + 1 < n:
                value_chars.append(raw[j + 1])
                j += 2
                continue
            if ch == '"':
                break
            value_chars.append(ch)
            j += 1
        if key:
            labels[key] = "".join(value_chars)
        # Move past closing quote and any trailing comma.
        j += 1
        while j < n and raw[j] in ", \t":
            j += 1
        i = j
    return labels


def _split_metric_line(line: str) -> tuple[str, dict[str, str], float] | None:
    """Split one exposition line into (name, labels, value).

    Returns ``None`` for comments, blanks, and anything malformed —
    the parser is deliberately lenient (spec §05: collect what's
    there, never crash the poll on a weird adapter).
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "{" in line:
        brace_open = line.index("{")
        brace_close = line.rfind("}")
        if brace_close < brace_open:
            return None
        name = line[:brace_open].strip()
        labels = _parse_labels(line[brace_open + 1 : brace_close])
        rest = line[brace_close + 1 :].strip()
    else:
        parts = line.split()
        if len(parts) < 2:
            return None
        name = parts[0]
        labels = {}
        rest = " ".join(parts[1:])
    if not name:
        return None
    # ``rest`` is "<value> [timestamp]" — take the first token.
    value_token = rest.split()[0] if rest.split() else ""
    try:
        value = float(value_token)
    except ValueError:
        return None
    return name, labels, value


@dataclass
class MetricsSample:
    """One /metrics scrape, reduced to the contract signals."""

    scraped_at: float
    # Cumulative histogram buckets: upper bound (seconds) → count.
    # +Inf is represented as math.inf.
    latency_buckets: dict[float, float] = field(default_factory=dict)
    latency_sum: float | None = None
    latency_count: float | None = None
    # Cumulative per-outcome counters: outcome label → count.
    outcomes: dict[str, float] = field(default_factory=dict)
    # Point-in-time gauges.
    inflight: int | None = None
    queue_depth: int | None = None
    # Optional hardware gauges (None when the adapter doesn't export them).
    cpu_percent: float | None = None
    memory_bytes: float | None = None
    gpu_utilization: float | None = None
    gpu_memory_bytes: float | None = None
    # Model identity labels from ``adapter_model_info`` (SDK ≥1.2):
    # adapter/model/version/framework/fingerprint. Empty when absent.
    model_info: dict[str, str] = field(default_factory=dict)
    # Adapter-defined DOMAIN series (SDK ≥1.2): any other ``adapter_*``
    # counter/gauge/sum/count line, keyed by its full series identity
    # (name plus one optional label, e.g. ``adapter_detections_total{label="person"}``).
    # Buckets are skipped — bounded by what the adapter registered.
    domain: dict[str, float] = field(default_factory=dict)


def parse_adapter_metrics(text: str, *, scraped_at: float | None = None) -> MetricsSample:
    """Parse Prometheus exposition text into a :class:`MetricsSample`.

    Only the four contract metric families are read; everything else
    (including malformed lines) is ignored.
    """
    sample = MetricsSample(scraped_at=scraped_at if scraped_at is not None else time.time())
    for line in text.splitlines():
        parsed = _split_metric_line(line)
        if parsed is None:
            continue
        name, labels, value = parsed
        if name == f"{_HISTOGRAM}_bucket":
            le_raw = labels.get("le")
            if le_raw is None:
                continue
            try:
                le = math.inf if le_raw in ("+Inf", "Inf", "inf") else float(le_raw)
            except ValueError:
                continue
            # SDK ≥1.2 splits the histogram by a ``task`` label — one
            # series per task, same ``le`` bounds. SUM across them (an
            # assignment here would keep only the last task's counts and
            # silently corrupt every percentile).
            sample.latency_buckets[le] = sample.latency_buckets.get(le, 0.0) + value
        elif name == f"{_HISTOGRAM}_sum":
            sample.latency_sum = (sample.latency_sum or 0.0) + value
        elif name == f"{_HISTOGRAM}_count":
            sample.latency_count = (sample.latency_count or 0.0) + value
        elif name == _OUTCOMES:
            outcome = labels.get("outcome")
            if outcome:
                sample.outcomes[outcome] = sample.outcomes.get(outcome, 0.0) + value
        elif name == _INFLIGHT:
            sample.inflight = int(value)
        elif name == _QUEUE_DEPTH:
            sample.queue_depth = int(value)
        elif name == _CPU_PERCENT:
            sample.cpu_percent = value
        elif name == _MEM_BYTES:
            sample.memory_bytes = value
        elif name == _GPU_UTIL:
            sample.gpu_utilization = value
        elif name == _GPU_MEM_BYTES:
            sample.gpu_memory_bytes = value
        elif name == "adapter_model_info":
            # Identity is in the labels; the value is always 1.
            sample.model_info = dict(labels)
        elif (
            name.startswith("adapter_")
            and not name.endswith("_bucket")
            # standard-but-uninteresting gauges stay out of the domain map
            and name not in ("adapter_model_loaded", "adapter_stream_connections_active")
        ):
            # Adapter-defined DOMAIN series (detections by class, audio
            # seconds, RTF sums...). Key by name plus the one meaningful
            # label if present, so per-class counters stay distinct.
            # Bounded: adapters register these with closed label sets.
            meaningful = {k: v for k, v in labels.items() if k != "le"}
            if meaningful:
                k, v = sorted(meaningful.items())[0]
                key = f'{name}{{{k}="{v}"}}'
            else:
                key = name
            sample.domain[key] = sample.domain.get(key, 0.0) + value
    return sample


# ── Percentile math ────────────────────────────────────────────────


def histogram_quantile(buckets: dict[float, float], quantile: float) -> float | None:
    """Prometheus-style ``histogram_quantile`` over cumulative buckets.

    ``buckets`` maps upper bound (seconds; +Inf as math.inf) →
    cumulative count. Linear interpolation inside the target bucket;
    the +Inf bucket clamps to the highest finite bound (same behaviour
    as Prometheus). Returns ``None`` when there's no data.
    """
    if not buckets:
        return None
    bounds = sorted(buckets)
    total = buckets[bounds[-1]]
    if total <= 0:
        return None
    rank = quantile * total
    prev_bound = 0.0
    prev_count = 0.0
    finite = [b for b in bounds if not math.isinf(b)]
    for bound in bounds:
        count = buckets[bound]
        if count >= rank:
            if math.isinf(bound):
                # Observation beyond the last finite bucket — clamp.
                return finite[-1] if finite else None
            if count == prev_count:
                return bound
            return prev_bound + (bound - prev_bound) * (rank - prev_count) / (count - prev_count)
        prev_bound = 0.0 if math.isinf(bound) else bound
        prev_count = count
    return finite[-1] if finite else None


def _bucket_delta(newest: dict[float, float], oldest: dict[float, float]) -> dict[float, float]:
    """Windowed histogram: newest − oldest cumulative buckets.

    A negative delta anywhere means the adapter's counters reset
    (restart) inside the window — fall back to the newest cumulative
    values, which is exactly the "since restart" window. Deliberately
    dumb, per the spec.
    """
    delta: dict[float, float] = {}
    for bound, count in newest.items():
        d = count - oldest.get(bound, 0.0)
        if d < 0:
            return dict(newest)
        delta[bound] = d
    return delta


def _outcome_delta(newest: dict[str, float], oldest: dict[str, float]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for outcome, count in newest.items():
        d = count - oldest.get(outcome, 0.0)
        if d < 0:  # counter reset — fall back to cumulative
            return {k: int(v) for k, v in newest.items()}
        counts[outcome] = int(d)
    return counts


# ── KAI-C's OWN Prometheus exposition (client-side vantage point) ──
#
# Everything above is what ADAPTERS report about themselves. This is the
# other half of the observability story: what KAI-C — the client every
# governed inference flows through — observes. Proxy-side latency
# includes the network hop and any queueing in front of the adapter, and
# it keeps working when an adapter is too wedged to answer its own
# /metrics. Rendered at KAI-C's GET /metrics for external Prometheus.

_PROXY_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0,
)
_PROXY_RESULTS: tuple[str, ...] = ("ok", "http_error", "exception", "refused")


def _esc_label(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class ProxyMetrics:
    """Per-adapter counters + latency histogram for KAI-C's proxied
    inference calls, measured around the HTTP hop to the adapter.
    Bounded: one label (adapter name), series only for adapters that
    actually served traffic. Thread-safe (recorded from the async route,
    rendered from a sync handler)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # adapter → result → count
        self._totals: dict[str, dict[str, float]] = {}
        # adapter → {"buckets": {ub: n}, "inf": n, "sum": s, "count": n}
        self._lat: dict[str, dict] = {}

    def record(self, adapter: str, result: str, latency_seconds: float | None) -> None:
        if result not in _PROXY_RESULTS:
            result = "exception"
        with self._lock:
            totals = self._totals.setdefault(
                adapter, {r: 0.0 for r in _PROXY_RESULTS})
            totals[result] += 1
            if latency_seconds is not None:
                hist = self._lat.get(adapter)
                if hist is None:
                    hist = self._lat[adapter] = {
                        "buckets": {ub: 0 for ub in _PROXY_BUCKETS},
                        "inf": 0, "sum": 0.0, "count": 0,
                    }
                for ub in _PROXY_BUCKETS:
                    if latency_seconds <= ub:
                        hist["buckets"][ub] += 1
                hist["inf"] += 1
                hist["sum"] += latency_seconds
                hist["count"] += 1

    def render(self, adapter_summaries: list[dict[str, Any]] | None = None) -> str:
        """Prometheus text exposition: proxy series plus, when the
        registry's summaries are supplied, per-adapter up/health gauges
        (so a scrape of KAI-C alone answers 'is the fleet alive')."""
        lines: list[str] = []
        with self._lock:
            lines.append(
                "# HELP kaic_proxy_infer_total Proxied /infer calls by adapter and result.")
            lines.append("# TYPE kaic_proxy_infer_total counter")
            for adapter in sorted(self._totals):
                for result in _PROXY_RESULTS:
                    lines.append(
                        f'kaic_proxy_infer_total{{adapter="{_esc_label(adapter)}",result="{result}"}} '
                        f"{self._totals[adapter][result]}"
                    )
            lines.append(
                "# HELP kaic_proxy_infer_latency_seconds Client-observed latency of "
                "proxied inference (network + adapter queue + inference).")
            lines.append("# TYPE kaic_proxy_infer_latency_seconds histogram")
            for adapter in sorted(self._lat):
                hist = self._lat[adapter]
                al = f'adapter="{_esc_label(adapter)}"'
                for ub in _PROXY_BUCKETS:
                    lines.append(
                        f'kaic_proxy_infer_latency_seconds_bucket{{{al},le="{ub}"}} '
                        f"{hist['buckets'][ub]}"
                    )
                lines.append(
                    f'kaic_proxy_infer_latency_seconds_bucket{{{al},le="+Inf"}} {hist["inf"]}')
                lines.append(f'kaic_proxy_infer_latency_seconds_sum{{{al}}} {hist["sum"]}')
                lines.append(f'kaic_proxy_infer_latency_seconds_count{{{al}}} {hist["count"]}')
        if adapter_summaries:
            lines.append("# HELP kaic_adapter_up 1 when the adapter's last health probe was ok.")
            lines.append("# TYPE kaic_adapter_up gauge")
            fail_lines: list[str] = []
            for summary in adapter_summaries:
                name = summary.get("name")
                if not name:
                    continue
                al = f'adapter="{_esc_label(str(name))}"'
                up = 1 if summary.get("health_status") == "ok" else 0
                lines.append(f"kaic_adapter_up{{{al}}} {up}")
                fail_lines.append(
                    f"kaic_adapter_consecutive_health_failures{{{al}}} "
                    f"{int(summary.get('consecutive_health_failures') or 0)}"
                )
            if fail_lines:
                lines.append(
                    "# HELP kaic_adapter_consecutive_health_failures Consecutive failed health probes (3+ = unavailable).")
                lines.append("# TYPE kaic_adapter_consecutive_health_failures gauge")
                lines.extend(fail_lines)
        return "\n".join(lines) + "\n"


# Module-global singleton (mirrors the registry's rollup pattern).
proxy_metrics = ProxyMetrics()


# ── Rollup store ───────────────────────────────────────────────────


@dataclass
class _AdapterSeries:
    samples: deque[MetricsSample] = field(
        default_factory=lambda: deque(maxlen=MAX_SAMPLES)
    )
    fingerprint_changes: deque[str] = field(
        default_factory=lambda: deque(maxlen=MAX_FINGERPRINT_CHANGES)
    )


class MetricsRollup:
    """Bounded in-memory rollup, one ring buffer per adapter.

    Thread-safe the same way the registry is: a plain lock around the
    state dict so synchronous readers (the FastAPI handler) and the
    async poll loop don't race.
    """

    def __init__(self) -> None:
        self._series: dict[str, _AdapterSeries] = {}
        self._lock = threading.Lock()

    def _get_or_create(self, adapter: str) -> _AdapterSeries:
        series = self._series.get(adapter)
        if series is None:
            series = _AdapterSeries()
            self._series[adapter] = series
        return series

    def record_sample(self, adapter: str, sample: MetricsSample) -> None:
        with self._lock:
            self._get_or_create(adapter).samples.append(sample)

    def record_fingerprint_change(self, adapter: str, *, at: float | None = None) -> None:
        ts = datetime.fromtimestamp(
            at if at is not None else time.time(), tz=timezone.utc
        ).isoformat()
        with self._lock:
            self._get_or_create(adapter).fingerprint_changes.append(ts)

    def forget(self, adapter: str) -> None:
        """Drop an adapter's series (called on deregistration) so the
        store stays bounded by the number of LIVE adapters."""
        with self._lock:
            self._series.pop(adapter, None)

    def snapshot(self, adapter: str) -> dict[str, Any]:
        """The §05 rollup for one adapter — the shape
        ``GET /api/v1/adapters/{name}/metrics`` serves. All-null fields
        when nothing has been scraped yet."""
        with self._lock:
            series = self._series.get(adapter)
            samples = list(series.samples) if series else []
            fingerprint_changes = list(series.fingerprint_changes) if series else []

        latency_ms: dict[str, float | None] = {"p50": None, "p95": None, "p99": None}
        outcomes: dict[str, int] = {}
        inflight: int | None = None
        queue_depth: int | None = None

        if samples:
            newest = samples[-1]
            oldest = samples[0]
            if len(samples) > 1:
                buckets = _bucket_delta(newest.latency_buckets, oldest.latency_buckets)
                outcomes = _outcome_delta(newest.outcomes, oldest.outcomes)
            else:
                buckets = dict(newest.latency_buckets)
                outcomes = {k: int(v) for k, v in newest.outcomes.items()}
            for key, q in (("p50", 0.50), ("p95", 0.95), ("p99", 0.99)):
                seconds = histogram_quantile(buckets, q)
                latency_ms[key] = round(seconds * 1000.0, 3) if seconds is not None else None
            inflight = newest.inflight
            queue_depth = newest.queue_depth

        # Per-sample series for the UI's sparklines ("over the period of
        # time"): point-in-time gauges verbatim; rate + interval p95
        # derived from CONSECUTIVE sample pairs (counter resets → null
        # for that interval rather than a bogus negative spike).
        series: list[dict[str, Any]] = []
        prev = None
        for smp in samples:
            entry: dict[str, Any] = {
                "ts": smp.scraped_at,
                "inflight": smp.inflight,
                "queue_depth": smp.queue_depth,
                "cpu_percent": smp.cpu_percent,
                "memory_bytes": smp.memory_bytes,
                "gpu_utilization": smp.gpu_utilization,
                "gpu_memory_bytes": smp.gpu_memory_bytes,
                "rpm": None,
                "p95_ms": None,
            }
            if prev is not None and smp.scraped_at > prev.scraped_at:
                d_out = _outcome_delta(smp.outcomes, prev.outcomes)
                total = sum(d_out.values())
                span_min = (smp.scraped_at - prev.scraped_at) / 60.0
                if span_min > 0 and total >= 0:
                    entry["rpm"] = round(total / span_min, 2)
                d_buckets = _bucket_delta(smp.latency_buckets, prev.latency_buckets)
                q = histogram_quantile(d_buckets, 0.95)
                if q is not None:
                    entry["p95_ms"] = round(q * 1000.0, 3)
            series.append(entry)
            prev = smp

        # Domain series (SDK ≥1.2): windowed delta per series over the
        # sample window (counter reset → fall back to newest cumulative,
        # same posture as _outcome_delta), plus the model identity from
        # the newest scrape. ``_sum``/``_count`` pairs ride along so the
        # UI can derive windowed averages (e.g. mean realtime factor).
        domain: dict[str, float] = {}
        model_info: dict[str, str] = {}
        if samples:
            newest = samples[-1]
            oldest = samples[0]
            model_info = dict(newest.model_info)
            for key, val in newest.domain.items():
                d = val - oldest.domain.get(key, 0.0)
                domain[key] = round(val if d < 0 else d, 4)

        # Trend keys for the UI's domain sparklines — bounded to 6 series
        # so the payload stays small at 60 samples: derived averages for
        # _sum/_count pairs first (e.g. realtime factor — the efficiency
        # trend), then the busiest counters by windowed delta.
        avg_stems = sorted(
            k[:-4] for k in domain
            if k.endswith("_sum") and (k[:-4] + "_count") in domain
        )[:2]
        counter_keys = sorted(
            (k for k in domain if not k.endswith("_sum") and not k.endswith("_count")),
            key=lambda k: domain[k], reverse=True,
        )
        domain_trend_keys = (
            [s + "_avg" for s in avg_stems] + counter_keys[: 6 - len(avg_stems)]
        )

        def _domain_point(prev_s: MetricsSample, cur: MetricsSample) -> dict[str, float | None]:
            """Per-interval domain values for one series point: counter
            deltas (reset → None) and _sum/_count-derived interval
            averages (no observations in the interval → None)."""
            out: dict[str, float | None] = {}
            for key in domain_trend_keys:
                if key.endswith("_avg"):
                    stem = key[:-4]
                    ds = cur.domain.get(stem + "_sum", 0.0) - prev_s.domain.get(stem + "_sum", 0.0)
                    dc = cur.domain.get(stem + "_count", 0.0) - prev_s.domain.get(stem + "_count", 0.0)
                    out[key] = round(ds / dc, 4) if dc > 0 and ds >= 0 else None
                else:
                    cur_v = cur.domain.get(key)
                    prev_v = prev_s.domain.get(key)
                    if cur_v is None or prev_v is None or cur_v < prev_v:
                        out[key] = None
                    else:
                        out[key] = round(cur_v - prev_v, 4)
            return out

        # Second pass over the already-built series points: inject the
        # per-interval domain values (series[i] pairs samples[i-1]→[i]).
        if domain_trend_keys and len(samples) > 1:
            series[0]["domain"] = {k: None for k in domain_trend_keys}
            for i in range(1, len(samples)):
                series[i]["domain"] = _domain_point(samples[i - 1], samples[i])

        newest_hw = samples[-1] if samples else None
        return {
            "adapter": adapter,
            "window_s": WINDOW_SECONDS,
            "model_info": model_info,
            "domain": domain,
            "domain_trend_keys": domain_trend_keys,
            "latency_ms": latency_ms,
            "outcomes": outcomes,
            "inflight": inflight,
            "queue_depth": queue_depth,
            "hardware": {
                "cpu_percent": newest_hw.cpu_percent if newest_hw else None,
                "memory_bytes": newest_hw.memory_bytes if newest_hw else None,
                "gpu_utilization": newest_hw.gpu_utilization if newest_hw else None,
                "gpu_memory_bytes": newest_hw.gpu_memory_bytes if newest_hw else None,
            },
            "series": series,
            "fingerprint_changes": fingerprint_changes,
            "samples": len(samples),
        }
