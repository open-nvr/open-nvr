"""Cameras are OpenNVR's. The roster and every frame come through core —
the app never holds an RTSP URL of its own."""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from .config import get_settings

logger = logging.getLogger(__name__)


class PlatformUnavailable(RuntimeError):
    """Core could not be asked about its cameras."""


class CameraSource:
    _shared: "CameraSource | None" = None
    _lock = threading.Lock()

    def __init__(self, nvr: Any) -> None:
        self._nvr = nvr

    @classmethod
    def get_shared(cls) -> "CameraSource":
        with cls._lock:
            if cls._shared is None:
                from opennvr_app_sdk import OpenNVR

                s = get_settings()
                nvr = OpenNVR(s.opennvr_url, token=s.internal_api_key or None, kaic_url=s.kaic_url,
                              kaic_api_key=s.internal_api_key or None, timeout=s.platform_timeout_seconds,
                              client_id="attendly")
                cls._shared = CameraSource(nvr)
            return cls._shared

    @classmethod
    def set_shared(cls, source: "CameraSource | None") -> None:
        with cls._lock:
            cls._shared = source

    def list_cameras(self) -> List[Dict[str, Any]]:
        """OpenNVR's cameras. Raises :class:`PlatformUnavailable` when core
        could not be asked — the SDK's ``roster()`` tells that apart from
        "no cameras", and the rooms page should say which it is."""
        rows = self._nvr.roster()
        if rows is None:
            raise PlatformUnavailable("OpenNVR core could not be reached (or refused the key)")
        out: List[Dict[str, Any]] = []
        for cam in rows:
            out.append({"id": int(cam.id), "handle": str(cam.handle), "name": str(cam.name),
                        "role": str(getattr(cam, "role", "") or "")})
        return out

    def camera_name(self, camera_id: int) -> Optional[str]:
        for cam in self.list_cameras():
            if cam["id"] == int(camera_id):
                return cam["name"]
        return None

    def snapshot_jpeg(self, camera_id: int) -> Optional[bytes]:
        try:
            return self._nvr.snapshot(int(camera_id))
        except Exception as exc:  # the platform being briefly unreachable is not fatal
            logger.warning("snapshot of camera %s failed: %s", camera_id, exc)
            return None

    def snapshot(self, camera_id: int):
        """The camera's current frame as a BGR array, or None."""
        jpeg = self.snapshot_jpeg(camera_id)
        if not jpeg:
            return None
        import cv2
        import numpy as np

        arr = np.frombuffer(jpeg, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return frame if frame is not None and frame.size else None
