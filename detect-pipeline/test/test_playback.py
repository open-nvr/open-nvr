# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Evidence from the recording (playback.py): the request/result contract,
candidate selection by time and by the guard, budgets, and how the tracker
and the worker take a result — with the fetch, probe and decode injected."""
from __future__ import annotations

import threading
import time

import numpy as np

from detect_pipeline.detector import Detection
from detect_pipeline.playback import EvidenceRequest, EvidenceResult, PlaybackEvidence
from detect_pipeline.tracking import Tracker, TrackConfig, _crop_bgr

SUB = (360, 640)
MAIN = (720, 1280)


def _scene(rng, shape, subject=None):
    img = rng.integers(40, 90, size=(*shape, 3), dtype=np.uint8)
    if subject is not None:
        x1, y1, x2, y2 = subject
        img[y1:y2, x1:x2] = 230
        cy, cx = (y1 + y2) // 2, (x1 + x2) // 2
        img[cy - 4:cy + 4, x1:x2] = 20
        img[y1:y2, cx - 4:cx + 4] = 20
    return img


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t
    def __call__(self):
        return self.t


def _playback(frames, *, clock, fps=10.0, **kw):
    """A PlaybackEvidence whose recorder returns ``frames`` for any range;
    time is the injected clock, wall == mono."""
    calls = []

    def fetch(url):
        calls.append(url)
        return b"clip"

    pb = PlaybackEvidence(
        "cam", lambda: "http://pb/get?path=cam-1&jwt=t", match_min=0.5,
        fetch=fetch, probe=lambda data: (MAIN[1], MAIN[0], fps), decode=lambda data, w, h: list(frames),
        _clock=clock, _wall=clock, _capture_wall=lambda mono: mono,
        freshness_s=1.0, lead_s=0.3, window_s=0.35, min_interval_s=0.0, **kw)
    pb.calls = calls
    return pb


def _wait_results(pb, n=1, timeout=3.0):
    t0 = time.time()
    out = []
    while time.time() - t0 < timeout:
        out += pb.results()
        if len(out) >= n:
            return out
        time.sleep(0.01)
    return out


def test_the_frame_of_the_moment_is_chosen_and_the_guard_accepts_it():
    rng = np.random.default_rng(1)
    box = (100, 100, 200, 300)
    sub = _scene(rng, SUB, subject=box)
    # The clip: 10 frames at 10 fps ending at t+0.3; the subject is in the box
    # only on the frames around t (indices 6..7), elsewhere before that.
    frames = []
    for k in range(10):
        where = box if k in (6, 7) else (400, 100, 500, 300)
        frames.append(np.kron(_scene(rng, SUB, subject=where), np.ones((2, 2, 1), np.uint8)))
    clock = _Clock(1000.0)
    pb = _playback(frames, clock=clock)
    req = EvidenceRequest(track_id=1, mono_ts=1000.0, box=box, frame_shape=SUB,
                          sub_crop=_crop_bgr(sub, box, margin=0.0), label="person")
    pb.request(req)
    assert pb.pending_for(1)
    time.sleep(0.05)
    assert pb.results() == [], "served only once the recorder can have the moment"
    clock.t = 1001.5                                  # now ≥ t + lead + freshness
    res, = _wait_results(pb)
    assert res.outcome == "accepted" and res.track_id == 1 and res.score > 0.8
    assert res.skew_s <= 0.35 and res.candidates >= 3
    assert res.crop.shape[0] > 400, "the crop is cut from the main frame, with margin"
    assert "start=" in pb.calls[0] and "duration=" in pb.calls[0] and "format=fmp4" in pb.calls[0]
    assert not pb.pending_for(1)
    pb.close()


def test_a_clip_where_the_subject_is_elsewhere_is_refused_with_a_score():
    rng = np.random.default_rng(2)
    box = (100, 100, 200, 300)
    sub = _scene(rng, SUB, subject=box)
    frames = [np.kron(_scene(rng, SUB, subject=(400, 100, 500, 300)), np.ones((2, 2, 1), np.uint8))
              for _ in range(10)]
    clock = _Clock(1001.5)
    pb = _playback(frames, clock=clock)
    pb.request(EvidenceRequest(1, 1000.0, box, SUB, _crop_bgr(sub, box, margin=0.0)))
    res, = _wait_results(pb)
    assert res.outcome == "refused" and res.reason == "low_corr" and res.score < 0.5
    assert pb.outcomes == {"refused": 1}
    pb.close()


def test_requests_coalesce_per_track_and_stale_ones_are_dropped():
    rng = np.random.default_rng(3)
    box = (100, 100, 200, 300)
    sub = _scene(rng, SUB, subject=box)
    frames = [np.kron(sub, np.ones((2, 2, 1), np.uint8))] * 10
    clock = _Clock(1000.0)
    pb = _playback(frames, clock=clock, max_age_s=5.0)
    pb.request(EvidenceRequest(7, 1000.0, box, SUB, _crop_bgr(sub, box, margin=0.0)))
    pb.request(EvidenceRequest(7, 1000.4, box, SUB, _crop_bgr(sub, box, margin=0.0)))   # a better moment
    assert pb.requests == 2
    clock.t = 1002.0
    res, = _wait_results(pb)
    assert res.request.mono_ts == 1000.4, "the newest moment replaced the queued one"
    assert len(pb.calls) == 1
    # a request that waited too long is dropped, not served
    late = EvidenceRequest(8, 1000.0, box, SUB, _crop_bgr(sub, box, margin=0.0))
    late.enqueued_mono = 990.0
    pb.request(late)
    res, = _wait_results(pb)
    assert res.outcome == "dropped" and pb.dropped == 1
    pb.close()


def test_errors_and_empty_clips_never_kill_the_thread():
    rng = np.random.default_rng(4)
    box = (100, 100, 200, 300)
    sub = _scene(rng, SUB, subject=box)
    clock = _Clock(1002.0)

    def boom(url):
        raise OSError("recorder down")

    pb = PlaybackEvidence("cam", lambda: "http://pb/get?path=c", fetch=boom, probe=lambda d: None,
                          decode=lambda d, w, h: [], _clock=clock, _wall=clock,
                          _capture_wall=lambda m: m, min_interval_s=0.0, freshness_s=0.0)
    pb.request(EvidenceRequest(1, 1000.0, box, SUB, _crop_bgr(sub, box, margin=0.0)))
    res, = _wait_results(pb)
    assert res.outcome == "error" and res.reason == "OSError"
    pb._fetch = lambda url: b""
    pb.request(EvidenceRequest(2, 1000.0, box, SUB, _crop_bgr(sub, box, margin=0.0)))
    res, = _wait_results(pb)
    assert res.outcome == "no_frames" and res.reason == "empty_clip"
    assert pb._thread.is_alive()
    pb.close()


# ── the tracker files requests and takes results ──────────────────────

def _tracker():
    tk = Tracker(SUB, TrackConfig(fps=2))
    tk.retain_scene = False
    return tk


def test_tracker_files_a_request_when_the_best_crop_improves_and_takes_the_result():
    rng = np.random.default_rng(5)
    box = (100, 100, 200, 300)
    sub = _scene(rng, SUB, subject=box)
    filed = []
    tk = _tracker()
    tk.evidence_request = filed.append
    tk.update([Detection("person", box, 0.8)], sub, frame_ts=10.0)
    tr = tk._tracks[0]
    assert len(filed) == 1 and filed[0].track_id == tr.id and filed[0].mono_ts == 10.0
    assert filed[0].frame_shape == SUB and filed[0].sub_crop.shape == (200, 100, 3)
    assert tr.best_crop_source != "playback" and tr.best_crop_req_mono == 10.0
    main = np.kron(sub, np.ones((2, 2, 1), np.uint8))
    crop = _crop_bgr(main, (200, 200, 400, 600), margin=0.25)
    res = EvidenceResult(filed[0], "accepted", crop=crop, frame=main, score=0.93, skew_s=0.1)
    assert tk.apply_evidence_result(res) is True
    assert tr.best_crop is crop and tr.best_crop_source == "playback" and tr.best_crop_skew_s == 0.1
    assert tk.evidence_stats["playback"] == 1
    # a result for an older moment than the latest request is ignored
    tk.update([Detection("person", (102, 100, 212, 320), 0.95)], sub, frame_ts=11.0)
    assert tr.best_crop_req_mono == 11.0
    stale = EvidenceResult(filed[0], "accepted", crop=crop, frame=main, score=0.9, skew_s=0.1)
    assert tk.apply_evidence_result(stale) is True and tr.best_crop_source != "playback"
    # an unknown track: not taken (the visit may be held in the worker)
    assert tk.apply_evidence_result(EvidenceResult(EvidenceRequest(99, 1.0, box, SUB, None), "accepted", crop=crop)) is False
