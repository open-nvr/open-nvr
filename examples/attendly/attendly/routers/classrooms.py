from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

from ..cameras import CameraSource
from ..storage.repo import streams_list, streams_put, streams_soft_delete, streams_update
from ..utils.logging_utils import log_call

router = APIRouter()


def _to_classroom(item: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": int(item.get("id", 0)), "name": str(item.get("name", "")), "camera_id": item.get("camera_id"),
            "camera_name": item.get("camera_name"), "rtsp_url": str(item.get("url", "")),
            "status": str(item.get("status", "configured"))}


def _camera_name(camera_id: int) -> Optional[str]:
    try:
        return CameraSource.get_shared().camera_name(camera_id)
    except Exception:
        return None


def _parse_camera_id(payload: Dict[str, Any]) -> Optional[int]:
    raw = payload.get("camera_id")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="camera_id must be an integer OpenNVR camera id")


@router.get("/", response_model=List[Dict[str, Any]])
@log_call()
def list_classrooms():
    return [_to_classroom(it) for it in streams_list(include_deleted=False)]


@router.post("/", response_model=Dict[str, Any])
@log_call()
def create_classroom(payload: Dict[str, Any]):
    name = str(payload.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    camera_id = _parse_camera_id(payload)
    if camera_id is None:
        raise HTTPException(status_code=400, detail="camera_id is required: pick one of OpenNVR's cameras")
    camera_name = str(payload.get("camera_name") or "").strip() or _camera_name(camera_id)
    return _to_classroom(streams_put(name=name, camera_id=camera_id, camera_name=camera_name))


@router.put("/{id}", response_model=Dict[str, Any])
@log_call()
def update_classroom(id: int, payload: Dict[str, Any]):
    name = payload.get("name")
    camera_id = _parse_camera_id(payload)
    if name is None and camera_id is None:
        raise HTTPException(status_code=400, detail="no fields to update")
    camera_name = None
    if camera_id is not None:
        camera_name = str(payload.get("camera_name") or "").strip() or _camera_name(camera_id) or ""
    updated = streams_update(int(id), name=name, camera_id=camera_id, camera_name=camera_name)
    if not updated:
        raise HTTPException(status_code=404, detail="room not found")
    return _to_classroom(updated)


@router.delete("/{id}")
@log_call()
def delete_classroom(id: int):
    return {"ok": bool(streams_soft_delete(int(id)))}
