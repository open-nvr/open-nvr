# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Continuous frame source for the Tier-0 pipeline.

Spawns the hwaccel ffmpeg command from :mod:`ffmpeg_presets` against MediaMTX's
substream republish and reads fixed-size raw ``yuv420p`` (I420) frames from its
stdout. Camera/stream drops are normal, so the source restarts ffmpeg with a
small backoff and keeps going.

The frame-reading core (:func:`read_frames`) is a pure generator over any binary
stream, so it is unit-tested with an in-memory buffer; the process lifecycle
(:class:`FrameSource`) takes an injectable ``spawn`` so tests never touch ffmpeg.
"""
from __future__ import annotations

import logging
import random
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from .ffmpeg_presets import (
    DEFAULT_RTSP_TIMEOUT_S,
    HwAccel,
    build_decode_command,
    frame_size_bytes,
    rtsp_timeout_args,
)

log = logging.getLogger("detect_pipeline.frame_source")

# A spawn function takes an argv and returns something Popen-like (``.stdout``
# readable, ``.terminate()``, ``.wait(timeout=...)``, ``.poll()``).
SpawnFn = Callable[[list[str]], "subprocess.Popen[bytes]"]


@dataclass(frozen=True)
class Frame:
    """One decoded I420 frame plus light metadata."""

    data: bytes          # raw yuv420p, len == width*height*3//2
    width: int
    height: int
    seq: int             # monotonic counter since the stream (re)started
    ts: float            # time.monotonic() when the frame was fully read

    @property
    def y_plane(self) -> bytes:
        """The luma (Y) plane — the first ``width*height`` bytes.

        Motion detection and downscaling operate on Y only, so this avoids
        touching the chroma planes on the hot path.
        """
        return self.data[: self.width * self.height]


def _read_exact(stream, n: int) -> bytes | None:
    """Read exactly ``n`` bytes; ``None`` at EOF (a partial tail is discarded)."""
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frames(stream, width: int, height: int, *, _clock=time.monotonic) -> Iterator[Frame]:
    """Yield full I420 frames from a binary readable ``stream`` until EOF.

    A trailing partial frame (torn read at stream end) is discarded, never
    yielded — a half-frame must not reach the detector.
    """
    size = frame_size_bytes(width, height)
    seq = 0
    while True:
        buf = _read_exact(stream, size)
        if buf is None:
            return
        yield Frame(buf, width, height, seq, _clock())
        seq += 1


def _default_spawn(argv: list[str]) -> "subprocess.Popen[bytes]":
    return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _jittered(delay: float, rand: Callable[[], float]) -> float:
    """Spread a retry delay over [delay/2, delay] ("equal jitter").

    Cameras fail TOGETHER — MediaMTX restarts, core restarts, the switch
    blips — and identical backoff makes them retry together too, so the
    fleet re-dials in lockstep and the herd never thins. Jitter is what
    turns a synchronised storm into a spread of reconnects; halving the
    floor also means the fleet is not uniformly slower to come back.
    """
    if delay <= 0:
        return 0.0
    return delay * 0.5 + delay * 0.5 * rand()


def redact_url(url: str) -> str:
    """Drop the query string from a stream URL before logging it.

    The MediaMTX tap URL carries a signed ``?jwt=`` granting wildcard read
    access to every camera. Logging it verbatim writes a live credential to
    disk and to any log shipper — and buries the actual message under a
    900-character token. Path is kept; secrets are not.
    """
    base, sep, _query = str(url).partition("?")
    return f"{base}?<redacted>" if sep else base


class _StderrTail:
    """Drain an ffmpeg process's stderr on a daemon thread, keeping the tail.

    stderr is a pipe with a ~64KB kernel buffer. ffmpeg at ``-loglevel
    warning`` chatters on a lossy RTSP feed, and if NOBODY reads that pipe it
    fills, ffmpeg BLOCKS writing to it, and it stops producing frames on
    stdout — a permanent stall that looks exactly like a dead camera and gets
    worse the flakier the feed is. Draining is what keeps the decoder alive.

    Keeping the last few lines is the second half: "source ended" with
    ffmpeg's own reason attached is diagnosable; without it, every failure
    (401, refused, unsupported codec, timeout) reads identically.
    """

    def __init__(self, stream, *, keep: int = 6) -> None:
        self._lines: deque[str] = deque(maxlen=keep)
        self._stream = stream
        if stream is None:          # injected fake proc in tests
            return
        threading.Thread(
            target=self._drain, name="ffmpeg-stderr", daemon=True
        ).start()

    def _drain(self) -> None:
        try:
            for raw in iter(self._stream.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._lines.append(line)
        except Exception:  # pragma: no cover - pipe torn down on terminate
            pass

    def text(self) -> str:
        return " | ".join(self._lines)


def _terminate(proc) -> None:
    """Best-effort teardown of an ffmpeg process."""
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
    except Exception:  # pragma: no cover - defensive
        log.debug("frame source: error terminating ffmpeg", exc_info=True)


class FrameSource:
    """Continuous, self-restarting I420 frame source for one camera substream."""

    def __init__(
        self,
        rtsp_url: str,
        *,
        width: int,
        height: int,
        fps: int,
        hwaccel: HwAccel = HwAccel.CPU,
        device: str = "/dev/dri/renderD128",
        codec: str = "h264",
        decode_skip: str = "none",
        decode_threads: int = 2,
        fast_decode: bool = False,
        rtsp_transport: str = "tcp",
        rtsp_timeout_s: float = DEFAULT_RTSP_TIMEOUT_S,
        url_provider: Callable[[], str | None] | None = None,
        spawn: SpawnFn | None = None,
        max_restarts: int | None = None,
        max_fruitless_restarts: int = 5,
        backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 15.0,
        min_healthy_seconds: float = 5.0,
        _sleep: Callable[[float], None] | None = None,
        _rand: Callable[[], float] | None = None,
    ) -> None:
        self.rtsp_url = rtsp_url
        self.width = width
        self.height = height
        self.fps = fps
        self.hwaccel = hwaccel
        self.device = device
        self.codec = codec
        self.rtsp_transport = rtsp_transport
        self.rtsp_timeout_s = rtsp_timeout_s
        # Optional "give me the current URL for this camera" callback, checked
        # before each respawn. The tap URL embeds a 60-MINUTE JWT resolved at
        # worker start, so without this every respawn past the first hour
        # re-authenticates with a dead token and 401s — the camera can only
        # recover by dying and waiting for a reconcile tick.
        self._url_provider = url_provider
        self.decode_skip = decode_skip
        self.decode_threads = decode_threads
        self.fast_decode = fast_decode
        self._spawn = spawn or _default_spawn
        # None = restart forever (production); an int caps restarts (tests).
        self.max_restarts = max_restarts
        # Consecutive restarts that produced ZERO frames before ending the
        # stream. A dead credential (expired signed tap URL) makes ffmpeg exit
        # instantly forever — restarting with the same URL can never recover.
        # Giving up ends the worker, and the manager's next reconcile
        # resurrects it with a FRESH tap URL from the provider.
        self.max_fruitless_restarts = max_fruitless_restarts
        self.backoff_seconds = backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        # How long a session must last to count as "the stream works". Below
        # this, a restart is treated as fruitless (see stream()).
        self.min_healthy_seconds = min_healthy_seconds
        # Set by close(): ends the restart loop and interrupts the backoff
        # sleep, so a worker parked in backoff stops in milliseconds instead
        # of holding its caller's join() open for the full delay.
        self._closing = False
        # Injectable so retry timing is deterministic in tests.
        self._rand = _rand or random.random
        self._wake = threading.Event()
        self._sleep = _sleep or (lambda secs: self._wake.wait(secs))
        self._current_proc = None
        self._skip_backoff_once = False

    def command(self) -> list[str]:
        return build_decode_command(
            self.rtsp_url,
            width=self.width,
            height=self.height,
            fps=self.fps,
            hwaccel=self.hwaccel,
            device=self.device,
            codec=self.codec,
            rtsp_transport=self.rtsp_transport,
            rtsp_timeout_s=self.rtsp_timeout_s,
            decode_skip=self.decode_skip,
            decode_threads=self.decode_threads,
            fast_decode=self.fast_decode,
        )

    def set_decode_skip(self, mode: str) -> None:
        """Adaptive decode: change the skip mode for the NEXT ffmpeg spawn and
        terminate the current process so the built-in restart loop picks the
        new mode up immediately (without the restart backoff — this is a
        deliberate flip, not a stream failure). Called from the same worker
        thread that consumes ``stream()``, between frames."""
        self.decode_skip = mode
        self._skip_backoff_once = True
        if self._current_proc is not None:
            _terminate(self._current_proc)

    def close(self) -> None:
        """Stop the restart loop and unblock the reader NOW.

        Without this a stopping worker is unreachable: it is parked in a
        blocking ``proc.stdout.read()`` (up to the RTSP timeout) or in the
        restart backoff (up to max_backoff_seconds), and only notices the
        stop flag between frames. Terminating the process makes the read
        return at once, and waking the sleep drops the backoff — so join()
        succeeds in milliseconds instead of timing out and leaving an
        orphaned thread and ffmpeg behind.

        Safe to call from another thread, and more than once.

        Signals only — it must NOT wait for the process. ``_terminate``
        blocks up to 3s reaping the child, which would make a caller
        stopping N sources pay that serially (measured: 15s for 6 real
        cameras). SIGTERM is enough: the child exits, its stdout closes, the
        blocked read returns, and the stream loop's own ``finally`` does the
        full terminate-and-reap.
        """
        self._closing = True
        self._wake.set()
        proc = self._current_proc
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:  # pragma: no cover - defensive
                log.debug("frame source: error signalling ffmpeg", exc_info=True)

    def _refresh_url(self) -> None:
        """Adopt the freshest known URL for this camera, if one is offered.

        Cheap and best-effort: the manager hands over the URL it already
        fetched on its last reconcile, so this costs no I/O. It is what lets
        an expired tap JWT heal on the next respawn instead of 401-ing until
        the worker dies and a reconcile tick rebuilds it.
        """
        if self._url_provider is None:
            return
        try:
            fresh = self._url_provider()
        except Exception:  # pragma: no cover - defensive
            log.debug("frame source: url provider failed", exc_info=True)
            return
        if fresh and fresh != self.rtsp_url:
            log.info(
                "frame source for %s: adopted refreshed URL",
                redact_url(self.rtsp_url),
            )
            self.rtsp_url = fresh

    def stream(self) -> Iterator[Frame]:
        """Yield frames until the source is unrecoverable.

        Restarts ffmpeg on exit (camera/stream drops are normal). Ends the
        stream after ``max_fruitless_restarts`` consecutive restarts that
        produced no frames at all — the signature of a dead credential (e.g.
        an expired signed tap URL), which no amount of restarting with the
        same URL can fix. The caller's worker then exits and the reconcile
        loop resurrects it with a freshly resolved URL.
        """
        restarts = 0
        fruitless = 0
        while True:
            if self._closing:
                return
            self._refresh_url()
            proc = self._spawn(self.command())
            self._current_proc = proc
            stderr_tail = _StderrTail(getattr(proc, "stderr", None))
            got_frame = False
            started = time.monotonic()
            try:
                for frame in read_frames(proc.stdout, self.width, self.height):
                    got_frame = True
                    yield frame
            finally:
                self._current_proc = None
                _terminate(proc)
            if self._closing:      # closed mid-session: do not respawn
                return
            reason = stderr_tail.text()
            # A decode-mode flip terminates ffmpeg ON PURPOSE (adaptive
            # decode). It is not a stream failure, so it must not touch the
            # give-up budget at all — otherwise a camera whose scene keeps
            # waking it would be killed as though its feed were dead.
            deliberate = self._skip_backoff_once
            if not deliberate:
                # A session only clears the give-up counter if it actually
                # STAYED up. Counting any single frame as healthy (the old
                # rule) let a stream that connects, emits one frame and drops
                # reset the counter forever — an unbounded restart loop that
                # never reaches give-up and so never gets a fresh URL.
                healthy = (
                    got_frame
                    and (time.monotonic() - started) >= self.min_healthy_seconds
                )
                fruitless = 0 if healthy else fruitless + 1
                if fruitless >= self.max_fruitless_restarts:
                    log.error(
                        "frame source for %s: %d consecutive restarts with no "
                        "healthy session (dead stream or expired ticket) — giving "
                        "up so the worker is resurrected with a fresh URL%s",
                        redact_url(self.rtsp_url), fruitless,
                        f"; ffmpeg said: {reason}" if reason else "",
                    )
                    return
            if self.max_restarts is not None and restarts >= self.max_restarts:
                return
            restarts += 1
            if deliberate:
                # Restart immediately, quietly — no backoff, no penalty.
                self._skip_backoff_once = False
                log.info("frame source for %s: decode mode -> %s",
                         redact_url(self.rtsp_url), self.decode_skip)
                continue
            # Escalating backoff, capped. A flat delay meant a stream failing
            # instantly was re-dialled at a fixed rate forever, each cycle a
            # full ffmpeg spawn + RTSP handshake; healthy sessions reset it.
            delay = _jittered(
                min(
                    self.backoff_seconds * (2 ** max(0, fruitless - 1)),
                    self.max_backoff_seconds,
                ),
                self._rand,
            )
            log.warning(
                "frame source for %s ended; restart #%d (retry in %.1fs)%s",
                redact_url(self.rtsp_url), restarts, delay,
                f"; ffmpeg said: {reason}" if reason else "",
            )
            self._sleep(delay)


class VideoFileSource:
    """Frame source backed by a video file or RTSP URL via OpenCV VideoCapture.

    For manual verification and the demo CLI: yields the same I420 ``Frame``
    objects the ffmpeg path produces, so it exercises the real pipeline. (The
    production path uses the hwaccel ffmpeg FrameSource; this is the convenient
    "run it on a clip" path.)
    """

    def __init__(self, path: str, *, max_frames: int | None = None) -> None:
        self.path = path
        self.max_frames = max_frames

    def set_decode_skip(self, mode: str) -> None:
        """No-op: OpenCV decodes every frame, so there is no skip mode.

        This used to be a copy of FrameSource's version, referencing
        ``_current_proc``/``_skip_backoff_once`` — attributes this class never
        defines — so calling it raised AttributeError. Unreachable only
        because the worker gates adaptive decode on the ffmpeg path; accepting
        and ignoring the call is the honest behaviour for a file source.
        """
        self.decode_skip = mode

    def stream(self) -> Iterator[Frame]:
        import cv2  # local import: only the demo path needs OpenCV here

        cap = cv2.VideoCapture(self.path)
        if not cap.isOpened():
            raise RuntimeError(f"could not open video source: {self.path!r}")
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        seq = 0
        try:
            while True:
                ok, bgr = cap.read()
                if not ok:
                    return
                h, w = bgr.shape[:2]
                # I420 needs even dimensions
                w -= w % 2
                h -= h % 2
                bgr = bgr[:h, :w]
                yuv = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420)
                yield Frame(yuv.tobytes(), w, h, seq, seq / fps)
                seq += 1
                if self.max_frames is not None and seq >= self.max_frames:
                    return
        finally:
            cap.release()


def _parse_ffprobe(text: str) -> tuple[int, int, float]:
    """Parse ffprobe -show_entries stream=width,height,avg_frame_rate -of json."""
    import json

    stream = json.loads(text)["streams"][0]
    w, h = int(stream["width"]), int(stream["height"])
    rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/1"
    num, _, den = rate.partition("/")
    fps = float(num) / float(den) if den and float(den) != 0 else 0.0
    return w, h, fps


def probe_stream(
    url: str,
    *,
    rtsp_transport: str = "tcp",
    timeout: float = 15.0,
    rtsp_timeout_s: float = DEFAULT_RTSP_TIMEOUT_S,
) -> tuple[int, int, float] | None:
    """Return (width, height, fps) of a stream via ffprobe, or None on failure.

    Failure is logged with ffprobe's own reason: "could not probe" alone
    cannot distinguish a camera that is off from a rejected JWT from a tap
    path MediaMTX isn't publishing, and the worker retries this every
    reconcile tick until someone can tell which it is.
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-rtsp_transport", rtsp_transport,
        *rtsp_timeout_args(rtsp_timeout_s),
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate",
        "-of", "json", url,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0 or not out.stdout.strip():
            log.warning(
                "ffprobe failed for %s (rc=%s): %s",
                url, out.returncode, (out.stderr or "").strip() or "no output",
            )
            return None
        return _parse_ffprobe(out.stdout)
    except subprocess.TimeoutExpired:
        log.warning("ffprobe timed out after %.0fs for %s", timeout, url)
        return None
    except Exception as e:
        log.warning("ffprobe error for %s: %s", url, e)
        return None
