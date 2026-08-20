# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Camera discovery via OpenNVR's existing internal endpoint.

Reuses opennvr-core's ``GET /api/v1/internal/camera-agent/cameras`` — the same
internal, ``X-Internal-Api-Key``-authenticated endpoint the camera-agent uses. It
already resolves each active camera to a pullable ``frame_url`` (the MediaMTX tap
with a signed JWT — i.e. OpenNVR keeps ownership of the single camera connection —
or the stored RTSP URL as a fallback). So the Tier-0 service needs **no new
server endpoint**: it consumes the exact frame source OpenNVR already exposes.

Stdlib-only (urllib); the opener is injectable for tests. Discovery failure
returns ``[]`` so the manager keeps its current workers and retries next tick.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request

from .service import CameraSpec

log = logging.getLogger("detect_pipeline.providers")

DEFAULT_PATH = "/api/v1/internal/camera-agent/cameras"


class HttpCameraProvider:
    """Reads active cameras (as frame sources) from opennvr-core."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        path: str = DEFAULT_PATH,
        opener=None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.path = path
        self._opener = opener or urllib.request.urlopen
        self.timeout = timeout

    def list_cameras(self) -> list[CameraSpec]:
        req = urllib.request.Request(f"{self.base_url}{self.path}")
        if self.api_key:
            req.add_header("X-Internal-Api-Key", self.api_key)
        try:
            with self._opener(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            log.warning("camera discovery failed at %s%s", self.base_url, self.path, exc_info=True)
            return []
        return [_to_spec(c) for c in data.get("cameras", [])]


def _default_fps() -> int:
    """Per-camera analysis rate: DETECT_FPS env, else 5.

    Detection currently runs on EVERY analyzed frame (the gate skips
    alarms, not inference), so this is the single biggest CPU dial the
    pipeline has: on CPU-only hosts — a laptop, or ANY macOS/Windows
    Docker install (the VM has no GPU) — dropping 5 → 1-2 fps cuts
    steady-state pipeline CPU nearly proportionally, at the cost of
    coarser motion/track granularity. Clamped to [1, 30]; a camera dict
    carrying an explicit per-camera ``fps`` still wins.
    """
    try:
        fps = int(os.environ.get("DETECT_FPS", "5"))
    except ValueError:
        log.warning("DETECT_FPS=%r is not an integer; using 5",
                    os.environ.get("DETECT_FPS"))
        return 5
    return max(1, min(30, fps))


def _to_spec(c: dict) -> CameraSpec:
    # The endpoint returns active cameras with a resolved ``frame_url``. All
    # active cameras are analyzed by default (on-by-default); an ``analyze`` flag
    # is honoured if the endpoint ever adds per-camera opt-out.
    return CameraSpec(
        camera_id=str(c["camera_id"]),
        name=c.get("name", str(c["camera_id"])),
        substream_url=c["frame_url"],
        analyze=bool(c.get("analyze", True)),
        width=c.get("width"),
        height=c.get("height"),
        fps=int(c.get("fps", _default_fps())),
        hwaccel=c.get("hwaccel", "cpu"),
    )


DETECT_CONFIG_PATH = "/api/v1/internal/camera-agent/detect-config"


def fetch_detect_config(
    base_url: str,
    api_key: str | None = None,
    *,
    opener=None,
    timeout: float = 5.0,
) -> dict | None:
    """Fetch the managed Tier-0 config override from core (guided promotion).

    Returns e.g. ``{"gate_mode": "enforce"}`` — ``gate_mode: None`` means "no
    override, follow env". Any failure returns None (caller keeps current
    settings; this must never disturb the pipeline)."""
    _opener = opener or urllib.request.urlopen
    req = urllib.request.Request(f"{base_url.rstrip('/')}{DETECT_CONFIG_PATH}")
    if api_key:
        req.add_header("X-Internal-Api-Key", api_key)
    try:
        with _opener(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        log.debug("detect-config fetch failed", exc_info=True)
        return None
