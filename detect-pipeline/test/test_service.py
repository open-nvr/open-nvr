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


def test_manager_shares_one_detector_pool_across_workers():
    """One detector per camera made resident memory a function of the FLEET
    rather than the hardware — the first hard wall at scale."""
    made = []

    class _Det:
        def __init__(self): made.append(1)
        def detect(self, crop): return []

    mgr = WorkerManager(
        _FakeProvider([]), _FakeSink(),
        detector_factory=_Det, detector_pool=3,
    )
    workers = [mgr._default_factory(_spec(f"c{i}"), _FakeSink()) for i in range(20)]
    # Every worker holds the SAME pooled detector...
    assert len({id(w.detector) for w in workers}) == 1
    # ...and nothing is built until inference actually runs.
    assert made == []
    workers[0].detector.detect(None)
    assert len(made) == 1


def test_detector_pool_zero_restores_one_per_worker():
    made = []

    class _Det:
        def __init__(self): made.append(1)
        def detect(self, crop): return []

    mgr = WorkerManager(
        _FakeProvider([]), _FakeSink(),
        detector_factory=_Det, detector_pool=0,
    )
    workers = [mgr._default_factory(_spec(f"c{i}"), _FakeSink()) for i in range(5)]
    assert len({id(w.detector) for w in workers}) == 5
    assert len(made) == 5


# ── start stagger: N cameras must not dial RTSP in the same instant ──

def test_new_workers_get_spread_start_delays():
    """One tick starts every new camera at once, and each ffprobes then
    spawns ffmpeg — so a cold start (or a fleet-wide recovery) would hit
    MediaMTX with N simultaneous session setups. Exercises the real
    _make_worker path, no stubbing."""
    mgr = WorkerManager(_FakeProvider([]), _FakeSink(), start_spread_s=10.0)
    workers = [mgr._make_worker(_spec(f"c{i}"), 7.25) for i in range(30)]
    try:
        delays = [w.start_delay for w in workers]
        assert all(0.0 <= d <= 7.25 for d in delays), delays
        assert len(set(round(d, 3) for d in delays)) > 20, "delays are not spread"
    finally:
        for w in workers:
            w.request_stop()


def test_small_batches_are_not_delayed():
    """Two cameras must still come up immediately — the spread is
    proportional to the batch, so a small install pays nothing. Drives the
    real reconcile() rather than recomputing its arithmetic, so deleting the
    scaling actually fails this."""
    prov = _FakeProvider([_spec("a"), _spec("b")])
    mgr = WorkerManager(prov, _FakeSink(), start_spread_s=10.0)
    mgr.reconcile()
    try:
        delays = [mgr._workers[c].start_delay for c in mgr.running_ids()]
        assert len(delays) == 2
        assert all(d <= 0.25 for d in delays), delays
    finally:
        mgr.stop()


def test_large_batches_are_spread_through_reconcile():
    """The same path with a real fleet: 40 new cameras must not all dial at
    once. Pins reconcile's scaling, not a formula copied into the test."""
    prov = _FakeProvider([_spec(f"c{i}") for i in range(40)])
    mgr = WorkerManager(prov, _FakeSink(), start_spread_s=10.0)
    mgr.reconcile()
    try:
        delays = [mgr._workers[c].start_delay for c in mgr.running_ids()]
        assert len(delays) == 40
        assert max(delays) > 1.0, "batch was not spread at all"
        assert all(0.0 <= d <= 10.0 for d in delays), delays
        assert len(set(round(d, 3) for d in delays)) > 30
    finally:
        mgr.stop()


def test_start_stagger_is_interrupted_by_stop():
    """A worker waiting out its stagger must stop at once, not sit through it."""
    import time as _time

    from detect_pipeline.service import CameraWorker

    w = CameraWorker(_spec("slow"), _FakeSink(), frame_source=_FramesSource(1),
                     start_delay=30.0)
    w.start()
    _time.sleep(0.05)
    t0 = _time.monotonic()
    assert w.stop(timeout=5.0), "worker did not exit during its stagger"
    assert _time.monotonic() - t0 < 2.0


def test_superseded_worker_stopped_mid_stagger_keeps_the_gauge_up():
    """A fleet stop shares ONE deadline, so a worker still sitting in its
    start stagger can be marked superseded and only wake AFTERWARDS. The
    race that matters is that ordering: the replacement writes UP first, and
    the straggler must not then write DOWN over it — UP is emitted once at
    start, so the healthy camera would read down for good.
    """
    import time as _time

    from detect_pipeline import metrics as m
    from detect_pipeline.service import CameraWorker

    m.metrics.reset()
    old = CameraWorker(_spec("cam1"), _FakeSink(), frame_source=_FramesSource(1),
                       start_delay=30.0)
    old.start()
    _time.sleep(0.05)                       # parked in the stagger
    old.mark_superseded()                   # manager replaced it...
    m.record_worker_state("cam1", True, target_fps=2)   # ...replacement is UP

    old.request_stop()                      # only NOW does the straggler wake
    assert old.join(5.0)
    _time.sleep(0.05)

    assert m.metrics.value("tier0_worker_up", {"camera": "cam1"}) == 1.0, \
        "straggler zeroed its replacement's gauge"
    m.metrics.reset()


def test_non_superseded_worker_stopped_mid_stagger_reports_down():
    """The converse: a genuine stop must still be visible as DOWN."""
    import time as _time

    from detect_pipeline import metrics as m
    from detect_pipeline.service import CameraWorker

    m.metrics.reset()
    w = CameraWorker(_spec("cam2"), _FakeSink(), frame_source=_FramesSource(1),
                     start_delay=30.0)
    w.start()
    _time.sleep(0.05)
    assert w.stop(timeout=5.0)
    _time.sleep(0.05)
    assert m.metrics.value("tier0_worker_up", {"camera": "cam2"}) == 0.0
    m.metrics.reset()


def test_decode_mode_flips_are_not_counted_as_feed_restarts():
    """Adaptive decode is ON by default and respawns ffmpeg on every
    idle<->active flip, resetting seq to 0. Counting that as a feed restart
    made healthy cameras look like flapping ones on the metric operators are
    told to alert on."""
    from detect_pipeline import metrics as m
    from detect_pipeline.frame_source import Frame
    from detect_pipeline.service import CameraWorker

    size = frame_size_bytes(W, H)

    class _FlipSource:
        width, height = W, H

        def stream(self):
            yield Frame(bytes(size), W, H, 0, 0.0)
            yield Frame(bytes(size), W, H, 1, 1.0)
            # a deliberate decode flip: seq restarts, but it is not a failure
            yield Frame(bytes(size), W, H, 0, 2.0, True)
            yield Frame(bytes(size), W, H, 1, 3.0)
            # a real feed drop: seq restarts with no marker
            yield Frame(bytes(size), W, H, 0, 4.0)

    m.metrics.reset()
    w = CameraWorker(_spec("cam-flip"), _FakeSink(), frame_source=_FlipSource())
    w.start()
    for _ in range(200):
        if not w.is_alive():
            break
        time.sleep(0.01)
    w.stop(timeout=5)

    cam = {"camera": "cam-flip"}
    assert m.metrics.value("tier0_worker_restarts_total", cam) == 1.0
    assert m.metrics.value("tier0_decode_mode_changes_total", cam) == 1.0
    m.metrics.reset()
