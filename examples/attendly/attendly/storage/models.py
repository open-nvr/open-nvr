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
