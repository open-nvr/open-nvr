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

Deferred follow-ups:

* ``rtsp://`` URLs   — needs ffmpeg subprocess + decode loop. Significant
                       extra code; the snapshot path covers the common
                       polling use case for v1.
* ``opennvr://``     — Once OpenNVR backend exposes a typed snapshot
  scheme              endpoint, we'll add ``opennvr://cameras/{id}/snapshot``
                       resolving to the right HTTPS URL.
"""
from __future__ import annotations

import logging
import os
import pathlib
import re
import subprocess
from abc import ABC, abstractmethod
from typing import Protocol
from urllib.parse import urlparse, urlsplit, urlunsplit

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


class RtspFrameSource:
    """Grab a single JPEG frame from an RTSP stream via ffmpeg.

    IP cameras speak RTSP, not HTTP snapshots, so this shells out to
    ffmpeg to pull exactly one frame on demand and encode it as JPEG.
    ``-rtsp_transport tcp`` avoids the UDP packet loss that corrupts
    frames on busy networks. The whole thing is bounded by a timeout so
    an unreachable camera can't hang the tool call forever.

    Requires the ``ffmpeg`` binary on PATH (installed in the
    camera-agent Docker image).
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
            # Emit a KEYFRAME, not merely the first decodable frame. On a
            # long-GOP H.265/HEVC stream (CPplus and many OEM cameras keep a
            # keyframe only every 1-4s) connecting mid-GOP means the first
            # frame ffmpeg decodes is a P/B-frame with no reference I-frame in
            # hand — it decodes to a uniform ~128 grey wash with faint blocky
            # noise, not the real scene. `-skip_frame nokey` tells the decoder
            # to drop every non-key frame, so `-frames:v 1` below outputs the
            # first true keyframe instead. It's an input/decoder option, hence
            # before `-i`. If no keyframe arrives within the timeout we now
            # fail loudly (VISION DEGRADED) rather than hand back grey mush.
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


class DeviceFrameSource:
    """Grab a JPEG from a **local capture device** — a laptop webcam, a USB
    camera, a Pi camera, or any ``/dev/videoN`` node the host exposes. This is
    what lets the agent run on *whatever hardware it's already on* (a dev
    laptop, a drone, a robot) with zero camera provisioning: if the machine has
    a camera, the agent uses it.

    Accepts ``device:0`` / ``device:1`` (capture index), ``device:/dev/video0``
    (explicit node), or ``device:auto`` (→ index 0). The device is opened
    **lazily on first fetch** so constructing the source never touches hardware
    (safe in tests / headless boots). Requires OpenCV (``opencv-python``);
    it's imported lazily so the rest of the module works without it.
    """

    def __init__(self, *, camera_id: str, device: str) -> None:
        self.camera_id = camera_id
        self._spec = _parse_device_spec(device)
        self._cap = None  # opened lazily

    def _open(self):
        try:
            import cv2  # lazy: only the device source needs OpenCV
        except Exception as exc:  # pragma: no cover - import env specific
            raise FrameSourceError(
                "device frame source needs OpenCV (opencv-python) but it could "
                f"not be imported: {type(exc).__name__}: {exc}"
            ) from exc
        cap = cv2.VideoCapture(self._spec)
        if cap is None or not cap.isOpened():
            raise FrameSourceError(
                f"device frame source: could not open capture device "
                f"{self._spec!r} for camera {self.camera_id!r}. Is a camera "
                "connected and not in use by another app?"
            )
        return cap

    def fetch(self) -> bytes:
        try:
            import cv2
        except Exception as exc:  # pragma: no cover - import env specific
            raise FrameSourceError(
                f"device frame source needs OpenCV: {exc}"
            ) from exc
        if self._cap is None:
            self._cap = self._open()
        ok, frame = self._cap.read()
        if not ok or frame is None:
            # Drop the handle so the next cycle re-opens (USB camera unplugged,
            # drone stream blipped, etc.) rather than wedging on a dead device.
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
            raise FrameSourceError(
                f"device frame source: read failed for camera {self.camera_id!r}"
            )
        ok, buf = cv2.imencode(".jpg", frame)
        if not ok:
            raise FrameSourceError(
                f"device frame source: JPEG encode failed for {self.camera_id!r}"
            )
        return buf.tobytes()


_SYNTH_OPEN = b"<SYNTH>"
_SYNTH_CLOSE = b"</SYNTH>"
_LABEL_ALIASES = {"people": "person", "persons": "person", "ppl": "person"}


def parse_synth_spec(spec: str) -> dict[str, int]:
    """``"people=2,cars=1"`` → ``{"person": 2, "car": 1}``. Labels are
    normalised to the singular YOLO-style class the detector emits."""
    out: dict[str, int] = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        label, _, count = part.partition("=")
        label = label.strip().lower()
        label = _LABEL_ALIASES.get(label, label)
        if label.endswith("s") and label not in ("bus",):
            label = label[:-1]  # cars → car
        try:
            n = int(count.strip())
        except ValueError:
            continue
        if n > 0 and label:
            out[label] = out.get(label, 0) + n
    return out


def synth_detections_from_frame(frame_jpeg: bytes) -> list[dict]:
    """Read the ground-truth scene a SyntheticFrameSource embedded after the
    JPEG and expand it into detector-shaped detections (non-overlapping boxes
    so the de-dup keeps each). Returns [] for a frame with no marker."""
    i = frame_jpeg.rfind(_SYNTH_OPEN)
    j = frame_jpeg.rfind(_SYNTH_CLOSE)
    if i == -1 or j == -1 or j < i:
        return []
    import json
    try:
        spec = json.loads(frame_jpeg[i + len(_SYNTH_OPEN):j].decode("utf-8"))
    except Exception:
        return []
    labels: list[str] = []
    for label, count in spec.items():
        labels.extend([str(label)] * int(count))
    total = len(labels) or 1
    dets = []
    for idx, label in enumerate(labels):
        dets.append({
            "label": label,
            "confidence": 0.95,
            "bbox": {"x": (idx + 1) / (total + 1), "y": 0.5, "w": 0.14, "h": 0.45},
        })
    return dets


class SyntheticFrameSource:
    """A camera that needs no hardware: it renders a deterministic JPEG of a
    scripted scene and embeds that scene's ground truth after the image, so a
    demo detector (``synthetic_detection``) returns exactly what's drawn. This
    lets the whole agent + demo UI run with zero cameras/adapters — ideal for
    recording the demo GIF or trying the agent on a bare machine.

    URL form: ``synth:people=2,cars=1`` (or ``synth:`` for an empty scene).
    Requires Pillow (imported lazily). Clearly a DEMO source — not real vision.
    """

    def __init__(self, *, camera_id: str, spec: str) -> None:
        self.camera_id = camera_id
        self._spec = parse_synth_spec(spec)
        self._cached: bytes | None = None

    def fetch(self) -> bytes:
        if self._cached is not None:
            return self._cached  # deterministic: same frame every call
        try:
            from PIL import Image, ImageDraw
        except Exception as exc:  # pragma: no cover - import env specific
            raise FrameSourceError(
                "synthetic frame source needs Pillow (pip install pillow): "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        import io
        import json as _json

        W, H = 640, 480
        img = Image.new("RGB", (W, H), (24, 28, 40))
        draw = ImageDraw.Draw(img)
        draw.text((12, 10), f"DEMO · {self.camera_id}", fill=(180, 190, 210))
        colors = {"person": (90, 200, 160), "car": (230, 140, 90), "dog": (200, 200, 110)}
        labels: list[str] = []
        for label, count in self._spec.items():
            labels.extend([label] * count)
        total = len(labels) or 1
        for idx, label in enumerate(labels):
            cx = (idx + 1) / (total + 1) * W
            bw, bh = 0.14 * W, 0.45 * H
            x1, y1 = cx - bw / 2, H * 0.5 - bh / 2
            col = colors.get(label, (150, 160, 200))
            draw.rectangle([x1, y1, x1 + bw, y1 + bh], outline=col, width=4)
            draw.text((x1 + 4, y1 - 14), label, fill=col)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        # Append the ground truth AFTER the JPEG's EOI — decoders ignore
        # trailing bytes, so the image still displays, but the demo detector
        # can read exactly what's in the scene.
        payload = _SYNTH_OPEN + _json.dumps(self._spec).encode("utf-8") + _SYNTH_CLOSE
        self._cached = buf.getvalue() + payload
        return self._cached


def _parse_device_spec(device: str):
    """``device:0`` → int 0; ``device:auto`` → int 0; ``device:/dev/video1`` →
    the path string (OpenCV accepts either)."""
    d = (device or "").strip()
    if d == "" or d.lower() == "auto":
        return 0
    if d.isdigit():
        return int(d)
    return d


def _list_video_devices() -> list[str]:
    """Linux ``/dev/video*`` nodes, sorted. Split out so tests can monkeypatch
    it without real hardware."""
    import glob
    return sorted(glob.glob("/dev/video*"))


def discover_local_cameras(*, all_devices: bool = False) -> list[tuple[str, str]]:
    """Discover camera(s) physically attached to *this* machine and return
    ``(camera_id, frame_url)`` pairs ready for the config.

    Default returns a single onboard camera (the lowest ``/dev/video*`` node,
    or capture index 0 when nothing is enumerable — e.g. macOS/Windows or a
    webcam OpenCV opens by index). Set ``all_devices=True`` to expose every
    enumerated node (on Linux one physical camera can appear as several nodes,
    so the single-camera default is the saner one for a quick start)."""
    try:
        nodes = _list_video_devices()
    except Exception:  # pragma: no cover - platform specific
        nodes = []
    if not nodes:
        return [("local0", "device:0")]
    if all_devices:
        return [(f"local{i}", f"device:{node}") for i, node in enumerate(nodes)]
    return [("local0", f"device:{nodes[0]}")]


def _scrub_creds(text: str) -> str:
    """Remove URL userinfo (``user:pass@``) from arbitrary text such as
    ffmpeg stderr, which echoes the input URL including credentials."""
    return re.sub(r"://[^/\s@]+@", "://", text)


def _redact(url: str) -> str:
    """Strip credentials AND query secrets (e.g. ``?jwt=...`` on MediaMTX tap
    URLs) from an RTSP URL before logging it."""
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
        # urlparse("file:///path") → path is in parsed.path
        path = parsed.path
        if os.name == "nt":
            # A drive path lands in the netloc (file://C:\x.jpg) or behind
            # a leading slash (file:///C:/x.jpg).
            if re.match(r"[A-Za-z]:", parsed.netloc):
                path = parsed.netloc + path
            elif re.match(r"/[A-Za-z]:", path):
                path = path[1:]
        return FileFrameSource(camera_id=camera_id, path=path)
    if scheme in ("http", "https"):
        return HttpSnapshotSource(camera_id=camera_id, url=url)
    if scheme == "opennvr":
        raise FrameSourceError(
            "opennvr:// scheme is reserved for a future OpenNVR-backend snapshot "
            "endpoint and is not yet implemented. Use http(s):// against the "
            "camera's snapshot URL directly for now."
        )
    if scheme == "rtsp":
        return RtspFrameSource(camera_id=camera_id, url=url)
    if scheme == "device":
        # ``device:0`` → urlparse puts "0" in .path; ``device:/dev/video0`` too.
        return DeviceFrameSource(camera_id=camera_id, device=parsed.path or parsed.netloc)
    if scheme == "synth":
        # ``synth:people=2,cars=1`` — a scripted, hardware-free demo camera.
        return SyntheticFrameSource(camera_id=camera_id, spec=parsed.path or parsed.netloc)
    raise FrameSourceError(
        f"unsupported frame source scheme {scheme!r}; expected "
        "file/http/https/rtsp/device."
    )
