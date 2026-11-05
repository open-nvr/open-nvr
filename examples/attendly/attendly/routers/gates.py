"""Flap barriers: gates, their live status, the decision log, and a direct
check for a barrier controller that brings its own picture."""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from .. import schemas
from ..cameras import CameraSource, PlatformUnavailable
from ..config import get_settings
from ..face.engine import FaceEngine
from ..services.access import GateEngine, decide_and_record, get_publisher, parse_meal_windows
from ..storage import gates_repo as repo
from ..storage.repo import get_student, get_students_batch, list_hostel_blocks
from ..utils.logging_utils import log_call
from ..utils.time import now_local

logger = logging.getLogger(__name__)
router = APIRouter()


def _camera_name(camera_id: int) -> Optional[str]:
    try:
        return CameraSource.get_shared().camera_name(camera_id)
    except (PlatformUnavailable, Exception):
        return None


def _out(g: dict) -> schemas.GateOut:
    return schemas.GateOut(**{k: g.get(k) for k in schemas.GateOut.model_fields})


def _validate(kind: str, hostel_block: Optional[str], meal_windows: Optional[str]) -> None:
    if kind == "mess" and meal_windows and not parse_meal_windows(meal_windows):
        raise HTTPException(status_code=400, detail="meal_windows must look like breakfast=07:00-09:30,lunch=12:00-14:30")


@router.get("/", response_model=List[schemas.GateOut])
@log_call()
def list_gates():
    return [_out(g) for g in repo.gate_list()]


@router.post("/", response_model=schemas.GateOut)
@log_call()
def create_gate(payload: schemas.GateCreate):
    _validate(payload.kind, payload.hostel_block, payload.meal_windows)
    g = repo.gate_put(
        payload.name, payload.kind, camera_id=payload.camera_id, camera_name=_camera_name(payload.camera_id),
        hostel_block=payload.hostel_block, allow_staff=payload.allow_staff, enabled=payload.enabled,
        open_url=payload.open_url, meal_windows=payload.meal_windows, mode=payload.mode,
        direction=payload.direction, alert_on=payload.alert_on,
    )
    _refresh_engine()
    return _out(g)


@router.put("/{gate_id}", response_model=schemas.GateOut)
@log_call()
def update_gate(gate_id: int, payload: schemas.GateUpdate):
    fields = payload.model_dump(exclude_unset=True)
    if fields.get("camera_id") is not None:
        fields["camera_name"] = _camera_name(int(fields["camera_id"])) or ""
    current = repo.gate_get(gate_id)
    if current is None:
        raise HTTPException(status_code=404, detail="gate not found")
    _validate(fields.get("kind") or current["kind"], fields.get("hostel_block"), fields.get("meal_windows"))
    g = repo.gate_update(gate_id, **fields)
    _refresh_engine()
    return _out(g)


@router.delete("/{gate_id}")
@log_call()
def delete_gate(gate_id: int):
    if not repo.gate_delete(gate_id):
        raise HTTPException(status_code=404, detail="gate not found")
    _refresh_engine()
    return {"ok": True}


def _refresh_engine() -> None:
    try:
        GateEngine.get_shared().reload()
    except Exception as exc:  # noqa: BLE001 — the loop retries
        logger.warning("gate engine reload failed: %s", exc)


@router.get("/status")
@log_call()
def gates_status():
    s = get_settings()
    pub = get_publisher()
    today = now_local().strftime("%Y-%m-%d")
    return {
        "workers": GateEngine.get_shared().status(), "today": repo.access_counts(today),
        "bus": {"configured": bool(pub.nats_url), "published": pub.published, "alerts": pub.alerts,
                "last_error": pub.last_error},
        "presence": repo.presence_summary(),
        "meal_windows": parse_meal_windows(s.meal_windows), "hostel_blocks": list_hostel_blocks(),
        "threshold": s.effective_gate_threshold, "cooldown_seconds": s.gate_cooldown_seconds,
    }


@router.get("/events")
@log_call()
def list_events(gate_id: Optional[int] = None, limit: int = Query(100, ge=1, le=1000),
                date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"), decision: Optional[str] = None):
    return repo.access_events_list(gate_id, limit=limit, date=date, decision=decision)


@router.get("/presence")
@log_call()
def presence(hostel_block: Optional[str] = None, status: Optional[str] = None):
    """Who is inside the hostel right now (and who went out), from the
    hostel gates' sightings."""
    if status and status not in ("in", "out"):
        raise HTTPException(status_code=400, detail="status must be in or out")
    rows = repo.presence_list(hostel_block, status)
    students = get_students_batch([r["student_code"] for r in rows])
    out = []
    for r in rows:
        s = students.get(r["student_code"])
        out.append({**r, "student": schemas.student_out(s).model_dump() if s else None})
    return {"summary": repo.presence_summary(), "rows": out}


@router.put("/presence/{student_code}")
@log_call()
def set_presence(student_code: str, payload: schemas.PresenceSet):
    """An operator's correction (a missed sighting, a student who left
    by another way)."""
    student = get_student(student_code)
    if student is None:
        raise HTTPException(status_code=404, detail="student not found")
    return repo.presence_set(student_code, payload.status, hostel_block=student.get("hostel_block"))


@router.post("/{gate_id}/check")
@log_call()
def check_at_gate(gate_id: int, image: UploadFile = File(...), actuate: bool = True):
    """A barrier controller with its own camera asks for a decision: the
    largest face in the picture is recognised and judged by this gate's
    policy. The decision is logged and (unless ``actuate=false``)
    published exactly as a camera sighting would be."""
    gate = repo.gate_get(gate_id)
    if gate is None:
        raise HTTPException(status_code=404, detail="gate not found")
    data = image.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    import cv2
    import numpy as np

    frame = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(status_code=400, detail="Not an image")
    from ..services.recognize import recognize_faces_in_frame

    faces = recognize_faces_in_frame(FaceEngine.get_shared(), frame, threshold=get_settings().effective_gate_threshold,
                                     tag=f"check{gate_id}", largest_only=True)
    if not faces:
        return {"gate_id": gate_id, "decision": "deny", "reason": "no_face", "person_code": None, "person_name": None}
    face = faces[0]
    return decide_and_record(gate, face.get("code"), face.get("similarity"), source="check", actuate=actuate)
