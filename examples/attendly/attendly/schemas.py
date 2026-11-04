from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class StudentCreate(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=256)
    course_code: str = Field(..., min_length=1, max_length=64)
    section: str = Field(..., min_length=1, max_length=8)
    email: EmailStr


class StudentUpdate(BaseModel):
    full_name: Optional[str] = Field(None, min_length=1, max_length=256)
    class_name: Optional[str] = None
    course_code: Optional[str] = Field(None, min_length=1, max_length=64)
    section: Optional[str] = Field(None, min_length=1, max_length=8)
    email: Optional[EmailStr] = None


class StudentOut(BaseModel):
    id: int
    student_code: str
    full_name: str
    class_name: Optional[str] = None
    course_code: Optional[str] = None
    section: Optional[str] = None
    email: Optional[str] = None
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
