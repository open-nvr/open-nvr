# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Adaptive decode (DETECT_DECODE_IDLE): quiet cameras decode keyframes only;
the first sign of activity restores full decode. The state machine is pure
(AdaptiveDecode), and the source-side flip is a terminate-and-respawn with
the new -skip_frame and no restart backoff."""
from __future__ import annotations

import io

from detect_pipeline.frame_source import FrameSource
from detect_pipeline.ffmpeg_presets import frame_size_bytes
from detect_pipeline.service import AdaptiveDecode


class _R:
    """Minimal FrameResult stand-in."""
    def __init__(self, tracks=(), motion=(), calibrating=False):
        self.tracks = list(tracks)
        self.motion_boxes = list(motion)
        self.calibrating = calibrating


def _clockat(t):
    return lambda: t[0]


def test_starts_active_and_idles_after_quiet_period():
    t = [0.0]
    ad = AdaptiveDecode("nokey", "nonref", idle_after=60, _clock=_clockat(t))
    assert ad.mode == AdaptiveDecode.ACTIVE and ad.skip == "nonref"
    assert ad.observe(_R(), now=30.0) is False          # quiet, but not long enough
    assert ad.observe(_R(), now=61.0) is True           # quiet past the threshold
    assert ad.mode == AdaptiveDecode.IDLE and ad.skip == "nokey"


def test_first_activity_promotes_immediately():
    t = [0.0]
    ad = AdaptiveDecode("nokey", "nonref", idle_after=60, _clock=_clockat(t))
    ad.observe(_R(), now=61.0)                          # -> IDLE
    assert ad.observe(_R(motion=[(0, 0, 5, 5)]), now=62.0) is True
    assert ad.mode == AdaptiveDecode.ACTIVE
    # and the quiet timer restarted: not idle again until 62+60
    assert ad.observe(_R(), now=100.0) is False
    assert ad.observe(_R(), now=123.0) is True


def test_tracks_and_calibration_count_as_activity():
    ad = AdaptiveDecode("nokey", "nonref", idle_after=60)
    assert ad.observe(_R(tracks=["t1"]), now=100.0) is False       # already active
    assert ad.observe(_R(), now=159.0) is False                    # 59s quiet
    assert ad.observe(_R(calibrating=True), now=160.0) is False    # calibration resets
    assert ad.observe(_R(), now=219.0) is False
    assert ad.observe(_R(), now=221.0) is True                     # finally idle


def test_set_decode_skip_respawns_with_new_mode_and_no_backoff():
    size = frame_size_bytes(16, 16)
    spawned = []
    slept = []

    class _Proc:
        def __init__(self):
            self.stdout = io.BytesIO(b"\x00" * size)   # one frame then EOF
        def poll(self): return 0
        def terminate(self): pass
        def wait(self, timeout=None): return 0
        def kill(self): pass

    def spawn(argv):
        spawned.append(argv)
        return _Proc()

    src = FrameSource(
        "rtsp://mtx/cam1-sub", width=16, height=16, fps=2,
        decode_skip="nonref", spawn=spawn, max_restarts=1,
        _sleep=slept.append,
    )
    frames = src.stream()
    next(frames)                                       # first spawn is live
    assert "-skip_frame" in spawned[0]
    assert spawned[0][spawned[0].index("-skip_frame") + 1] == "noref"   # ffmpeg token for nonref
    src.set_decode_skip("nokey")                       # flip mid-stream
    for _ in frames:                                   # drain to the respawn
        pass
    assert len(spawned) == 2
    assert spawned[1][spawned[1].index("-skip_frame") + 1] == "nokey"
    assert slept == []                                 # deliberate flip: no backoff
