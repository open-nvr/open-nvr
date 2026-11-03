# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Evidence from the main stream while detection runs on the sub.

The deal (evidence.py): the detector sees the cheap 640x360 sub; the crop
the operator is shown, the scene behind it and the plate the OCR reads are
cut from the 1920x1080 main — at the same MOMENT, or not at all. These
tests pin the three halves: the source that runs the main decode only while
the camera is busy, the tracker that crops from the main frame nearest in
capture time (and refuses one too far away), and the worker/manager
plumbing that carries core's main-tap URL to it.
"""
from __future__ import annotations

import threading
import time

import numpy as np

from detect_pipeline.evidence import EvidenceFrame, EvidenceSource
from detect_pipeline.frame_source import Frame, frame_size_bytes
from detect_pipeline.service import CameraSpec, CameraWorker, WorkerManager
from detect_pipeline.tracking import Detection, TrackConfig, Tracker


# ── fakes ────────────────────────────────────────────────────────────

class _Result:
    def __init__(self, tracks=(), motion=()):
        self.tracks = list(tracks)
        self.motion_boxes = list(motion)
        self.calibrating = False


class _Src:
    """A main-stream source: yields its frames, then holds open until closed."""

    instances: list = []

    def __init__(self, frames):
        self.frames = frames
        self.closed = threading.Event()
        self.started = False
        _Src.instances.append(self)

    def stream(self):
        self.started = True
        for f in self.frames:
            if self.closed.is_set():
                return
            yield f
        self.closed.wait(5)

    def close(self):
        self.closed.set()


def _frame(w, h, ts, fill=0):
    """An I420 frame whose luma is ``fill`` everywhere (chroma neutral)."""
    data = bytes([fill]) * (w * h) + bytes([128]) * (w * h // 2)
    return Frame(data, w, h, int(ts * 10), float(ts))


def _wait(pred, timeout=3.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


# ── EvidenceSource: runs only while busy ─────────────────────────────

def test_source_starts_on_activity_stops_when_idle_and_keeps_a_ring():
    _Src.instances.clear()
    clock = [100.0]
    frames = [_frame(64, 32, ts) for ts in (100.0, 100.2, 100.4, 100.6)]
    ev = EvidenceSource("cam", lambda: _Src(list(frames)), idle_after=5.0,
                        max_skew_s=0.15, ring=3, _clock=lambda: clock[0])
    assert not ev.active
    # quiet: nothing starts
    assert ev.observe(_Result()) is False and not ev.active
    # a motion box starts the main decode
    assert ev.observe(_Result(motion=[(0, 0, 1, 1)])) is True
    assert ev.active and ev.starts == 1
    assert _wait(lambda: ev.frames_total == 4)
    # ring=3 keeps the newest three; nearest picks by capture time
    assert ev.nearest(100.4).ts == 100.4
    assert ev.nearest(100.55).ts == 100.6
    assert ev.nearest(100.0) is None, "the oldest frame was evicted and 100.2 is too far"
    assert ev.nearest(99.0) is None, "beyond max_skew → refused"
    # still busy: no flip
    clock[0] = 103.0
    assert ev.observe(_Result(tracks=[1])) is False and ev.active
    # idle_after seconds of quiet: stopped, source closed, ring cleared
    clock[0] = 103.0 + 5.0
    assert ev.observe(_Result()) is True
    assert not ev.active and _Src.instances[0].closed.is_set()
    assert ev.nearest(100.6) is None
    # busy again: a FRESH source (never a resumed one)
    clock[0] = 110.0
    assert ev.observe(_Result(tracks=[1])) is True
    assert ev.starts == 2 and len(_Src.instances) == 2
    ev.close()
    assert not ev.active and _Src.instances[1].closed.is_set()
    # closed stays closed
    assert ev.observe(_Result(tracks=[1])) is False and not ev.active


def test_source_factory_failure_is_quiet_and_lookups_are_empty():
    def boom():
        raise RuntimeError("no main stream")
    ev = EvidenceSource("cam", boom, idle_after=1.0)
    assert ev.observe(_Result(tracks=[1])) is True   # it tried
    assert not ev.active and ev.starts == 0
    assert ev.nearest(1.0) is None
    ev.close()


def test_nearest_with_no_timestamp_uses_now():
    clock = [50.0]
    frames = [_frame(8, 8, 49.9)]
    ev = EvidenceSource("cam", lambda: _Src(frames), max_skew_s=0.2,
                        _clock=lambda: clock[0])
    ev.observe(_Result(tracks=[1]))
    assert _wait(lambda: ev.frames_total == 1)
    assert ev.nearest(None) is not None
    clock[0] = 51.0
    assert ev.nearest(None) is None
    ev.close()


def test_evidence_frame_converts_to_bgr_once():
    ef = EvidenceFrame(_frame(16, 8, 1.0, fill=200))
    a = ef.bgr()
    assert a.shape == (8, 16, 3)
    assert ef.bgr() is a


# ── Tracker: crops from the main frame of the same moment ────────────

class _Main:
    """What EvidenceSource.nearest returns: a frame with .bgr() and .ts."""
    def __init__(self, bgr, ts):
        self._bgr, self.ts = bgr, ts
    def bgr(self):
        return self._bgr


SUB = (360, 640)
MAIN = (720, 1280)


def _tracker(evidence):
    tk = Tracker(SUB, TrackConfig(fps=2))
    tk.crop_margin = 0.0
    tk.evidence = evidence
    return tk


def test_best_crop_is_cut_from_the_main_frame_scaled_by_the_resolution_ratio():
    sub = np.full((*SUB, 3), 50, np.uint8)
    main = np.full((*MAIN, 3), 200, np.uint8)
    seen = []

    def evidence(ts):
        seen.append(ts)
        return _Main(main, ts)
    tk = _tracker(evidence)
    tk.update([Detection("person", (100, 100, 200, 300), 0.8)], sub, frame_ts=12.5)
    tr = tk._tracks[0]
    assert seen == [12.5], "looked up once, with the detect frame's capture stamp"
    assert tr.best_crop.shape == (400, 200, 3), "the 100x200 sub box is 200x400 in main"
    assert int(tr.best_crop[0, 0, 0]) == 200, "pixels come from the main frame"
    assert tr.best_crop_source == "main"
    assert tr.best_crop_skew_s == 0.0
    assert tk.evidence_stats == {"main": 1, "detect": 0}
    assert tk._ev is None, "the main frame is not retained between updates"


def test_no_main_frame_close_enough_means_the_detect_crop_and_says_so():
    sub = np.full((*SUB, 3), 50, np.uint8)
    tk = _tracker(lambda ts: None)
    tk.update([Detection("person", (100, 100, 200, 300), 0.8)], sub, frame_ts=1.0)
    tr = tk._tracks[0]
    assert tr.best_crop.shape == (200, 100, 3)
    assert int(tr.best_crop[0, 0, 0]) == 50
    assert tr.best_crop_source == "detect" and tr.best_crop_skew_s is None
    assert tk.evidence_stats == {"main": 0, "detect": 1}


def test_without_an_evidence_source_nothing_changes():
    """The pre-feature path: no source attribute set, no stats, no lookup."""
    sub = np.full((*SUB, 3), 50, np.uint8)
    tk = Tracker(SUB, TrackConfig(fps=2))
    tk.update([Detection("person", (100, 100, 200, 300), 0.8)], sub, frame_ts=1.0)
    tr = tk._tracks[0]
    assert tr.best_crop is not None and tr.best_crop_source is None
    assert tk.evidence_stats == {"main": 0, "detect": 0}


def test_scene_and_plate_candidates_come_from_the_same_main_frame(monkeypatch):
    import detect_pipeline.bestframe as bf
    scenes = []

    def enc(bgr, *, max_px=1280, quality=78):
        scenes.append(bgr.shape)
        return b"SCENE"
    monkeypatch.setattr(bf, "encode_scene_jpeg", enc)
    sub = np.full((*SUB, 3), 50, np.uint8)
    main = np.full((*MAIN, 3), 200, np.uint8)
    tk = _tracker(lambda ts: _Main(main, ts))
    tk.retain_scene = True
    tk.retain_plate_candidates = True
    tk.update([Detection("car", (100, 100, 200, 300), 0.8)], sub, frame_ts=1.0)
    tr = tk._tracks[0]
    assert scenes == [(720, 1280, 3)], "the scene is the MAIN frame, encoded once"
    assert tr.best_scene_jpeg == b"SCENE"
    cand = tr.plate_ring._items[0].crop
    assert cand.shape == (400, 200, 3) and int(cand[0, 0, 0]) == 200


def test_skew_is_the_distance_between_the_two_captures():
    sub = np.full((*SUB, 3), 50, np.uint8)
    main = np.full((*MAIN, 3), 200, np.uint8)
    tk = _tracker(lambda ts: _Main(main, ts + 0.12))
    tk.update([Detection("person", (10, 10, 60, 120), 0.8)], sub, frame_ts=5.0)
    assert abs(tk._tracks[0].best_crop_skew_s - 0.12) < 1e-6


def test_a_lookup_that_raises_falls_back_to_the_detect_frame():
    sub = np.full((*SUB, 3), 50, np.uint8)

    def boom(ts):
        raise RuntimeError("ring broke")
    tk = _tracker(boom)
    tk.update([Detection("person", (10, 10, 60, 120), 0.8)], sub, frame_ts=5.0)
    assert tk._tracks[0].best_crop_source == "detect"


# ── Worker: core's main URL becomes a running evidence source ────────

W, H = 320, 240


class _SubSource:
    def __init__(self, n):
        self.width, self.height = W, H
        self.n = n

    def stream(self):
        for i in range(self.n):
            yield Frame(bytes(frame_size_bytes(W, H)), W, H, i, float(i))
            time.sleep(0.05)


class _GrowingDetector:
    """Each call returns a bigger box, so the best crop keeps updating."""
    def __init__(self):
        self.n = 0

    def detect(self, crop):
        from detect_pipeline.detector import RawDetection
        self.n += 1
        g = min(0.45, 0.1 + 0.05 * self.n)
        return [RawDetection("person", 0.9, (0.5 - g, 0.5 - g, 0.5 + g, 0.5 + g))]


class _TrackSink:
    def __init__(self):
        self.sources = []

    def publish(self, camera_id, result, frame):
        self.sources.extend(t.best_crop_source for t in result.tracks)
        return True


def _force_motion(monkeypatch):
    import detect_pipeline.motion as motion_mod
    real = motion_mod.MotionDetector.detect

    def fake(self, luma):
        real(self, luma)
        self.calibrating = False
        return [(40, 30, 280, 210)]
    monkeypatch.setattr(motion_mod.MotionDetector, "detect", fake)


def _spec(cid, **kw):
    return CameraSpec(cid, cid, f"rtsp://m/{cid}-sub?jwt=a", **kw)


def test_worker_cuts_evidence_from_the_main_stream_core_offered(monkeypatch):
    _force_motion(monkeypatch)
    _Src.instances.clear()
    # main frames at 2x, stamped like the sub frames (ts = i)
    main_frames = [_frame(2 * W, 2 * H, float(i), fill=180) for i in range(12)]
    sink = _TrackSink()
    worker = CameraWorker(
        _spec("cam-ev", evidence_url="rtsp://m/cam-ev?jwt=a"), sink,
        detector=_GrowingDetector(), frame_source=_SubSource(6),
        evidence_source_factory=lambda: _Src(list(main_frames)),
        evidence_max_skew_s=10.0,          # fake stamps, generous window
    )
    worker.start()
    assert _wait(lambda: not worker.is_alive(), timeout=10.0)
    assert _Src.instances and _Src.instances[0].started, "the main decode ran"
    assert _Src.instances[-1].closed.is_set(), "and was closed with the worker"
    assert "main" in sink.sources, sink.sources
    from detect_pipeline.metrics import metrics
    assert metrics.value("tier0_evidence_crops_total", {"camera": "cam-ev", "source": "main"}) >= 1


def test_worker_without_a_main_url_or_with_evidence_detect_runs_no_second_decode(monkeypatch):
    _force_motion(monkeypatch)
    _Src.instances.clear()
    for spec, kw in (
        (_spec("cam-plain"), {}),
        (_spec("cam-off", evidence_url="rtsp://m/cam-off"), {"evidence_stream": "detect"}),
    ):
        sink = _TrackSink()
        worker = CameraWorker(
            spec, sink, detector=_GrowingDetector(), frame_source=_SubSource(3),
            evidence_source_factory=lambda: _Src([]), **kw,
        )
        worker.start()
        assert _wait(lambda: not worker.is_alive(), timeout=10.0)
        assert worker._evidence is None
        assert all(s is None for s in sink.sources), sink.sources
    assert _Src.instances == []


# ── Manager: the main URL rides beside the sub URL, and its KIND is baked ──

def test_baked_spec_ignores_token_rotation_but_not_the_tap_kind_or_evidence():
    a = CameraSpec("c", "c", "rtsp://m/cam-1?jwt=a")
    b = CameraSpec("c", "c", "rtsp://m/cam-1?jwt=b")
    assert WorkerManager._baked(a) == WorkerManager._baked(b)
    sub = CameraSpec("c", "c", "rtsp://m/cam-1-sub?jwt=a")
    assert WorkerManager._baked(a) != WorkerManager._baked(sub), "main → sub restarts the worker"
    with_ev = CameraSpec("c", "c", "rtsp://m/cam-1-sub?jwt=a", evidence_url="rtsp://m/cam-1?jwt=a")
    rotated = CameraSpec("c", "c", "rtsp://m/cam-1-sub?jwt=b", evidence_url="rtsp://m/cam-1?jwt=b")
    assert WorkerManager._baked(with_ev) == WorkerManager._baked(rotated)
    assert WorkerManager._baked(with_ev) != WorkerManager._baked(sub), "evidence appearing restarts it"


class _Provider:
    def __init__(self, specs):
        self.specs = specs

    def list_cameras(self):
        return list(self.specs)


class _Worker:
    def __init__(self, spec, sink):
        self.spec, self.alive = spec, False

    def start(self):
        self.alive = True

    def stop(self, timeout=None):
        self.alive = False
        return True

    def request_stop(self):
        self.alive = False

    def join(self, timeout):
        return True

    def mark_superseded(self):
        pass

    def is_alive(self):
        return self.alive


def test_manager_keeps_the_freshest_main_url_per_camera():
    specs = [CameraSpec("c1", "c1", "rtsp://m/cam-1-sub?jwt=a", evidence_url="rtsp://m/cam-1?jwt=a")]
    prov = _Provider(specs)
    mgr = WorkerManager(prov, object(), worker_factory=_Worker)
    mgr.reconcile()
    assert mgr.current_evidence_url("c1") == "rtsp://m/cam-1?jwt=a"
    prov.specs = [CameraSpec("c1", "c1", "rtsp://m/cam-1-sub?jwt=b", evidence_url="rtsp://m/cam-1?jwt=b")]
    mgr.reconcile()
    assert mgr.current_evidence_url("c1") == "rtsp://m/cam-1?jwt=b"
    prov.specs = [CameraSpec("c1", "c1", "rtsp://m/cam-1?jwt=c")]      # back on the main tap
    mgr.reconcile()
    assert mgr.current_evidence_url("c1") is None


# ── Provider + config ────────────────────────────────────────────────

def test_provider_carries_evidence_url_and_config_reads_the_dials(monkeypatch):
    from detect_pipeline.providers import _to_spec
    from detect_pipeline.run import config_from_env
    row = {"camera_id": "c1", "open_nvr_camera_id": "1", "frame_url": "rtsp://m/cam-1-sub?jwt=t",
           "evidence_url": "rtsp://m/cam-1?jwt=t"}
    assert _to_spec(row).evidence_url == "rtsp://m/cam-1?jwt=t"
    assert _to_spec({**row, "evidence_url": None}).evidence_url is None
    assert _to_spec({"camera_id": "c2", "frame_url": "rtsp://m/cam-2"}).evidence_url is None
    cfg = config_from_env({})
    assert (cfg.evidence_stream, cfg.evidence_fps, cfg.evidence_idle_after,
            cfg.evidence_max_skew_s) == ("main", 5, 15.0, 0.35)
    cfg = config_from_env({"DETECT_EVIDENCE_STREAM": "detect", "DETECT_EVIDENCE_FPS": "8",
                           "DETECT_EVIDENCE_IDLE_AFTER": "30", "DETECT_EVIDENCE_MAX_SKEW_S": "0.5"})
    assert (cfg.evidence_stream, cfg.evidence_fps, cfg.evidence_idle_after,
            cfg.evidence_max_skew_s) == ("detect", 8, 30.0, 0.5)
    assert config_from_env({"DETECT_EVIDENCE_STREAM": "sometimes"}).evidence_stream == "main"


# ── Worker: a dead sub tap falls back to the main stream, visibly ────

def test_a_sub_tap_that_cannot_be_probed_falls_back_to_the_main_stream(monkeypatch, caplog):
    """Core hands out `cam-N-sub` as soon as a substream URL is stored; if
    MediaMTX cannot serve it yet the worker used to retry the sub every
    tick and the camera stayed dark. With the main URL in hand it detects
    on that instead — louder and costlier, but not blind — and runs no
    evidence source (detect and evidence are then the same stream)."""
    import logging
    import detect_pipeline.service as svc
    probes = []

    def fake_probe(url, **kw):
        probes.append(url.split("?")[0])
        return None if url.startswith("rtsp://m/cam-7-sub") else (1280, 720, 25.0)

    class _FS:
        def __init__(self, url, **kw):
            self.url, self.kw = url, kw
            self.width, self.height = kw["width"], kw["height"]
    monkeypatch.setattr(svc, "probe_stream", fake_probe)
    monkeypatch.setattr(svc, "FrameSource", _FS)
    w = CameraWorker(_spec("cam-7", evidence_url="rtsp://m/cam-7?jwt=a"), _TrackSink(),
                     evidence_url_provider=lambda: "rtsp://m/cam-7?jwt=fresh")
    with caplog.at_level(logging.WARNING):
        src, width, height = w._make_source()
    assert probes == ["rtsp://m/cam-7-sub", "rtsp://m/cam-7"]
    assert src.url == "rtsp://m/cam-7?jwt=a" and (width, height) == (1280, 720)
    assert src.kw["url_provider"]() == "rtsp://m/cam-7?jwt=fresh", "respawns read the MAIN url"
    assert "detecting on the MAIN stream" in caplog.text and "cam-7-sub" in caplog.text
    assert w._make_evidence(width, height) is None, "no second decode of the same stream"
    # no main URL to fall back to: the old behaviour, a plain failure
    w2 = CameraWorker(_spec("cam-7"), _TrackSink())
    try:
        w2._make_source()
    except RuntimeError as e:
        assert "could not probe" in str(e)
    else:
        raise AssertionError("expected a probe failure")


# ── Keyframe-only evidence (DETECT_EVIDENCE_DECODE=nokey) ────────────

def test_evidence_decode_dial_is_read_and_defaults_to_nokey():
    from detect_pipeline.run import config_from_env
    assert config_from_env({}).evidence_decode == "nokey"
    assert config_from_env({"DETECT_EVIDENCE_DECODE": "Full"}).evidence_decode == "full"
    assert config_from_env({"DETECT_EVIDENCE_DECODE": "NoKey"}).evidence_decode == "nokey"
    assert config_from_env({"DETECT_EVIDENCE_DECODE": "half"}).evidence_decode == "nokey"


def test_nokey_evidence_decodes_keyframes_without_a_rate_filter_and_widens_the_skew(monkeypatch):
    """The main FrameSource gets -skip_frame nokey and NO fps stage (a
    duplicate of a keyframe stamped on arrival would pass the skew check
    as fresh), and the skew window floors at NOKEY_MIN_SKEW_S."""
    import detect_pipeline.service as svc

    monkeypatch.setattr(svc, "probe_stream", lambda url: (1920, 1080, 15.0))
    built = []

    class _FS:
        def __init__(self, url, **kw):
            built.append(kw)

    monkeypatch.setattr(svc, "FrameSource", _FS)
    spec = _spec("cam-nk", evidence_url="rtsp://m/cam-nk?jwt=a")
    for decode, skew in (("nokey", 0.35), ("nokey", 5.0), ("full", 0.35)):
        built.clear()
        worker = CameraWorker(spec, _TrackSink(), detector=_GrowingDetector(),
                              frame_source=_SubSource(1), evidence_decode=decode,
                              evidence_max_skew_s=skew)
        ev = worker._make_evidence(640, 360)
        assert ev is not None
        ev._factory()                       # build the source without starting it
        kw = built[0]
        if decode == "nokey":
            assert kw["decode_skip"] == "nokey" and kw["fps"] == 0
            assert ev.max_skew_s == max(skew, CameraWorker.NOKEY_MIN_SKEW_S)
        else:
            assert kw["decode_skip"] != "nokey" and kw["fps"] == worker.evidence_fps
            assert ev.max_skew_s == skew


# ── Tracker: a main crop must LOOK like the detect crop ──────────────

def _scene(rng, shape, subject=None):
    """A textured frame (so the compare has something to correlate) with
    an optional textured subject — a bright patch with a dark cross —
    drawn at ``subject`` = (x1, y1, x2, y2)."""
    img = rng.integers(40, 90, size=(*shape, 3), dtype=np.uint8)
    if subject is not None:
        x1, y1, x2, y2 = subject
        img[y1:y2, x1:x2] = 230
        cy, cx = (y1 + y2) // 2, (x1 + x2) // 2
        img[cy - 4:cy + 4, x1:x2] = 20
        img[y1:y2, cx - 4:cx + 4] = 20
    return img


def test_main_crop_that_does_not_look_like_the_detect_crop_is_refused():
    """The Frigate concern, guarded: the box was found on the sub frame,
    but on the (older) main keyframe the subject is somewhere else — the
    main crop would be a sharp picture of empty floor. Refused, counted,
    the detect crop kept."""
    rng = np.random.default_rng(1)
    box = (100, 100, 200, 300)
    sub = _scene(rng, SUB, subject=box)
    # Main: the same (upscaled) background, the subject 300 px further on.
    main = np.kron(_scene(rng, SUB, subject=(400, 100, 500, 300)), np.ones((2, 2, 1), np.uint8))
    tk = _tracker(lambda ts: _Main(main, ts))
    tk.update([Detection("person", box, 0.8)], sub, frame_ts=1.0)
    tr = tk._tracks[0]
    assert tr.best_crop_source == "detect"
    assert tr.best_crop.shape == (200, 100, 3), "the detect frame's own crop"
    assert tk.evidence_mismatches == 1
    assert tk.evidence_stats == {"main": 0, "detect": 1}


def test_main_crop_that_matches_the_detect_crop_is_kept():
    rng = np.random.default_rng(2)
    box = (100, 100, 200, 300)
    sub = _scene(rng, SUB, subject=box)
    main = np.kron(sub, np.ones((2, 2, 1), np.uint8))      # the same sensor, 2x
    tk = _tracker(lambda ts: _Main(main, ts))
    tk.update([Detection("person", box, 0.8)], sub, frame_ts=1.0)
    tr = tk._tracks[0]
    assert tr.best_crop_source == "main" and tr.best_crop.shape == (400, 200, 3)
    assert tk.evidence_mismatches == 0


def test_match_check_is_undecidable_on_flat_crops_and_can_be_switched_off():
    from detect_pipeline.tracking import crop_similarity
    flat_a = np.full((20, 10, 3), 50, np.uint8)
    flat_b = np.full((40, 20, 3), 200, np.uint8)
    assert crop_similarity(flat_a, flat_b) is None, "no texture → no verdict"
    assert crop_similarity(None, flat_b) is None
    rng = np.random.default_rng(3)
    box = (100, 100, 200, 300)
    sub = _scene(rng, SUB, subject=box)
    main = np.kron(_scene(rng, SUB), np.ones((2, 2, 1), np.uint8))   # subject absent
    tk = _tracker(lambda ts: _Main(main, ts))
    tk.evidence_match_min = 0.0
    tk.update([Detection("person", box, 0.8)], sub, frame_ts=1.0)
    assert tk._tracks[0].best_crop_source == "main", "0 = the pre-guard behaviour"
    assert tk.evidence_mismatches == 0


def test_similarity_separates_the_same_place_from_a_shifted_one():
    """Pins the measure the 0.5 floor was chosen against: the same place
    on the two streams scores near 1, the box shifted by its own width
    (the subject gone) near 0, even with the sub at a different aspect."""
    from detect_pipeline.tracking import crop_similarity
    rng = np.random.default_rng(4)
    main = _scene(rng, (1080, 960), subject=(300, 300, 500, 700))
    # A 4:3 CIF sub of the same 16:9-squeezed picture: nearest-sampled.
    yi = np.linspace(0, 1079, 288).astype(int)
    xi = np.linspace(0, 959, 352).astype(int)
    sub = main[yi][:, xi]
    sub_box = sub[int(300 * 288 / 1080):int(700 * 288 / 1080),
                  int(300 * 352 / 960):int(500 * 352 / 960)]
    same = crop_similarity(main[300:700, 300:500], sub_box)
    shifted = crop_similarity(main[300:700, 500:700], sub_box)
    assert same > 0.8 and shifted < 0.3, (same, shifted)


# ── EvidenceSource: keyframes too far apart → full decode ────────────

def test_keyframes_too_far_apart_switch_the_source_to_full_decode(caplog):
    _Src.instances.clear()
    clock = [0.0]
    key_frames = [_frame(8, 8, ts) for ts in (0.0, 4.0, 8.0, 12.0)]   # a 4 s GOP
    full_frames = [_frame(8, 8, ts) for ts in (12.2, 12.4)]
    built = []

    def keyframes():
        built.append("nokey")
        return _Src(list(key_frames))

    def full():
        built.append("full")
        return _Src(list(full_frames))
    ev = EvidenceSource("cam", keyframes, idle_after=60.0, max_skew_s=2.0,
                        keyframes_only=True, max_gop_s=2.0,
                        full_factory=full, full_max_skew_s=0.35,
                        _clock=lambda: clock[0])
    assert ev.observe(_Result(tracks=[1])) is True and ev.keyframes_only
    assert _wait(lambda: ev.frames_total == 4)
    clock[0] = 12.0
    with caplog.at_level("WARNING"):
        assert ev.observe(_Result(tracks=[1])) is True, "the switch is a state flip"
    assert ev.switched_to_full and not ev.keyframes_only
    assert ev.gop_s == 4.0
    assert ev.max_skew_s == 0.35, "the operator's tight window is back"
    assert built == ["nokey", "full"] and _Src.instances[0].closed.is_set()
    assert "switching this camera's evidence decode to FULL" in caplog.text
    assert _wait(lambda: ev.frames_total == 6)
    assert ev.nearest(12.3).ts == 12.4
    # the decision sticks: an idle stop and a restart build the FULL source
    clock[0] = 100.0
    assert ev.observe(_Result()) is True and not ev.active
    clock[0] = 101.0
    assert ev.observe(_Result(tracks=[1])) is True
    assert built == ["nokey", "full", "full"]
    ev.close()


def test_a_short_gop_stays_on_keyframes_and_reports_its_length():
    _Src.instances.clear()
    frames = [_frame(8, 8, ts) for ts in (0.0, 1.0, 2.0, 3.0, 3.9)]
    built = []
    ev = EvidenceSource("cam", lambda: (built.append("nokey"), _Src(list(frames)))[1],
                        keyframes_only=True, max_gop_s=2.0,
                        full_factory=lambda: (built.append("full"), _Src([]))[1],
                        full_max_skew_s=0.35, max_skew_s=2.0, _clock=lambda: 50.0)
    ev.observe(_Result(tracks=[1]))
    assert _wait(lambda: ev.frames_total == 5)
    assert ev.observe(_Result(tracks=[1])) is False
    assert ev.keyframes_only and not ev.switched_to_full and ev.max_skew_s == 2.0
    assert ev.gop_s == 1.0 and built == ["nokey"]
    ev.close()


def test_gop_guard_dials_are_read_from_the_environment():
    from detect_pipeline.run import config_from_env
    cfg = config_from_env({})
    assert cfg.evidence_nokey_max_gop_s == 2.0 and cfg.evidence_match_min == 0.5
    cfg = config_from_env({"DETECT_EVIDENCE_NOKEY_MAX_GOP_S": "4",
                           "DETECT_EVIDENCE_MATCH_MIN": "0"})
    assert cfg.evidence_nokey_max_gop_s == 4.0 and cfg.evidence_match_min == 0.0


def test_worker_gives_the_keyframe_source_a_full_decode_to_fall_back_to(monkeypatch):
    import detect_pipeline.service as svc

    monkeypatch.setattr(svc, "probe_stream", lambda url: (1920, 1080, 15.0))
    built = []

    class _FS:
        def __init__(self, url, **kw):
            built.append(kw)

    monkeypatch.setattr(svc, "FrameSource", _FS)
    spec = _spec("cam-gop", evidence_url="rtsp://m/cam-gop?jwt=a")
    worker = CameraWorker(spec, _TrackSink(), detector=_GrowingDetector(),
                          frame_source=_SubSource(1), evidence_decode="nokey",
                          evidence_max_skew_s=0.35, evidence_nokey_max_gop_s=3.0,
                          evidence_match_min=0.7)
    ev = worker._make_evidence(640, 360)
    assert ev.keyframes_only and ev.max_gop_s == 3.0 and ev._full_max_skew_s == 0.35
    ev._full_factory()
    assert built[-1]["decode_skip"] != "nokey" and built[-1]["fps"] == worker.evidence_fps
    assert worker.evidence_match_min == 0.7
    full = CameraWorker(spec, _TrackSink(), detector=_GrowingDetector(),
                        frame_source=_SubSource(1), evidence_decode="full")
    assert full._make_evidence(640, 360).keyframes_only is False


def test_a_nominal_2s_gop_that_measures_a_little_longer_stays_on_keyframes():
    """A camera set to a 2 s keyframe interval does not deliver exactly 2.000 s:
    the CP Plus on the Ventuno Q measured 2.03 s (15 fps nominal) and the guard,
    comparing against 2.0 with no slack, switched it to full decode nine seconds
    after start — the most common default GOP, pushed onto the expensive path by
    timestamp jitter. The threshold now carries a jitter allowance; a GOP that
    is clearly longer still switches."""
    _Src.instances.clear()
    for gop, expect_switch in ((2.03, False), (2.2, False), (2.4, True)):
        frames = [_frame(8, 8, ts) for ts in (0.0, gop, 2 * gop, 3 * gop)]
        ev = EvidenceSource("cam", lambda: _Src(list(frames)),
                            keyframes_only=True, max_gop_s=2.0,
                            full_factory=lambda: _Src([]), full_max_skew_s=0.35,
                            max_skew_s=2.0, _clock=lambda: 50.0)
        ev.observe(_Result(tracks=[1]))
        assert _wait(lambda: ev.frames_total == 4)
        ev.observe(_Result(tracks=[1]))
        assert ev.switched_to_full is expect_switch, f"gop {gop}"
        assert round(ev.gop_s, 2) == gop
        ev.close()
