# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
#
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""
Persistent per-camera RTSP capture pool.

Why this exists
---------------
The original inference path opened a *fresh* ``cv2.VideoCapture(rtsp_url)``
for every single frame, read one frame, and released it. Opening an RTSP
session is expensive: a TCP connect, the RTSP DESCRIBE / SETUP / PLAY
handshake, and then a wait for the first decodable keyframe — on H.264
with a 1-2 second GOP that is hundreds of milliseconds to ~2 seconds of
setup latency *per inference*. It also wrote each frame to disk
(``latest.jpg``) for the adapter to read back over a shared volume.

This pool keeps **one long-lived capture open per camera**, reuses it
across calls, and returns **JPEG bytes in memory** (no disk round-trip).
A capture that starts failing is transparently reopened. The result is
that steady-state frame grabs cost a single ``grab()/retrieve()`` plus a
JPEG encode, not a full reconnect.

Thread-safety
-------------
One lock per camera guards that camera's capture object, so concurrent
inference loops for *different* cameras never block each other, while two
calls for the *same* camera serialise (a single OpenCV capture is not
safe to read from two threads at once). The pool dict itself is guarded
by a short-held lock only while looking up / creating the per-camera
entry.

Testability
----------
``cv2`` is reached only through an injectable ``capture_factory`` /
``encode_jpeg`` so the pool can be unit-tested with fakes — no real RTSP
stream or OpenCV build required.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


# A capture object only needs read()/release() for our purposes.
class _CaptureProtocol:  # pragma: no cover - structural doc only
    def read(self): ...
    def release(self) -> None: ...
    def isOpened(self) -> bool: ...


CaptureFactory = Callable[[str], "_CaptureProtocol"]
EncodeJpeg = Callable[[object, int], Optional[bytes]]


def _default_capture_factory(url: str) -> "_CaptureProtocol":
    import cv2

    cap = cv2.VideoCapture(url)
    # Keep only the freshest frame so we never hand inference a stale,
    # buffered frame after a slow cycle.
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass
    return cap


def _default_encode_jpeg(frame: object, quality: int) -> Optional[bytes]:
    import cv2

    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        return None
    return bytes(buf.tobytes())


#: Draining stops at the first grab that took this long: a grab that had
#: to wait is one the stream delivered just now. Shorter than any camera's
#: frame interval (25 fps = 40 ms), longer than a queued grab (<1 ms).
DRAIN_LIVE_EDGE_SECONDS = 0.015
#: And never more than this many grabs per call, so a stream that keeps a
#: deep queue (or lies about timing) costs a bounded amount per request.
DRAIN_MAX_GRABS = 150


@dataclass
class _CameraEntry:
    capture: Optional["_CaptureProtocol"] = None
    url: Optional[str] = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    consecutive_failures: int = 0


class PersistentCapturePool:
    """Keeps one open capture per camera and returns JPEG bytes."""

    def __init__(
        self,
        *,
        capture_factory: CaptureFactory | None = None,
        encode_jpeg: EncodeJpeg | None = None,
        jpeg_quality: int = 85,
    ) -> None:
        self._factory = capture_factory or _default_capture_factory
        self._encode = encode_jpeg or _default_encode_jpeg
        self._jpeg_quality = int(jpeg_quality)
        self._cameras: dict[int, _CameraEntry] = {}
        self._dict_lock = threading.Lock()

    def _entry(self, camera_id: int) -> _CameraEntry:
        with self._dict_lock:
            entry = self._cameras.get(camera_id)
            if entry is None:
                entry = _CameraEntry()
                self._cameras[camera_id] = entry
            return entry

    def get_jpeg(self, camera_id: int, url: str) -> Optional[bytes]:
        """Grab the latest frame from ``camera_id`` (opening/reusing a
        persistent capture for ``url``) and return JPEG bytes, or None on
        failure. Reopens the capture transparently if the URL changed or
        a read fails."""
        entry = self._entry(camera_id)
        with entry.lock:
            # (Re)open if we have no capture or the URL changed (e.g. a
            # rotated MediaMTX JWT in the query string).
            if entry.capture is None or entry.url != url:
                self._open_locked(entry, url)
            if entry.capture is None:
                return None

            frame = self._read_locked(entry)
            if frame is None:
                # One transparent reopen + retry — covers a capture that
                # silently died (stream restart, token rotation).
                self._open_locked(entry, url)
                if entry.capture is None:
                    return None
                frame = self._read_locked(entry)
            if frame is None:
                entry.consecutive_failures += 1
                return None

            entry.consecutive_failures = 0
            try:
                return self._encode(frame, self._jpeg_quality)
            except Exception:
                return None

    def _open_locked(self, entry: _CameraEntry, url: str) -> None:
        # Caller holds entry.lock.
        if entry.capture is not None:
            try:
                entry.capture.release()
            except Exception:
                pass
            entry.capture = None
        try:
            cap = self._factory(url)
        except Exception:
            entry.capture = None
            entry.url = url
            return
        # An unopened capture is useless — discard it so we retry next call.
        is_open = True
        try:
            is_open = bool(cap.isOpened())
        except Exception:
            is_open = True  # factories without isOpened() are assumed open
        if not is_open:
            try:
                cap.release()
            except Exception:
                pass
            entry.capture = None
        else:
            entry.capture = cap
        entry.url = url

    def _read_locked(self, entry: _CameraEntry):
        # Caller holds entry.lock.
        cap = entry.capture
        if cap is None:
            return None
        if not (hasattr(cap, "grab") and hasattr(cap, "retrieve")):
            try:
                ok, frame = cap.read()
            except Exception:
                return None
            return frame if ok and frame is not None else None
        # Drain to the live edge. A persistent RTSP capture read once every
        # few seconds does not return the current picture: FFmpeg queues
        # the frames that arrived meanwhile (CAP_PROP_BUFFERSIZE is a
        # V4L/GStreamer knob and does nothing here), so one read() per
        # call walks through the past a frame at a time and falls ever
        # further behind — a frame-polling app at 5 s on a 25 fps camera
        # was two minutes stale within a minute. grab() decodes nothing
        # and returns at once while frames are queued; the first grab
        # that has to WAIT is the one that fetched a frame the camera
        # produced just now. Bounded, so a stalled stream cannot spin.
        try:
            started = time.monotonic()
            if not cap.grab():
                return None
            waited = time.monotonic() - started >= DRAIN_LIVE_EDGE_SECONDS
            for _ in range(DRAIN_MAX_GRABS):
                if waited:
                    break                       # that grab was the live one
                started = time.monotonic()
                if not cap.grab():
                    break                       # keep the last good grab
                waited = time.monotonic() - started >= DRAIN_LIVE_EDGE_SECONDS
            ok, frame = cap.retrieve()
        except Exception:
            return None
        if not ok or frame is None:
            return None
        return frame

    def release(self, camera_id: int) -> None:
        """Release the capture for one camera (e.g. when its inference
        loop stops)."""
        entry = self._entry(camera_id)
        with entry.lock:
            if entry.capture is not None:
                try:
                    entry.capture.release()
                except Exception:
                    pass
                entry.capture = None
                entry.url = None

    def release_all(self) -> None:
        with self._dict_lock:
            entries = list(self._cameras.values())
        for entry in entries:
            with entry.lock:
                if entry.capture is not None:
                    try:
                        entry.capture.release()
                    except Exception:
                        pass
                    entry.capture = None
                    entry.url = None
