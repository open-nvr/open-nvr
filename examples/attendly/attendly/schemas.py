from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class StudentCreate(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=256)
    course_code: str = Field(..., min_length=1, max_length=64)
    section: str = Field(..., min_length=1, max_length=8)
    email: EmailStr
    residence: Optional[str] = Field(None, description="hostler or day_scholar")
    hostel_block: Optional[str] = Field(None, max_length=64)


class StudentUpdate(BaseModel):
    full_name: Optional[str] = Field(None, min_length=1, max_length=256)
    class_name: Optional[str] = None
    course_code: Optional[str] = Field(None, min_length=1, max_length=64)
    section: Optional[str] = Field(None, min_length=1, max_length=8)
    email: Optional[EmailStr] = None
    residence: Optional[str] = None
    hostel_block: Optional[str] = Field(None, max_length=64)


class StudentOut(BaseModel):
    id: int
    student_code: str
    full_name: str
    class_name: Optional[str] = None
    course_code: Optional[str] = None
    section: Optional[str] = None
    email: Optional[str] = None
    residence: str = "day_scholar"
    hostel_block: Optional[str] = None
    created_at: str

    model_config = ConfigDict(from_attributes=True)


def student_out(item: dict) -> StudentOut:
    return StudentOut(
        id=int(item.get("id") or 0),
        student_code=str(item.get("student_code") or ""),
        full_name=str(item.get("full_name") or ""),
        class_name=item.get("class_name"),
        course_code=item.get("course_code"),
        section=item.get("section"),
        email=item.get("email"),
        residence=str(item.get("residence") or "day_scholar"),
        hostel_block=item.get("hostel_block"),
        created_at=str(item.get("created_at") or ""),
    )


class RegisterFacesResponse(BaseModel):
    student: StudentOut
    num_faces_registered: int
    angles: List[str]
    validation_errors: Optional[List[str]] = None


class RecognitionResult(BaseModel):
    student: Optional[StudentOut]
    confidence: float
    face_count: int


class FinalizedAttendanceOut(BaseModel):
    id: int
    class_id: str
    student: Optional[StudentOut]
    slots_present: int
    total_slots: int
    ratio: float
    status: str
    finalized_at: str

    model_config = ConfigDict(from_attributes=True)


class StreamCreate(BaseModel):
    name: str
    camera_id: int


class StreamOut(BaseModel):
    id: int
    name: str
    camera_id: Optional[int] = None
    camera_name: Optional[str] = None
    url: str = ""
    status: str


class CameraOut(BaseModel):
    id: int
    handle: str
    name: str
    role: str = ""


# ── Auth (proxied to OpenNVR) ──────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str
    code: Optional[str] = None


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    id: str
    username: str
    email: str
    mfa_enabled: bool = False
    is_admin: Optional[bool] = None
    role_name: Optional[str] = None
    tier: str = "viewer"
    created_at: str = ""


# ── Staff and departments ──────────────────────────────────────────
class StaffCreate(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=256)
    email: Optional[EmailStr] = None
    department_id: Optional[int] = None
    designation: Optional[str] = Field(None, max_length=128)


class StaffUpdate(BaseModel):
    full_name: Optional[str] = Field(None, min_length=1, max_length=256)
    email: Optional[EmailStr] = None
    department_id: Optional[int] = None
    designation: Optional[str] = Field(None, max_length=128)


class StaffOut(BaseModel):
    id: int
    staff_code: str
    full_name: str
    email: Optional[str] = None
    department_id: Optional[int] = None
    department_name: Optional[str] = None
    designation: Optional[str] = None
    created_at: str


class RegisterStaffFacesResponse(BaseModel):
    staff: StaffOut
    num_faces_registered: int
    angles: List[str]
    validation_errors: Optional[List[str]] = None


class DepartmentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    camera_id: int
    work_start: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")
    work_end: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")


class DepartmentUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    camera_id: Optional[int] = None
    work_start: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")
    work_end: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")


class DepartmentOut(BaseModel):
    id: int
    name: str
    camera_id: Optional[int] = None
    camera_name: Optional[str] = None
    work_start: str
    work_end: str


# ── Gates and meals ────────────────────────────────────────────────
class GateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    kind: str = Field(..., pattern=r"^(hostel|mess)$")
    camera_id: int
    hostel_block: Optional[str] = Field(None, max_length=64)
    allow_staff: bool = False
    enabled: bool = True
    open_url: Optional[str] = Field(None, max_length=512)
    meal_windows: Optional[str] = Field(None, max_length=256)


class GateUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    kind: Optional[str] = Field(None, pattern=r"^(hostel|mess)$")
    camera_id: Optional[int] = None
    hostel_block: Optional[str] = Field(None, max_length=64)
    allow_staff: Optional[bool] = None
    enabled: Optional[bool] = None
    open_url: Optional[str] = Field(None, max_length=512)
    meal_windows: Optional[str] = Field(None, max_length=256)


class GateOut(BaseModel):
    id: int
    name: str
    kind: str
    camera_id: Optional[int] = None
    camera_name: Optional[str] = None
    hostel_block: Optional[str] = None
    allow_staff: bool = False
    enabled: bool = True
    open_url: Optional[str] = None
    meal_windows: Optional[str] = None


class MealBookingCreate(BaseModel):
    student_code: Optional[str] = None
    email: Optional[EmailStr] = None
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    meals: List[str] = Field(..., min_length=1)
