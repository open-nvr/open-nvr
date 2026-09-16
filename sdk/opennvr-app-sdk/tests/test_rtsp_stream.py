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
    """A Popen-alike whose stdout is whatever bytes the test supplies.

    It has a stderr too, because the real one does and the reader reads
    it — a fake without it hides the crash it would cause.
    """

    def __init__(self, payload: bytes, *, complaint: bytes = b""):
        self.stdout = io.BytesIO(payload)
        self.stderr = io.BytesIO(complaint)
        self.terminated = False

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


def test_ffmpeg_is_quoted_when_a_stream_never_starts(caplog):
    """Its stderr used to go to /dev/null, so every death read as the
    same shrug — "stream ended" — while ffmpeg had been saying exactly
    what was wrong the whole time."""
    proc = FakeProc(b"", complaint=b"Server returned 401 Unauthorized\n")
    stream = RtspFrameStream("rtsp://x/y", size=(W, H), spawn=lambda argv: proc)
    with caplog.at_level("WARNING"):
        stream.start()
        time.sleep(0.5)
        stream.close()
    assert any("401" in r.getMessage() for r in caplog.records), caplog.text


def test_a_bug_in_the_reader_is_not_reported_as_a_camera_fault(caplog):
    """A missing import once crashed every session instantly, and the
    only symptom was a stream that reconnected for ever. A fault in our
    own code must say so."""
    def exploding_spawn(argv):
        raise RuntimeError("boom in the reader")

    stream = RtspFrameStream("rtsp://x/y", size=(W, H), spawn=exploding_spawn)
    with caplog.at_level("ERROR"):
        stream.start()
        time.sleep(0.5)
        stream.close()
    assert any("bug" in r.getMessage() for r in caplog.records), caplog.text


# ── a wedged stream ──────────────────────────────────────────────────
#
# The failure these cover is the one STALL_TIMEOUT_S was written for and
# nothing enforced: a camera that accepts the TCP connection and then
# stops sending. ffmpeg stays up, the reader stays parked in a blocking
# read on its stdout, and read_frames never returns — so the supervisor,
# which only restarts when a session ENDS, never restarts. `healthy`
# reported the stall to anyone who asked and nothing acted on it.


class BlockingProc:
    """A Popen-alike whose stdout blocks until the process is killed.

    This is what a wedged camera looks like from inside the reader: the
    connection is open, the pipe is open, and nothing ever arrives.
    """

    def __init__(self, payload: bytes = b""):
        self.stderr = io.BytesIO(b"")
        self.terminated = threading.Event()
        self.stdout = self._Stdout(self.terminated, payload)
        self.kills = 0

    class _Stdout:
        def __init__(self, gate, payload):
            self._gate = gate
            self._pending = payload

        def read(self, n):
            if self._pending:
                out, self._pending = self._pending[:n], self._pending[n:]
                return out
            # Blocks exactly as a real pipe does, and returns EOF only
            # when the process is killed.
            self._gate.wait(timeout=30)
            return b""

    def terminate(self):
        self.kills += 1
        self.terminated.set()

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminate()


def _stalling_stream(monkeypatch, proc, *, timeout=0.3):
    monkeypatch.setattr("opennvr_app_sdk.rtsp.STALL_TIMEOUT_S", timeout)
    return RtspFrameStream(url="rtsp://camera/stalls", size=(W, H),
                           spawn=lambda argv: proc, name="stall-test")


def test_a_stalled_stream_is_killed_and_restarted(monkeypatch):
    """One frame, then silence for longer than the stall timeout: the
    watchdog kills ffmpeg, which ends the read, which ends the session,
    which reconnects. Before this the stream sat there for ever."""
    procs = []

    def spawn(argv):
        proc = BlockingProc(_frame(1) if not procs else b"")
        procs.append(proc)
        return proc

    monkeypatch.setattr("opennvr_app_sdk.rtsp.STALL_TIMEOUT_S", 0.3)
    monkeypatch.setattr("opennvr_app_sdk.rtsp.FIRST_BACKOFF_S", 0.05)
    stream = RtspFrameStream(url="rtsp://camera/stalls", size=(W, H),
                             spawn=spawn, name="stall-test")
    stream.start()
    try:
        assert stream.latest(timeout=5.0) is not None, "the first frame never arrived"
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and len(procs) < 2:
            time.sleep(0.05)
        assert len(procs) >= 2, "the wedged session was never restarted"
        assert procs[0].kills >= 1, "the wedged ffmpeg was never killed"
    finally:
        stream.close()


def test_a_stream_that_is_delivering_is_left_alone(monkeypatch):
    """The other half: the watchdog must not kill a slow-but-live feed.
    A camera at 1 fps is a camera, not a fault."""
    proc = BlockingProc(_frame(1) * 6)
    stream = _stalling_stream(monkeypatch, proc, timeout=5.0)
    stream.start()
    try:
        assert stream.latest(timeout=5.0) is not None
        time.sleep(0.5)
        assert proc.kills == 0, "a live stream was killed by the stall watchdog"
    finally:
        stream.close()


def test_close_kills_ffmpeg_and_stops_the_thread(monkeypatch):
    """close() used to join a thread that was parked in a blocking read
    and could not see the stop flag, so it burned its five seconds and
    returned with ffmpeg still decoding. An app that reopens a stream per
    screening leaked one process per cycle."""
    proc = BlockingProc(_frame(1))
    stream = _stalling_stream(monkeypatch, proc, timeout=30.0)
    stream.start()
    assert stream.latest(timeout=5.0) is not None

    started = time.monotonic()
    stream.close()
    elapsed = time.monotonic() - started

    assert proc.kills >= 1, "ffmpeg was left running"
    assert elapsed < 4.0, f"close() blocked for {elapsed:.1f}s on the join"
    assert stream._thread is None


def test_a_closed_stream_can_be_started_again(monkeypatch):
    """close() sets the stop flag and it used to stay set for the life of
    the object, so a later start() spawned a thread that returned at once
    and delivered nothing — for ever, silently."""
    procs = []

    def spawn(argv):
        proc = BlockingProc(_frame(len(procs) + 1))
        procs.append(proc)
        return proc

    monkeypatch.setattr("opennvr_app_sdk.rtsp.STALL_TIMEOUT_S", 30.0)
    stream = RtspFrameStream(url="rtsp://camera/x", size=(W, H),
                             spawn=spawn, name="reopen-test")
    stream.start()
    assert stream.latest(timeout=5.0) is not None
    stream.close()

    stream.start()
    try:
        assert stream.latest(timeout=5.0) is not None, (
            "a reopened stream delivered no frames")
        assert len(procs) >= 2
    finally:
        stream.close()
