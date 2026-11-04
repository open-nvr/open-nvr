from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException

from ..storage.repo import (
    list_sections_by_course,
    planner_course_delete,
    planner_course_get,
    planner_course_list,
    planner_course_put,
    planner_course_update,
)
from ..utils.logging_utils import log_call

router = APIRouter()


@router.get("/", response_model=List[Dict[str, Any]])
@log_call()
def list_courses():
    return planner_course_list()


@router.post("/", response_model=Dict[str, Any])
@log_call()
def create_course(payload: Dict[str, Any]):
    code = str(payload.get("code", "")).strip()
    name = str(payload.get("name", "")).strip() or None
    students = int(payload.get("students", 0) or 0)
    sections = int(payload.get("sections", 0) or 0)
    sections_list = payload.get("sections_list") or []
    if not isinstance(sections_list, list):
        sections_list = []
    if not code:
        raise HTTPException(status_code=400, detail="code is required")
    if planner_course_get(code):
        raise HTTPException(status_code=409, detail="course already exists")
    sections_list = list(dict.fromkeys([str(s).strip() for s in sections_list if str(s).strip()]))
    return planner_course_put(code=code, students=students, sections=sections, name=name, sections_list=sections_list)


@router.put("/{code}", response_model=Dict[str, Any])
@log_call()
def update_course(code: str, payload: Dict[str, Any]):
    sections_list = payload.get("sections_list")
    if sections_list is not None:
        if not isinstance(sections_list, list):
            raise HTTPException(status_code=400, detail="sections_list must be a list of strings")
        sections_list = list(dict.fromkeys([str(s).strip() for s in sections_list if str(s).strip()]))
    updated = planner_course_update(code=code, students=payload.get("students"), sections=payload.get("sections"),
                                    name=payload.get("name"), sections_list=sections_list)
    if not updated:
        raise HTTPException(status_code=404, detail="course not found")
    return updated


@router.delete("/{code}")
@log_call()
def delete_course(code: str):
    return {"ok": bool(planner_course_delete(code))}


@router.get("/{code}/sections")
@log_call()
def list_course_sections(code: str):
    course = planner_course_get(code)
    if course and course.get("sections_list"):
        secs = list(course.get("sections_list") or [])
    else:
        secs = list_sections_by_course(code)
    return {"course_code": code, "sections": secs}
