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
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from .ffmpeg_presets import HwAccel, build_decode_command, frame_size_bytes

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
        spawn: SpawnFn | None = None,
        max_restarts: int | None = None,
        max_fruitless_restarts: int = 5,
        backoff_seconds: float = 1.0,
        _sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rtsp_url = rtsp_url
        self.width = width
        self.height = height
        self.fps = fps
        self.hwaccel = hwaccel
        self.device = device
        self.codec = codec
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
        self._sleep = _sleep

    def command(self) -> list[str]:
        return build_decode_command(
            self.rtsp_url,
            width=self.width,
            height=self.height,
            fps=self.fps,
            hwaccel=self.hwaccel,
            device=self.device,
            codec=self.codec,
        )

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
            proc = self._spawn(self.command())
            got_frame = False
            try:
                for frame in read_frames(proc.stdout, self.width, self.height):
                    got_frame = True
                    yield frame
            finally:
                _terminate(proc)
            fruitless = 0 if got_frame else fruitless + 1
            if fruitless >= self.max_fruitless_restarts:
                log.error(
                    "frame source for %s: %d consecutive restarts with no frames "
                    "(dead stream or expired ticket) — giving up so the worker "
                    "is resurrected with a fresh URL",
                    self.rtsp_url, fruitless,
                )
                return
            if self.max_restarts is not None and restarts >= self.max_restarts:
                return
            restarts += 1
            log.warning("frame source for %s ended; restart #%d", self.rtsp_url, restarts)
            self._sleep(self.backoff_seconds)


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
    url: str, *, rtsp_transport: str = "tcp", timeout: float = 15.0
) -> tuple[int, int, float] | None:
    """Return (width, height, fps) of a stream via ffprobe, or None on failure."""
    cmd = [
        "ffprobe", "-v", "error",
        "-rtsp_transport", rtsp_transport,
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate",
        "-of", "json", url,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return _parse_ffprobe(out.stdout)
    except Exception:
        return None
