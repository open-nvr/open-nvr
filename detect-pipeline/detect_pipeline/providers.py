# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Camera discovery via OpenNVR's internal read-only endpoint.

Calls ``GET {base}/internal/detect/cameras`` (service-token auth) and maps the
response to :class:`CameraSpec`. Stdlib-only (urllib) so the package stays
dependency-light; the opener is injectable for tests.
"""
from __future__ import annotations

import json
import logging
import urllib.request

from .service import CameraSpec

log = logging.getLogger("detect_pipeline.providers")


class HttpCameraProvider:
    """Reads the camera list from opennvr-core's internal endpoint."""

    def __init__(self, base_url: str, token: str | None = None, *, opener=None, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._opener = opener or urllib.request.urlopen
        self.timeout = timeout

    def list_cameras(self) -> list[CameraSpec]:
        req = urllib.request.Request(f"{self.base_url}/internal/detect/cameras")
        if self.token:
            req.add_header("X-Service-Token", self.token)
        try:
            with self._opener(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            # Discovery failure must not crash the manager — reconcile keeps the
            # current workers and retries on the next tick.
            log.warning("camera discovery failed at %s", self.base_url, exc_info=True)
            return []
        return [_to_spec(c) for c in data.get("cameras", [])]


def _to_spec(c: dict) -> CameraSpec:
    return CameraSpec(
        camera_id=str(c["camera_id"]),
        name=c.get("name", str(c["camera_id"])),
        substream_url=c["substream_url"],
        analyze=bool(c.get("analyze", True)),
        width=c.get("width"),
        height=c.get("height"),
        fps=int(c.get("fps", 5)),
        hwaccel=c.get("hwaccel", "cpu"),
    )
