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
        return None if self.specs is None else list(self.specs)


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


def _spec(cid, analyze=True, **kw):
    return CameraSpec(cid, cid, f"rtsp://h/{cid}", analyze=analyze, **kw)


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


class _RestartingFramesSource:
    """Emits seq 0,1,2 then 0,1 — the second 0 is an ffmpeg-restart signal."""

    def __init__(self):
        self.width, self.height = W, H

    def stream(self):
        for i in (0, 1, 2, 0, 1):
            yield Frame(bytes(frame_size_bytes(W, H)), W, H, i, float(i))


def test_camera_worker_records_restart_and_stores_best_frame(monkeypatch):
    import detect_pipeline.motion as motion_mod
    from detect_pipeline.bestframe import BestFrameStore
    from detect_pipeline.metrics import metrics

    real_detect = motion_mod.MotionDetector.detect

    def fake_detect(self, luma):
        real_detect(self, luma)
        self.calibrating = False
        return [(80, 60, 160, 200)]

    monkeypatch.setattr(motion_mod.MotionDetector, "detect", fake_detect)

    metrics.reset()
    store = BestFrameStore()
    sink = _FakeSink()
    worker = CameraWorker(
        _spec("cam-front"), sink,
        detector=_MotionAllDetector(), frame_source=_RestartingFramesSource(),
        best_frames=store,
    )
    worker.start()
    for _ in range(100):                             # wait for the 5 frames to drain
        if len(sink.events) >= 5:
            break
        time.sleep(0.02)
    worker.stop()

    # restart detected exactly once (the seq reset back to 0)
    assert metrics.value("tier0_worker_restarts_total", {"camera": "cam-front"}) == 1
    # worker liveness + target fps recorded; down after stop
    assert metrics.value("tier0_target_fps", {"camera": "cam-front"}) == 5
    assert metrics.value("tier0_worker_up", {"camera": "cam-front"}) == 0.0
    # a best-frame crop was retained and is fetchable as JPEG (encode path works)
    assert len(store) >= 1
    assert store.latest_jpeg("cam-front") is not None
    metrics.reset()


# ── Per-camera assignment labels (slice 3) ──────────────────────────
#
# "Camera 4 wants person + truck": the camera's object_detection
# assignment sets THAT worker's allowlist; every other camera keeps the
# global DETECT_LABELS. A label change on the settings page restarts
# just that worker on the next reconcile tick.


def test_allowed_labels_precedence(monkeypatch):
    from detect_pipeline.service import allowed_labels_for

    monkeypatch.setenv("DETECT_LABELS", "person,car")
    assigned = CameraSpec("a", "a", "rtsp://h/a", labels=frozenset({"truck"}))
    unassigned = CameraSpec("b", "b", "rtsp://h/b")
    assert allowed_labels_for(assigned) == frozenset({"truck"})
    assert allowed_labels_for(unassigned) == frozenset({"person", "car"})


def test_reconcile_restarts_worker_when_labels_change():
    made = []

    def factory(spec, sink):
        w = _FakeWorker(spec, sink)
        made.append(w)
        return w

    prov = _FakeProvider([_spec("a"), _spec("b")])
    mgr = WorkerManager(prov, _FakeSink(), worker_factory=factory)
    mgr.reconcile()
    assert len(made) == 2

    # Same cameras next tick → nothing restarts (frame_url JWT churn and
    # friends must never bounce workers).
    prov.specs = [_spec("a"), _spec("b")]
    mgr.reconcile()
    assert len(made) == 2

    # Operator narrows camera a to trucks → only a's worker is rebuilt.
    prov.specs = [
        CameraSpec("a", "a", "rtsp://h/a", labels=frozenset({"truck"})),
        _spec("b"),
    ]
    mgr.reconcile()
    assert len(made) == 3
    assert made[2].spec.labels == frozenset({"truck"})
    assert made[0].stopped and not made[1].stopped

    # ...and un-assigning restores the global default via one more restart.
    prov.specs = [_spec("a"), _spec("b")]
    mgr.reconcile()
    assert len(made) == 4 and made[3].spec.labels is None


def test_reconcile_restarts_worker_when_nvr_camera_id_changes():
    # The worker bakes nvr_camera_id into its VisitLifecycle at start, so a
    # late-arriving id (core upgraded mid-flight) must rebuild the worker.
    made = []

    def factory(spec, sink):
        w = _FakeWorker(spec, sink)
        made.append(w)
        return w

    prov = _FakeProvider([_spec("a")])
    mgr = WorkerManager(prov, _FakeSink(), worker_factory=factory)
    mgr.reconcile()
    assert len(made) == 1

    prov.specs = [_spec("a", nvr_camera_id=5)]
    mgr.reconcile()
    assert len(made) == 2 and made[1].spec.nvr_camera_id == 5
    assert made[0].stopped

    prov.specs = [_spec("a", nvr_camera_id=5)]
    mgr.reconcile()
    assert len(made) == 2                        # unchanged id → no bounce

    # A fetch that DEGRADES the id (mixed-version core replicas during a
    # rolling upgrade) must not flap the worker: None carries no new
    # information, and the running worker's baked-in id still works.
    prov.specs = [_spec("a")]
    mgr.reconcile()
    prov.specs = [_spec("a", nvr_camera_id=5)]
    mgr.reconcile()
    assert len(made) == 2                        # N → None → N: zero bounces


def test_reconcile_keeps_workers_when_discovery_fails():
    # provider.list_cameras() → None means "discovery failed", not "no
    # cameras" — a one-tick core outage must not tear down detection.
    made = []

    def factory(spec, sink):
        w = _FakeWorker(spec, sink)
        made.append(w)
        return w

    prov = _FakeProvider([_spec("a"), _spec("b")])
    mgr = WorkerManager(prov, _FakeSink(), worker_factory=factory)
    mgr.reconcile()
    assert len(made) == 2

    prov.specs = None                            # core briefly down
    mgr.reconcile()
    assert len(made) == 2 and not any(w.stopped for w in made)

    prov.specs = []                              # genuinely no cameras
    mgr.reconcile()
    assert all(w.stopped for w in made)


# ── DETECT_HWACCEL actually reaching the workers ────────────────────

def test_service_wide_hwaccel_reaches_the_worker():
    """The regression this pins: DETECT_HWACCEL was parsed and LOGGED but
    never passed to WorkerManager, so every camera decoded on CPU no matter
    what was configured — while core, keying off the same setting, handed
    the pipeline the full-resolution MAIN stream."""
    from detect_pipeline.service import CameraWorker

    mgr = WorkerManager(
        _FakeProvider([_spec("a")]), _FakeSink(),
        hwaccel="vaapi", device="/dev/dri/renderD128",
    )
    worker = mgr._default_factory(_spec("a"), _FakeSink())
    assert isinstance(worker, CameraWorker)
    assert worker.hwaccel == "vaapi"
    assert worker.device == "/dev/dri/renderD128"


def test_hwaccel_precedence_camera_declaration_then_global():
    from detect_pipeline.service import hwaccel_for

    # Nothing declared per camera → the service-wide value applies. This is
    # the normal case: core does not send `hwaccel` per camera.
    assert hwaccel_for(_spec("a"), "vaapi") == "vaapi"
    # A per-camera declaration wins, mirroring allowed_labels_for.
    assert hwaccel_for(_spec("a", hwaccel="qsv"), "vaapi") == "qsv"
    assert hwaccel_for(_spec("a"), "cpu") == "cpu"


def test_worker_degrades_to_cpu_when_the_device_is_absent():
    from detect_pipeline.ffmpeg_presets import HwAccel

    mgr = WorkerManager(
        _FakeProvider([_spec("a")]), _FakeSink(),
        hwaccel="vaapi", device="/definitely/not/here",
    )
    worker = mgr._default_factory(_spec("a"), _FakeSink())
    # Degrades rather than failing every camera at ffmpeg spawn.
    assert worker._effective_hwaccel() is HwAccel.CPU


# ── fleet shutdown: bounded, interruptible, and non-orphaning ───────

class _BlockingSource:
    """A source parked in a blocking read until close() releases it.

    Models the real failure: the worker only checks the stop flag BETWEEN
    frames, so a source that isn't producing frames leaves it unreachable.
    """

    width, height = W, H

    def __init__(self):
        import threading as _t
        self.released = _t.Event()
        self.closed = False

    def stream(self):
        self.released.wait(timeout=30)      # unblocked only by close()
        return
        yield                                # pragma: no cover - generator marker

    def close(self):
        self.closed = True
        self.released.set()


def test_stop_unblocks_a_worker_parked_in_a_blocking_read():
    """Before: join(5) always timed out here and the thread was orphaned."""
    import time as _time

    from detect_pipeline.service import CameraWorker

    src = _BlockingSource()
    worker = CameraWorker(_spec("cam-stuck"), _FakeSink(), frame_source=src)
    worker.start()
    for _ in range(200):                     # let it reach the blocking read
        if worker.is_alive():
            break
        _time.sleep(0.01)

    t0 = _time.monotonic()
    exited = worker.stop(timeout=5.0)
    elapsed = _time.monotonic() - t0

    assert exited, "worker did not exit — join timed out"
    assert src.closed, "stop() must close the source to unblock the reader"
    assert elapsed < 2.0, f"took {elapsed:.1f}s; the source was not interrupted"


def test_fleet_stop_costs_one_timeout_not_one_per_camera(monkeypatch):
    """The regression: reconcile stopped workers serially with a 5s join
    each, so a spec change across a fleet blocked the loop for minutes and
    an admin gate toggle took N x 5s with every camera dark."""
    import time as _time

    from detect_pipeline import service as svc

    monkeypatch.setattr(svc, "STOP_TIMEOUT_S", 0.4)

    class _Deaf:
        """Never exits — worst case for the join."""
        def __init__(self, *a):
            self.signalled = False
            self.superseded = False

        def start(self): pass
        def is_alive(self): return True
        def request_stop(self): self.signalled = True
        def join(self, timeout):
            _time.sleep(timeout)             # burn the whole budget
            return False
        def mark_superseded(self): self.superseded = True
        def stop(self, timeout=5.0): self.request_stop(); return self.join(timeout)

    made = []
    prov = _FakeProvider([_spec(c) for c in "abcdefgh"])   # 8 cameras
    mgr = WorkerManager(prov, _FakeSink(),
                        worker_factory=lambda s, k: (made.append(_Deaf()), made[-1])[1])
    mgr.reconcile()
    assert len(made) == 8

    prov.specs = []                                        # all removed
    t0 = _time.monotonic()
    mgr.reconcile()
    elapsed = _time.monotonic() - t0

    assert all(w.signalled for w in made), "every worker must be signalled first"
    # One shared budget, not 8 x 0.4s.
    assert elapsed < 8 * 0.4 * 0.6, f"{elapsed:.2f}s looks serial"
    # Stragglers are superseded so they cannot clobber a replacement's gauge.
    assert all(w.superseded for w in made)


def test_straggler_cannot_zero_the_replacement_gauge():
    from detect_pipeline import metrics as m
    from detect_pipeline.service import CameraWorker

    m.metrics.reset()
    src = _BlockingSource()
    old = CameraWorker(_spec("cam1"), _FakeSink(), frame_source=src)
    old.start()
    old.mark_superseded()                    # manager replaced it
    old.request_stop()
    old.join(5.0)

    # The replacement's UP gauge must survive the old worker's teardown.
    m.record_worker_state("cam1", True, target_fps=2)
    assert m.metrics.value("tier0_worker_up", {"camera": "cam1"}) == 1.0
    m.metrics.reset()
