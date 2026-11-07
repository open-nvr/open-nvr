"""The staff attendance report and the department workers' status."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ..services.staff_attendance import StaffEngine
from ..storage import staff_repo as repo
from ..utils.logging_utils import log_call
from ..utils.time import now_local

router = APIRouter()


@router.get("/")
@log_call()
def staff_attendance(date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
                     department_id: Optional[int] = None):
    day = date or now_local().strftime("%Y-%m-%d")
    if department_id is not None and repo.department_get(department_id) is None:
        raise HTTPException(status_code=404, detail="department not found")
    names = {d["id"]: d["name"] for d in repo.department_list()}
    rows = repo.staff_attendance_for_day(day, department_id)
    for r in rows:
        r["department_name"] = names.get(r.get("department_id")) or names.get((r.get("staff") or {}).get("department_id"))
    present = sum(1 for r in rows if r["status"] == "PRESENT")
    return {"date": day, "department_id": department_id, "present": present, "absent": len(rows) - present, "rows": rows}


@router.get("/dates")
@log_call()
def staff_attendance_dates():
    return repo.staff_attendance_dates()


@router.get("/engine_status")
@log_call()
def engine_status():
    return {"workers": StaffEngine.get_shared().status()}
