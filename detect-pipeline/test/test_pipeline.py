# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for the per-camera Tier-0 worker (fakes, no ffmpeg/model)."""
from __future__ import annotations

from detect_pipeline.detector import RawDetection
from detect_pipeline.ffmpeg_presets import frame_size_bytes
from detect_pipeline.frame_source import Frame
from detect_pipeline.pipeline import DetectPipeline, nms, select_regions
from detect_pipeline.tracking import Detection, TrackConfig, Tracker

W, H = 320, 240


def _frame(seq: int) -> Frame:
    return Frame(bytes(frame_size_bytes(W, H)), W, H, seq, float(seq))


class _FakeSource:
    def __init__(self, n: int):
        self.n = n

    def stream(self):
        for i in range(self.n):
            yield _frame(i)


class _FakeMotion:
    """Returns a fixed motion box; calibrating controllable."""

    def __init__(self, box=(100, 80, 160, 200), calibrating=False):
        self.box = box
        self._calibrating = calibrating

    def detect(self, luma):
        return [self.box]

    def is_calibrating(self):
        return self._calibrating


class _OneBoxDetector:
    def detect(self, crop):
        return [RawDetection("person", 0.9, (0.25, 0.25, 0.75, 0.75))]


# ── unit: NMS + region selection ────────────────────────────────────

def test_nms_suppresses_overlapping_same_label():
    a = Detection("person", (0, 0, 100, 100), 0.9)
    b = Detection("person", (5, 5, 105, 105), 0.6)      # heavy overlap, lower score
    c = Detection("car", (0, 0, 100, 100), 0.8)         # different label, kept
    out = nms([a, b, c])
    assert a in out and c in out and b not in out


def test_select_regions_dedups_overlapping():
    boxes = [(100, 100, 160, 200), (105, 100, 165, 205)]  # near-identical
    regions = select_regions(boxes, [], (H, W), min_region=320)
    assert len(regions) == 1                              # collapsed to one region


# ── integration ─────────────────────────────────────────────────────

def test_pipeline_produces_track_from_motion_and_detection():
    tracker = Tracker((H, W), TrackConfig(fps=5, min_initialized=1))
    pipe = DetectPipeline(
        _FakeSource(3), _FakeMotion(calibrating=False), _OneBoxDetector(), tracker,
    )
    seen: list[int] = []

    def on_tracks(frame, tracks, calibrating):
        seen.append(len(tracks))

    pipe.run(on_tracks)
    assert len(seen) == 3                    # one callback per frame
    assert seen[-1] == 1                     # a person track is established
    assert tracker.tracks[0].label == "person"
    assert tracker.tracks[0].best is not None


def test_process_frame_exposes_detections_and_detector_latency():
    # The benchmarking signals: the frame's detections + the pure detector time.
    tracker = Tracker((H, W), TrackConfig(fps=5, min_initialized=1))
    pipe = DetectPipeline(
        _FakeSource(1), _FakeMotion(calibrating=False), _OneBoxDetector(), tracker,
    )
    result = pipe.process_frame(_frame(0))
    assert [d.label for d in result.detections] == ["person"]
    assert result.detect_latency_s is not None and result.detect_latency_s >= 0.0
    # stage breakdown present for a full (non-calibrating) frame
    assert {"motion", "region", "detect", "track"} <= set(result.stage_latency_s)


def test_process_frame_no_detector_latency_while_calibrating():
    tracker = Tracker((H, W), TrackConfig(fps=5, min_initialized=1))
    pipe = DetectPipeline(
        _FakeSource(1), _FakeMotion(calibrating=True), _OneBoxDetector(), tracker,
    )
    result = pipe.process_frame(_frame(0))
    assert result.detections == [] and result.detect_latency_s is None


def test_pipeline_skips_detection_while_calibrating():
    calls = {"n": 0}

    class _CountingDetector:
        def detect(self, crop):
            calls["n"] += 1
            return []

    tracker = Tracker((H, W), TrackConfig(fps=5, min_initialized=1))
    pipe = DetectPipeline(
        _FakeSource(3), _FakeMotion(calibrating=True), _CountingDetector(), tracker,
    )
    results = []
    pipe.run(lambda f, t, c: results.append(c))
    assert calls["n"] == 0                    # detector never called while calibrating
    assert all(c is True for c in results)    # calibrating flag surfaced
    assert tracker.tracks == []
