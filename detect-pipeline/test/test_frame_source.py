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
        _rand=lambda: 1.0,          # top of the jitter band → the raw schedule
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


# ── rotating tap JWT (60-min lifetime, baked in at worker start) ────

def test_expired_url_heals_on_respawn_without_killing_the_worker():
    """The tap URL's JWT expires after 60 minutes.

    Before, a long-running source kept re-dialling the dead token until it
    burned the give-up budget and the worker died; only a reconcile tick
    could hand it a fresh URL. Now it adopts the freshest URL itself.
    """
    from detect_pipeline.frame_source import FrameSource, frame_size_bytes

    size = frame_size_bytes(4, 4)
    seen: list[str] = []
    current = {"url": "rtsp://mtx/cam-1?jwt=EXPIRED"}

    def spawn(cmd):
        seen.append(next(a for a in cmd if a.startswith("rtsp://")))
        return _FakeProc(b"\x00" * size)

    src = FrameSource(
        "rtsp://mtx/cam-1?jwt=EXPIRED", width=4, height=4, fps=5,
        spawn=spawn, url_provider=lambda: current["url"],
        max_restarts=2, backoff_seconds=0, min_healthy_seconds=0,
        _sleep=lambda s: None,
    )
    stream = src.stream()
    next(stream)                                  # first spawn: expired token
    current["url"] = "rtsp://mtx/cam-1?jwt=FRESH"  # reconcile re-minted it
    for _ in stream:                              # drain remaining respawns
        pass

    assert seen[0].endswith("EXPIRED")
    assert seen[-1].endswith("FRESH"), "source never adopted the refreshed URL"


def test_url_provider_failure_is_survivable():
    from detect_pipeline.frame_source import FrameSource, frame_size_bytes

    def boom():
        raise RuntimeError("core unreachable")

    src = FrameSource(
        "rtsp://mtx/cam-1?jwt=OLD", width=4, height=4, fps=5,
        spawn=lambda cmd: _FakeProc(b"\x00" * frame_size_bytes(4, 4)),
        url_provider=boom, max_restarts=0, backoff_seconds=0,
        _sleep=lambda s: None,
    )
    assert len(list(src.stream())) == 1           # keeps the URL it has
    assert src.rtsp_url.endswith("OLD")


def test_stream_urls_are_redacted_in_logs(caplog):
    """The tap URL's ?jwt= is a live wildcard-read credential."""
    import logging

    from detect_pipeline.frame_source import FrameSource, redact_url

    secret = "rtsp://mtx:8554/cam-1?jwt=eyJhbGciOiJSUzI1NiJ9.SECRETPAYLOAD.SIG"
    assert redact_url(secret) == "rtsp://mtx:8554/cam-1?<redacted>"
    assert "SECRET" not in redact_url(secret)
    assert redact_url("rtsp://cam/no-query") == "rtsp://cam/no-query"

    with caplog.at_level(logging.INFO):
        src = FrameSource(
            secret, width=4, height=4, fps=5,
            spawn=lambda cmd: _EmptyProc(), max_fruitless_restarts=2,
            backoff_seconds=0, _sleep=lambda s: None,
        )
        list(src.stream())
    assert caplog.text, "expected restart/give-up logging"
    assert "SECRETPAYLOAD" not in caplog.text, "JWT leaked into logs"


# ── close(): make a parked source stoppable ────────────────────────

def test_close_unblocks_a_real_blocking_read():
    """A REAL child that never writes: without close() terminating it, the
    reader sits in stdout.read() until the RTSP timeout and the owning
    worker cannot be joined."""
    import subprocess
    import sys
    import threading

    from detect_pipeline.frame_source import FrameSource

    def spawn(argv):
        # Sleeps without ever writing a frame — a stalled feed.
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    src = FrameSource("rtsp://cam/stalled", width=4, height=4, fps=5,
                      spawn=spawn, backoff_seconds=0)
    done = threading.Event()

    def drain():
        list(src.stream())
        done.set()

    threading.Thread(target=drain, daemon=True).start()
    time.sleep(0.4)                      # let it reach the blocking read
    t0 = time.monotonic()
    src.close()
    assert done.wait(timeout=10), "close() did not unblock the reader"
    assert time.monotonic() - t0 < 8.0


def test_close_interrupts_the_restart_backoff():
    """A source waiting out its backoff must stop immediately, not after the
    full (escalating, up to max_backoff_seconds) delay."""
    import threading

    from detect_pipeline.frame_source import FrameSource

    src = FrameSource(
        "rtsp://cam/dead", width=4, height=4, fps=5,
        spawn=lambda cmd: _EmptyProc(),
        max_fruitless_restarts=50,        # would loop a long time
        backoff_seconds=5.0, max_backoff_seconds=30.0,
    )
    done = threading.Event()
    threading.Thread(target=lambda: (list(src.stream()), done.set()), daemon=True).start()
    time.sleep(0.3)                      # now parked in the backoff sleep
    t0 = time.monotonic()
    src.close()
    assert done.wait(timeout=5), "close() did not interrupt the backoff"
    assert time.monotonic() - t0 < 3.0


def test_close_stops_the_loop_from_respawning():
    spawns = []
    from detect_pipeline.frame_source import FrameSource

    src = FrameSource("rtsp://cam/x", width=4, height=4, fps=5,
                      spawn=lambda cmd: (spawns.append(1), _EmptyProc())[1],
                      max_fruitless_restarts=10, backoff_seconds=0,
                      _sleep=lambda s: src.close())   # close during backoff
    list(src.stream())
    assert len(spawns) == 1, "closed source must not respawn ffmpeg"


def test_close_does_not_block_on_the_child():
    """close() must SIGNAL, never reap.

    _terminate() waits up to 3s for the child. Calling it here made close()
    blocking, so a manager stopping N sources paid that serially — measured
    at 15s for 6 real cameras before this was fixed. The child below ignores
    SIGTERM, so a reaping close() would stall for the full wait.
    """
    import signal
    import subprocess
    import sys

    from detect_pipeline.frame_source import FrameSource

    child = (
        "import signal,time;"
        "signal.signal(signal.SIGTERM, lambda *a: None);"   # ignore SIGTERM
        "time.sleep(30)"
    )
    proc = subprocess.Popen([sys.executable, "-c", child],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    src = FrameSource("rtsp://cam/x", width=4, height=4, fps=5,
                      spawn=lambda cmd: proc)
    src._current_proc = proc
    try:
        t0 = time.monotonic()
        src.close()
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"close() blocked for {elapsed:.1f}s — it must not reap"
    finally:
        proc.kill()
        proc.wait(timeout=5)


# ── jitter: a fleet that fails together must not retry together ─────

def test_jittered_spreads_over_the_lower_half():
    from detect_pipeline.frame_source import _jittered

    assert _jittered(4.0, lambda: 0.0) == 2.0      # floor
    assert _jittered(4.0, lambda: 1.0) == 4.0      # ceiling
    assert _jittered(0, lambda: 0.5) == 0.0        # disabled backoff stays off


def test_identical_sources_do_not_retry_in_lockstep():
    """MediaMTX restarts and every camera drops in the same second. With a
    fixed backoff they all re-dial in the same instant, so the herd never
    thins and the storm sustains itself."""
    import random

    from detect_pipeline.frame_source import FrameSource

    def first_retry(rand):
        slept: list[float] = []
        src = FrameSource("rtsp://c/x", width=4, height=4, fps=5,
                          spawn=lambda cmd: _EmptyProc(), max_fruitless_restarts=2,
                          backoff_seconds=4.0, _sleep=slept.append, _rand=rand)
        list(src.stream())
        return slept[0]

    rng = random.Random(1234)
    times = [first_retry(rng.random) for _ in range(30)]
    assert len(set(round(t, 3) for t in times)) > 20, "retries are still synchronised"
    assert all(2.0 <= t <= 4.0 for t in times), times


def test_decode_flip_does_not_block_the_frame_loop():
    """set_decode_skip runs on the worker's frame loop. _terminate() waits up
    to 3s reaping the child, so every adaptive-decode flip — on by default —
    stalled that camera for as long as ffmpeg took to die. close() was written
    not to block for this reason; this path had been missed."""
    import subprocess
    import sys

    from detect_pipeline.frame_source import FrameSource

    child = ("import signal,time;"
             "signal.signal(signal.SIGTERM, lambda *a: None);"   # ignores SIGTERM
             "time.sleep(30)")
    proc = subprocess.Popen([sys.executable, "-c", child],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    src = FrameSource("rtsp://cam/x", width=4, height=4, fps=5,
                      spawn=lambda cmd: proc)
    src._current_proc = proc
    try:
        t0 = time.monotonic()
        src.set_decode_skip("nokey")
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"flip blocked the frame loop for {elapsed:.1f}s"
        assert src.decode_skip == "nokey"
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_probe_failures_never_log_the_jwt():
    """The tap URL's ?jwt= is a live wildcard-read credential. The restart
    logs were redacted; the probe-failure logs added later were not — and a
    powered-off camera hits exactly those, every reconcile tick."""
    import logging
    import subprocess

    from detect_pipeline.frame_source import probe_stream

    secret = "rtsp://mtx:8554/cam-1?jwt=eyJhbGciOiJSUzI1NiJ9.SECRETPAYLOAD.SIG"
    logger = logging.getLogger("detect_pipeline.frame_source")

    class _Capture(logging.Handler):
        def __init__(self): super().__init__(); self.text = ""
        def emit(self, record): self.text += record.getMessage()

    cap = _Capture()
    logger.addHandler(cap)
    try:
        # non-zero rc path
        probe_stream(secret, timeout=0.1)
        # timeout path
        original = subprocess.run

        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=0.1)

        subprocess.run = boom
        try:
            probe_stream(secret, timeout=0.1)
        finally:
            subprocess.run = original
    finally:
        logger.removeHandler(cap)

    assert cap.text, "expected the failure to be logged at all"
    assert "SECRETPAYLOAD" not in cap.text, "the probe logs leaked the JWT"
    assert "<redacted>" in cap.text


def test_child_process_output_is_scrubbed_of_credentials():
    """Redacting the URL we FORMAT is not enough — ffmpeg and ffprobe print
    the URL they were given in their own diagnostics, and this service
    surfaces that output on purpose because it is what makes a failure
    diagnosable. The credential comes back through the child's stderr."""
    from detect_pipeline.frame_source import _StderrTail, scrub_secrets

    line = ("[rtsp @ 0x1] Could not open "
            "rtsp://mtx:8554/cam-1?jwt=eyJhbGciOi.SECRETPAYLOAD.SIG: 401")
    assert "SECRETPAYLOAD" not in scrub_secrets(line)
    assert "jwt=<redacted>" in scrub_secrets(line)
    assert scrub_secrets("no url here") == "no url here"
    assert scrub_secrets("") == ""

    # ...and the tail the restart log prints goes through it too
    tail = _StderrTail(io.BytesIO(line.encode() + b"\n"))
    for _ in range(200):
        if tail.text():
            break
        time.sleep(0.01)
    assert "SECRETPAYLOAD" not in tail.text()
