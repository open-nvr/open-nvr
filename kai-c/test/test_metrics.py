# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Metrics collector tests (observability spec §05) — the tolerant
Prometheus text parser, the ring-buffer rollup percentile math, the
registry's /metrics scrape on the poll, and the
``GET /api/v1/adapters/{name}/metrics`` endpoint."""
from __future__ import annotations

import importlib
import math
import sys
from pathlib import Path

import httpx
import pytest

from kai_c.audit import AuditStore
from kai_c.metrics import (
    MetricsRollup,
    histogram_quantile,
    parse_adapter_metrics,
)
from kai_c.registry import AdapterRegistry

METRICS_TEXT = """\
# HELP adapter_infer_latency_seconds Inference latency.
# TYPE adapter_infer_latency_seconds histogram
adapter_infer_latency_seconds_bucket{le="0.01"} 0
adapter_infer_latency_seconds_bucket{le="0.025"} 40
adapter_infer_latency_seconds_bucket{le="0.05"} 90
adapter_infer_latency_seconds_bucket{le="0.1"} 99
adapter_infer_latency_seconds_bucket{le="+Inf"} 100
adapter_infer_latency_seconds_sum 3.5
adapter_infer_latency_seconds_count 100
# TYPE adapter_infer_total counter
adapter_infer_total{outcome="ok"} 96
adapter_infer_total{outcome="model_error"} 3
adapter_infer_total{outcome="transport_error"} 1
adapter_inflight_requests 2
adapter_queue_depth 5
"""


# ── Parser ─────────────────────────────────────────────────────────


def test_parse_extracts_histogram_buckets_sum_count():
    sample = parse_adapter_metrics(METRICS_TEXT)
    assert sample.latency_buckets == {
        0.01: 0.0, 0.025: 40.0, 0.05: 90.0, 0.1: 99.0, math.inf: 100.0,
    }
    assert sample.latency_sum == 3.5
    assert sample.latency_count == 100.0


def test_parse_extracts_outcomes_and_gauges():
    sample = parse_adapter_metrics(METRICS_TEXT)
    assert sample.outcomes == {"ok": 96.0, "model_error": 3.0, "transport_error": 1.0}
    assert sample.inflight == 2
    assert sample.queue_depth == 5


def test_parse_ignores_unknown_metrics_and_malformed_lines():
    text = (
        "process_cpu_seconds_total 12.5\n"
        "adapter_infer_total{outcome=\"ok\"} 7\n"
        "this line is garbage\n"
        "adapter_infer_latency_seconds_bucket{le=\"not-a-number\"} 3\n"
        "adapter_infer_latency_seconds_bucket 9\n"          # missing le label
        "adapter_queue_depth not_a_value\n"
        "{no_name=\"x\"} 1\n"
    )
    sample = parse_adapter_metrics(text)
    assert sample.outcomes == {"ok": 7.0}
    assert sample.latency_buckets == {}
    assert sample.queue_depth is None


def test_parse_tolerates_extra_labels_and_timestamps():
    text = (
        'adapter_infer_latency_seconds_bucket{task="detect",le="0.5"} 4 1719900000\n'
        'adapter_infer_total{outcome="ok",task="detect"} 4 1719900000\n'
    )
    sample = parse_adapter_metrics(text)
    assert sample.latency_buckets == {0.5: 4.0}
    assert sample.outcomes == {"ok": 4.0}


def test_parse_empty_text_yields_empty_sample():
    sample = parse_adapter_metrics("")
    assert sample.latency_buckets == {}
    assert sample.outcomes == {}
    assert sample.inflight is None
    assert sample.queue_depth is None


# ── Percentile math ────────────────────────────────────────────────


def test_histogram_quantile_linear_interpolation():
    buckets = {0.01: 0.0, 0.025: 40.0, 0.05: 90.0, 0.1: 99.0, math.inf: 100.0}
    assert histogram_quantile(buckets, 0.50) == pytest.approx(0.030)
    assert histogram_quantile(buckets, 0.95) == pytest.approx(0.05 + 0.05 * 5 / 9)
    assert histogram_quantile(buckets, 0.99) == pytest.approx(0.1)


def test_histogram_quantile_clamps_inf_bucket_to_highest_finite_bound():
    # All the p100 mass sits in +Inf — clamp to 0.1 like Prometheus does.
    buckets = {0.1: 0.0, math.inf: 10.0}
    assert histogram_quantile(buckets, 0.99) == pytest.approx(0.1)


def test_histogram_quantile_empty_or_zero_is_none():
    assert histogram_quantile({}, 0.5) is None
    assert histogram_quantile({0.1: 0.0, math.inf: 0.0}, 0.5) is None


# ── Rollup ─────────────────────────────────────────────────────────


def test_rollup_snapshot_with_no_samples_is_all_null():
    rollup = MetricsRollup()
    snap = rollup.snapshot("ghost")
    assert snap == {
        "adapter": "ghost",
        "window_s": 3600,
        "latency_ms": {"p50": None, "p95": None, "p99": None},
        "outcomes": {},
        "inflight": None,
        "queue_depth": None,
        "hardware": {"cpu_percent": None, "memory_bytes": None,
                     "gpu_utilization": None, "gpu_memory_bytes": None},
        "series": [],
        "fingerprint_changes": [],
        "samples": 0,
    }


def test_rollup_single_sample_uses_cumulative_values():
    rollup = MetricsRollup()
    rollup.record_sample("yolov8", parse_adapter_metrics(METRICS_TEXT))
    snap = rollup.snapshot("yolov8")
    assert snap["samples"] == 1
    assert snap["latency_ms"]["p50"] == pytest.approx(30.0)
    assert snap["latency_ms"]["p95"] == pytest.approx(77.778, abs=0.001)
    assert snap["latency_ms"]["p99"] == pytest.approx(100.0)
    assert snap["outcomes"] == {"ok": 96, "model_error": 3, "transport_error": 1}
    assert snap["inflight"] == 2
    assert snap["queue_depth"] == 5


def test_rollup_window_is_delta_between_oldest_and_newest():
    rollup = MetricsRollup()
    older = (
        'adapter_infer_latency_seconds_bucket{le="0.05"} 10\n'
        'adapter_infer_latency_seconds_bucket{le="+Inf"} 10\n'
        'adapter_infer_total{outcome="ok"} 10\n'
        "adapter_inflight_requests 1\n"
    )
    newer = (
        'adapter_infer_latency_seconds_bucket{le="0.05"} 30\n'
        'adapter_infer_latency_seconds_bucket{le="+Inf"} 40\n'
        'adapter_infer_total{outcome="ok"} 35\n'
        'adapter_infer_total{outcome="model_error"} 5\n'
        "adapter_inflight_requests 4\n"
    )
    rollup.record_sample("yolov8", parse_adapter_metrics(older))
    rollup.record_sample("yolov8", parse_adapter_metrics(newer))
    snap = rollup.snapshot("yolov8")
    assert snap["samples"] == 2
    # Delta buckets: 20 in ≤0.05s, 30 total → p50 rank 15 interpolates
    # to 0.05 * 15/20 = 0.0375 s.
    assert snap["latency_ms"]["p50"] == pytest.approx(37.5)
    # Outcomes are windowed counts (35−10, 5−0); gauges are latest.
    assert snap["outcomes"] == {"ok": 25, "model_error": 5}
    assert snap["inflight"] == 4


def test_rollup_counter_reset_falls_back_to_newest_cumulative():
    """An adapter restart drops its counters to ~0 — the window falls
    back to 'since restart' rather than reporting negative counts."""
    rollup = MetricsRollup()
    before_restart = (
        'adapter_infer_latency_seconds_bucket{le="0.05"} 500\n'
        'adapter_infer_latency_seconds_bucket{le="+Inf"} 500\n'
        'adapter_infer_total{outcome="ok"} 500\n'
    )
    after_restart = (
        'adapter_infer_latency_seconds_bucket{le="0.05"} 8\n'
        'adapter_infer_latency_seconds_bucket{le="+Inf"} 8\n'
        'adapter_infer_total{outcome="ok"} 8\n'
    )
    rollup.record_sample("yolov8", parse_adapter_metrics(before_restart))
    rollup.record_sample("yolov8", parse_adapter_metrics(after_restart))
    snap = rollup.snapshot("yolov8")
    assert snap["outcomes"] == {"ok": 8}
    assert snap["latency_ms"]["p50"] == pytest.approx(0.05 * (4 / 8) * 1000)


def test_rollup_ring_buffer_is_bounded_to_60_samples():
    rollup = MetricsRollup()
    for i in range(90):
        text = (
            f'adapter_infer_latency_seconds_bucket{{le="+Inf"}} {i}\n'
            f'adapter_infer_total{{outcome="ok"}} {i}\n'
        )
        rollup.record_sample("yolov8", parse_adapter_metrics(text))
    snap = rollup.snapshot("yolov8")
    assert snap["samples"] == 60
    # Oldest surviving sample is i=30 → window count is 89−30.
    assert snap["outcomes"] == {"ok": 59}


def test_rollup_fingerprint_changes_are_iso_timestamps():
    rollup = MetricsRollup()
    rollup.record_fingerprint_change("yolov8", at=1_750_000_000.0)
    snap = rollup.snapshot("yolov8")
    assert snap["fingerprint_changes"] == ["2025-06-15T15:06:40+00:00"]


def test_rollup_forget_drops_series():
    rollup = MetricsRollup()
    rollup.record_sample("yolov8", parse_adapter_metrics(METRICS_TEXT))
    rollup.forget("yolov8")
    assert rollup.snapshot("yolov8")["samples"] == 0


# ── Registry scrape on the poll ────────────────────────────────────


def _base_caps(*, fingerprint: str = "sha256:aaa") -> dict:
    return {
        "adapter": {
            "name": "test-adapter", "version": "1.0.0", "vendor": "open-nvr",
            "license": "AGPL-3.0", "supported_contract_versions": ["1"],
        },
        "model": {
            "name": "m1", "version": "v1", "framework": "f", "fingerprint": fingerprint,
        },
        "endpoints": {
            "infer": {"supported": True, "input_content_types": ["application/json"]},
            "infer_stream": {"supported": False},
        },
        "tasks_advertised": ["echo"],
        "permissions": {
            "gpu": False, "network_egress": [],
            "host_filesystem": [], "shared_memory_paths": [], "host_metadata": False,
        },
        "scheduling": {"max_inflight": 8, "preferred_batch_size": 1, "fair_queuing": "none"},
        "cost": {"currency": "USD", "estimated_per_call": 0.0, "estimated_per_hour": 0.0,
                 "rate_limit_per_minute": None, "is_metered": False},
    }


class _StubAdapter:
    """Contract-ish adapter stub that also serves /metrics."""

    def __init__(self, url: str = "http://127.0.0.1:9100") -> None:
        self.url = url
        self._caps = _base_caps()
        self.metrics_text: str | None = METRICS_TEXT

    def update_capabilities(self, caps: dict) -> None:
        self._caps = caps

    async def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/capabilities":
            return httpx.Response(200, json=self._caps)
        if path == "/health":
            return httpx.Response(200, json={
                "status": "ok",
                "adapter_name": "test-adapter", "adapter_version": "1.0.0",
                "model_name": "m1", "model_version": "v1",
                "started_at": "2026-05-19T00:00:00Z", "uptime_seconds": 1,
            })
        if path == "/metrics":
            if self.metrics_text is None:
                return httpx.Response(404)
            return httpx.Response(
                200, text=self.metrics_text,
                headers={"Content-Type": "text/plain; version=0.0.4"},
            )
        if path == "/infer":
            return httpx.Response(200, json={"status": "ok", "result": {}})
        return httpx.Response(404)


@pytest.fixture
def audit(tmp_path: Path) -> AuditStore:
    return AuditStore(path=str(tmp_path / "audit.jsonl"))


@pytest.fixture
def adapter_stub() -> _StubAdapter:
    return _StubAdapter()


@pytest.fixture
async def registry(audit, adapter_stub):
    transport = httpx.MockTransport(adapter_stub.respond)
    client = httpx.AsyncClient(transport=transport)
    reg = AdapterRegistry(
        sovereignty_mode="local_only", audit=audit, http_client=client,
        poll_interval_seconds=999,  # disable auto-poll
    )
    yield reg
    await reg.aclose()


@pytest.mark.asyncio
async def test_refresh_scrapes_metrics_into_rollup(registry, adapter_stub):
    await registry.register("yolov8", adapter_stub.url)
    assert registry.metrics.snapshot("yolov8")["samples"] == 0  # scrape rides the poll
    await registry.refresh("yolov8")
    snap = registry.metrics.snapshot("yolov8")
    assert snap["samples"] == 1
    assert snap["latency_ms"]["p50"] == pytest.approx(30.0)
    assert snap["outcomes"]["ok"] == 96


@pytest.mark.asyncio
async def test_refresh_tolerates_missing_metrics_endpoint(registry, adapter_stub):
    adapter_stub.metrics_text = None  # adapter has no /metrics
    await registry.register("yolov8", adapter_stub.url)
    await registry.refresh("yolov8")
    snap = registry.metrics.snapshot("yolov8")
    assert snap["samples"] == 0
    assert snap["latency_ms"] == {"p50": None, "p95": None, "p99": None}
    # The rest of the poll still ran.
    assert registry.get("yolov8") is not None


@pytest.mark.asyncio
async def test_fingerprint_drift_lands_in_rollup_timeline(registry, adapter_stub):
    await registry.register("yolov8", adapter_stub.url)
    adapter_stub.update_capabilities(_base_caps(fingerprint="sha256:bbb"))
    await registry.refresh("yolov8")
    changes = registry.metrics.snapshot("yolov8")["fingerprint_changes"]
    assert len(changes) == 1
    assert changes[0].startswith("20")  # ISO-8601 timestamp


@pytest.mark.asyncio
async def test_deregister_drops_metrics_series(registry, adapter_stub):
    await registry.register("yolov8", adapter_stub.url)
    await registry.refresh("yolov8")
    await registry.deregister("yolov8")
    assert registry.metrics.snapshot("yolov8")["samples"] == 0


# ── HTTP endpoint ──────────────────────────────────────────────────


@pytest.fixture
def kaic_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The FastAPI app with the metrics-serving stub adapter wired in —
    same pattern as test_main_v2.py."""
    monkeypatch.setenv("AI_SOVEREIGNTY", "local_only")
    monkeypatch.setenv("ADAPTER_URL", "http://127.0.0.1:9100")
    monkeypatch.setenv("KAI_C_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("INTERNAL_API_KEY", "")
    # Internal surface fails closed on an unset key (c6d7b1f); run these
    # functional tests as an authorised local-dev box via the explicit opt-in.
    monkeypatch.setenv("KAI_C_ALLOW_ANONYMOUS", "true")

    if "main" in sys.modules:
        importlib.reload(sys.modules["main"])
    import main as kaic_main

    stub = _StubAdapter()
    transport = httpx.MockTransport(stub.respond)

    original_init = kaic_main.AdapterRegistry.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["http_client"] = httpx.AsyncClient(transport=transport)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(kaic_main.AdapterRegistry, "__init__", patched_init)

    from fastapi.testclient import TestClient

    with TestClient(kaic_main.app) as client:
        yield client, stub


def test_metrics_endpoint_unknown_adapter_is_404(kaic_app):
    client, _ = kaic_app
    response = client.get("/api/v1/adapters/nonexistent/metrics")
    assert response.status_code == 404


def test_metrics_endpoint_all_null_before_first_scrape(kaic_app):
    client, _ = kaic_app
    client.post("/api/v1/adapters/register", json={"name": "stub-x", "url": "http://127.0.0.1:9100"})
    response = client.get("/api/v1/adapters/stub-x/metrics")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["adapter"] == "stub-x"
    assert body["window_s"] == 3600
    assert body["latency_ms"] == {"p50": None, "p95": None, "p99": None}
    assert body["outcomes"] == {}
    assert body["inflight"] is None
    assert body["queue_depth"] is None
    assert body["fingerprint_changes"] == []
    assert body["samples"] == 0
    # max_inflight comes from /capabilities even before any scrape.
    assert body["max_inflight"] == 8


def test_metrics_endpoint_serves_rollup_after_refresh(kaic_app):
    client, _ = kaic_app
    client.post("/api/v1/adapters/register", json={"name": "stub-x", "url": "http://127.0.0.1:9100"})
    client.post("/api/v1/adapters/refresh?name=stub-x")
    response = client.get("/api/v1/adapters/stub-x/metrics")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["samples"] == 1
    assert body["latency_ms"]["p50"] == pytest.approx(30.0)
    assert body["latency_ms"]["p95"] == pytest.approx(77.778, abs=0.001)
    assert body["latency_ms"]["p99"] == pytest.approx(100.0)
    assert body["outcomes"] == {"ok": 96, "model_error": 3, "transport_error": 1}
    assert body["inflight"] == 2
    assert body["max_inflight"] == 8
    assert body["queue_depth"] == 5


def test_parser_reads_optional_hardware_gauges():
    text = """
adapter_infer_latency_seconds_bucket{le="0.1"} 5
adapter_infer_latency_seconds_bucket{le="+Inf"} 5
adapter_infer_total{outcome="ok"} 5
adapter_inflight_requests 2
adapter_process_cpu_percent 137.5
adapter_process_memory_bytes 1073741824
adapter_gpu_utilization 62.0
adapter_gpu_memory_bytes 2147483648
"""
    sample = parse_adapter_metrics(text, scraped_at=100.0)
    assert sample.cpu_percent == 137.5
    assert sample.memory_bytes == 1073741824.0
    assert sample.gpu_utilization == 62.0
    assert sample.gpu_memory_bytes == 2147483648.0


def test_snapshot_series_and_hardware():
    """The series carries per-interval rate + p95 and point-in-time
    gauges — the UI's 'over the period of time' sparklines."""
    rollup = MetricsRollup()
    t0 = 1000.0
    for i in range(3):
        text = f"""
adapter_infer_latency_seconds_bucket{{le="0.1"}} {10*(i+1)}
adapter_infer_latency_seconds_bucket{{le="+Inf"}} {10*(i+1)}
adapter_infer_total{{outcome="ok"}} {60*(i+1)}
adapter_inflight_requests {i}
adapter_process_cpu_percent {50+i}
"""
        rollup.record_sample("a", parse_adapter_metrics(text, scraped_at=t0 + i*60))
    snap = rollup.snapshot("a")
    assert len(snap["series"]) == 3
    first, second = snap["series"][0], snap["series"][1]
    assert first["rpm"] is None                  # no prior sample
    assert second["rpm"] == 60.0                 # 60 requests / 1 min
    assert second["p95_ms"] is not None
    assert snap["series"][2]["cpu_percent"] == 52
    assert snap["hardware"]["cpu_percent"] == 52
    assert snap["hardware"]["gpu_utilization"] is None   # never exported


def test_fleet_metrics_endpoint(kaic_app):
    """One call for the whole fleet — each adapter's snapshot keyed by
    name, with its declared ceiling and registry status alongside."""
    client, _ = kaic_app
    client.post("/api/v1/adapters/register", json={"name": "stub-x", "url": "http://127.0.0.1:9100"})
    client.post("/api/v1/adapters/refresh?name=stub-x")
    response = client.get("/api/v1/adapters-metrics")
    assert response.status_code == 200, response.text
    fleet = response.json()["adapters"]
    assert "stub-x" in fleet
    snap = fleet["stub-x"]
    assert snap["samples"] == 1
    assert "max_inflight" in snap and "status" in snap
    assert isinstance(snap["series"], list) and len(snap["series"]) == 1
