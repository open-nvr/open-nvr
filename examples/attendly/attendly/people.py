"""Who is behind a person id the face model returned? Students and staff
share the adapter's one face database (codes ``AT…`` and ``ST…``), so a
camera at a gate or in a department can meet either."""
from __future__ import annotations

from typing import Any, Dict, Optional

from .storage.repo import get_student
from .storage.staff_repo import get_staff


def find_person(code: Optional[str]) -> Optional[Dict[str, Any]]:
    """``{"kind": "student"|"staff", "code", "name", "record"}`` or None."""
    if not code:
        return None
    student = get_student(code)
    if student:
        return {"kind": "student", "code": code, "name": student.get("full_name") or "", "record": student}
    staff = get_staff(code)
    if staff:
        return {"kind": "staff", "code": code, "name": staff.get("full_name") or "", "record": staff}
    return None
