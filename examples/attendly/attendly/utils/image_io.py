"""Optional debug frames on disk. Off unless ``ATTENDLY_SAVE_DEBUG_FRAMES``
is set: a classroom worker would otherwise write a JPEG per attempt for
every class, all day."""
from __future__ import annotations

import os
import pathlib
from datetime import datetime, timezone
from typing import Optional

from ..config import get_settings


def _dir(name: str) -> str:
    settings = get_settings()
    path = pathlib.Path(settings.data_dir) / name
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def ensure_snapshots_dir() -> str:
    return _dir("snapshots")


def ensure_detected_faces_dir() -> str:
    return _dir("detected_faces")


def _write(frame, directory: str, filename: str) -> Optional[str]:
    try:
        import cv2

        path = os.path.join(directory, filename)
        return path if cv2.imwrite(path, frame) else None
    except Exception:
        return None


def save_detected_face(face_bgr, *, tag: Optional[str] = None) -> Optional[str]:
    if face_bgr is None or not get_settings().save_debug_frames:
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    parts = ["face", ts] + ([str(tag)] if tag else [])
    return _write(face_bgr, ensure_detected_faces_dir(), "_".join(parts) + ".jpg")


def save_frame_snapshot(
    frame, *, class_id: Optional[str] = None, stream_id: Optional[int] = None,
    tag: Optional[str] = None,
) -> Optional[str]:
    if frame is None or not get_settings().save_debug_frames:
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    parts = ["snapshot", ts]
    if class_id:
        parts.append(f"class-{class_id}")
    if stream_id is not None:
        parts.append(f"stream-{stream_id}")
    if tag:
        parts.append(str(tag))
    return _write(frame, ensure_snapshots_dir(), "_".join(parts) + ".jpg")
