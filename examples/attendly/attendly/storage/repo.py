"""The storage API, with the names and dict shapes the DynamoDB module had,
on SQLite. Routers and services were written against those names."""
from __future__ import annotations

import re
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from sqlalchemy import func, select

from ..config import get_settings
from ..utils.time import now_local_iso
from .db import session_scope
from .models import (
    AttendanceClassMeta,
    AttendanceFinal,
    AttendanceRecord,
    AttendanceSnapshot,
    Counter,
    Department,
    Gate,
    PlannerCourse,
    Staff,
    PlannerTemplate,
    Stream,
    Student,
    StudentEnrolment,
)

_CLASS_ID_RE = re.compile(r"^(?P<course>.+)-(?P<section>[^-]+)-\d{8}-\d{4}-\d{4}$")


def _now_iso() -> str:
    return now_local_iso()


def _ulid_like() -> str:
    return uuid4().hex


def _stable_id(text: str) -> int:
    return int(zlib.crc32(text.encode("utf-8")) & 0x7FFFFFFF)


def parse_class_id(class_id: str) -> tuple[Optional[str], Optional[str]]:
    """``COURSE-SECTION-YYYYMMDD-HHMM-HHMM`` (COURSE may contain dashes)."""
    m = _CLASS_ID_RE.match(class_id)
    if m:
        return m.group("course"), m.group("section")
    parts = class_id.split("-")
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None, None


# ── Counters ────────────────────────────────────────────────────────


def _next_counter(db, name: str) -> int:
    row = db.get(Counter, name)
    if row is None:
        row = Counter(name=name, value=0)
        db.add(row)
    row.value = int(row.value or 0) + 1
    db.flush()
    return int(row.value)


# ── Students ────────────────────────────────────────────────────────


def _student_dict(s: Student) -> Dict[str, Any]:
    return {
        "id": int(s.id),
        "student_code": s.student_code,
        "full_name": s.full_name,
        "class_name": s.class_name,
        "email": s.email,
        "course_code": s.course_code,
        "section": s.section,
        "residence": s.residence or "day_scholar",
        "hostel_block": s.hostel_block,
        "created_at": s.created_at,
        "deleted_at": s.deleted_at,
    }


def generate_student_code(prefix: Optional[str] = None, width: int = 5) -> str:
    prefix = prefix or get_settings().student_code_prefix
    with session_scope() as db:
        value = _next_counter(db, "student_code")
    return f"{prefix}{value:0{width}d}"


RESIDENCES = ("day_scholar", "hostler")


def normalise_residence(value: Optional[str]) -> str:
    v = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if v in ("hostler", "hosteller", "hostel", "boarder", "resident"):
        return "hostler"
    return "day_scholar"


def put_student(
    student_code: str, full_name: str, class_name: Optional[str], email: Optional[str] = None,
    *, course_code: Optional[str] = None, section: Optional[str] = None,
    residence: Optional[str] = None, hostel_block: Optional[str] = None,
) -> Dict[str, Any]:
    with session_scope() as db:
        row = db.execute(select(Student).where(Student.student_code == student_code)).scalar_one_or_none()
        if row is None:
            row = Student(student_code=student_code, full_name=full_name, created_at=_now_iso())
            db.add(row)
        row.full_name = full_name
        row.class_name = class_name or None
        row.email = email or None
        row.course_code = course_code or None
        row.section = section or None
        row.residence = normalise_residence(residence)
        row.hostel_block = (hostel_block or "").strip() or None if row.residence == "hostler" else None
        row.deleted_at = None
        db.flush()
        return _student_dict(row)


def get_student(student_code: str) -> Optional[Dict[str, Any]]:
    with session_scope() as db:
        row = db.execute(
            select(Student).where(Student.student_code == student_code, Student.deleted_at.is_(None))
        ).scalar_one_or_none()
        item = _student_dict(row) if row else None
    if item is not None:
        item["enrolments"] = list_student_enrolments(student_code)
    return item


def update_student(
    student_code: str, *, full_name: Optional[str] = None, class_name: Optional[str] = None,
    email: Optional[str] = None, course_code: Optional[str] = None, section: Optional[str] = None,
    residence: Optional[str] = None, hostel_block: Optional[str] = None,
) -> Dict[str, Any]:
    with session_scope() as db:
        row = db.execute(
            select(Student).where(Student.student_code == student_code, Student.deleted_at.is_(None))
        ).scalar_one_or_none()
        if row is None:
            raise KeyError("student not found")
        if full_name is not None:
            row.full_name = full_name
        if class_name is not None:
            row.class_name = class_name
        if email is not None:
            row.email = email
        if course_code is not None:
            row.course_code = course_code
        if section is not None:
            row.section = section
        if residence is not None:
            row.residence = normalise_residence(residence)
        if hostel_block is not None:
            row.hostel_block = hostel_block.strip() or None
        if row.residence != "hostler":
            row.hostel_block = None
        db.flush()
        return _student_dict(row)


def get_student_by_email(email: str) -> Optional[Dict[str, Any]]:
    if not email:
        return None
    with session_scope() as db:
        row = db.execute(
            select(Student).where(func.lower(Student.email) == email.strip().lower(), Student.deleted_at.is_(None))
        ).scalars().first()
        return _student_dict(row) if row else None


def move_student_to_deleted(student_code: str) -> Optional[Dict[str, Any]]:
    """Soft delete: the row stays (attendance history keeps its name) but
    leaves every listing and frees the email."""
    with session_scope() as db:
        row = db.execute(
            select(Student).where(Student.student_code == student_code, Student.deleted_at.is_(None))
        ).scalar_one_or_none()
        if row is None:
            return None
        row.deleted_at = _now_iso()
        db.flush()
        return _student_dict(row)


def get_students_batch(student_codes: List[str], include_deleted: bool = True) -> Dict[str, Dict[str, Any]]:
    codes = list(dict.fromkeys(c for c in student_codes if c))
    if not codes:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    with session_scope() as db:
        for i in range(0, len(codes), 500):
            chunk = codes[i:i + 500]
            stmt = select(Student).where(Student.student_code.in_(chunk))
            if not include_deleted:
                stmt = stmt.where(Student.deleted_at.is_(None))
            for row in db.execute(stmt).scalars():
                out[row.student_code] = _student_dict(row)
    return out


def list_students(residence: Optional[str] = None, hostel_block: Optional[str] = None) -> List[Dict[str, Any]]:
    with session_scope() as db:
        stmt = select(Student).where(Student.deleted_at.is_(None))
        if residence:
            stmt = stmt.where(Student.residence == normalise_residence(residence))
        if hostel_block:
            stmt = stmt.where(Student.hostel_block == hostel_block)
        rows = db.execute(stmt.order_by(Student.created_at.desc(), Student.id.desc())).scalars().all()
        items = [_student_dict(r) for r in rows]
    extra = enrolments_for([it["student_code"] for it in items])
    for it in items:
        it["enrolments"] = extra.get(it["student_code"], [])
    return items


def list_hostel_blocks() -> List[str]:
    with session_scope() as db:
        rows = db.execute(
            select(Student.hostel_block).where(Student.deleted_at.is_(None), Student.hostel_block.isnot(None))
        ).scalars().all()
    return sorted({str(b).strip() for b in rows if b and str(b).strip()})


# ── Enrolments: a student may attend more than one course/section ───


def list_student_enrolments(student_code: str) -> List[Dict[str, str]]:
    with session_scope() as db:
        rows = db.execute(
            select(StudentEnrolment).where(StudentEnrolment.student_code == student_code)
            .order_by(StudentEnrolment.course_code, StudentEnrolment.section)
        ).scalars().all()
        return [{"course_code": r.course_code, "section": r.section} for r in rows]


def enrolments_for(student_codes: List[str]) -> Dict[str, List[Dict[str, str]]]:
    out: Dict[str, List[Dict[str, str]]] = {}
    codes = [c for c in dict.fromkeys(student_codes) if c]
    if not codes:
        return out
    with session_scope() as db:
        for i in range(0, len(codes), 500):
            rows = db.execute(select(StudentEnrolment).where(StudentEnrolment.student_code.in_(codes[i:i + 500]))).scalars()
            for r in rows:
                out.setdefault(r.student_code, []).append({"course_code": r.course_code, "section": r.section})
    return out


def set_student_enrolments(student_code: str, pairs: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Replace the extra enrolments (the primary pair on the row is not
    repeated here)."""
    wanted = []
    seen = set()
    for pr in pairs or []:
        course = str(pr.get("course_code") or "").strip()
        section = str(pr.get("section") or "").strip()
        if not course or not section or (course, section) in seen:
            continue
        seen.add((course, section))
        wanted.append((course, section))
    with session_scope() as db:
        row = db.execute(select(Student).where(Student.student_code == student_code)).scalar_one_or_none()
        if row is None:
            raise KeyError("student not found")
        primary = (row.course_code, row.section)
        for r in db.execute(select(StudentEnrolment).where(StudentEnrolment.student_code == student_code)).scalars().all():
            db.delete(r)
        for course, section in wanted:
            if (course, section) == primary:
                continue
            db.add(StudentEnrolment(student_code=student_code, course_code=course, section=section))
    return list_student_enrolments(student_code)


def student_enrolled_in(student: Dict[str, Any], course_code: Optional[str], section: Optional[str]) -> bool:
    """Does the class's course/section cover this student (primary pair or
    an extra enrolment)? A missing course or section does not filter."""
    if not course_code:
        return True
    if str(student.get("course_code")) == str(course_code) and (not section or str(student.get("section")) == str(section)):
        return True
    for e in student.get("enrolments") or list_student_enrolments(str(student.get("student_code") or "")):
        if e["course_code"] == str(course_code) and (not section or e["section"] == str(section)):
            return True
    return False


def list_sections_by_course(course_code: str) -> List[str]:
    with session_scope() as db:
        rows = db.execute(
            select(Student.section).where(Student.course_code == course_code, Student.deleted_at.is_(None))
        ).scalars().all()
        extra = db.execute(
            select(StudentEnrolment.section).join(Student, Student.student_code == StudentEnrolment.student_code)
            .where(StudentEnrolment.course_code == course_code, Student.deleted_at.is_(None))
        ).scalars().all()
    return sorted({str(s).strip() for s in list(rows) + list(extra) if s and str(s).strip()})


def count_students() -> int:
    with session_scope() as db:
        return int(db.execute(select(func.count()).select_from(Student).where(Student.deleted_at.is_(None))).scalar() or 0)


def list_student_codes_by_class(class_name: str) -> List[str]:
    with session_scope() as db:
        rows = db.execute(
            select(Student.student_code).where(Student.class_name == class_name, Student.deleted_at.is_(None))
        ).scalars().all()
    return sorted(str(c) for c in rows)


def list_student_codes_by_course_section(course_code: str, section: str) -> List[str]:
    with session_scope() as db:
        rows = db.execute(
            select(Student.student_code).where(
                Student.course_code == course_code, Student.section == section, Student.deleted_at.is_(None)
            )
        ).scalars().all()
        extra = db.execute(
            select(StudentEnrolment.student_code).join(Student, Student.student_code == StudentEnrolment.student_code)
            .where(StudentEnrolment.course_code == course_code, StudentEnrolment.section == section,
                   Student.deleted_at.is_(None))
        ).scalars().all()
    return sorted({str(c) for c in list(rows) + list(extra)})


# ── Planner: courses ────────────────────────────────────────────────


def _course_dict(c: PlannerCourse) -> Dict[str, Any]:
    return {
        "code": c.code, "name": c.name, "students": int(c.students or 0), "sections": int(c.sections or 0),
        "sections_list": list(c.sections_list or []), "created_at": c.created_at, "updated_at": c.updated_at,
        "deleted_at": c.deleted_at,
    }


def planner_course_put(
    code: str, students: int, sections: int, *, name: Optional[str] = None,
    sections_list: Optional[List[str]] = None,
) -> Dict[str, Any]:
    with session_scope() as db:
        row = db.get(PlannerCourse, code)
        ts = _now_iso()
        if row is None:
            row = PlannerCourse(code=code, created_at=ts, updated_at=ts)
            db.add(row)
        row.students = int(students)
        row.sections = int(sections)
        row.name = name or None
        row.sections_list = list(dict.fromkeys(sections_list or []))
        row.updated_at = ts
        row.deleted_at = None
        db.flush()
        return _course_dict(row)


def planner_course_get(code: str) -> Optional[Dict[str, Any]]:
    with session_scope() as db:
        row = db.get(PlannerCourse, code)
        if row is None or row.deleted_at:
            return None
        return _course_dict(row)


def planner_course_list() -> List[Dict[str, Any]]:
    with session_scope() as db:
        rows = db.execute(select(PlannerCourse).where(PlannerCourse.deleted_at.is_(None)).order_by(PlannerCourse.code)).scalars().all()
        return [_course_dict(r) for r in rows]


def planner_course_update(
    code: str, students: Optional[int] = None, sections: Optional[int] = None, *,
    name: Optional[str] = None, sections_list: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    with session_scope() as db:
        row = db.get(PlannerCourse, code)
        if row is None or row.deleted_at:
            return None
        if students is not None:
            row.students = int(students)
        if sections is not None:
            row.sections = int(sections)
        if name is not None:
            row.name = name
        if sections_list is not None:
            row.sections_list = list(dict.fromkeys(sections_list))
        row.updated_at = _now_iso()
        db.flush()
        return _course_dict(row)


def planner_course_delete(code: str) -> bool:
    with session_scope() as db:
        row = db.get(PlannerCourse, code)
        if row is None:
            return False
        row.deleted_at = row.updated_at = _now_iso()
        return True


# ── Planner: weekly templates ───────────────────────────────────────


def _template_dict(t: PlannerTemplate) -> Dict[str, Any]:
    return {
        "id": t.id, "day_of_week": t.day_of_week, "course": t.course, "section": t.section,
        "course_section": f"{t.course}#{t.section}", "room_id": int(t.room_id), "start": t.start,
        "end": t.end, "active": bool(t.active), "created_at": t.created_at, "updated_at": t.updated_at,
        "deleted_at": t.deleted_at,
    }


def planner_template_put(
    day_of_week: str, course: str, section: str, room_id: int, start: str, end: str, active: bool = True,
) -> Dict[str, Any]:
    with session_scope() as db:
        ts = _now_iso()
        row = PlannerTemplate(
            id=_ulid_like(), day_of_week=day_of_week, course=course, section=section, room_id=int(room_id),
            start=start, end=end, active=bool(active), created_at=ts, updated_at=ts,
        )
        db.add(row)
        db.flush()
        return _template_dict(row)


def planner_template_get(id: str) -> Optional[Dict[str, Any]]:
    with session_scope() as db:
        row = db.get(PlannerTemplate, id)
        if row is None or row.deleted_at:
            return None
        return _template_dict(row)


def planner_template_list(day_of_week: Optional[str] = None) -> List[Dict[str, Any]]:
    with session_scope() as db:
        stmt = select(PlannerTemplate).where(PlannerTemplate.deleted_at.is_(None))
        if day_of_week:
            stmt = stmt.where(PlannerTemplate.day_of_week == day_of_week)
        rows = db.execute(stmt.order_by(PlannerTemplate.day_of_week, PlannerTemplate.start)).scalars().all()
        return [_template_dict(r) for r in rows]


def planner_template_update(id: str, **fields) -> Optional[Dict[str, Any]]:
    with session_scope() as db:
        row = db.get(PlannerTemplate, id)
        if row is None or row.deleted_at:
            return None
        for k in ("day_of_week", "course", "section", "start", "end"):
            if fields.get(k) is not None:
                setattr(row, k, str(fields[k]))
        if fields.get("room_id") is not None:
            row.room_id = int(fields["room_id"])
        if fields.get("active") is not None:
            row.active = bool(fields["active"])
        row.updated_at = _now_iso()
        db.flush()
        return _template_dict(row)


def planner_template_delete(id: str) -> bool:
    with session_scope() as db:
        row = db.get(PlannerTemplate, id)
        if row is None:
            return False
        row.deleted_at = row.updated_at = _now_iso()
        return True


# ── Streams (rooms) ─────────────────────────────────────────────────


def _stream_dict(s: Stream) -> Dict[str, Any]:
    return {
        "id": int(s.id), "name": s.name, "camera_id": s.camera_id, "camera_name": s.camera_name,
        "url": s.url or "", "status": s.status, "section_name": s.section_name,
        "schedule": [dict(e) for e in (s.schedule or [])], "courses": list(s.courses or []),
        "created_at": s.created_at, "updated_at": s.updated_at, "deleted_at": s.deleted_at,
    }


def _normalise_schedule(schedule: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for ent in schedule or []:
        ent2 = dict(ent)
        ent2.setdefault("is_finalized", False)
        out.append(ent2)
    return out


def streams_list(include_deleted: bool = True) -> List[Dict[str, Any]]:
    with session_scope() as db:
        stmt = select(Stream)
        if not include_deleted:
            stmt = stmt.where(Stream.deleted_at.is_(None))
        rows = db.execute(stmt.order_by(Stream.id)).scalars().all()
        return [_stream_dict(r) for r in rows]


def streams_get(stream_id: int) -> Optional[Dict[str, Any]]:
    with session_scope() as db:
        row = db.get(Stream, int(stream_id))
        return _stream_dict(row) if row else None


def streams_put(
    name: str, url: str = "", section_name: Optional[str] = None,
    schedule: Optional[List[Dict[str, Any]]] = None, *, camera_id: Optional[int] = None,
    camera_name: Optional[str] = None,
) -> Dict[str, Any]:
    with session_scope() as db:
        ts = _now_iso()
        row = Stream(
            name=name, url=url or (f"opennvr://cam{camera_id}" if camera_id is not None else ""),
            camera_id=camera_id, camera_name=camera_name, status="configured", section_name=section_name,
            schedule=_normalise_schedule(schedule), courses=[], created_at=ts, updated_at=ts,
        )
        db.add(row)
        db.flush()
        return _stream_dict(row)


def streams_update(
    stream_id: int, name: Optional[str] = None, url: Optional[str] = None, status: Optional[str] = None,
    section_name: Optional[str] = None, schedule: Optional[List[Dict[str, Any]]] = None,
    courses: Optional[List[str]] = None, *, camera_id: Optional[int] = None,
    camera_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    with session_scope() as db:
        row = db.get(Stream, int(stream_id))
        if row is None:
            return None
        if name is not None:
            row.name = name
        if url is not None:
            row.url = url
        if status is not None:
            row.status = status
        if section_name is not None:
            row.section_name = section_name
        if schedule is not None:
            row.schedule = _normalise_schedule(schedule)
        if courses is not None:
            row.courses = list(courses)
        if camera_id is not None:
            row.camera_id = int(camera_id)
            row.url = f"opennvr://cam{int(camera_id)}"
        if camera_name is not None:
            row.camera_name = camera_name
        row.updated_at = _now_iso()
        db.flush()
        return _stream_dict(row)


def streams_find_class(class_id: str) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """``(room, schedule entry)`` holding this class id, or Nones."""
    for it in streams_list(include_deleted=False):
        for ent in it.get("schedule") or []:
            if str(ent.get("class_id") or "") == class_id:
                return it, ent
    return None, None


def streams_set_class_called_off(class_id: str, called_off: bool) -> bool:
    with session_scope() as db:
        for row in db.execute(select(Stream).where(Stream.deleted_at.is_(None))).scalars().all():
            schedule = [dict(e) for e in (row.schedule or [])]
            hit = False
            for ent in schedule:
                if str(ent.get("class_id") or "") == class_id:
                    ent["called_off"] = bool(called_off)
                    hit = True
            if hit:
                row.schedule = schedule
                row.updated_at = _now_iso()
                return True
    return False


def list_scheduled_classes() -> List[Dict[str, Any]]:
    """Every published class window on every room, latest first."""
    out: List[Dict[str, Any]] = []
    for it in streams_list(include_deleted=False):
        for ent in it.get("schedule") or []:
            out.append({
                "class_id": str(ent.get("class_id") or ""), "course": ent.get("course"), "section": ent.get("section"),
                "start_iso": ent.get("start_iso"), "end_iso": ent.get("end_iso"), "is_finalized": bool(ent.get("is_finalized")),
                "called_off": bool(ent.get("called_off")), "stream_id": int(it["id"]), "room": it.get("name"),
            })
    out.sort(key=lambda e: str(e.get("start_iso") or ""), reverse=True)
    return out


def streams_mark_schedule_finalized(stream_id: int, class_id: str) -> bool:
    with session_scope() as db:
        row = db.get(Stream, int(stream_id))
        if row is None:
            return False
        schedule = [dict(e) for e in (row.schedule or [])]
        changed = False
        for ent in schedule:
            if str(ent.get("class_id") or "") == str(class_id) and not ent.get("is_finalized"):
                ent["is_finalized"] = True
                changed = True
        if not changed:
            return False
        row.schedule = schedule
        row.updated_at = _now_iso()
        return True


def streams_soft_delete(stream_id: int) -> bool:
    with session_scope() as db:
        row = db.get(Stream, int(stream_id))
        if row is None:
            return False
        row.deleted_at = row.updated_at = _now_iso()
        return True


def streams_delete(stream_id: int) -> bool:
    with session_scope() as db:
        row = db.get(Stream, int(stream_id))
        if row is None:
            return False
        db.delete(row)
        return True


# ── Ad-hoc identification log ───────────────────────────────────────


def put_attendance(student_code: str, camera_name: Optional[str], confidence: Optional[float]) -> Dict[str, Any]:
    with session_scope() as db:
        row = AttendanceRecord(
            student_code=student_code, camera_name=camera_name,
            confidence=float(confidence) if confidence is not None else None, recognized_at=_now_iso(),
        )
        db.add(row)
        db.flush()
        return {
            "id": int(row.id), "student_code": row.student_code, "camera_name": row.camera_name,
            "confidence": row.confidence, "recognized_at": row.recognized_at,
        }


# ── Snapshot attendance ─────────────────────────────────────────────


SLOT_MARKER = "__slot__"
UNWATCHED_MARKER = "__unwatched__"


def save_snapshot(class_id: str, student_codes, slot_time: datetime, *, watched: bool = True,
                  seen_at: Optional[Dict[str, str]] = None, evidence_path: Optional[str] = None) -> Dict[str, Any]:
    """One slot marker (so an empty slot counts) plus one present row per
    code. ``watched=False`` records a slot in which the camera could not
    be looked at (no frame, detection down): it counts against nobody.
    ``student_codes`` may be a ``{code: first_seen_iso}`` mapping."""
    slot_iso = slot_time.isoformat()
    if isinstance(student_codes, dict):
        seen_at = {**(seen_at or {}), **{k: v for k, v in student_codes.items() if v}}
        student_codes = list(student_codes.keys())
    with session_scope() as db:
        db.add(AttendanceSnapshot(
            class_id=class_id, student_code=SLOT_MARKER if watched else UNWATCHED_MARKER, slot_time=slot_iso,
            present=False, is_slot_marker=True, evidence_path=evidence_path if watched else None,
        ))
        if watched:
            for code in dict.fromkeys(student_codes):
                db.add(AttendanceSnapshot(class_id=class_id, student_code=code, slot_time=slot_iso, present=True,
                                          seen_at=(seen_at or {}).get(code) or slot_iso))
    return {"ok": True, "count": len(student_codes) if watched else 0, "class_id": class_id, "slot_time": slot_iso,
            "watched": watched}


def list_snapshot_classes() -> List[str]:
    with session_scope() as db:
        rows = db.execute(select(AttendanceSnapshot.class_id).distinct()).scalars().all()
    return sorted(str(c) for c in rows if c)


def snapshot_summary(class_id: str) -> Dict[str, Any]:
    with session_scope() as db:
        rows = db.execute(select(AttendanceSnapshot).where(AttendanceSnapshot.class_id == class_id)).scalars().all()
    unwatched = {r.slot_time for r in rows if r.student_code == UNWATCHED_MARKER}
    watched = {r.slot_time for r in rows if r.slot_time and r.student_code != UNWATCHED_MARKER}
    unwatched -= watched  # a slot watched by one attempt is watched
    counts: Dict[str, int] = {}
    first_seen: Dict[str, str] = {}
    evidence: List[Dict[str, Any]] = []
    for r in rows:
        if r.student_code == SLOT_MARKER and r.evidence_path:
            evidence.append({"slot_time": r.slot_time, "path": r.evidence_path})
        if r.student_code and r.student_code not in (SLOT_MARKER, UNWATCHED_MARKER) and r.present:
            counts[r.student_code] = counts.get(r.student_code, 0) + 1
            at = r.seen_at or r.slot_time
            if at and (r.student_code not in first_seen or at < first_seen[r.student_code]):
                first_seen[r.student_code] = at
    slot_times = sorted(watched)
    evidence.sort(key=lambda e: e["slot_time"])
    return {"total_slots": len(slot_times), "unwatched_slots": len(unwatched), "counts": counts, "slot_times": slot_times,
            "first_seen": first_seen, "evidence": evidence}


_CLASS_WINDOW_RE = re.compile(r"-(\d{8})-(\d{4})-(\d{4})$")
MARK_STATUSES = ("PRESENT", "LATE", "ABSENT", "EXCUSED", "UNWATCHED")


def class_window(class_id: str) -> tuple[Optional[datetime], Optional[datetime]]:
    """The class's start and end (aware, local) from its id, or Nones."""
    m = _CLASS_WINDOW_RE.search(class_id)
    if not m:
        return None, None
    from ..utils.time import local_tz

    try:
        day = datetime.strptime(m.group(1), "%Y%m%d")
        start = day.replace(hour=int(m.group(2)[:2]), minute=int(m.group(2)[2:]), tzinfo=local_tz())
        end = day.replace(hour=int(m.group(3)[:2]), minute=int(m.group(3)[2:]), tzinfo=local_tz())
        return start, end
    except ValueError:
        return None, None


def _aware(iso: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(iso)
    except Exception:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def ensure_finalization_marker(class_id: str, total_slots: Optional[int] = None) -> None:
    with session_scope() as db:
        if db.get(AttendanceClassMeta, class_id) is not None:
            return
        db.add(AttendanceClassMeta(class_id=class_id, finalized_at=_now_iso(), total_slots=int(total_slots or 0)))


def finalize_attendance(class_id: str) -> Dict[str, Any]:
    """Presence ratio per student from the snapshots → marks. Idempotent,
    and safe to run again:

    * PRESENT when seen in ≥ ``attendance_presence_threshold`` of the
      watched slots, LATE when the first sighting came after
      ``late_grace_minutes``, ABSENT otherwise; students of the class never
      seen get ABSENT rows;
    * a class watched in fewer than ``min_coverage_ratio`` of its slots (or
      in none) is flagged ``no_coverage`` and its automatic marks are
      UNWATCHED — nobody is absent for a camera that was down;
    * rows set by a person (``source='manual'``) keep their status and
      note; their counts are refreshed;
    * the roster is the one the class was first decided for: a student who
      joined the course afterwards is not added by a re-finalisation.
    A called-off class is left alone.
    """
    s = get_settings()
    with session_scope() as db:
        meta = db.get(AttendanceClassMeta, class_id)
        if meta is not None and meta.called_off:
            return {"ok": False, "class_id": class_id, "reason": "called_off", "written": 0, "total_slots": 0}
        existing_codes = set(db.execute(
            select(AttendanceFinal.student_code).where(AttendanceFinal.class_id == class_id)).scalars().all())
    summary = snapshot_summary(class_id)
    total_slots = int(summary["total_slots"])
    unwatched_slots = int(summary.get("unwatched_slots") or 0)
    counts: Dict[str, int] = summary["counts"]
    first_seen: Dict[str, str] = summary.get("first_seen") or {}
    threshold = float(s.attendance_presence_threshold)
    finalized_at = _now_iso()
    all_slots = total_slots + unwatched_slots
    no_coverage = total_slots == 0 or (all_slots > 0 and total_slots / all_slots < float(s.min_coverage_ratio))

    if existing_codes:
        roster = set(existing_codes)
    else:
        course_code, section = parse_class_id(class_id)
        roster = set(list_student_codes_by_course_section(course_code, section)) if course_code and section else set()
    missing = sorted(c for c in roster if c not in counts)

    start, end = class_window(class_id)
    late_after = start + timedelta(minutes=int(s.late_grace_minutes)) if start else None

    def auto_status(code: str, slots_present: int, ratio: float) -> str:
        if no_coverage:
            return "UNWATCHED"
        if ratio < threshold:
            return "ABSENT"
        seen = _aware(first_seen.get(code, "")) if first_seen.get(code) else None
        # Late means arrived during the class after the grace; a sighting
        # recorded after the class (a snapshot taken later by hand) is not.
        if late_after is not None and seen is not None and seen > late_after and (end is None or seen <= end):
            return "LATE"
        return "PRESENT"

    written = 0
    by_status: Dict[str, int] = {}
    with session_scope() as db:
        def upsert(code: str, slots_present: int, ratio: float) -> None:
            nonlocal written
            row = db.execute(select(AttendanceFinal).where(
                AttendanceFinal.class_id == class_id, AttendanceFinal.student_code == code)).scalar_one_or_none()
            if row is None:
                row = AttendanceFinal(class_id=class_id, student_code=code, status="ABSENT", finalized_at=finalized_at)
                db.add(row)
            row.slots_present = int(slots_present)
            row.total_slots = int(total_slots)
            row.ratio = float(ratio)
            row.first_seen = first_seen.get(code)
            row.finalized_at = finalized_at
            if (row.source or "auto") != "manual":
                row.status = auto_status(code, slots_present, ratio)
            by_status[row.status] = by_status.get(row.status, 0) + 1
            written += 1

        for code, slots_present in counts.items():
            ratio = (float(slots_present) / float(total_slots)) if total_slots > 0 else 0.0
            upsert(code, slots_present, ratio)
        for code in missing:
            upsert(code, 0, 0.0)

        meta = db.get(AttendanceClassMeta, class_id)
        if meta is None:
            db.add(AttendanceClassMeta(class_id=class_id, finalized_at=finalized_at, total_slots=total_slots,
                                       unwatched_slots=unwatched_slots, no_coverage=no_coverage))
        else:
            meta.finalized_at = finalized_at
            meta.total_slots = total_slots
            meta.unwatched_slots = unwatched_slots
            meta.no_coverage = no_coverage

    return {"ok": True, "class_id": class_id, "written": written, "total_slots": total_slots,
            "unwatched_slots": unwatched_slots, "no_coverage": no_coverage, "by_status": by_status}


def set_mark(class_id: str, student_code: str, status: str, note: Optional[str] = None) -> Dict[str, Any]:
    """A person's correction. ``AUTO`` hands the row back to the cameras
    (the class is re-finalised); any other status is kept through every
    re-finalisation."""
    status = str(status or "").upper()
    if status == "AUTO":
        with session_scope() as db:
            row = db.execute(select(AttendanceFinal).where(
                AttendanceFinal.class_id == class_id, AttendanceFinal.student_code == student_code)).scalar_one_or_none()
            if row is not None:
                row.source = "auto"
                row.note = (note or "").strip() or None
        finalize_attendance(class_id)
    else:
        if status not in MARK_STATUSES:
            raise ValueError(f"status must be one of {MARK_STATUSES} or AUTO")
        with session_scope() as db:
            row = db.execute(select(AttendanceFinal).where(
                AttendanceFinal.class_id == class_id, AttendanceFinal.student_code == student_code)).scalar_one_or_none()
            if row is None:
                row = AttendanceFinal(class_id=class_id, student_code=student_code, slots_present=0, total_slots=0,
                                      ratio=0.0, status=status, finalized_at=_now_iso())
                db.add(row)
            row.status = status
            row.source = "manual"
            row.note = (note or "").strip() or None
            row.finalized_at = _now_iso()
    with session_scope() as db:
        row = db.execute(select(AttendanceFinal).where(
            AttendanceFinal.class_id == class_id, AttendanceFinal.student_code == student_code)).scalar_one_or_none()
        if row is None:
            raise KeyError("no mark for this student in this class")
        return _final_dict(row)


def class_meta(class_id: str) -> Optional[Dict[str, Any]]:
    with session_scope() as db:
        m = db.get(AttendanceClassMeta, class_id)
        if m is None:
            return None
        return {"class_id": m.class_id, "finalized_at": m.finalized_at, "total_slots": int(m.total_slots or 0),
                "unwatched_slots": int(m.unwatched_slots or 0), "no_coverage": bool(m.no_coverage),
                "called_off": bool(m.called_off)}


def set_class_called_off(class_id: str, called_off: bool) -> Dict[str, Any]:
    """A holiday, a class that did not happen. The marks (if any) stay,
    hidden behind the flag, for a restore."""
    with session_scope() as db:
        m = db.get(AttendanceClassMeta, class_id)
        if m is None:
            m = AttendanceClassMeta(class_id=class_id, finalized_at=_now_iso(), total_slots=0)
            db.add(m)
        m.called_off = bool(called_off)
    return class_meta(class_id) or {}


def class_evidence(class_id: str) -> List[Dict[str, Any]]:
    return list(snapshot_summary(class_id).get("evidence") or [])


def _final_dict(r: AttendanceFinal) -> Dict[str, Any]:
    return {
        "id": int(r.id), "class_id": r.class_id, "student_code": r.student_code,
        "slots_present": int(r.slots_present), "total_slots": int(r.total_slots), "ratio": float(r.ratio),
        "status": r.status, "source": r.source or "auto", "note": r.note, "first_seen": r.first_seen,
        "finalized_at": r.finalized_at,
    }


def get_attendance_by_class(class_id: str) -> List[Dict[str, Any]]:
    """Finalised rows for a class; when none exist, ABSENT rows synthesised
    from the course-section roster embedded in the class id."""
    with session_scope() as db:
        rows = db.execute(select(AttendanceFinal).where(AttendanceFinal.class_id == class_id)).scalars().all()
        items = [_final_dict(r) for r in rows]
        meta = db.get(AttendanceClassMeta, class_id)
        meta_total = int(meta.total_slots) if meta else 0
        meta_at = meta.finalized_at if meta else now_local_iso()
    if not items:
        course_code, section = parse_class_id(class_id)
        roster = list_student_codes_by_course_section(course_code, section) if course_code and section else []
        items = [{
            "id": _stable_id(f"{class_id}:{code}"), "class_id": class_id, "student_code": code,
            "slots_present": 0, "total_slots": meta_total, "ratio": 0.0, "status": "ABSENT", "source": "auto",
            "note": None, "first_seen": None, "finalized_at": meta_at,
        } for code in roster]
    items.sort(key=lambda x: x.get("student_code", ""))
    return items


def list_finalized_rows() -> List[Dict[str, Any]]:
    with session_scope() as db:
        rows = db.execute(select(AttendanceFinal).order_by(AttendanceFinal.class_id, AttendanceFinal.student_code)).scalars().all()
        return [_final_dict(r) for r in rows]


def list_attendance_classes() -> List[str]:
    """Finalised class ids, latest first."""
    with session_scope() as db:
        rows = db.execute(select(AttendanceClassMeta).order_by(AttendanceClassMeta.finalized_at.desc())).scalars().all()
        return [r.class_id for r in rows]


def list_all_attendance_classes() -> List[str]:
    with session_scope() as db:
        a = set(db.execute(select(AttendanceFinal.class_id).distinct()).scalars().all())
        b = set(db.execute(select(AttendanceClassMeta.class_id)).scalars().all())
    return sorted(str(c) for c in (a | b) if c)


# ── Dashboard counts (the /state view) ──────────────────────────────


def counts() -> Dict[str, int]:
    with session_scope() as db:
        students = int(db.execute(select(func.count()).select_from(Student).where(Student.deleted_at.is_(None))).scalar() or 0)
        rooms = int(db.execute(select(func.count()).select_from(Stream).where(Stream.deleted_at.is_(None))).scalar() or 0)
        courses = int(db.execute(select(func.count()).select_from(PlannerCourse).where(PlannerCourse.deleted_at.is_(None))).scalar() or 0)
        classes = int(db.execute(select(func.count()).select_from(AttendanceClassMeta)).scalar() or 0)
        hostlers = int(db.execute(select(func.count()).select_from(Student).where(
            Student.deleted_at.is_(None), Student.residence == "hostler")).scalar() or 0)
        staff = int(db.execute(select(func.count()).select_from(Staff).where(Staff.deleted_at.is_(None))).scalar() or 0)
        departments = int(db.execute(select(func.count()).select_from(Department).where(Department.deleted_at.is_(None))).scalar() or 0)
        gates = int(db.execute(select(func.count()).select_from(Gate).where(Gate.deleted_at.is_(None))).scalar() or 0)
    return {"students": students, "rooms": rooms, "courses": courses, "finalized_classes": classes,
            "hostlers": hostlers, "staff": staff, "departments": departments, "gates": gates}
