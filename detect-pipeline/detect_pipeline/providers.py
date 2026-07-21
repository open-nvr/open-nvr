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
        fps=int(c.get("fps", 5)),
        hwaccel=c.get("hwaccel", "cpu"),
    )
