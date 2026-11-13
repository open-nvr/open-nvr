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
