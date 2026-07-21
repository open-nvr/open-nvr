# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the worker-manager reconcile logic + a real CameraWorker run.

No OpenNVR / NATS / ffmpeg — provider, sink, and workers are fakes; the real
CameraWorker is exercised with an injected frame source.
"""
from __future__ import annotations

import time

from detect_pipeline.detector import RawDetection
from detect_pipeline.ffmpeg_presets import frame_size_bytes
from detect_pipeline.frame_source import Frame
from detect_pipeline.service import (
    CameraSpec,
    CameraWorker,
    WorkerManager,
)

W, H = 320, 240


class _FakeProvider:
    def __init__(self, specs):
        self.specs = specs

    def list_cameras(self):
        return list(self.specs)


class _FakeSink:
    def __init__(self):
        self.events = []

    def publish(self, camera_id, result, frame):
        self.events.append((camera_id, len(result.tracks)))


class _FakeWorker:
    def __init__(self, spec, sink):
        self.spec = spec
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def is_alive(self):
        return self.started and not self.stopped


def _spec(cid, analyze=True):
    return CameraSpec(cid, cid, f"rtsp://h/{cid}", analyze=analyze)


# ── manager reconcile ───────────────────────────────────────────────

def test_starts_one_worker_per_analyze_camera():
    prov = _FakeProvider([_spec("a"), _spec("b"), _spec("c", analyze=False)])
    mgr = WorkerManager(prov, _FakeSink(), worker_factory=_FakeWorker)
    mgr.reconcile()
    assert mgr.running_ids() == {"a", "b"}          # c is analyze=False -> skipped


def test_reconcile_adds_and_removes_workers():
    prov = _FakeProvider([_spec("a"), _spec("b")])
    mgr = WorkerManager(prov, _FakeSink(), worker_factory=_FakeWorker)
    mgr.reconcile()
    assert mgr.running_ids() == {"a", "b"}
    prov.specs = [_spec("b"), _spec("d")]           # a removed, d added
    mgr.reconcile()
    assert mgr.running_ids() == {"b", "d"}


def test_disabled_manager_runs_nothing_and_stops_all():
    prov = _FakeProvider([_spec("a")])
    mgr = WorkerManager(prov, _FakeSink(), worker_factory=_FakeWorker)
    mgr.reconcile()
    assert mgr.running_ids() == {"a"}
    mgr.enabled = False
    mgr.reconcile()
    assert mgr.running_ids() == set()               # global disable stops everything


def test_dead_worker_is_replaced():
    made = []

    def factory(spec, sink):
        w = _FakeWorker(spec, sink)
        made.append(w)
        return w

    prov = _FakeProvider([_spec("a")])
    mgr = WorkerManager(prov, _FakeSink(), worker_factory=factory)
    mgr.reconcile()
    made[0].stopped = True                          # simulate a crashed worker
    mgr.reconcile()
    assert len(made) == 2                            # a fresh worker was started
    assert mgr.running_ids() == {"a"}


# ── real CameraWorker with an injected frame source ─────────────────

class _FramesSource:
    def __init__(self, n):
        self.width, self.height = W, H
        self.n = n

    def stream(self):
        for i in range(self.n):
            yield Frame(bytes(frame_size_bytes(W, H)), W, H, i, float(i))


class _MotionAllDetector:
    """Detector that always returns one box, so tracks form deterministically."""

    def detect(self, crop):
        return [RawDetection("person", 0.9, (0.25, 0.25, 0.75, 0.75))]


def test_camera_worker_publishes_results(monkeypatch):
    # Force motion to be "not calibrating" so detection runs on the fake frames.
    import detect_pipeline.motion as motion_mod

    real_detect = motion_mod.MotionDetector.detect

    def fake_detect(self, luma):
        real_detect(self, luma)
        self.calibrating = False
        return [(80, 60, 160, 200)]

    monkeypatch.setattr(motion_mod.MotionDetector, "detect", fake_detect)

    sink = _FakeSink()
    worker = CameraWorker(
        _spec("cam-front"), sink,
        detector=_MotionAllDetector(), frame_source=_FramesSource(4),
    )
    worker.start()
    for _ in range(50):
        if len(sink.events) >= 4:
            break
        time.sleep(0.02)
    worker.stop()
    assert len(sink.events) == 4
    assert all(cid == "cam-front" for cid, _ in sink.events)
    assert sink.events[-1][1] >= 1                   # a track was published
