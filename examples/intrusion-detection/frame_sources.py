# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Frame source abstractions.

A frame source is anything that produces raw image bytes on demand.
For v1 we support:

* ``file://`` URLs   — read a JPEG/PNG from disk. Useful for tests and
                       demos without a real camera.
* ``http://`` /      — GET an HTTP snapshot URL. Real cameras (Hikvision,
  ``https://`` URLs    Axis, Reolink, …) all expose a snapshot endpoint;
                       OpenNVR cameras expose one too via the backend.

Deferred to A2.5b:

* ``rtsp://`` URLs   — needs ffmpeg subprocess + decode loop. Significant
                       extra code; the snapshot path covers the common
                       polling use case for v1.
* ``opennvr://``     — Once OpenNVR backend exposes a typed snapshot
  scheme              endpoint, we'll add ``opennvr://cameras/{id}/snapshot``
                       resolving to the right HTTPS URL.
"""
from __future__ import annotations

import logging
import pathlib
from abc import ABC, abstractmethod
from typing import Protocol
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


class FrameSourceError(Exception):
    """Raised when a frame source cannot produce a frame this cycle.
    Caller (the detector loop) decides whether to skip or abort —
    transient failures are normal (network blips, camera offline)."""


class FrameSource(Protocol):
    """Anything with ``fetch() -> bytes`` and a stable ``camera_id``
    is a frame source."""

    camera_id: str

    def fetch(self) -> bytes:
        ...  # pragma: no cover — Protocol


# ── Concrete sources ───────────────────────────────────────────────


class FileFrameSource:
    """Read a JPEG/PNG from disk. ``camera_id`` is operator-supplied.

    Path-traversal protected: we resolve the configured path once at
    init time and reject any subsequent change. (Operators shouldn't
    be passing user-controlled paths anyway, but the example sets the
    pattern for future sources.)
    """

    def __init__(self, *, camera_id: str, path: str) -> None:
        resolved = pathlib.Path(path).expanduser().resolve()
        if not resolved.is_file():
            raise FrameSourceError(
                f"file frame source: {path!r} does not exist or is not a file"
            )
        self.camera_id = camera_id
        self._path = resolved

    def fetch(self) -> bytes:
        return self._path.read_bytes()


class HttpSnapshotSource:
    """GET an HTTP snapshot URL. Supports basic-auth via the URL
    (``http://user:pass@host/snapshot.jpg``) — standard pattern for
    consumer-grade cameras.

    Timeout is intentionally low (default 5s): a slow snapshot in the
    polling loop blocks every camera. If the camera is consistently
    slow, the operator should lower the poll interval or move to RTSP.
    """

    def __init__(
        self,
        *,
        camera_id: str,
        url: str,
        timeout_seconds: float = 5.0,
        verify_tls: bool = True,
    ) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise FrameSourceError(
                f"http snapshot source: expected http(s) URL, got {parsed.scheme!r}"
            )
        self.camera_id = camera_id
        self._url = url
        self._timeout = timeout_seconds
        self._verify_tls = verify_tls

    def fetch(self) -> bytes:
        try:
            response = httpx.get(
                self._url,
                timeout=self._timeout,
                verify=self._verify_tls,
                trust_env=False,
            )
        except Exception as exc:
            raise FrameSourceError(
                f"http snapshot {self._url}: {type(exc).__name__}: {exc}"
            ) from exc
        if response.status_code != 200:
            raise FrameSourceError(
                f"http snapshot {self._url}: HTTP {response.status_code}"
            )
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
        if content_type and not content_type.startswith("image/"):
            logger.warning(
                "http snapshot %s returned non-image Content-Type %r; passing through anyway",
                self._url,
                content_type,
            )
        return response.content


# ── Factory ────────────────────────────────────────────────────────


def build_frame_source(*, camera_id: str, url: str) -> FrameSource:
    """Pick the right source class based on the URL scheme. Anything
    unrecognised raises ``FrameSourceError`` — fail fast at config-load
    time rather than mid-loop."""
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme == "file":
        # urlparse("file:///path") → path is in parsed.path
        return FileFrameSource(camera_id=camera_id, path=parsed.path)
    if scheme in ("http", "https"):
        return HttpSnapshotSource(camera_id=camera_id, url=url)
    if scheme == "opennvr":
        raise FrameSourceError(
            "opennvr:// scheme is reserved for a future OpenNVR-backend snapshot "
            "endpoint and is not yet implemented. Use http(s):// against the "
            "camera's snapshot URL directly for now."
        )
    if scheme == "rtsp":
        raise FrameSourceError(
            "rtsp:// scheme requires the ffmpeg-based RTSP pipeline that lands in "
            "A2.5b. For now use the camera's HTTP snapshot endpoint instead."
        )
    raise FrameSourceError(
        f"unsupported frame source scheme {scheme!r}; expected file/http/https."
    )
