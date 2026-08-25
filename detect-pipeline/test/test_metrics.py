# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the Tier-0 metrics registry + service-layer recorders."""
from __future__ import annotations

from dataclasses import dataclass, field

from detect_pipeline.gate import GateDecision, GateResult
import os

from detect_pipeline.metrics import (
    Metrics,
    metrics,
    record_frame,
    record_gate,
    record_processing_fps,
    record_published,
    record_worker_restart,
    record_worker_state,
    sample_process_metrics,
)


def test_counter_and_gauge():
    m = Metrics()
    m.inc("c_total", {"camera": "a"})
    m.inc("c_total", {"camera": "a"}, 2)
    m.gauge("g", 5.0, {"camera": "a"})
    assert m.value("c_total", {"camera": "a"}) == 3
    assert m.value("g", {"camera": "a"}) == 5.0
    assert m.value("missing", {"camera": "a"}) == 0.0


def test_histogram_cumulative_buckets_and_render():
    m = Metrics()
    for v in (0.02, 0.2, 3.0):
        m.observe("lat_seconds", v, {"camera": "a"})
    out = m.render()
    assert "# TYPE lat_seconds histogram" in out
    assert 'lat_seconds_count{camera="a"} 3' in out
    # le=0.05 should include only the 0.02 sample (cumulative)
    assert 'lat_seconds_bucket{camera="a",le="0.05"} 1' in out
    assert 'lat_seconds_bucket{camera="a",le="+Inf"} 3' in out


def test_render_has_type_lines_and_labels():
    m = Metrics()
    m.inc("frames_total", {"camera": "cam_3"})
    out = m.render()
    assert "# TYPE frames_total counter" in out
    assert 'frames_total{camera="cam_3"} 1' in out


# ── service-layer recorders ──

@dataclass
class _Det:
    label: str


@dataclass
class _Result:
    tracks: list = field(default_factory=list)
    regions: list = field(default_factory=list)
    calibrating: bool = False
    detections: list = field(default_factory=list)
    detect_latency_s: float | None = None


def test_record_frame_calibrating_vs_ran_vs_no_motion():
    metrics.reset()
    record_frame("a", _Result(calibrating=True))
    record_frame("a", _Result())  # no regions, not calibrating -> no_motion
    record_frame("a", _Result(regions=[(0, 0, 1, 1)], tracks=[1, 2]))  # ran; last -> gauge=2
    assert metrics.value("tier0_frames_total", {"camera": "a"}) == 3
    assert metrics.value("tier0_detector_runs_total", {"camera": "a"}) == 1
    assert metrics.value("tier0_detector_skipped_total", {"camera": "a", "reason": "calibrating"}) == 1
    assert metrics.value("tier0_detector_skipped_total", {"camera": "a", "reason": "no_motion"}) == 1
    assert metrics.value("tier0_tracks_active", {"camera": "a"}) == 2  # gauge = last frame
    metrics.reset()


def test_record_gate_escalations_and_shadow_suppress():
    metrics.reset()
    gr = GateResult(
        decisions=[
            GateDecision(1, "person", True, "new_track", True),
            GateDecision(2, "car", False, "stationary", True),
        ],
        shadow=True,
    )
    record_gate("a", gr)
    record_published("a")
    assert metrics.value("gate_escalations_total", {"camera": "a", "reason": "new_track"}) == 1
    assert metrics.value("gate_suppressions_total", {"camera": "a", "reason": "stationary"}) == 1
    assert metrics.value("gate_shadow_would_suppress_total", {"camera": "a"}) == 1
    assert metrics.value("tier0_events_published_total", {"camera": "a"}) == 1
    metrics.reset()


def test_record_frame_model_label_detector_latency_and_detections():
    metrics.reset()
    result = _Result(
        regions=[(0, 0, 1, 1)], tracks=[1],
        detections=[_Det("person"), _Det("person"), _Det("car")],
        detect_latency_s=0.02,
    )
    record_frame("a", result, latency_s=0.05, detector_latency_s=0.02, model="yolov8n")
    # detector runs are model-attributable
    assert metrics.value("tier0_detector_runs_total", {"camera": "a", "model": "yolov8n"}) == 1
    # per-class detection volume
    assert metrics.value("tier0_detections_total", {"camera": "a", "label": "person"}) == 2
    assert metrics.value("tier0_detections_total", {"camera": "a", "label": "car"}) == 1
    # pure detector latency histogram carries the model label
    out = metrics.render()
    assert "# TYPE tier0_detector_latency_seconds histogram" in out
    assert 'tier0_detector_latency_seconds_count{camera="a",model="yolov8n"} 1' in out
    metrics.reset()


def test_record_frame_without_model_falls_back_to_camera_only():
    metrics.reset()
    record_frame("a", _Result(regions=[(0, 0, 1, 1)], tracks=[]))  # no model
    assert metrics.value("tier0_detector_runs_total", {"camera": "a"}) == 1
    metrics.reset()


def test_record_frame_emits_stage_latency():
    metrics.reset()
    record_frame("a", _Result(regions=[(0, 0, 1, 1)], tracks=[]),
                 stage_latency_s={"motion": 0.003, "detect": 0.02, "track": 0.001})
    out = metrics.render()
    assert "# TYPE tier0_stage_latency_seconds histogram" in out
    assert 'tier0_stage_latency_seconds_count{camera="a",stage="detect"} 1' in out
    assert 'tier0_stage_latency_seconds_count{camera="a",stage="motion"} 1' in out
    metrics.reset()


def test_operator_health_recorders():
    metrics.reset()
    record_worker_state("a", True, target_fps=5)
    record_processing_fps("a", 4.2)
    record_worker_restart("a")
    record_worker_restart("a")
    assert metrics.value("tier0_worker_up", {"camera": "a"}) == 1.0
    assert metrics.value("tier0_target_fps", {"camera": "a"}) == 5.0
    assert metrics.value("tier0_processing_fps", {"camera": "a"}) == 4.2
    assert metrics.value("tier0_worker_restarts_total", {"camera": "a"}) == 2
    record_worker_state("a", False)
    assert metrics.value("tier0_worker_up", {"camera": "a"}) == 0.0
    metrics.reset()


def test_sample_process_metrics_sets_rss_on_linux():
    m = Metrics()
    sample_process_metrics(m)          # first sample: RSS set, CPU% needs a delta
    if not os.path.exists("/proc/self/stat"):
        return                          # no-op off Linux; nothing to assert
    assert m.value("tier0_process_resident_memory_bytes") > 0
    sample_process_metrics(m)          # second sample: CPU% gauge now present
    assert "tier0_process_cpu_percent" in m.render()


# ── per-camera staleness (a dead feed must not hide behind a live one) ──

def test_stale_cameras_names_the_dead_feed_a_fleet_max_would_hide():
    from detect_pipeline import metrics as m

    m._last_frame_wall.clear()
    now = 1_000_000.0
    m._last_frame_wall["cam1"] = now - 1       # healthy
    m._last_frame_wall["cam2"] = now - 600     # dead for 10 minutes

    # The fleet-wide signal is dominated by the healthy camera...
    assert m.newest_frame_age_s(now) < 5
    # ...while the per-camera view names the casualty.
    assert m.stale_cameras(60.0, now) == ["cam2"]
    ages = m.frame_ages_s(now)
    assert round(ages["cam2"]) == 600
    m._last_frame_wall.clear()


def test_frame_age_gauge_moves_when_a_feed_stalls():
    """The gauge must be sampled at SCRAPE time, not in the frame loop —
    a loop-written gauge freezes at its last good value when frames stop."""
    from detect_pipeline import metrics as m

    m._last_frame_wall.clear()
    m.metrics.reset()
    now = 2_000_000.0
    m._last_frame_wall["cam9"] = now - 300
    m.sample_frame_age_metrics(now)
    out = m.metrics.render()
    assert "tier0_frame_age_seconds" in out
    assert 'camera="cam9"' in out
    m._last_frame_wall.clear()
    m.metrics.reset()
