# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The capture pool returns the CURRENT frame, not the next queued one."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import frame_capture
from services.frame_capture import PersistentCapturePool


class _QueuedCapture:
    """A cv2-shaped capture with a backlog: grabs of queued frames return
    at once; a grab past the queue 'waits' for the stream (sleeps) and
    then yields the live frame."""

    def __init__(self, url, queued, live_wait=0.03):
        self.url = url
        self.queued = list(queued)
        self.live_wait = live_wait
        self.live_no = 0
        self.current = None
        self.grabs = 0

    def isOpened(self):  # noqa: N802 - cv2 spelling
        return True

    def grab(self):
        self.grabs += 1
        if self.queued:
            self.current = self.queued.pop(0)
            return True
        time.sleep(self.live_wait)
        self.live_no += 1
        self.current = f"live-{self.live_no}"
        return True

    def retrieve(self):
        return (self.current is not None), self.current

    def read(self):
        ok = self.grab()
        return ok, self.current

    def release(self):
        pass


def _pool(cap):
    return PersistentCapturePool(capture_factory=lambda url: cap,
                                 encode_jpeg=lambda frame, q: str(frame).encode())


def test_a_stale_backlog_is_drained_and_the_live_frame_returned():
    cap = _QueuedCapture("rtsp://cam", queued=[f"old-{i}" for i in range(40)])
    pool = _pool(cap)
    assert pool.get_jpeg(1, "rtsp://cam") == b"live-1"
    assert cap.grabs == 41, "40 queued grabs, then the one that waited"
    # Next call: nothing queued, one grab waits, that is the frame.
    assert pool.get_jpeg(1, "rtsp://cam") == b"live-2"


def test_the_drain_is_bounded(monkeypatch):
    monkeypatch.setattr(frame_capture, "DRAIN_MAX_GRABS", 10)
    cap = _QueuedCapture("rtsp://cam", queued=[f"old-{i}" for i in range(500)])
    pool = _pool(cap)
    out = pool.get_jpeg(1, "rtsp://cam")
    assert out is not None and cap.grabs == 11


class _ReadOnlyCapture:
    def __init__(self, url):
        self.n = 0

    def isOpened(self):  # noqa: N802 - cv2 spelling
        return True

    def read(self):
        self.n += 1
        return True, f"frame-{self.n}"

    def release(self):
        pass


def test_a_capture_without_grab_still_works():
    pool = PersistentCapturePool(capture_factory=_ReadOnlyCapture,
                                 encode_jpeg=lambda frame, q: str(frame).encode())
    assert pool.get_jpeg(1, "rtsp://cam") == b"frame-1"
    assert pool.get_jpeg(1, "rtsp://cam") == b"frame-2"


class _Camera:
    """A 25 fps camera on a fake clock. Each capture opened on it queues
    the frames the camera produced since that capture's last grab, the
    way FFmpeg does for a persistent RTSP session (#796); a fresh capture
    starts at the live edge."""

    FPS = 25

    def __init__(self):
        self.now = 1000.0
        self.opened = []

    def clock(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        for cap in self.opened:
            if not cap.released:
                cap.queued.extend(f"t={self.now - seconds + i / self.FPS:.2f}"
                                  for i in range(int(seconds * self.FPS)))

    def factory(self, url):
        cam = self

        class _Cap(_QueuedCapture):
            released = False

            def grab(self):
                if self.queued:
                    return super().grab()                  # instant, the past
                ok = super().grab()                        # waits for the stream
                self.current = f"t={cam.now:.2f}"          # the live frame
                return ok

            def release(self):
                self.released = True

        cap = _Cap(url, queued=[], live_wait=0.03)
        self.opened.append(cap)
        return cap

    def pool(self):
        return PersistentCapturePool(capture_factory=self.factory, clock=self.clock,
                                     encode_jpeg=lambda frame, q: str(frame).encode())


def test_after_a_long_gap_the_capture_is_reopened_and_the_frame_is_live():
    cam = _Camera()
    pool = cam.pool()
    assert pool.get_jpeg(1, "rtsp://cam") == b"t=1000.00"
    cam.advance(150)                      # an app that looks every 150 s
    assert pool.get_jpeg(1, "rtsp://cam") == b"t=1150.00", "not the 2.5-minute-old frame"
    assert len(cam.opened) == 2 and cam.opened[0].released
    assert cam.opened[0].grabs == 1, "the stale capture was not drained first"


def test_a_short_gap_is_drained_without_reopening():
    cam = _Camera()
    pool = cam.pool()
    pool.get_jpeg(1, "rtsp://cam")
    cam.advance(3)                        # 75 queued frames
    assert pool.get_jpeg(1, "rtsp://cam") == b"t=1003.00"
    assert len(cam.opened) == 1


def test_a_backlog_deeper_than_the_drain_reopens_even_below_the_idle_limit():
    cam = _Camera()
    pool = cam.pool()
    pool.get_jpeg(1, "rtsp://cam")
    cam.advance(10)                       # 250 queued: more than DRAIN_MAX_GRABS
    assert 10 < frame_capture.REOPEN_AFTER_IDLE_SECONDS
    assert pool.get_jpeg(1, "rtsp://cam") == b"t=1010.00"
    assert len(cam.opened) == 2


def test_a_grab_failing_mid_drain_reopens_instead_of_returning_the_backlog():
    cam = _Camera()
    pool = cam.pool()
    pool.get_jpeg(1, "rtsp://cam")
    cam.advance(2)
    broken = cam.opened[0]
    real_grab = broken.grab
    calls = {"n": 0}

    def grab():
        calls["n"] += 1
        return real_grab() if calls["n"] <= 3 else False   # dies in the backlog

    broken.grab = grab
    assert pool.get_jpeg(1, "rtsp://cam") == b"t=1002.00"
    assert len(cam.opened) == 2


def test_a_failed_first_grab_reopens_like_a_failed_read():
    class _Dead(_QueuedCapture):
        def grab(self):
            return False

    made = []

    def factory(url):
        cap = _Dead(url, queued=[]) if not made else _QueuedCapture(url, queued=["x"])
        made.append(cap)
        return cap

    pool = PersistentCapturePool(capture_factory=factory, encode_jpeg=lambda f, q: str(f).encode())
    assert pool.get_jpeg(1, "rtsp://cam") is not None
    assert len(made) == 2
