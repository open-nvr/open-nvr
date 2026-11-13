# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""PTZ presets per camera, cached (HA-114).

Presets live on the camera and reading them is an ONVIF round trip, so the
entity resolver refreshes them every few minutes for PTZ cameras and the
``select`` descriptor reads this cache. A camera that fails to answer keeps
its last good list.
"""

from __future__ import annotations

import time

REFRESH_S = 300.0

_presets: dict[int, list[dict[str, str]]] = {}
_refreshed: dict[int, float] = {}


def get(camera_id: int) -> list[dict[str, str]] | None:
    return _presets.get(camera_id)


def token_for(camera_id: int, name: str) -> str | None:
    for p in _presets.get(camera_id) or []:
        if p.get("name") == name:
            return p.get("token")
    return None


def due(camera_id: int, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    return now - _refreshed.get(camera_id, -1e9) >= REFRESH_S


async def refresh(cam, now: float | None = None) -> None:
    from services.ptz_service import PTZService

    _refreshed[cam.id] = time.monotonic() if now is None else now
    if not cam.username or not cam.password:
        return
    try:
        _presets[cam.id] = await PTZService.presets(
            camera_id=cam.id, ip=cam.ip_address, username=cam.username,
            password=cam.password, camera_port=cam.port)
    except Exception:  # noqa: BLE001 - keep the last good list
        pass


def put(camera_id: int, presets: list[dict[str, str]]) -> None:
    """For tests and for callers that just read presets anyway."""
    _presets[camera_id] = presets
