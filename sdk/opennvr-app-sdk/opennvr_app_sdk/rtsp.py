# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""Continuous RTSP frames for an app that has to WATCH.

A snapshot answers "what is there now". Some rules are about a shape in
time — a sweep of a hand scanner, a fall, a gesture sequence — and by
the time the next still arrives a second later, the thing has happened
and left. Those apps need the stream itself.

What this is
------------
One long-lived ffmpeg per camera, decoding to raw BGR at a bounded size
and frame rate, with the newest frame kept in a slot the app reads.

Three decisions worth knowing about:

**Drop the oldest, never queue.** When inference falls behind, a queue
converts lateness into MORE lateness: the app ends up reasoning about a
frame from ten seconds ago while the customer it describes has walked
out. A slot that only ever holds the newest frame degrades to a lower
frame rate instead, which is the failure everybody would choose.

**Capture time, not arrival time.** Every frame carries the wall clock
of when it was decoded, derived from a monotonic reading by its AGE (see
``capture_wall``). Sessions timed on arrival drift with inference
backlog, and then the alert's clock disagrees with the recording's.

**A drop is normal, not an error.** Cameras reboot, networks blink, a
switch gets bumped. ffmpeg exiting is a restart with backoff, not an
exception into the app's rule code, and the app is told the stream
restarted so it can decide what that means for any half-finished work.

Requires ffmpeg on PATH in the app image. numpy is only needed if the
app asks for frames as arrays.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Default decode size. Wide enough for body keypoints at a doorway,
#: small enough that four cameras fit on a mid-range CPU.
DEFAULT_WIDTH = 640
DEFAULT_FPS = 10.0

#: Restart pacing. The first retry is quick (a camera reboot is usually
#: over in seconds); repeated failures back off so a dead camera is not
#: a spin loop in the logs.
FIRST_BACKOFF_S = 1.0
MAX_BACKOFF_S = 15.0

#: How long without a frame before we conclude the stream is wedged and
#: restart it. ffmpeg can sit open on a stream that stopped sending.
STALL_TIMEOUT_S = 20.0

SpawnFn = Callable[[list[str]], "subprocess.Popen[bytes]"]


def capture_wall(mono_ts: float, *, _mono=time.monotonic, _wall=time.time) -> float:
    """Wall-clock seconds for a ``time.monotonic()`` frame stamp.

    By AGE, not by a stored anchor: an anchor's error is all the clock
    drift since it was taken and every NTP step in between, while this
    form's error is only the drift over the frame's own age — a few
    milliseconds. It also survives an ffmpeg restart with no
    re-anchoring. (Same reasoning, same maths as the Tier-0 pipeline's
    ``captime``.)
    """
    return _wall() - (_mono() - float(mono_ts))


@dataclass
class Frame:
    """One decoded frame: raw BGR bytes plus when it was taken."""

    data: bytes
    width: int
    height: int
    seq: int
    #: ``time.monotonic()`` when the frame finished decoding. Use this
    #: for durations and gaps — it cannot jump.
    mono_ts: float
    #: The same instant as wall clock, for anything an operator reads.
    wall_ts: float = field(default=0.0)
    #: True on the first frame after a restart. Whatever the app was
    #: part-way through, it did not see what happened in the gap.
    restarted: bool = False

    def to_ndarray(self):
        """The frame as an ``(h, w, 3)`` BGR array (needs numpy)."""
        import numpy as np

        return np.frombuffer(self.data, dtype=np.uint8).reshape(
            self.height, self.width, 3)


def build_command(url: str, *, width: int, fps: float,
                  transport: str = "tcp") -> list[str]:
    """The ffmpeg argv for one camera.

    TCP because UDP loses frames silently on a busy network and the
    losses look like detection failures. ``-an`` because no rule here
    listens. ``-fflags nobuffer`` and a small probe keep latency down —
    an app watching gestures would rather have a rough frame now than a
    tidy one a second late.
    """
    return [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", transport,
        "-fflags", "nobuffer", "-flags", "low_delay",
        "-probesize", "500000", "-analyzeduration", "1000000",
        # Reconnect handles the blips ffmpeg itself can ride out; the
        # supervisor below handles the ones it cannot.
        "-timeout", "5000000",
        "-i", url,
        "-an", "-sn",
        "-vf", f"scale={width}:-2,fps={fps}",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "pipe:1",
    ]


def probe_size(url: str, *, width: int, timeout: float = 15.0) -> tuple[int, int]:
    """The decoded (width, height) for ``url`` at this scale.

    Raw video carries no dimensions, so the reader must know the frame
    size before it can split the byte stream at all. Height comes from
    the source's aspect ratio, rounded to even by the scale filter.
    """
    if not shutil.which("ffprobe"):
        raise FrameStreamError("ffprobe not found on PATH")
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x",
             "-rtsp_transport", "tcp", url],
            capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        # A camera that accepts the connection and then says nothing is
        # a stream problem like any other: reconnect on the usual
        # backoff rather than tearing down the worker.
        raise FrameStreamError(
            f"stream did not answer within {timeout:.0f}s") from exc
    text = (out.stdout or "").strip().splitlines()
    if not text or "x" not in text[0]:
        raise FrameStreamError(
            f"could not read stream dimensions: {(out.stderr or '').strip()[:200]}")
    src_w, src_h = (int(v) for v in text[0].split("x")[:2])
    if src_w <= 0 or src_h <= 0:
        raise FrameStreamError("stream reported a zero dimension")
    height = int(round(src_h * (width / src_w) / 2)) * 2
    return width, max(height, 2)


class FrameStreamError(RuntimeError):
    """The stream could not be opened or understood."""


def read_frames(stream, frame_bytes: int) -> Iterator[bytes]:
    """Split a byte stream into fixed-size frames.

    A pure generator over any readable, so the framing logic is tested
    with an in-memory buffer and never needs a camera. A short read at
    the end is a truncated frame and is dropped — half a frame is not a
    frame.
    """
    while True:
        buf = stream.read(frame_bytes)
        if not buf or len(buf) < frame_bytes:
            return
        yield buf


class _StallWatch:
    """Last-frame clock for one ffmpeg session.

    ``beat()`` on every frame; ``silent_for()`` is how long since the
    last one. ``done`` ends the watching thread when the session does.
    """

    __slots__ = ("done", "_last", "_timeout")

    def __init__(self, timeout: float) -> None:
        self._timeout = timeout
        self._last = time.monotonic()
        self.done = threading.Event()

    def beat(self) -> None:
        self._last = time.monotonic()

    def silent_for(self) -> float:
        return time.monotonic() - self._last

    def cancel(self) -> None:
        self.done.set()


class RtspFrameStream:
    """Newest-frame-wins reader for one RTSP URL.

    Start it, then either poll :meth:`latest` or iterate :meth:`frames`.
    Both hand back the most recent decoded frame; neither ever hands
    back a backlog.

    ``url_factory`` is called for each (re)connect, so a caller whose
    URL carries a short-lived token — every app reading through the
    platform's scoped stream grant — renews simply by returning a fresh
    one.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        url_factory: Callable[[], str] | None = None,
        width: int = DEFAULT_WIDTH,
        fps: float = DEFAULT_FPS,
        spawn: SpawnFn | None = None,
        size: tuple[int, int] | None = None,
        name: str = "rtsp",
    ) -> None:
        if not url and not url_factory:
            raise ValueError("RtspFrameStream needs url or url_factory")
        self._url_factory = url_factory or (lambda: str(url))
        self.width = width
        self.fps = fps
        self.name = name
        self._spawn = spawn or self._default_spawn
        self._size = size
        self._frame: Frame | None = None
        self._new = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        #: The ffmpeg process of the session running right now. close()
        #: and the stall watchdog both need to reach it: the reader is
        #: parked in a blocking read on its stdout, and killing the
        #: process is the only thing that unblocks it.
        self._proc = None
        self._seq = 0
        self.restarts = 0
        #: Frames the app never asked for before the next arrived. Not a
        #: fault — it is the honest measure of how far inference is
        #: behind the camera, and worth logging when it climbs.
        self.dropped = 0

    # ── lifecycle ──

    @staticmethod
    def _default_spawn(argv: list[str]) -> "subprocess.Popen[bytes]":
        if not shutil.which("ffmpeg"):
            raise FrameStreamError("ffmpeg not found on PATH")
        # Keep stderr. Sending it to /dev/null makes every stream death
        # look identical from the outside — "stream ended" — when ffmpeg
        # was saying exactly what went wrong (401, no route, codec
        # unsupported) the whole time.
        return subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)

    def start(self) -> "RtspFrameStream":
        if self._thread is not None:
            return self
        # Clear the stop flag: close() sets it, and it used to stay set
        # for the life of the object. A caller that closed and reopened
        # a stream — one per screening, say — got a fresh thread that
        # returned immediately and no frames at all, for ever.
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"frames-{self.name}",
                                        daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        self._new.set()
        # Kill ffmpeg FIRST. The reader thread is blocked in
        # stream.read() on its stdout and checks _stop only between
        # frames, so on a stalled stream it never checks again: the join
        # below just burned its five seconds and returned with the
        # thread still parked and ffmpeg still decoding. An app that
        # reopens per screening leaked one of those per cycle.
        self._terminate(self._proc)
        self._proc = None
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)
            if thread.is_alive():
                logger.warning("%s: reader thread did not stop within 5s",
                               self.name)

    def __enter__(self) -> "RtspFrameStream":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    # ── reading ──

    def latest(self, *, timeout: float | None = None) -> Frame | None:
        """The newest frame, waiting up to ``timeout`` for a new one.

        Returns None on timeout — which is a fact about the camera, not
        an error, and the caller decides what it means.
        """
        if timeout is not None and not self._new.wait(timeout):
            return None
        self._new.clear()
        with self._lock:
            return self._frame

    def frames(self, *, timeout: float = 5.0) -> Iterator[Frame]:
        """Frames as they arrive, skipping any the caller was too slow
        to collect. Ends when the stream is closed."""
        while not self._stop.is_set():
            frame = self.latest(timeout=timeout)
            if frame is not None:
                yield frame

    @property
    def healthy(self) -> bool:
        """Whether a frame arrived recently enough to call this live."""
        with self._lock:
            frame = self._frame
        return (frame is not None
                and (time.monotonic() - frame.mono_ts) < STALL_TIMEOUT_S)

    # ── the supervisor ──

    def _run(self) -> None:
        backoff = FIRST_BACKOFF_S
        restarted = False
        while not self._stop.is_set():
            try:
                got_frames = self._session(restarted)
            except FrameStreamError as exc:
                logger.warning("%s: %s", self.name, exc)
                got_frames = False
            except Exception:  # noqa: BLE001
                # A BUG in here must not read as "the camera dropped".
                # It did once: a missing import crashed every session
                # instantly and the only symptom was a stream that
                # reconnected for ever with nothing to show for it.
                logger.exception("%s: reader crashed (this is a bug, not "
                                 "a camera fault)", self.name)
                got_frames = False
            if self._stop.is_set():
                return
            restarted = True
            self.restarts += 1
            if got_frames:
                # It worked and then stopped: a reboot or a blip, so try
                # again promptly rather than punishing a healthy camera
                # for one interruption.
                backoff = FIRST_BACKOFF_S
            logger.info("%s: stream ended, reconnecting in %.1fs", self.name, backoff)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)

    def _session(self, restarted: bool) -> bool:
        """One ffmpeg run. True if it produced at least one frame."""
        url = self._url_factory()
        if not url:
            raise FrameStreamError("no stream URL available")
        if self._size is None:
            self._size = probe_size(url, width=self.width)
        width, height = self._size
        frame_bytes = width * height * 3

        proc = self._spawn(build_command(url, width=self.width, fps=self.fps))
        self._proc = proc
        errors: deque[str] = deque(maxlen=8)
        self._drain_stderr(proc, errors)
        # STALL_TIMEOUT_S has been defined since this file was written and
        # nothing enforced it: `healthy` reported the stall and the
        # supervisor only ever restarted when read_frames RETURNED, which
        # a wedged stream never does — ffmpeg sits open on a camera that
        # accepted the connection and stopped sending, and the blocking
        # read never comes back. The watchdog kills the process, which
        # ends the read, which ends the session, which reconnects.
        stall = self._watch_for_stall(proc)
        first = True
        produced = False
        try:
            for data in read_frames(proc.stdout, frame_bytes):
                stall.beat()
                if self._stop.is_set():
                    return produced
                mono = time.monotonic()
                self._seq += 1
                frame = Frame(data=data, width=width, height=height,
                              seq=self._seq, mono_ts=mono,
                              wall_ts=capture_wall(mono),
                              restarted=restarted and first)
                with self._lock:
                    if self._frame is not None and not self._new.is_set():
                        pass  # collected; nothing lost
                    elif self._frame is not None:
                        self.dropped += 1
                    self._frame = frame
                self._new.set()
                produced = True
                first = False
        finally:
            stall.cancel()
            self._terminate(proc)
            if self._proc is proc:
                self._proc = None
            if not produced and errors:
                # Died without a single frame: say why, in ffmpeg's own
                # words, rather than leaving an operator to guess.
                logger.warning("%s: ffmpeg said: %s", self.name,
                               " | ".join(errors))
        return produced

    def _watch_for_stall(self, proc) -> "_StallWatch":
        """Kill ``proc`` if no frame arrives for STALL_TIMEOUT_S.

        A separate thread because the reader is inside a blocking read
        and cannot notice its own silence.
        """
        watch = _StallWatch(STALL_TIMEOUT_S)

        def _guard() -> None:
            while not watch.done.wait(1.0):
                if self._stop.is_set():
                    return
                if watch.silent_for() >= STALL_TIMEOUT_S:
                    logger.warning(
                        "%s: no frame for %.0fs — the stream is wedged, "
                        "restarting it", self.name, STALL_TIMEOUT_S)
                    self._terminate(proc)
                    return

        threading.Thread(target=_guard, daemon=True,
                         name=f"stall-{self.name}").start()
        return watch

    @staticmethod
    def _drain_stderr(proc, sink) -> None:
        """Keep ffmpeg's last few complaints without blocking on them."""
        stderr = getattr(proc, "stderr", None)
        if stderr is None:
            return

        def _pump():
            try:
                for line in iter(stderr.readline, b""):
                    text = line.decode("utf-8", "replace").strip()
                    if text:
                        sink.append(text)
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=_pump, daemon=True,
                         name="ffmpeg-stderr").start()

    @staticmethod
    def _terminate(proc) -> None:
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass


class RtspStillSource:
    """A stream dressed as a snapshot source.

    The polling ``FrameApp`` asks for "a frame now" every few seconds
    and expects encoded bytes. Pointing it at a stream would otherwise
    mean spawning ffmpeg per tick (what the camera-agent does, and it
    costs a second each time), so this keeps ONE decoder warm and hands
    over the newest frame, JPEG-encoded on demand.

    Needs opencv for the encode — an app that only wants stills and has
    no opencv should use the camera's HTTP snapshot URL instead.
    """

    def __init__(self, *, camera_id: str, url: str, width: int = DEFAULT_WIDTH,
                 fps: float = 4.0, quality: int = 85) -> None:
        self.camera_id = camera_id
        self.quality = quality
        self._stream = RtspFrameStream(url, width=width, fps=fps,
                                       name=f"still-{camera_id}")
        self._started = False

    def fetch(self) -> bytes | None:
        """The newest frame as JPEG, or None if the stream has nothing."""
        import cv2

        if not self._started:
            self._stream.start()
            self._started = True
        # First call waits for the decoder to come up; later calls take
        # whatever is there, because a poll loop must not block.
        frame = self._stream.latest(timeout=10.0 if self._started else 2.0)
        if frame is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame.to_ndarray(),
                               [cv2.IMWRITE_JPEG_QUALITY, self.quality])
        return buf.tobytes() if ok else None

    def close(self) -> None:
        self._stream.close()


__all__ = [
    "DEFAULT_FPS",
    "DEFAULT_WIDTH",
    "Frame",
    "FrameStreamError",
    "RtspFrameStream",
    "RtspStillSource",
    "build_command",
    "capture_wall",
    "probe_size",
    "read_frames",
]
