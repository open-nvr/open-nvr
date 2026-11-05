"""Rooms (streams) and the manual snapshot / probe paths."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..storage.repo import (
    finalize_attendance as repo_finalize_attendance,
    get_student,
    save_snapshot,
    streams_delete,
    streams_get,
    streams_list,
    streams_put,
    streams_update,
)
from ..utils.image_io import save_frame_snapshot
from ..utils.logging_utils import log_call
from .recognize import RecognitionUnavailable, recognize_codes_in_frame

logger = logging.getLogger(__name__)


def _public(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": int(item["id"]), "name": item.get("name") or "", "camera_id": item.get("camera_id"),
        "camera_name": item.get("camera_name"), "url": item.get("url") or "", "status": item.get("status") or "configured",
    }


class StreamsRegistry:
    """The rooms as the API sees them; persistence is the repo."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def list(self) -> List[Dict[str, Any]]:
        return [_public(it) for it in streams_list(include_deleted=False)]

    def get(self, stream_id: int) -> Optional[Dict[str, Any]]:
        it = streams_get(stream_id)
        return _public(it) if it and not it.get("deleted_at") else None

    def add(self, name: str, camera_id: int, camera_name: Optional[str] = None) -> Dict[str, Any]:
        return _public(streams_put(name=name, camera_id=int(camera_id), camera_name=camera_name))

    def update(self, stream_id: int, *, name: Optional[str] = None, camera_id: Optional[int] = None,
               camera_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        updated = streams_update(stream_id, name=name, camera_id=camera_id, camera_name=camera_name)
        return _public(updated) if updated else None

    def remove(self, stream_id: int) -> bool:
        return streams_delete(stream_id)

    def load_from_db(self) -> None:  # kept for the routers that call it
        return None


registry = StreamsRegistry()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _engine_and_frames():
    from ..cameras import CameraSource
    from ..face.engine import FaceEngine

    return FaceEngine.get_shared(), CameraSource.get_shared()


@log_call()
def run_snapshot(stream_id: int, class_id: str, slot_time: Optional[datetime] = None) -> Dict[str, object]:
    entry = streams_get(stream_id)
    if not entry or entry.get("deleted_at"):
        return {"ok": False, "error": "stream_not_found"}
    if entry.get("camera_id") is None:
        return {"ok": False, "error": "no_camera"}
    engine, frames = _engine_and_frames()
    frame = frames.snapshot(int(entry["camera_id"]))
    if frame is None:
        return {"ok": False, "error": "no_frame"}
    save_frame_snapshot(frame, class_id=class_id, stream_id=stream_id, tag="slot")
    try:
        codes = recognize_codes_in_frame(engine, frame, tag=f"manual-s{stream_id}")
    except RecognitionUnavailable as exc:
        return {"ok": False, "error": "recognition_unavailable", "detail": str(exc)}
    slot_dt = slot_time or _utcnow()
    save_snapshot(class_id=class_id, student_codes=codes, slot_time=slot_dt)
    return {"ok": True, "class_id": class_id, "stream_id": stream_id, "recognized": codes,
            "slot_time": slot_dt.isoformat()}


@log_call()
def probe(stream_id: int) -> Dict[str, object]:
    """One frame, every face recognised, nothing written."""
    entry = streams_get(stream_id)
    if not entry or entry.get("deleted_at"):
        return {"ok": False, "error": "not_found"}
    if entry.get("camera_id") is None:
        return {"ok": False, "error": "no_camera"}
    engine, frames = _engine_and_frames()
    frame = frames.snapshot(int(entry["camera_id"]))
    if frame is None:
        return {"ok": False, "error": "no_frame"}
    try:
        codes = recognize_codes_in_frame(engine, frame, tag=f"probe-s{stream_id}")
    except RecognitionUnavailable as exc:
        return {"ok": False, "error": "recognition_unavailable", "detail": str(exc)}
    return {"ok": True, "matches": [{"student_code": c, "student": get_student(c)} for c in codes]}


@log_call()
def finalize_attendance(class_id: str) -> Dict[str, object]:
    return repo_finalize_attendance(class_id)
