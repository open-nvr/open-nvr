# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the Tier-0 metrics registry + service-layer recorders."""
from __future__ import annotations

from dataclasses import dataclass, field

from detect_pipeline.gate import GateDecision, GateResult
from detect_pipeline.metrics import Metrics, metrics, record_frame, record_gate, record_published


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
class _Result:
    tracks: list = field(default_factory=list)
    regions: list = field(default_factory=list)
    calibrating: bool = False


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
