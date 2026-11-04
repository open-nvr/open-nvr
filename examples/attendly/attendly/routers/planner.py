from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query

from ..services import planner_publisher
from ..services.planner_publisher import DAYS
from ..storage.repo import (
    planner_course_delete,
    planner_course_get,
    planner_course_list,
    planner_course_put,
    planner_course_update,
    planner_template_delete,
    planner_template_get,
    planner_template_list,
    planner_template_put,
    planner_template_update,
    streams_list,
)
from ..utils.logging_utils import log_call

router = APIRouter()


def _room_exists(room_id: int) -> bool:
    return any(int(it.get("id", 0)) == room_id for it in streams_list(include_deleted=False))


@router.get("/courses")
@log_call()
def list_courses():
    return planner_course_list()


@router.post("/courses")
@log_call()
def create_course(payload: Dict[str, Any]):
    code = str(payload.get("code", "")).strip()
    if not code:
        raise HTTPException(status_code=400, detail="code is required")
    if planner_course_get(code):
        raise HTTPException(status_code=409, detail="course already exists")
    return planner_course_put(code=code, students=int(payload.get("students", 0) or 0), sections=int(payload.get("sections", 0) or 0))


@router.put("/courses/{code}")
@log_call()
def update_course(code: str, payload: Dict[str, Any]):
    students, sections = payload.get("students"), payload.get("sections")
    if students is None and sections is None:
        raise HTTPException(status_code=400, detail="no fields to update")
    updated = planner_course_update(code=code, students=students, sections=sections)
    if not updated:
        raise HTTPException(status_code=404, detail="course not found")
    return updated


@router.delete("/courses/{code}")
@log_call()
def delete_course(code: str):
    return {"ok": bool(planner_course_delete(code))}


def _validate_time(t: Any) -> bool:
    return isinstance(t, str) and len(t) == 5 and t[:2].isdigit() and t[3:].isdigit() and t[2] == ":"


@router.get("/schedules")
@log_call()
def list_templates(day: Optional[str] = Query(default=None)):
    return planner_template_list(day_of_week=day)


@router.post("/schedules")
@log_call()
def create_template(payload: Dict[str, Any]):
    day = str(payload.get("day_of_week", "")).strip() or None
    course = str(payload.get("course", "")).strip()
    section = str(payload.get("section", "")).strip()
    start = str(payload.get("start", "")).strip()
    end = str(payload.get("end", "")).strip()
    active = bool(payload.get("active", True))
    if day not in DAYS:
        raise HTTPException(status_code=400, detail="day_of_week must be Monday–Sunday")
    if not course or not section:
        raise HTTPException(status_code=400, detail="course and section are required")
    try:
        room_id = int(payload.get("room_id"))
    except Exception:
        raise HTTPException(status_code=400, detail="room_id is required and must be an integer")
    if not _room_exists(room_id):
        raise HTTPException(status_code=404, detail="room not found")
    if not (_validate_time(start) and _validate_time(end)):
        raise HTTPException(status_code=400, detail="start/end must be HH:MM")
    if not start < end:
        raise HTTPException(status_code=400, detail="start must be earlier than end")
    return planner_template_put(day_of_week=day, course=course, section=section, room_id=room_id, start=start, end=end, active=active)


@router.put("/schedules/{id}")
@log_call()
def update_template(id: str, payload: Dict[str, Any]):
    start = payload.get("start")
    end = payload.get("end")
    if start is not None and not _validate_time(start):
        raise HTTPException(status_code=400, detail="start must be HH:MM")
    if end is not None and not _validate_time(end):
        raise HTTPException(status_code=400, detail="end must be HH:MM")
    if start is not None and end is not None and not start < end:
        raise HTTPException(status_code=400, detail="start must be earlier than end")
    if not planner_template_get(id):
        raise HTTPException(status_code=404, detail="template not found")
    if payload.get("room_id") is not None:
        try:
            rid = int(payload.get("room_id"))
        except Exception:
            raise HTTPException(status_code=400, detail="room_id must be an integer")
        if not _room_exists(rid):
            raise HTTPException(status_code=404, detail="room not found")
    return planner_template_update(id, **payload) or {}


@router.delete("/schedules/{id}")
@log_call()
def delete_template(id: str):
    return {"ok": bool(planner_template_delete(id))}


@router.post("/publish")
@log_call()
def publish(payload: Dict[str, Any]):
    from_date = str(payload.get("from_date", "")).strip()
    to_date = str(payload.get("to_date", "")).strip()
    if not from_date or not to_date:
        raise HTTPException(status_code=400, detail="from_date and to_date are required (YYYY-MM-DD)")
    return planner_publisher.publish(from_date=from_date, to_date=to_date, templates=planner_template_list())
