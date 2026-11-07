from __future__ import annotations

import logging
from datetime import datetime
from typing import List

from fastapi import APIRouter, Body, HTTPException

from .. import schemas
from ..cameras import CameraSource
from ..config import get_settings
from ..services import streams_service as svc
from ..services.engine import StreamEngine
from ..services.streams_service import registry
from ..storage.repo import streams_get, streams_update
from ..utils.logging_utils import log_call

router = APIRouter()
logger = logging.getLogger(__name__)


def _reload_engine() -> None:
    try:
        StreamEngine.get_shared().reload_streams()
    except Exception:
        pass


def _camera_name(camera_id: int):
    try:
        return CameraSource.get_shared().camera_name(camera_id)
    except Exception:
        return None


@router.get("/", response_model=List[schemas.StreamOut])
@log_call()
def list_streams():
    return registry.list()


@router.post("/", response_model=schemas.StreamOut)
@log_call()
def add_stream(payload: schemas.StreamCreate):
    return registry.add(payload.name, payload.camera_id, _camera_name(payload.camera_id))


@router.post("/{stream_id}/config")
@log_call()
def set_stream_schedule(stream_id: int, section_name: str = Body(None), schedule: List[dict] = Body(default=[])):
    min_minutes = float(get_settings().min_schedule_duration_minutes)
    for sch in schedule or []:
        start_iso, end_iso = sch.get("start_iso"), sch.get("end_iso")
        if not start_iso or not end_iso:
            raise HTTPException(status_code=400, detail="start_iso and end_iso are required")
        try:
            minutes = (datetime.fromisoformat(end_iso) - datetime.fromisoformat(start_iso)).total_seconds() / 60.0
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid ISO timestamps")
        if minutes < min_minutes:
            raise HTTPException(status_code=400, detail=f"Minimum schedule duration is {min_minutes} minutes")
    updated = streams_update(stream_id, section_name=section_name, schedule=schedule)
    _reload_engine()
    if not updated:
        raise HTTPException(status_code=404, detail="Stream not found")
    return updated


@router.get("/{stream_id}/config")
@log_call()
def get_stream_schedule(stream_id: int):
    cur = streams_get(stream_id)
    if not cur:
        raise HTTPException(status_code=404, detail="Stream not found")
    return {"id": int(cur.get("id", 0)), "section_name": cur.get("section_name"),
            "schedule": cur.get("schedule") or [], "courses": cur.get("courses") or []}


@router.post("/{stream_id}/start")
@log_call()
def start_stream(stream_id: int):
    streams_update(stream_id, status="configured")
    return {"ok": True, "note": "Engine-managed scheduling; manual start disabled"}


@router.post("/{stream_id}/stop")
@log_call()
def stop_stream(stream_id: int):
    streams_update(stream_id, status="configured")
    return {"ok": True, "note": "Engine-managed scheduling; manual stop disabled"}


@router.delete("/{stream_id}")
@log_call()
def delete_stream(stream_id: int):
    ok = registry.remove(stream_id)
    _reload_engine()
    return {"ok": ok}


@router.post("/{stream_id}/probe")
@log_call()
def probe_stream(stream_id: int):
    """One frame from the room's camera, every face recognised."""
    return svc.probe(stream_id)


@router.put("/{stream_id}", response_model=schemas.StreamOut)
@log_call()
def update_stream(stream_id: int, payload: schemas.StreamCreate):
    updated = registry.update(stream_id, name=payload.name, camera_id=payload.camera_id,
                              camera_name=_camera_name(payload.camera_id) or "")
    if not updated:
        raise HTTPException(status_code=404, detail="Stream not found")
    return updated


@router.post("/{stream_id}/snapshot")
@log_call()
def run_snapshot(stream_id: int, class_id: str):
    return svc.run_snapshot(stream_id=stream_id, class_id=class_id)


@router.post("/classes/{class_id}/finalize")
@log_call()
def finalize_attendance(class_id: str):
    return svc.finalize_attendance(class_id)


@router.post("/{stream_id}/classes")
@log_call()
def add_one_off_class(stream_id: int, payload: schemas.OneOffClass):
    """A class that is not on the timetable: an extra lecture, an exam.
    Local times; it must not have ended yet."""
    from datetime import datetime

    from ..services.planner_publisher import _class_id
    from ..utils.time import local_tz, now_local

    cur = streams_get(stream_id)
    if not cur or cur.get("deleted_at"):
        raise HTTPException(status_code=404, detail="Stream not found")
    try:
        day = datetime.fromisoformat(payload.date).replace(tzinfo=local_tz())
        sdt = day.replace(hour=int(payload.start[:2]), minute=int(payload.start[3:]))
        edt = day.replace(hour=int(payload.end[:2]), minute=int(payload.end[3:]))
    except ValueError:
        raise HTTPException(status_code=400, detail="bad date or time")
    if edt <= sdt:
        raise HTTPException(status_code=400, detail="the class must end after it starts")
    if edt <= now_local():
        raise HTTPException(status_code=400, detail="the class has already ended; nobody could have watched it")
    class_id = _class_id(payload.course.strip(), payload.section.strip(), day, payload.start, payload.end)
    schedule = list(cur.get("schedule") or [])
    if any(str(e.get("class_id")) == class_id for e in schedule):
        raise HTTPException(status_code=409, detail="that class is already scheduled on this room")
    schedule.append({"class_id": class_id, "start_iso": sdt.isoformat(), "end_iso": edt.isoformat(),
                     "course": payload.course.strip(), "section": payload.section.strip()})
    streams_update(stream_id, schedule=schedule)
    _reload_engine()
    return {"ok": True, "class_id": class_id, "stream_id": stream_id, "start_iso": sdt.isoformat(), "end_iso": edt.isoformat()}


@router.post("/{stream_id}/schedule")
@log_call()
def schedule_class(stream_id: int, class_id: str, class_start_iso: str, class_end_iso: str):
    cur = streams_get(stream_id)
    if not cur:
        raise HTTPException(status_code=404, detail="Stream not found")
    schedule = list(cur.get("schedule") or [])
    schedule.append({"class_id": class_id, "start_iso": class_start_iso, "end_iso": class_end_iso})
    updated = streams_update(stream_id, schedule=schedule)
    _reload_engine()
    return {"ok": True, "stream": updated}


@router.get("/{stream_id}/courses")
@log_call()
def get_stream_courses(stream_id: int):
    cur = streams_get(stream_id)
    if not cur:
        raise HTTPException(status_code=404, detail="Stream not found")
    return {"id": int(cur.get("id", 0)), "courses": cur.get("courses") or []}


@router.post("/{stream_id}/courses")
@log_call()
def add_stream_course(stream_id: int, course_code: str):
    cur = streams_get(stream_id)
    if not cur:
        raise HTTPException(status_code=404, detail="Stream not found")
    code = (course_code or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="course_code is required")
    courses = list(cur.get("courses") or [])
    if code not in courses:
        courses.append(code)
    updated = streams_update(stream_id, courses=courses)
    return {"id": stream_id, "courses": updated.get("courses") if updated else courses}


@router.delete("/{stream_id}/courses/{course_code}")
@log_call()
def remove_stream_course(stream_id: int, course_code: str):
    cur = streams_get(stream_id)
    if not cur:
        raise HTTPException(status_code=404, detail="Stream not found")
    courses = [c for c in (cur.get("courses") or []) if str(c) != str(course_code)]
    streams_update(stream_id, courses=courses)
    return {"id": stream_id, "courses": courses}
