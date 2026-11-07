"""Meal bookings: the day's list, one student's bookings, a CSV import, and
the summary the mess gate's decisions update."""
from __future__ import annotations

import csv
import io
import logging
from typing import List, Optional

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from .. import schemas
from ..storage import gates_repo as repo
from ..storage.repo import get_student, get_student_by_email, get_students_batch
from ..utils.logging_utils import log_call
from ..utils.time import now_local

logger = logging.getLogger(__name__)
router = APIRouter()


def _with_students(rows: List[dict]) -> List[dict]:
    students = get_students_batch([r["student_code"] for r in rows])
    out = []
    for r in rows:
        s = students.get(r["student_code"])
        out.append({**r, "student": schemas.student_out(s).model_dump() if s else None})
    return out


def _resolve_student(student_code: Optional[str], email: Optional[str]) -> dict:
    student = get_student(student_code) if student_code else None
    if student is None and email:
        student = get_student_by_email(email)
    if student is None:
        raise HTTPException(status_code=404, detail="student not found")
    return student


@router.get("/kinds")
@log_call()
def meal_kinds():
    return list(repo.MEALS)


@router.get("/bookings")
@log_call()
def list_bookings(date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"), meal: Optional[str] = None,
                  student_code: Optional[str] = None):
    day = date or now_local().strftime("%Y-%m-%d")
    if meal and meal not in repo.MEALS:
        raise HTTPException(status_code=400, detail=f"meal must be one of {list(repo.MEALS)}")
    return {"date": day, "bookings": _with_students(repo.meal_bookings_list(day, meal, student_code))}


@router.post("/bookings")
@log_call()
def create_booking(payload: schemas.MealBookingCreate):
    """One student, one date, one or more meals. Idempotent — the existing
    booking system can push the same booking again without harm."""
    student = _resolve_student(payload.student_code, str(payload.email) if payload.email else None)
    bad = [m for m in payload.meals if m not in repo.MEALS]
    if bad:
        raise HTTPException(status_code=400, detail=f"unknown meals: {bad}")
    created, rows = 0, []
    for meal in dict.fromkeys(payload.meals):
        row, was_new = repo.meal_booking_put(student["student_code"], payload.date, meal, source="api")
        created += int(was_new)
        rows.append(row)
    return {"ok": True, "created": created, "bookings": _with_students(rows)}


@router.delete("/bookings/{booking_id}")
@log_call()
def delete_booking(booking_id: int):
    if not repo.meal_booking_delete(booking_id):
        raise HTTPException(status_code=404, detail="booking not found")
    return {"ok": True}


@router.post("/bookings/import")
@log_call()
def import_bookings(file: UploadFile = File(...), date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$")):
    """CSV with a header. Columns: ``student_code`` or ``email``; ``meal``
    (or ``meals`` as ``breakfast|lunch``); optional ``date`` (else the
    query's, else tomorrow — bookings are made the previous day)."""
    raw = file.file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="CSV must be UTF-8")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV has no header row")
    fields = {f.strip().lower(): f for f in reader.fieldnames if f}
    if "student_code" not in fields and "email" not in fields:
        raise HTTPException(status_code=400, detail="CSV needs a student_code or email column")
    from datetime import timedelta

    default_date = date or (now_local() + timedelta(days=1)).strftime("%Y-%m-%d")
    created = skipped = 0
    errors: List[str] = []
    for n, row in enumerate(reader, start=2):
        def col(name: str) -> str:
            key = fields.get(name)
            return str(row.get(key) or "").strip() if key else ""

        code, email = col("student_code"), col("email")
        day = col("date") or default_date
        meals_raw = col("meals") or col("meal")
        meals = [m.strip().lower() for m in meals_raw.replace(";", "|").replace(",", "|").split("|") if m.strip()]
        try:
            student = _resolve_student(code or None, email or None)
        except HTTPException:
            errors.append(f"line {n}: student not found ({code or email})")
            continue
        if not meals:
            errors.append(f"line {n}: no meal")
            continue
        for meal in meals:
            if meal not in repo.MEALS:
                errors.append(f"line {n}: unknown meal {meal}")
                continue
            _, was_new = repo.meal_booking_put(student["student_code"], day, meal, source="import")
            created += int(was_new)
            skipped += int(not was_new)
    return {"ok": True, "created": created, "already_booked": skipped, "errors": errors}


@router.get("/summary")
@log_call()
def summary(date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$")):
    day = date or now_local().strftime("%Y-%m-%d")
    return {"date": day, "meals": repo.meal_summary(day)}
