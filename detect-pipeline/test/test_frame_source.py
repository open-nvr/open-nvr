# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the continuous frame source (no real ffmpeg involved)."""
from __future__ import annotations

import io
import time

from detect_pipeline.ffmpeg_presets import HwAccel, frame_size_bytes
from detect_pipeline.frame_source import Frame, FrameSource, read_frames

W, H = 8, 6                       # tiny frames: 8*6*3//2 = 72 bytes each
SIZE = frame_size_bytes(W, H)


def _synthetic(n: int, extra: int = 0) -> bytes:
    """n full frames of distinct byte values, plus `extra` trailing junk bytes."""
    body = b"".join(bytes([i % 256]) * SIZE for i in range(n))
    return body + (b"\xab" * extra)


def test_read_frames_yields_all_full_frames():
    frames = list(read_frames(io.BytesIO(_synthetic(3)), W, H, _clock=lambda: 1.0))
    assert [f.seq for f in frames] == [0, 1, 2]
    assert all(len(f.data) == SIZE for f in frames)
    assert all(f.ts == 1.0 for f in frames)


def test_read_frames_discards_partial_tail():
    # 2 full frames + 5 leftover bytes → exactly 2 frames, tail dropped
    frames = list(read_frames(io.BytesIO(_synthetic(2, extra=5)), W, H))
    assert len(frames) == 2


def test_read_frames_empty_stream():
    assert list(read_frames(io.BytesIO(b""), W, H)) == []


def test_y_plane_is_luma_only():
    f = Frame(bytes(range(1)) * SIZE, W, H, 0, 0.0)
    assert len(f.y_plane) == W * H            # luma only, not the full 12bpp frame
    assert len(f.data) == SIZE


class _FakeProc:
    def __init__(self, payload: bytes):
        self.stdout = io.BytesIO(payload)
        self._done = False

    def poll(self):
        return 0 if self._done else None

    def terminate(self):
        self._done = True

    def wait(self, timeout=None):
        self._done = True
        return 0


def test_frame_source_streams_then_stops_and_terminates():
    procs: list[_FakeProc] = []

    def spawn(argv):
        p = _FakeProc(_synthetic(3))
        procs.append(p)
        return p

    fs = FrameSource(
        "rtsp://h:8554/cam_sub", width=W, height=H, fps=5,
        spawn=spawn, max_restarts=0, backoff_seconds=0, _sleep=lambda s: None,
    )
    frames = list(fs.stream())
    assert len(frames) == 3
    assert len(procs) == 1               # spawned once (max_restarts=0)
    assert procs[0].poll() == 0          # ffmpeg was terminated on exit


def test_frame_source_restarts_up_to_cap():
    calls = {"n": 0}

    def spawn(argv):
        calls["n"] += 1
        return _FakeProc(_synthetic(2))

    fs = FrameSource(
        "rtsp://h:8554/cam_sub", width=W, height=H, fps=5,
        spawn=spawn, max_restarts=1, backoff_seconds=0, _sleep=lambda s: None,
    )
    frames = list(fs.stream())
    # 2 drains (initial + 1 restart) × 2 frames each = 4
    assert len(frames) == 4
    assert calls["n"] == 2


def test_command_delegates_to_preset_builder():
    fs = FrameSource(
        "rtsp://h:8554/cam_sub", width=640, height=360, fps=5, hwaccel=HwAccel.VAAPI,
    )
    cmd = fs.command()
    assert cmd[0] == "ffmpeg"
    assert "rtsp://h:8554/cam_sub" in cmd
    assert "-vf" in cmd


# ── fruitless-restart give-up (expired-ticket zombie) ──────────────

class _EmptyProc:
    """ffmpeg that exits immediately with no output (401 on a dead ticket)."""
    def __init__(self):
        import io
        self.stdout = io.BytesIO(b"")
    def poll(self): return 0
    def wait(self, timeout=None): return 0
    def terminate(self): pass
    def kill(self): pass


def test_gives_up_after_consecutive_fruitless_restarts():
    from detect_pipeline.frame_source import FrameSource

    spawns = []
    src = FrameSource(
        "rtsp://cam/dead-ticket", width=4, height=4, fps=5,
        spawn=lambda cmd: (spawns.append(1), _EmptyProc())[1],
        max_fruitless_restarts=3, _sleep=lambda s: None,
    )
    frames = list(src.stream())
    assert frames == []
    # exactly max_fruitless_restarts spawns, then it ends (worker dies →
    # manager reconcile resurrects it with a FRESH tap URL)
    assert len(spawns) == 3


def test_fruitless_counter_resets_after_a_good_cycle():
    from detect_pipeline.frame_source import FrameSource, frame_size_bytes

    class _OneFrameProc:
        def __init__(self):
            import io
            self.stdout = io.BytesIO(b"\x00" * frame_size_bytes(4, 4))
        def poll(self): return 0
        def wait(self, timeout=None): return 0
        def terminate(self): pass
        def kill(self): pass

    # good, empty, good, empty, empty, empty -> gives up only after the 3
    # consecutive empties at the end
    plan = [_OneFrameProc(), _EmptyProc(), _OneFrameProc(),
            _EmptyProc(), _EmptyProc(), _EmptyProc()]
    it = iter(plan)
    src = FrameSource(
        "rtsp://cam/flaky", width=4, height=4, fps=5,
        spawn=lambda cmd: next(it),
        # min_healthy_seconds=0 → any frame counts as a healthy session, the
        # rule this test was written for. The duration rule has its own test.
        max_fruitless_restarts=3, min_healthy_seconds=0, _sleep=lambda s: None,
    )
    frames = list(src.stream())
    assert len(frames) == 2


# ── stream robustness: stalls, flapping, and ffmpeg's own reason ────

def test_short_sessions_do_not_reset_the_giveup_counter():
    """A stream that connects, emits one frame and dies is NOT healthy.

    Counting any single frame as success let a flapping camera restart
    forever without ever reaching the give-up path — so it never got a
    fresh tap URL, which is the only thing that can fix an expired ticket.
    """
    from detect_pipeline.frame_source import FrameSource, frame_size_bytes

    class _BlipProc:
        def __init__(self):
            import io
            self.stdout = io.BytesIO(b"\x00" * frame_size_bytes(4, 4))
        def poll(self): return 0
        def wait(self, timeout=None): return 0
        def terminate(self): pass
        def kill(self): pass

    spawns = []
    src = FrameSource(
        "rtsp://cam/flapping", width=4, height=4, fps=5,
        spawn=lambda cmd: (spawns.append(1), _BlipProc())[1],
        max_fruitless_restarts=3,
        min_healthy_seconds=5.0,      # instant blips are below this
        _sleep=lambda s: None,
    )
    frames = list(src.stream())
    # Each blip yields its one frame, but none resets the counter, so the
    # source gives up after exactly max_fruitless_restarts attempts.
    assert len(spawns) == 3
    assert len(frames) == 3


def test_restart_backoff_escalates_and_is_capped():
    slept: list[float] = []
    src = FrameSource(
        "rtsp://cam/dead", width=4, height=4, fps=5,
        spawn=lambda cmd: _EmptyProc(),
        max_fruitless_restarts=6, backoff_seconds=1.0, max_backoff_seconds=4.0,
        _sleep=slept.append,
    )
    list(src.stream())
    # 1, 2, 4, then capped at 4 — never a flat re-dial rate.
    assert slept[:3] == [1.0, 2.0, 4.0]
    assert all(s <= 4.0 for s in slept)


def test_stderr_flood_does_not_deadlock_the_reader():
    """ffmpeg writing more than the pipe buffer must not stall the decode.

    With stderr=PIPE and nobody draining it, the ~64KB kernel buffer fills,
    the child BLOCKS writing to stderr, and stdout stops — a permanent hang
    that looks exactly like a dead camera. Uses a REAL subprocess, because
    the deadlock is a kernel pipe behaviour a fake cannot reproduce.
    """
    import subprocess
    import sys

    from detect_pipeline.frame_source import FrameSource, frame_size_bytes

    size = frame_size_bytes(4, 4)
    # Write 512KB of stderr (8x the typical buffer) interleaved with frames.
    child = (
        "import sys;"
        f"n={size};"
        "sys.stderr.buffer.write(b'x'*262144);sys.stderr.buffer.flush();"
        "sys.stdout.buffer.write(b'\\0'*n);sys.stdout.buffer.flush();"
        "sys.stderr.buffer.write(b'y'*262144);sys.stderr.buffer.flush();"
        "sys.stdout.buffer.write(b'\\1'*n);sys.stdout.buffer.flush();"
    )

    def spawn(argv):
        return subprocess.Popen(
            [sys.executable, "-c", child],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    src = FrameSource(
        "rtsp://cam/chatty", width=4, height=4, fps=5,
        spawn=spawn, max_restarts=0, backoff_seconds=0, _sleep=lambda s: None,
    )
    frames = list(src.stream())
    assert len(frames) == 2, "stderr flood stalled the frame reader"


def test_stderr_tail_keeps_ffmpegs_reason():
    from detect_pipeline.frame_source import _StderrTail

    tail = _StderrTail(io.BytesIO(b"Connection refused\nbad ticket 401\n"), keep=6)
    # The drain runs on a daemon thread; give it a moment to consume.
    for _ in range(200):
        if tail.text():
            break
        time.sleep(0.01)
    assert "401" in tail.text()


def test_stderr_tail_tolerates_a_proc_without_stderr():
    from detect_pipeline.frame_source import _StderrTail

    assert _StderrTail(None).text() == ""       # injected fakes have no stderr


def test_decode_flips_never_count_toward_giveup():
    """Adaptive decode terminates ffmpeg on purpose — that is not a failure.

    Flips are short-lived by nature (a scene waking the camera), so if they
    counted toward the fruitless budget a healthy camera with intermittent
    motion would be torn down as though its feed were dead.
    """
    from detect_pipeline.frame_source import FrameSource, frame_size_bytes

    size = frame_size_bytes(4, 4)
    spawns = []

    def spawn(cmd):
        spawns.append(1)
        p = _FakeProc(b"\x00" * size)
        return p

    src = FrameSource(
        "rtsp://cam/adaptive", width=4, height=4, fps=5,
        spawn=spawn, max_fruitless_restarts=3,
        min_healthy_seconds=5.0,        # every fake session is instant
        max_restarts=6, backoff_seconds=0, _sleep=lambda s: None,
    )
    stream = src.stream()
    # Consume a frame, then flip, repeatedly — more flips than the fruitless
    # budget would allow if flips were penalised.
    for _ in range(6):
        next(stream)
        src.set_decode_skip("nokey")
    # Still alive after 6 deliberate flips (budget was 3).
    assert len(spawns) >= 6
