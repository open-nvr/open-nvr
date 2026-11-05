from __future__ import annotations

from sqlalchemy import JSON, Boolean, Column, Float, Index, Integer, String

from .db import Base


class Student(Base):
    __tablename__ = "students"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_code = Column(String(32), unique=True, nullable=False, index=True)
    full_name = Column(String(256), nullable=False)
    class_name = Column(String(128), nullable=True)
    email = Column(String(256), nullable=True, index=True)
    course_code = Column(String(64), nullable=True, index=True)
    section = Column(String(16), nullable=True)
    # "hostler" or "day_scholar"; the block a hostler lives in.
    residence = Column(String(16), nullable=False, default="day_scholar", server_default="day_scholar")
    hostel_block = Column(String(64), nullable=True, index=True)
    created_at = Column(String(40), nullable=False)
    deleted_at = Column(String(40), nullable=True, index=True)


class Counter(Base):
    __tablename__ = "counters"

    name = Column(String(64), primary_key=True)
    value = Column(Integer, nullable=False, default=0)


class PlannerCourse(Base):
    __tablename__ = "planner_courses"

    code = Column(String(64), primary_key=True)
    name = Column(String(256), nullable=True)
    students = Column(Integer, nullable=False, default=0)
    sections = Column(Integer, nullable=False, default=0)
    sections_list = Column(JSON, nullable=False, default=list)
    created_at = Column(String(40), nullable=False)
    updated_at = Column(String(40), nullable=False)
    deleted_at = Column(String(40), nullable=True)


class PlannerTemplate(Base):
    __tablename__ = "planner_templates"

    id = Column(String(32), primary_key=True)
    day_of_week = Column(String(16), nullable=False, index=True)
    course = Column(String(64), nullable=False)
    section = Column(String(16), nullable=False)
    room_id = Column(Integer, nullable=False)
    start = Column(String(5), nullable=False)
    end = Column(String(5), nullable=False)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(String(40), nullable=False)
    updated_at = Column(String(40), nullable=False)
    deleted_at = Column(String(40), nullable=True)


class Stream(Base):
    """A room: an OpenNVR camera plus its class schedule."""

    __tablename__ = "streams"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False)
    camera_id = Column(Integer, nullable=True, index=True)
    camera_name = Column(String(128), nullable=True)
    url = Column(String(512), nullable=False, default="")
    status = Column(String(32), nullable=False, default="configured")
    section_name = Column(String(64), nullable=True)
    schedule = Column(JSON, nullable=False, default=list)
    courses = Column(JSON, nullable=False, default=list)
    created_at = Column(String(40), nullable=False)
    updated_at = Column(String(40), nullable=False)
    deleted_at = Column(String(40), nullable=True)


class AttendanceSnapshot(Base):
    __tablename__ = "attendance_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    class_id = Column(String(160), nullable=False, index=True)
    student_code = Column(String(32), nullable=False)
    slot_time = Column(String(40), nullable=False)
    present = Column(Boolean, nullable=False, default=False)
    is_slot_marker = Column(Boolean, nullable=False, default=False)


class AttendanceFinal(Base):
    __tablename__ = "attendance_final"

    id = Column(Integer, primary_key=True)  # crc32(class_id:student_code), stable
    class_id = Column(String(160), nullable=False, index=True)
    student_code = Column(String(32), nullable=False)
    slots_present = Column(Integer, nullable=False, default=0)
    total_slots = Column(Integer, nullable=False, default=0)
    ratio = Column(Float, nullable=False, default=0.0)
    status = Column(String(16), nullable=False)
    finalized_at = Column(String(40), nullable=False)

    __table_args__ = (Index("ix_attendance_final_class_student", "class_id", "student_code", unique=True),)


class AttendanceClassMeta(Base):
    """The finalisation marker: one row per finalised class."""

    __tablename__ = "attendance_class_meta"

    class_id = Column(String(160), primary_key=True)
    finalized_at = Column(String(40), nullable=False)
    total_slots = Column(Integer, nullable=False, default=0)


class AttendanceRecord(Base):
    """Ad-hoc identifications from /recognition/identify."""

    __tablename__ = "attendance_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_code = Column(String(32), nullable=False, index=True)
    camera_name = Column(String(128), nullable=True)
    confidence = Column(Float, nullable=True)
    recognized_at = Column(String(40), nullable=False)


# ── Phase 2: staff, departments, gates, meals ──────────────────────


class Staff(Base):
    __tablename__ = "staff"

    id = Column(Integer, primary_key=True, autoincrement=True)
    staff_code = Column(String(32), unique=True, nullable=False, index=True)
    full_name = Column(String(256), nullable=False)
    email = Column(String(256), nullable=True, index=True)
    department_id = Column(Integer, nullable=True, index=True)
    designation = Column(String(128), nullable=True)
    created_at = Column(String(40), nullable=False)
    deleted_at = Column(String(40), nullable=True, index=True)


class Department(Base):
    """A staff attendance camera with its working window."""

    __tablename__ = "departments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False)
    camera_id = Column(Integer, nullable=True, index=True)
    camera_name = Column(String(128), nullable=True)
    work_start = Column(String(5), nullable=False, default="09:00")
    work_end = Column(String(5), nullable=False, default="18:00")
    created_at = Column(String(40), nullable=False)
    updated_at = Column(String(40), nullable=False)
    deleted_at = Column(String(40), nullable=True)


class StaffAttendance(Base):
    """One row per staff member per day, written by the department worker."""

    __tablename__ = "staff_attendance"

    id = Column(Integer, primary_key=True, autoincrement=True)
    staff_code = Column(String(32), nullable=False)
    date = Column(String(10), nullable=False, index=True)
    department_id = Column(Integer, nullable=True)
    first_seen = Column(String(40), nullable=False)
    last_seen = Column(String(40), nullable=False)
    sightings = Column(Integer, nullable=False, default=1)

    __table_args__ = (Index("ix_staff_attendance_day_staff", "date", "staff_code", unique=True),)


class Gate(Base):
    """A flap barrier watched by one camera: a hostel block entrance or the
    mess entrance. Attendly decides; the gate-controller app actuates."""

    __tablename__ = "gates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False)
    kind = Column(String(16), nullable=False)  # hostel | mess
    camera_id = Column(Integer, nullable=True, index=True)
    camera_name = Column(String(128), nullable=True)
    hostel_block = Column(String(64), nullable=True)
    allow_staff = Column(Boolean, nullable=False, default=False)
    enabled = Column(Boolean, nullable=False, default=True)
    # Optional: an HTTP trigger a barrier with its own relay accepts.
    open_url = Column(String(512), nullable=True)
    # Optional per-gate meal windows ("meal=HH:MM-HH:MM,..."); blank → settings.
    meal_windows = Column(String(256), nullable=True)
    created_at = Column(String(40), nullable=False)
    updated_at = Column(String(40), nullable=False)
    deleted_at = Column(String(40), nullable=True)


class AccessEvent(Base):
    __tablename__ = "access_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    gate_id = Column(Integer, nullable=False, index=True)
    gate_name = Column(String(128), nullable=True)
    kind = Column(String(16), nullable=False)
    person_code = Column(String(32), nullable=True, index=True)
    person_name = Column(String(256), nullable=True)
    person_kind = Column(String(16), nullable=True)  # student | staff
    decision = Column(String(8), nullable=False)
    reason = Column(String(32), nullable=False)
    unit = Column(String(64), nullable=True)
    similarity = Column(Float, nullable=True)
    actuated = Column(Boolean, nullable=False, default=False)
    source = Column(String(16), nullable=False, default="camera")  # camera | check
    ts = Column(String(40), nullable=False, index=True)


class MealBooking(Base):
    __tablename__ = "meal_bookings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_code = Column(String(32), nullable=False, index=True)
    date = Column(String(10), nullable=False, index=True)
    meal = Column(String(16), nullable=False)
    source = Column(String(16), nullable=False, default="manual")  # manual | import | api
    created_at = Column(String(40), nullable=False)
    consumed_at = Column(String(40), nullable=True)
    consumed_gate_id = Column(Integer, nullable=True)

    __table_args__ = (Index("ix_meal_bookings_unique", "student_code", "date", "meal", unique=True),)
