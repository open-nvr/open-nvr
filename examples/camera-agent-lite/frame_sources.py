# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Frame source abstractions (same pattern as the camera-agent example).

A frame source is anything that produces raw JPEG bytes on demand:

* ``file://`` URLs   — read a JPEG/PNG from disk. Useful for tests and
                       demos without a real camera.
* ``http(s)://``     — GET an HTTP snapshot URL (most IP cameras expose one).
* ``rtsp://``        — grab one keyframe via a bounded one-shot ffmpeg call.
                       This is what the OpenNVR MediaMTX tap URLs use, so the
                       agent never needs camera credentials or a user login —
                       the roster endpoint embeds a signed ``?jwt=`` token.
"""
from __future__ import annotations

import logging
import pathlib
import re
import subprocess
from typing import Protocol
from urllib.parse import urlparse, urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)


class FrameSourceError(Exception):
    """Raised when a frame source cannot produce a frame this cycle.
    Transient failures are normal (network blips, camera offline)."""


class FrameSource(Protocol):
    """Anything with ``fetch() -> bytes`` and a stable ``camera_id``
    is a frame source."""

    camera_id: str

    def fetch(self) -> bytes:
        ...  # pragma: no cover — Protocol


# ── Concrete sources ───────────────────────────────────────────────


class FileFrameSource:
    """Read a JPEG/PNG from disk. ``camera_id`` is operator-supplied."""

    def __init__(self, *, camera_id: str, path: str) -> None:
        # urlparse("file:///D:/x") leaves a leading slash before a Windows
        # drive letter; strip it so the path resolves on both platforms.
        if re.match(r"^/[A-Za-z]:", path):
            path = path[1:]
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
    """GET a camera's HTTP snapshot URL. Timeout is intentionally low: a
    slow snapshot blocks the tool call that asked for it."""

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
                f"http snapshot {_redact(self._url)}: {type(exc).__name__}: {exc}"
            ) from exc
        if response.status_code != 200:
            raise FrameSourceError(
                f"http snapshot {_redact(self._url)}: HTTP {response.status_code}"
            )
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
        if content_type and not content_type.startswith("image/"):
            logger.warning(
                "http snapshot %s returned non-image Content-Type %r; passing through anyway",
                _redact(self._url),
                content_type,
            )
        return response.content


class RtspFrameSource:
    """Grab a single JPEG frame from an RTSP stream via ffmpeg.

    ``-rtsp_transport tcp`` avoids the UDP packet loss that corrupts frames
    on busy networks; ``-skip_frame nokey`` waits for a true keyframe so a
    long-GOP H.265 stream doesn't yield a grey mid-GOP wash. The whole call
    is bounded by a timeout so an unreachable camera can't hang forever.

    Requires the ``ffmpeg`` binary on PATH (installed in the Docker image).
    """

    def __init__(
        self,
        *,
        camera_id: str,
        url: str,
        timeout_seconds: float = 15.0,
    ) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "rtsp":
            raise FrameSourceError(
                f"rtsp source: expected rtsp:// URL, got {parsed.scheme!r}"
            )
        self.camera_id = camera_id
        self._url = url
        self._timeout = timeout_seconds

    def fetch(self) -> bytes:
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-skip_frame", "nokey",
            "-i", self._url,
            "-frames:v", "1",   # exactly one frame
            "-q:v", "3",        # good JPEG quality
            "-f", "image2",
            "pipe:1",           # write the JPEG to stdout
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise FrameSourceError(
                f"rtsp grab for {self.camera_id} timed out after "
                f"{self._timeout}s ({_redact(self._url)})"
            ) from exc
        except FileNotFoundError as exc:
            raise FrameSourceError(
                "rtsp source needs the 'ffmpeg' binary on PATH but it "
                f"wasn't found: {exc}"
            ) from exc
        if proc.returncode != 0 or not proc.stdout:
            # ffmpeg echoes the input URL (with credentials) in its stderr,
            # so scrub userinfo before it lands in our error message / logs.
            err = _scrub_creds((proc.stderr or b"").decode("utf-8", "replace").strip())[:300]
            raise FrameSourceError(
                f"rtsp grab for {self.camera_id} failed "
                f"({_redact(self._url)}): {err or 'no frame produced'}"
            )
        return proc.stdout


# ── Redaction helpers ──────────────────────────────────────────────


def _scrub_creds(text: str) -> str:
    """Remove URL userinfo (``user:pass@``) from arbitrary text such as
    ffmpeg stderr, which echoes the input URL including credentials."""
    return re.sub(r"://[^/\s@]+@", "://", text)


def _redact(url: str) -> str:
    """Strip credentials AND query secrets (e.g. ``?jwt=...`` on MediaMTX tap
    URLs) from a URL before logging it."""
    try:
        parts = urlsplit(url)
        if parts.username or parts.password or parts.query:
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"
            query = "REDACTED" if parts.query else ""
            return urlunsplit((parts.scheme, host, parts.path, query, parts.fragment))
    except Exception:
        pass
    return url


# ── Factory ────────────────────────────────────────────────────────


def build_frame_source(*, camera_id: str, url: str) -> FrameSource:
    """Pick the right source class based on the URL scheme. Anything
    unrecognised raises ``FrameSourceError`` — fail fast at config-load
    time rather than mid-loop."""
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme == "file":
        return FileFrameSource(camera_id=camera_id, path=parsed.path)
    if scheme in ("http", "https"):
        return HttpSnapshotSource(camera_id=camera_id, url=url)
    if scheme == "rtsp":
        return RtspFrameSource(camera_id=camera_id, url=url)
    raise FrameSourceError(
        f"unsupported frame source scheme {scheme!r}; expected file/http/https/rtsp."
    )
