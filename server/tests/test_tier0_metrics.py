# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
Tests for the Tier-0 (compute-gated) metrics scrape/reduce used by the app's
Compute-gated panel (``services/tier0_metrics.py``).

Run with:

    cd server && pytest tests/test_tier0_metrics.py -v

Coverage:

* Prometheus text parses into samples; comments/blank lines ignored.
* reduce_metrics: motion-gate ratio, per-camera summing, gate + Tier-1
  rollups, histogram avg/p95 in ms.
* Mode inference: enforce / shadow / off from which families are present.
* get_tier0_metrics: disabled config -> available:false, not an error.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
import types as _types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

# Stub core.logging_config (real module wants a writable logs/ dir).
_lm = _types.ModuleType("core.logging_config")


class _L:
    def info(self, *a, **kw): pass
    def warning(self, *a, **kw): pass
    def error(self, *a, **kw): pass
    def debug(self, *a, **kw): pass
    def critical(self, *a, **kw): pass


for _name in ("main_logger", "ai_logger"):
    setattr(_lm, _name, _L())
sys.modules["core.logging_config"] = _lm

from services.tier0_metrics import (  # noqa: E402
    get_tier0_metrics,
    parse_prometheus_text,
    reduce_metrics,
)

_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def _hist(name: str, adapter: str, samples_le: dict[float, float], total_sum: float, count: float) -> str:
    """Render a Prometheus histogram block with cumulative le-buckets."""
    lines = [f"# TYPE {name} histogram"]
    for b in _BUCKETS:
        lines.append(f'{name}_bucket{{adapter="{adapter}",le="{b}"}} {samples_le.get(b, count)}')
    lines.append(f'{name}_bucket{{adapter="{adapter}",le="+Inf"}} {count}')
    lines.append(f'{name}_sum{{adapter="{adapter}"}} {total_sum}')
    lines.append(f'{name}_count{{adapter="{adapter}"}} {count}')
    return "\n".join(lines)


ENFORCE_TEXT = "\n".join([
    "# TYPE tier0_frames_total counter",
    'tier0_frames_total{camera="cam1"} 100',
    'tier0_frames_total{camera="cam2"} 50',
    "# TYPE tier0_detector_runs_total counter",
    'tier0_detector_runs_total{camera="cam1",model="yolov8n"} 30',
    "# TYPE tier0_detections_total counter",
    'tier0_detections_total{camera="cam1",label="person"} 12',
    'tier0_detections_total{camera="cam1",label="car"} 4',
    # detector latency: count=30, sum=0.3 -> avg 10ms; all in le<=0.025 -> p95 25ms
    _hist("tier0_detector_latency_seconds", "yolov8n",
          {0.005: 0, 0.01: 15, 0.025: 30}, 0.3, 30),
    "# TYPE tier0_detector_skipped_total counter",
    'tier0_detector_skipped_total{camera="cam1",reason="no_motion"} 60',
    'tier0_detector_skipped_total{camera="cam1",reason="calibrating"} 10',
    "# TYPE gate_escalations_total counter",
    'gate_escalations_total{camera="cam1",reason="new_track"} 5',
    "# TYPE gate_suppressions_total counter",
    'gate_suppressions_total{camera="cam1",reason="stationary"} 20',
    "# TYPE tier0_process_cpu_percent gauge",
    "tier0_process_cpu_percent 42.5",
    "# TYPE tier0_process_resident_memory_bytes gauge",
    "tier0_process_resident_memory_bytes 123456789",
    "# TYPE tier1_dispatch_total counter",
    'tier1_dispatch_total{camera="cam1",adapter="caption"} 5',
    "# TYPE tier1_dispatch_inflight gauge",
    "tier1_dispatch_inflight 1",
    # operator health: 2 cams, one behind (cam2 at 2/5 fps)
    "# TYPE tier0_worker_up gauge",
    'tier0_worker_up{camera="cam1"} 1',
    'tier0_worker_up{camera="cam2"} 1',
    "# TYPE tier0_target_fps gauge",
    'tier0_target_fps{camera="cam1"} 5',
    'tier0_target_fps{camera="cam2"} 5',
    "# TYPE tier0_processing_fps gauge",
    'tier0_processing_fps{camera="cam1"} 5',
    'tier0_processing_fps{camera="cam2"} 2',
    "# TYPE tier0_worker_restarts_total counter",
    'tier0_worker_restarts_total{camera="cam2"} 3',
    # stage latency: detect avg = 0.02s -> 20ms
    _hist("tier0_stage_latency_seconds", "x", {}, 0.02, 1).replace('adapter="x"', 'camera="cam1",stage="detect"'),
    # count=5, sum=1.0 -> avg 200ms; first le>=4.75 is 0.25 -> p95 250ms
    _hist("tier1_dispatch_latency_seconds", "caption",
          {0.005: 0, 0.01: 0, 0.025: 0, 0.05: 0, 0.1: 0}, 1.0, 5),
])


def test_parse_ignores_comments_and_blanks():
    samples = parse_prometheus_text("# a comment\n\ntier0_frames_total{camera=\"c\"} 3\n")
    assert len(samples) == 1
    assert samples[0].name == "tier0_frames_total"
    assert samples[0].labels == {"camera": "c"}
    assert samples[0].value == 3.0


def test_reduce_enforce_rollup():
    r = reduce_metrics(parse_prometheus_text(ENFORCE_TEXT))
    assert r["available"] is True
    assert r["mode"] == "enforce"
    # per-camera summing
    assert r["frames"]["total"] == 150
    assert r["frames"]["detector_runs"] == 30
    assert r["frames"]["skipped_no_motion"] == 60
    assert r["frames"]["skipped_calibrating"] == 10
    # motion-gate ratio = 60 / (30 + 60), calibrating excluded
    assert abs(r["frames"]["motion_gate_ratio"] - (60 / 90)) < 1e-9
    # process gauges
    assert r["process"]["cpu_percent"] == 42.5
    assert r["process"]["memory_bytes"] == 123456789
    # gate
    assert r["gate"]["escalations"] == 5
    assert r["gate"]["suppressions"] == 20
    # tier1 histogram: avg 200ms, p95 250ms
    assert r["tier1"]["dispatched"] == 5
    assert r["tier1"]["inflight"] == 1
    assert abs(r["tier1"]["latency_avg_ms"] - 200.0) < 1e-6
    assert r["tier1"]["latency_p95_ms"] == 250.0
    # model-benchmarking signals: active model, its inference latency, per-class volume
    assert r["model"] == "yolov8n"
    assert abs(r["detector"]["latency_avg_ms"] - 10.0) < 1e-6
    assert r["detector"]["latency_p95_ms"] == 25.0
    assert r["detector"]["detections_total"] == 16
    assert r["detector"]["detections_by_class"] == {"person": 12, "car": 4}
    assert abs(r["detector"]["stage_latency_ms"]["detect"] - 20.0) < 1e-6
    # operator health: both up, cam2 behind (2/5 = 0.4), restarts summed
    assert r["health"]["workers_up"] == 2 and r["health"]["workers_total"] == 2
    assert abs(r["health"]["min_fps_ratio"] - 0.4) < 1e-9
    assert r["health"]["worst_camera"] == "cam2"
    assert r["health"]["restarts_total"] == 3


def test_mode_off_when_no_gate_metrics():
    text = "\n".join([
        'tier0_frames_total{camera="c"} 100',
        'tier0_detector_runs_total{camera="c"} 40',
        'tier0_detector_skipped_total{camera="c",reason="no_motion"} 60',
    ])
    assert reduce_metrics(parse_prometheus_text(text))["mode"] == "off"


def test_mode_shadow_when_would_suppress_present():
    text = "\n".join([
        'tier0_frames_total{camera="c"} 100',
        'gate_suppressions_total{camera="c",reason="stationary"} 10',
        'gate_shadow_would_suppress_total{camera="c"} 10',
    ])
    r = reduce_metrics(parse_prometheus_text(text))
    assert r["mode"] == "shadow"
    assert r["gate"]["shadow_would_suppress"] == 10
    assert r["tier1"]["dispatched"] == 0  # shadow dispatches nothing


def test_get_tier0_metrics_disabled_config(monkeypatch):
    from services import tier0_metrics as mod
    monkeypatch.setattr(mod.settings, "detect_pipeline_metrics_url", "", raising=False)
    out = asyncio.run(get_tier0_metrics())
    assert out == {"available": False, "reason": "disabled"}


def test_nan_and_inf_values_are_dropped_not_crashing():
    # float() accepts NaN/+Inf; they must be filtered so int() casts can't blow up.
    text = "\n".join([
        'tier0_frames_total{camera="c"} NaN',
        'tier0_detector_runs_total{camera="c"} +Inf',
        'tier0_frames_total{camera="d"} 50',
    ])
    r = reduce_metrics(parse_prometheus_text(text))
    assert r["available"] is True
    assert r["frames"]["total"] == 50           # only the finite sample survived


def test_p95_buckets_match_le_numerically_not_by_string():
    # pipeline could render le as "1" instead of "1.0"; p95 must still resolve.
    text = "\n".join([
        "tier1_dispatch_latency_seconds_bucket{adapter=\"a\",le=\"0.5\"} 0",
        "tier1_dispatch_latency_seconds_bucket{adapter=\"a\",le=\"1\"} 10",   # "1" not "1.0"
        "tier1_dispatch_latency_seconds_bucket{adapter=\"a\",le=\"+Inf\"} 10",
        "tier1_dispatch_latency_seconds_sum{adapter=\"a\"} 8.0",
        "tier1_dispatch_latency_seconds_count{adapter=\"a\"} 10",
    ])
    r = reduce_metrics(parse_prometheus_text(text))
    assert r["tier1"]["latency_p95_ms"] == 1000.0   # le=1s bucket, matched numerically


def test_quiet_enforce_not_mislabelled_as_shadow():
    # suppressions>0, no escalations/dispatch, and NO shadow counter -> enforce.
    text = "\n".join([
        'tier0_frames_total{camera="c"} 100',
        'gate_suppressions_total{camera="c",reason="stationary"} 12',
    ])
    assert reduce_metrics(parse_prometheus_text(text))["mode"] == "enforce"


# ── guided promotion: gate-mode setting endpoints ───────────────────

from datetime import datetime, timedelta, timezone

from services.tier0_metrics import promotion_evidence


def _shadow_data(esc=20, would=80):
    return {"mode": "shadow", "gate": {"escalations": esc, "shadow_would_suppress": would}}


def test_promotion_ready_after_a_week_of_meaningful_savings():
    since = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    p = promotion_evidence(_shadow_data(), override=None, shadow_since_iso=since)
    assert p["ready"] is True
    assert p["would_save_ratio"] == 0.8
    assert p["shadow_days"] >= 7.9


def test_promotion_not_ready_before_seven_days():
    since = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    p = promotion_evidence(_shadow_data(), override=None, shadow_since_iso=since)
    assert p["ready"] is False


def test_promotion_not_ready_when_savings_are_marginal():
    since = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    p = promotion_evidence(_shadow_data(esc=95, would=5), override=None,
                           shadow_since_iso=since)
    assert p["ready"] is False          # 5% would-save is not worth the risk
    assert p["would_save_ratio"] == 0.05


def test_promotion_never_ready_outside_shadow_mode():
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    data = {"mode": "enforce", "gate": {"escalations": 1, "shadow_would_suppress": 99}}
    p = promotion_evidence(data, override="enforce", shadow_since_iso=since)
    assert p["ready"] is False


def test_promotion_handles_no_data_gracefully():
    p = promotion_evidence({"mode": "shadow", "gate": {}}, override=None,
                           shadow_since_iso=None)
    assert p == {"override": None, "shadow_since": None, "shadow_days": None,
                 "would_save_ratio": None, "ready": False}
