# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Evidence from the recording (playback.py): the request/result contract,
candidate selection by time and by the guard, budgets, and how the tracker
and the worker take a result — with the fetch, probe and decode injected."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

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


def test_a_served_result_keeps_the_track_pending_until_it_is_collected():
    """Between two frames the thread may serve a result; a visit ending on
    the next frame must still be held for it, so the track stays pending
    until the worker drains the result."""
    rng = np.random.default_rng(11)
    box = (100, 100, 200, 300)
    sub = _scene(rng, SUB, subject=box)
    frames = [np.kron(sub, np.ones((2, 2, 1), np.uint8))] * 10
    clock = _Clock(1002.0)
    pb = _playback(frames, clock=clock)
    pb.request(EvidenceRequest(4, 1000.0, box, SUB, _crop_bgr(sub, box, margin=0.0)))
    t0 = time.time()
    while time.time() - t0 < 3.0 and pb.outcomes.get("accepted", 0) < 1:
        time.sleep(0.01)
    assert pb.outcomes.get("accepted") == 1, "served"
    assert pb.pending_for(4), "served but not collected: still pending"
    res, = pb.results()
    assert res.track_id == 4 and not pb.pending_for(4)
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


def test_a_tentative_track_files_its_request_only_once_confirmed():
    """At 5 fps a track needs two hits to be confirmed; the first frame's
    request waits, is filed on confirmation for that best moment, and a
    track that dies tentative files nothing."""
    rng = np.random.default_rng(6)
    box = (100, 100, 200, 300)
    sub = _scene(rng, SUB, subject=box)
    filed = []
    tk = Tracker(SUB, TrackConfig(fps=5, min_initialized=2))
    tk.retain_scene = False
    tk.evidence_request = filed.append
    tk.update([Detection("person", box, 0.8)], sub, frame_ts=10.0)
    tr = tk._tracks[0]
    assert not tr.confirmed and filed == [] and tr.evidence_req_held is not None
    assert tr.best_crop_req_mono is None
    tk.update([Detection("person", box, 0.8)], sub, frame_ts=10.2)
    assert tr.confirmed and len(filed) == 1 and filed[0].mono_ts == 10.0
    assert tr.best_crop_req_mono == 10.0 and tr.evidence_req_held is None
    # a second person's box that is seen once and gone: no request
    tk.update([Detection("person", box, 0.8), Detection("person", (400, 100, 500, 300), 0.7)], sub, frame_ts=10.4)
    tk.update([Detection("person", box, 0.8)], sub, frame_ts=10.6)
    assert len(tk._tracks) == 1 and len(filed) == 1


def test_playback_results_leave_samples_and_dump_pairs(tmp_path):
    rng = np.random.default_rng(8)
    box = (100, 100, 200, 300)
    sub = _scene(rng, SUB, subject=box)
    tk = _tracker()
    tk.evidence_debug_dir, tk.evidence_debug_camera = str(tmp_path), "cam5"
    req = EvidenceRequest(3, 10.0, box, SUB, _crop_bgr(sub, box, margin=0.0))
    main = np.kron(sub, np.ones((2, 2, 1), np.uint8))
    tk.note_playback_result(EvidenceResult(req, "accepted", crop=_crop_bgr(main, (200, 200, 400, 600)), frame=main,
                                           score=0.97, skew_s=0.12, candidates=4))
    tk.note_playback_result(EvidenceResult(req, "refused", crop=_crop_bgr(main, (200, 200, 400, 600)), score=0.3,
                                           skew_s=0.2, reason="low_corr"))
    tk.note_playback_result(EvidenceResult(req, "error", reason="OSError"))
    s = tk.drain_evidence_samples()
    assert [x[6] for x in s] == ["playback", "playback"] and s[0][2] is True and s[1][3] == "low_corr"
    files = sorted(p.name for p in (tmp_path / "cam5").iterdir())
    assert len(files) == 6 and any(n.endswith("p.json") for n in files)
    import json
    rec = json.loads((tmp_path / "cam5" / [n for n in files if n.endswith("p.json")][0]).read_text())
    assert rec["source"] == "playback" and rec["candidates"] == 4 and rec["accepted"] is True


def test_the_stamp_lead_is_subtracted_and_calibrated_from_accepted_frames():
    """The detect frame's stamp is later than the recorder's clock; the
    request asks for the moment stamp - lead and the lead follows the
    accepted frames' signed offset a quarter of the way each time."""
    rng = np.random.default_rng(9)
    box = (100, 100, 200, 300)
    sub = _scene(rng, SUB, subject=box)
    # 20 frames at 10 fps ending at end = asked moment + 0.3 s; the subject
    # is at the box only on the frame 0.4 s before the end (index 15), i.e.
    # the true moment 999.4; elsewhere it is elsewhere.
    frames = []
    for k in range(20):
        where = box if k == 15 else (400, 100, 500, 300)
        frames.append(np.kron(_scene(rng, SUB, subject=where), np.ones((2, 2, 1), np.uint8)))
    clock = _Clock(1003.0)
    # stamp 1000.0; true moment 999.4 (0.6 s earlier); lead starts at 0.5 →
    # asked moment 999.5, window ±0.5 → the right frame is a candidate
    pb = _playback(frames, clock=clock, stamp_lead_s=0.5)
    pb.request(EvidenceRequest(1, 1000.0, box, SUB, _crop_bgr(sub, box, margin=0.0)))
    res, = _wait_results(pb)
    assert res.outcome == "accepted" and res.score > 0.8
    assert abs(res.signed_skew_s - (-0.1)) < 0.06, "the frame sits 0.1 s before the asked moment"
    assert 0.5 < pb.stamp_lead_s < 0.56, "the lead moved a quarter of the way towards 0.6"
    pb.close()


# ── the worker holds a finished visit for its crop ────────────────────

class _Poster:
    def __init__(self):
        self.posted = []
    def submit(self, visit):
        self.posted.append(visit)


class _FakePlayback:
    def __init__(self):
        self.pending = set()
        self.queue = []
    def pending_for(self, tid):
        return tid in self.pending
    def results(self):
        out, self.queue = self.queue, []
        return out


class _NoTracker:
    """Every track has already ended: no result is taken live."""
    def note_playback_result(self, res):
        pass
    def apply_evidence_result(self, res):
        return False


@dataclass
class _Visit:
    track_id: int
    jpeg: bytes = b"sub"
    scene_jpeg: bytes | None = None
    evidence_source: str | None = "detect"
    evidence_skew_s: float | None = None


def test_a_held_visit_is_patched_when_its_crop_arrives_and_counted_when_the_hold_runs_out():
    from detect_pipeline.service import CameraSpec, CameraWorker
    poster = _Poster()
    w = CameraWorker(CameraSpec("c", "c", "rtsp://h/c"), None, visit_poster=poster, evidence_hold_s=6.0)
    pb = _FakePlayback()
    w._playback = pb
    tracker = _NoTracker()
    box = (10, 10, 50, 90)
    # track 1: its crop is still pending when the visit ends → held, not posted
    pb.pending.add(1)
    w._offer_visit(_Visit(1), now_mono=100.0)
    assert poster.posted == [] and 1 in w._held and w._held[1][1] == 106.0
    # the crop arrives 4 s later (inside the hold): patched in and posted
    main = np.full((40, 40, 3), 200, np.uint8)
    pb.pending.discard(1)
    pb.queue.append(EvidenceResult(EvidenceRequest(1, 99.0, box, SUB, None), "accepted",
                                   crop=main, frame=None, score=0.95, skew_s=0.05))
    w._drain_playback(tracker, now_mono=104.0)
    assert len(poster.posted) == 1 and poster.posted[0].jpeg != b"sub"
    assert poster.posted[0].evidence_source == "playback" and poster.posted[0].evidence_skew_s == 0.05
    assert w._visits_patched == 1 and w._holds_expired == 0 and w._held == {}
    # track 2: nothing comes back within the hold → posted as it was, counted
    pb.pending.add(2)
    w._offer_visit(_Visit(2), now_mono=110.0)
    w._drain_playback(tracker, now_mono=115.9)
    assert len(poster.posted) == 1, "still held just before the deadline"
    w._drain_playback(tracker, now_mono=116.0)
    assert len(poster.posted) == 2 and poster.posted[1].jpeg == b"sub"
    assert w._holds_expired == 1
    # its crop arriving after that is late, not patched
    pb.queue.append(EvidenceResult(EvidenceRequest(2, 109.0, box, SUB, None), "accepted",
                                   crop=main, score=0.9, skew_s=0.1))
    w._drain_playback(tracker, now_mono=117.0)
    assert w._results_late == 1 and len(poster.posted) == 2


def test_a_vehicle_track_hands_the_recording_crop_to_its_plate_candidate():
    """The plate candidate cut at the requested moment takes the full-res
    pixels even when the best crop has since moved to a newer moment."""
    rng = np.random.default_rng(12)
    box = (100, 100, 300, 250)
    sub = _scene(rng, SUB, subject=box)
    filed = []
    tk = _tracker()
    tk.retain_plate_candidates = True
    tk.evidence_request = filed.append
    tk.update([Detection("car", box, 0.8)], sub, frame_ts=10.0)
    tr = tk._tracks[0]
    ring = tr.plate_ring
    assert ring is not None and len(ring) == 1 and ring.ranked()[0].req_mono == 10.0
    tk.update([Detection("car", (100, 100, 330, 280), 0.95)], sub, frame_ts=11.0)   # a better moment
    assert tr.best_crop_req_mono == 11.0 and len(filed) == 2
    main = np.kron(sub, np.ones((2, 2, 1), np.uint8))
    crop = _crop_bgr(main, (200, 200, 600, 500), margin=0.25)
    old = EvidenceResult(filed[0], "accepted", crop=crop, frame=main, score=0.9, skew_s=0.1)
    assert tk.apply_evidence_result(old) is True
    assert tr.best_crop_source != "playback", "the best crop is for the newer moment"
    first = next(c for c in ring.ranked() if c.req_mono == 10.0)
    assert first.crop is crop and first.upgraded and tk.plate_upgrades == 1


def test_the_blur_veto_is_counted_when_a_soft_frame_would_have_won():
    import cv2
    rng = np.random.default_rng(13)
    box = (100, 100, 200, 300)
    sharp = _scene(rng, SUB, subject=box)
    sharp[100:300, 100:200] = rng.integers(0, 255, size=(200, 100, 3), dtype=np.uint8)
    soft = cv2.GaussianBlur(sharp, (0, 0), 5)
    tk = _tracker()
    tk.update([Detection("person", box, 0.7)], sharp, frame_ts=10.0)
    tr = tk._tracks[0]
    best_before = tr.best
    # bigger and higher-scoring, but blurred: refused and counted
    tk.update([Detection("person", (100, 100, 240, 380), 0.9)], soft, frame_ts=11.0)
    assert tr.best is best_before and tk.blur_vetoes == 1
    # the veto off: the same frame wins
    tk.blur_veto = 0.0
    tk.update([Detection("person", (100, 100, 240, 380), 0.9)], soft, frame_ts=12.0)
    assert tr.best is not best_before and tk.blur_vetoes == 1
