# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""The RTSP reader, without an RTSP server.

Everything here runs against an in-memory byte stream and a fake
process, because the parts worth testing — where a frame ends, what
happens when the feed dies, which clock a frame carries — are not the
parts that need a camera.
"""
from __future__ import annotations

import io
import threading
import time

import pytest

from opennvr_app_sdk.rtsp import (
    Frame,
    RtspFrameStream,
    build_command,
    capture_wall,
    read_frames,
)

W, H = 4, 2
FRAME_BYTES = W * H * 3


def _frame(fill: int) -> bytes:
    return bytes([fill]) * FRAME_BYTES


class FakeProc:
    """A Popen-alike whose stdout is whatever bytes the test supplies."""

    def __init__(self, payload: bytes, *, block_after: bool = False):
        self.stdout = io.BytesIO(payload)
        self.terminated = False
        self._block_after = block_after

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


# ── framing ──────────────────────────────────────────────────────────


def test_frames_are_split_on_exact_size():
    stream = io.BytesIO(_frame(1) + _frame(2) + _frame(3))
    got = list(read_frames(stream, FRAME_BYTES))
    assert [g[0] for g in got] == [1, 2, 3]
    assert all(len(g) == FRAME_BYTES for g in got)


def test_a_torn_final_frame_is_dropped_not_yielded():
    """Half a frame is not a frame. Yielding it would hand the app an
    array of the wrong shape at exactly the moment the feed died."""
    stream = io.BytesIO(_frame(1) + _frame(2)[: FRAME_BYTES // 2])
    assert len(list(read_frames(stream, FRAME_BYTES))) == 1


# ── the command ──────────────────────────────────────────────────────


def test_the_command_asks_for_tcp_and_no_audio():
    argv = build_command("rtsp://host/cam1", width=640, fps=10)
    assert argv[0] == "ffmpeg"
    assert "-rtsp_transport" in argv and "tcp" in argv
    assert "-an" in argv                      # nothing here listens
    assert "scale=640:-2,fps=10" in " ".join(argv)
    assert argv[-1] == "pipe:1"


# ── capture time ─────────────────────────────────────────────────────


def test_capture_time_is_measured_by_age_not_by_an_anchor():
    """A frame decoded 2s ago reads as 2s ago, however long the process
    has been up — the property a stored anchor loses to clock drift."""
    wall = capture_wall(100.0, _mono=lambda: 102.0, _wall=lambda: 1_000.0)
    assert wall == pytest.approx(998.0)


def test_every_frame_carries_both_clocks():
    stream = RtspFrameStream("rtsp://x/y", size=(W, H),
                             spawn=lambda argv: FakeProc(_frame(7)))
    stream.start()
    try:
        frame = stream.latest(timeout=5.0)
        assert frame is not None
        assert frame.mono_ts > 0 and frame.wall_ts > 0
        # The wall stamp is now-ish, not epoch zero or a monotonic value.
        assert abs(frame.wall_ts - time.time()) < 5.0
    finally:
        stream.close()


# ── newest wins ──────────────────────────────────────────────────────


def test_a_slow_reader_gets_the_newest_frame_not_a_backlog():
    """The decision this class exists for: when the app is behind, it
    must reason about NOW, not about a queue of stale frames."""
    payload = b"".join(_frame(i) for i in range(1, 21))
    stream = RtspFrameStream("rtsp://x/y", size=(W, H),
                             spawn=lambda argv: FakeProc(payload))
    stream.start()
    try:
        time.sleep(0.4)                       # let them all arrive
        frame = stream.latest(timeout=2.0)
        assert frame is not None
        assert frame.data[0] == 20            # the last one, not the first
        assert stream.dropped > 0             # and it says so
    finally:
        stream.close()


# ── restarts ─────────────────────────────────────────────────────────


def test_a_dead_feed_reconnects_and_says_so():
    """A camera reboot is a Tuesday, not an exception. The app is told
    the stream restarted so it can abandon half-finished work."""
    attempts: list[int] = []

    def spawn(argv):
        attempts.append(1)
        return FakeProc(_frame(len(attempts)))

    stream = RtspFrameStream("rtsp://x/y", size=(W, H), spawn=spawn)
    stream.start()
    try:
        first = stream.latest(timeout=5.0)
        assert first is not None and not first.restarted
        deadline = time.time() + 10.0
        later = None
        while time.time() < deadline:
            f = stream.latest(timeout=2.0)
            if f is not None and f.restarted:
                later = f
                break
        assert later is not None, "stream never reconnected"
        assert stream.restarts >= 1
    finally:
        stream.close()


def test_the_url_is_re_read_on_every_reconnect():
    """Stream grants expire. Re-reading the URL each time is how a
    token renews without anyone catching a 401 mid-screening."""
    urls: list[str] = []

    def factory():
        urls.append(f"rtsp://host/cam?jwt=token{len(urls)}")
        return urls[-1]

    stream = RtspFrameStream(url_factory=factory, size=(W, H),
                             spawn=lambda argv: FakeProc(_frame(1)))
    stream.start()
    try:
        deadline = time.time() + 8.0
        while len(urls) < 2 and time.time() < deadline:
            stream.latest(timeout=1.0)
        assert len(urls) >= 2
        assert urls[0] != urls[1]
    finally:
        stream.close()


def test_closing_stops_the_thread():
    stream = RtspFrameStream("rtsp://x/y", size=(W, H),
                             spawn=lambda argv: FakeProc(_frame(1)))
    stream.start()
    stream.close()
    time.sleep(0.2)
    assert all(t.name != "frames-rtsp" for t in threading.enumerate())


def test_health_is_about_recent_frames():
    stream = RtspFrameStream("rtsp://x/y", size=(W, H),
                             spawn=lambda argv: FakeProc(_frame(1)))
    assert stream.healthy is False            # nothing seen yet
    stream.start()
    try:
        assert stream.latest(timeout=5.0) is not None
        assert stream.healthy is True
    finally:
        stream.close()


def test_a_frame_becomes_an_array_of_the_right_shape():
    np = pytest.importorskip("numpy")
    frame = Frame(data=_frame(3), width=W, height=H, seq=1, mono_ts=1.0)
    arr = frame.to_ndarray()
    assert arr.shape == (H, W, 3)
    assert int(arr[0][0][0]) == 3
