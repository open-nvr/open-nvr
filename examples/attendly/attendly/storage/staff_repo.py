"""Staff, departments and the daily staff attendance rows."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import func, select

from ..config import get_settings
from ..utils.time import now_local_iso
from .db import session_scope
from .models import Department, Staff, StaffAttendance
from .repo import _next_counter


def _now_iso() -> str:
    return now_local_iso()


# ── Staff ───────────────────────────────────────────────────────────


def _staff_dict(s: Staff) -> Dict[str, Any]:
    return {
        "id": int(s.id), "staff_code": s.staff_code, "full_name": s.full_name, "email": s.email,
        "department_id": s.department_id, "designation": s.designation, "created_at": s.created_at,
        "deleted_at": s.deleted_at,
    }


def generate_staff_code(prefix: Optional[str] = None, width: int = 5) -> str:
    prefix = prefix or get_settings().staff_code_prefix
    with session_scope() as db:
        value = _next_counter(db, "staff_code")
    return f"{prefix}{value:0{width}d}"


def put_staff(
    staff_code: str, full_name: str, *, email: Optional[str] = None, department_id: Optional[int] = None,
    designation: Optional[str] = None,
) -> Dict[str, Any]:
    with session_scope() as db:
        row = db.execute(select(Staff).where(Staff.staff_code == staff_code)).scalar_one_or_none()
        if row is None:
            row = Staff(staff_code=staff_code, full_name=full_name, created_at=_now_iso())
            db.add(row)
        row.full_name = full_name
        row.email = (email or "").strip() or None
        row.department_id = int(department_id) if department_id is not None else None
        row.designation = (designation or "").strip() or None
        row.deleted_at = None
        db.flush()
        return _staff_dict(row)


def get_staff(staff_code: str) -> Optional[Dict[str, Any]]:
    with session_scope() as db:
        row = db.execute(
            select(Staff).where(Staff.staff_code == staff_code, Staff.deleted_at.is_(None))
        ).scalar_one_or_none()
        return _staff_dict(row) if row else None


def get_staff_by_email(email: str) -> Optional[Dict[str, Any]]:
    if not email:
        return None
    with session_scope() as db:
        row = db.execute(
            select(Staff).where(func.lower(Staff.email) == email.strip().lower(), Staff.deleted_at.is_(None))
        ).scalars().first()
        return _staff_dict(row) if row else None


def update_staff(staff_code: str, **fields) -> Dict[str, Any]:
    with session_scope() as db:
        row = db.execute(
            select(Staff).where(Staff.staff_code == staff_code, Staff.deleted_at.is_(None))
        ).scalar_one_or_none()
        if row is None:
            raise KeyError("staff not found")
        if fields.get("full_name") is not None:
            row.full_name = str(fields["full_name"])
        if "email" in fields and fields["email"] is not None:
            row.email = str(fields["email"]).strip() or None
        if "department_id" in fields:
            dep = fields["department_id"]
            row.department_id = int(dep) if dep not in (None, "", 0) else None
        if fields.get("designation") is not None:
            row.designation = str(fields["designation"]).strip() or None
        db.flush()
        return _staff_dict(row)


def list_staff(department_id: Optional[int] = None) -> List[Dict[str, Any]]:
    with session_scope() as db:
        stmt = select(Staff).where(Staff.deleted_at.is_(None))
        if department_id is not None:
            stmt = stmt.where(Staff.department_id == int(department_id))
        rows = db.execute(stmt.order_by(Staff.full_name, Staff.id)).scalars().all()
        return [_staff_dict(r) for r in rows]


def get_staff_batch(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    wanted = list(dict.fromkeys(c for c in codes if c))
    if not wanted:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    with session_scope() as db:
        for row in db.execute(select(Staff).where(Staff.staff_code.in_(wanted))).scalars():
            out[row.staff_code] = _staff_dict(row)
    return out


def soft_delete_staff(staff_code: str) -> Optional[Dict[str, Any]]:
    with session_scope() as db:
        row = db.execute(
            select(Staff).where(Staff.staff_code == staff_code, Staff.deleted_at.is_(None))
        ).scalar_one_or_none()
        if row is None:
            return None
        row.deleted_at = _now_iso()
        db.flush()
        return _staff_dict(row)


def count_staff() -> int:
    with session_scope() as db:
        return int(db.execute(select(func.count()).select_from(Staff).where(Staff.deleted_at.is_(None))).scalar() or 0)


# ── Departments ─────────────────────────────────────────────────────


def _department_dict(d: Department) -> Dict[str, Any]:
    return {
        "id": int(d.id), "name": d.name, "camera_id": d.camera_id, "camera_name": d.camera_name,
        "work_start": d.work_start, "work_end": d.work_end, "created_at": d.created_at,
        "updated_at": d.updated_at, "deleted_at": d.deleted_at,
    }


def department_put(
    name: str, *, camera_id: Optional[int], camera_name: Optional[str] = None,
    work_start: Optional[str] = None, work_end: Optional[str] = None,
) -> Dict[str, Any]:
    s = get_settings()
    with session_scope() as db:
        ts = _now_iso()
        row = Department(
            name=name, camera_id=int(camera_id) if camera_id is not None else None, camera_name=camera_name,
            work_start=work_start or s.staff_work_start, work_end=work_end or s.staff_work_end,
            created_at=ts, updated_at=ts,
        )
        db.add(row)
        db.flush()
        return _department_dict(row)


def department_get(department_id: int) -> Optional[Dict[str, Any]]:
    with session_scope() as db:
        row = db.get(Department, int(department_id))
        if row is None or row.deleted_at:
            return None
        return _department_dict(row)


def department_list() -> List[Dict[str, Any]]:
    with session_scope() as db:
        rows = db.execute(
            select(Department).where(Department.deleted_at.is_(None)).order_by(Department.name, Department.id)
        ).scalars().all()
        return [_department_dict(r) for r in rows]


def department_update(department_id: int, **fields) -> Optional[Dict[str, Any]]:
    with session_scope() as db:
        row = db.get(Department, int(department_id))
        if row is None or row.deleted_at:
            return None
        if fields.get("name") is not None:
            row.name = str(fields["name"])
        if fields.get("camera_id") is not None:
            row.camera_id = int(fields["camera_id"])
        if "camera_name" in fields and fields["camera_name"] is not None:
            row.camera_name = fields["camera_name"]
        if fields.get("work_start") is not None:
            row.work_start = str(fields["work_start"])
        if fields.get("work_end") is not None:
            row.work_end = str(fields["work_end"])
        row.updated_at = _now_iso()
        db.flush()
        return _department_dict(row)


def department_delete(department_id: int) -> bool:
    with session_scope() as db:
        row = db.get(Department, int(department_id))
        if row is None or row.deleted_at:
            return False
        row.deleted_at = row.updated_at = _now_iso()
        return True


# ── Staff attendance ────────────────────────────────────────────────


def _attendance_dict(r: StaffAttendance) -> Dict[str, Any]:
    return {
        "id": int(r.id), "staff_code": r.staff_code, "date": r.date, "department_id": r.department_id,
        "first_seen": r.first_seen, "last_seen": r.last_seen, "sightings": int(r.sightings or 0),
        "status": "PRESENT",
    }


def staff_mark_seen(staff_code: str, *, date: str, ts_iso: str, department_id: Optional[int]) -> Dict[str, Any]:
    """First sighting of the day creates the row; later ones move
    ``last_seen`` and count."""
    with session_scope() as db:
        row = db.execute(
            select(StaffAttendance).where(StaffAttendance.date == date, StaffAttendance.staff_code == staff_code)
        ).scalar_one_or_none()
        if row is None:
            row = StaffAttendance(staff_code=staff_code, date=date, department_id=department_id,
                                  first_seen=ts_iso, last_seen=ts_iso, sightings=1)
            db.add(row)
        else:
            row.last_seen = ts_iso
            row.sightings = int(row.sightings or 0) + 1
            if row.department_id is None and department_id is not None:
                row.department_id = department_id
        db.flush()
        return _attendance_dict(row)


def staff_attendance_for_day(date: str, department_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Every staff member (of the department, when given) with the day's
    row, or an ABSENT row when nobody saw them."""
    people = list_staff(department_id)
    with session_scope() as db:
        rows = db.execute(select(StaffAttendance).where(StaffAttendance.date == date)).scalars().all()
        seen = {r.staff_code: _attendance_dict(r) for r in rows}
    out: List[Dict[str, Any]] = []
    for p in people:
        code = p["staff_code"]
        if code in seen:
            out.append({**seen[code], "staff": p})
        else:
            out.append({"id": None, "staff_code": code, "date": date, "department_id": p.get("department_id"),
                        "first_seen": None, "last_seen": None, "sightings": 0, "status": "ABSENT", "staff": p})
    out.sort(key=lambda r: (r["status"] != "PRESENT", (r["staff"].get("full_name") or "").lower()))
    return out


def staff_attendance_dates(limit: int = 90) -> List[str]:
    with session_scope() as db:
        rows = db.execute(
            select(StaffAttendance.date).distinct().order_by(StaffAttendance.date.desc()).limit(limit)
        ).scalars().all()
    return [str(d) for d in rows]
