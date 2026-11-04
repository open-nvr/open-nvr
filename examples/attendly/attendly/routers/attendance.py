from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

from .. import schemas
from ..services.streams_service import finalize_attendance as svc_finalize
from ..storage.repo import (
    get_attendance_by_class,
    get_students_batch,
    list_attendance_classes,
    list_finalized_rows,
    list_snapshot_classes,
)
from ..utils.logging_utils import log_call

router = APIRouter()


def _rows_out(items: List[Dict[str, Any]]) -> List[schemas.FinalizedAttendanceOut]:
    students = get_students_batch([it.get("student_code") for it in items if it.get("student_code")])
    out: List[schemas.FinalizedAttendanceOut] = []
    for it in items:
        s = students.get(it.get("student_code"))
        out.append(schemas.FinalizedAttendanceOut(
            id=int(it.get("id")), class_id=str(it.get("class_id")), student=schemas.student_out(s) if s else None,
            slots_present=int(it.get("slots_present", 0)), total_slots=int(it.get("total_slots", 0)),
            ratio=float(it.get("ratio") or 0.0), status=str(it.get("status")), finalized_at=str(it.get("finalized_at")),
        ))
    return out


@router.get("/classes", response_model=List[str])
@log_call()
def list_classes():
    return list_attendance_classes()


@router.get("/by-name/{class_name}", response_model=List[schemas.FinalizedAttendanceOut])
@log_call()
def get_attendance_by_class_name(class_name: str, date: Optional[str] = None):
    items = get_attendance_by_class(class_name)
    if date:
        items = [it for it in items if str(it.get("finalized_at", "")).startswith(date)]
    return _rows_out(items)


@router.get("/by-course/{course_code}", response_model=List[schemas.FinalizedAttendanceOut])
@log_call()
def get_attendance_by_course(course_code: str, date: Optional[str] = None):
    rows = [it for it in list_finalized_rows() if str(it.get("class_id") or "").startswith(f"{course_code}-")]
    if date:
        rows = [it for it in rows if str(it.get("finalized_at", "")).startswith(date)]
    return _rows_out(rows)


@router.get("/section/{section_id}", response_model=List[schemas.FinalizedAttendanceOut])
@log_call()
def get_attendance_by_section(section_id: str, date: Optional[str] = None):
    rows = list_finalized_rows()
    if date:
        rows = [it for it in rows if str(it.get("finalized_at", "")).startswith(date)]
    return _rows_out(rows)


@router.post("/refinalize/all")
@log_call()
def refinalize_all_classes() -> dict:
    classes = list_snapshot_classes()
    total, results = 0, []
    for cid in classes:
        try:
            res = svc_finalize(cid)
            total += int(res.get("written", 0))  # type: ignore[arg-type]
            results.append({"class_id": cid, "written": res.get("written", 0)})
        except Exception as e:
            results.append({"class_id": cid, "error": str(e)})
    return {"ok": True, "classes": classes, "total_written": total, "results": results}


@router.get("/{class_id}", response_model=List[schemas.FinalizedAttendanceOut])
@log_call()
def get_attendance(class_id: str):
    return _rows_out(get_attendance_by_class(class_id))
