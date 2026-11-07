"""Staff: people enrolled under ``category=attendly_staff`` with ``ST`` codes,
and the departments (camera + working window) whose cameras mark them."""
from __future__ import annotations

import logging
from typing import List, Optional, Union

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from .. import schemas
from ..cameras import CameraSource, PlatformUnavailable
from ..config import get_settings
from ..face.engine import FaceEngine, FaceServiceError
from ..storage import staff_repo as repo
from ..utils.logging_utils import log_call
from .students import _enrol_images, check_for_duplicate_face_sync, enhance_image_for_recognition

logger = logging.getLogger(__name__)
router = APIRouter()
departments_router = APIRouter()


def _dept_names() -> dict:
    return {d["id"]: d["name"] for d in repo.department_list()}


def _out(item: dict, names: Optional[dict] = None) -> schemas.StaffOut:
    names = names if names is not None else _dept_names()
    return schemas.StaffOut(
        id=int(item.get("id") or 0), staff_code=str(item.get("staff_code") or ""),
        full_name=str(item.get("full_name") or ""), email=item.get("email"),
        department_id=item.get("department_id"), department_name=names.get(item.get("department_id")),
        designation=item.get("designation"), created_at=str(item.get("created_at") or ""),
    )


def _check_department(department_id: Optional[int]) -> None:
    if department_id is not None and repo.department_get(int(department_id)) is None:
        raise HTTPException(status_code=400, detail="department not found")


def _allocate_code() -> str:
    for _ in range(5):
        candidate = repo.generate_staff_code()
        if repo.get_staff(candidate) is None:
            return candidate
    raise HTTPException(status_code=500, detail="Failed to allocate staff code")


# ── Staff ───────────────────────────────────────────────────────────


@router.get("/", response_model=List[schemas.StaffOut])
@log_call()
def list_staff(department_id: Optional[int] = None):
    names = _dept_names()
    return [_out(s, names) for s in repo.list_staff(department_id)]


@router.post("/", response_model=schemas.StaffOut)
@log_call()
def create_staff(payload: schemas.StaffCreate):
    _check_department(payload.department_id)
    if payload.email and repo.get_staff_by_email(str(payload.email)):
        raise HTTPException(status_code=409, detail="Email already registered")
    code = _allocate_code()
    item = repo.put_staff(code, payload.full_name, email=str(payload.email) if payload.email else None,
                          department_id=payload.department_id, designation=payload.designation)
    return _out(item)


@router.put("/{staff_code}", response_model=schemas.StaffOut)
@log_call()
def update_staff(staff_code: str, payload: schemas.StaffUpdate):
    if payload.email is not None:
        existing = repo.get_staff_by_email(str(payload.email))
        if existing and existing.get("staff_code") != staff_code:
            raise HTTPException(status_code=409, detail="Email already registered")
    fields = payload.model_dump(exclude_unset=True)
    if "department_id" in fields:
        _check_department(fields["department_id"])
    if "email" in fields and fields["email"] is not None:
        fields["email"] = str(fields["email"])
    try:
        item = repo.update_staff(staff_code, **fields)
    except KeyError:
        raise HTTPException(status_code=404, detail="staff not found")
    return _out(item)


@router.delete("/{staff_code}")
@log_call()
def delete_staff(staff_code: str):
    item = repo.soft_delete_staff(staff_code)
    if item is None:
        raise HTTPException(status_code=404, detail="staff not found")
    faces_deleted = False
    try:
        faces_deleted = FaceEngine.get_shared().delete_person(staff_code)
    except FaceServiceError as e:
        logger.warning("could not remove faces of %s: %s", staff_code, e)
    return {"ok": True, "staff_code": staff_code, "faces_deleted": faces_deleted}


@router.get("/{staff_code}/faces")
@log_call()
def staff_faces(staff_code: str):
    if repo.get_staff(staff_code) is None:
        raise HTTPException(status_code=404, detail="staff not found")
    try:
        faces = FaceEngine.get_shared().get_faces_by_external_id(staff_code, category=get_settings().staff_face_category)
    except FaceServiceError as e:
        raise HTTPException(status_code=502, detail=f"Failed to list faces: {e}")
    return {"staff_code": staff_code, "total_faces": len(faces), "faces": [{"face_id": f.get("FaceId")} for f in faces]}


@router.post("/{staff_code}/register", response_model=schemas.RegisterStaffFacesResponse)
@log_call()
def register_faces(
    staff_code: str,
    images: List[UploadFile] = File(...),
    angle_tags: Optional[Union[str, List[str]]] = Form(None),
):
    item = repo.get_staff(staff_code)
    if not item:
        raise HTTPException(status_code=404, detail="staff not found")
    if not images or len(images) > 10:
        raise HTTPException(status_code=400, detail="Between 1 and 10 images are required")
    angle_list = [angle_tags] if isinstance(angle_tags, str) else angle_tags
    errors: List[str] = []
    registered, angles = _enrol_images(
        FaceEngine.get_shared(), images, student_code=staff_code, full_name=str(item.get("full_name") or ""),
        angle_list=angle_list, validation_errors=errors, category=get_settings().staff_face_category,
    )
    if registered == 0:
        raise HTTPException(status_code=400, detail={"message": "No faces were successfully registered", "errors": errors})
    return schemas.RegisterStaffFacesResponse(staff=_out(item), num_faces_registered=registered, angles=angles,
                                              validation_errors=errors or None)


@router.post("/register_new", response_model=schemas.RegisterStaffFacesResponse)
@log_call()
def register_and_create_staff(
    full_name: str = Form(...),
    email: Optional[str] = Form(None),
    department_id: Optional[int] = Form(None),
    designation: Optional[str] = Form(None),
    images: List[UploadFile] = File(...),
    angle_tags: Optional[Union[str, List[str]]] = Form(None),
):
    """Photos first; the staff row is written only once a face was accepted."""
    if not images or len(images) > 10:
        raise HTTPException(status_code=400, detail="Between 1 and 10 images are required")
    _check_department(department_id)
    if email and repo.get_staff_by_email(email):
        raise HTTPException(status_code=409, detail="Email already registered")
    engine = FaceEngine.get_shared()
    errors: List[str] = []
    for idx, up in enumerate(images[:3]):
        data = up.file.read()
        up.file.seek(0)
        if not data:
            continue
        dup = check_for_duplicate_face_sync(enhance_image_for_recognition(data), "__pending__")
        if dup:
            errors.append(f"Image {idx + 1}: Duplicate face detected - already registered for "
                          f"{dup['full_name']} (ID: {dup['student_code']}, {dup['similarity']:.1f}% similarity)")
            raise HTTPException(status_code=409, detail={"message": "Duplicate face detected", "errors": errors})
    angle_list = [angle_tags] if isinstance(angle_tags, str) else angle_tags
    code = _allocate_code()
    registered, angles = _enrol_images(
        engine, images, student_code=code, full_name=full_name, angle_list=angle_list, validation_errors=errors,
        category=get_settings().staff_face_category,
    )
    if registered == 0:
        try:
            engine.delete_person(code)
        except Exception:
            pass
        raise HTTPException(status_code=400, detail={"message": "No faces were successfully registered", "errors": errors})
    try:
        item = repo.put_staff(code, full_name, email=email, department_id=department_id, designation=designation)
    except Exception as e:
        logger.error("Failed to persist staff %s: %s", code, e)
        try:
            engine.delete_person(code)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail="Failed to save staff after registering faces")
    return schemas.RegisterStaffFacesResponse(staff=_out(item), num_faces_registered=registered, angles=angles,
                                              validation_errors=errors or None)


# ── Departments ─────────────────────────────────────────────────────


def _camera_name(camera_id: int) -> Optional[str]:
    try:
        return CameraSource.get_shared().camera_name(camera_id)
    except PlatformUnavailable:
        return None
    except Exception:
        return None


def _refresh_engine() -> None:
    try:
        from ..services.staff_attendance import StaffEngine

        StaffEngine.get_shared().reload()
    except Exception as exc:  # noqa: BLE001 — the loop retries
        logger.warning("staff engine reload failed: %s", exc)


def _dept_out(d: dict) -> schemas.DepartmentOut:
    return schemas.DepartmentOut(id=int(d["id"]), name=str(d["name"]), camera_id=d.get("camera_id"),
                                 camera_name=d.get("camera_name"), work_start=str(d["work_start"]), work_end=str(d["work_end"]))


def _check_window(start: Optional[str], end: Optional[str]) -> None:
    if start and end and start == end:
        raise HTTPException(status_code=400, detail="The working window cannot be empty")


@departments_router.get("/", response_model=List[schemas.DepartmentOut])
@log_call()
def list_departments():
    return [_dept_out(d) for d in repo.department_list()]


@departments_router.post("/", response_model=schemas.DepartmentOut)
@log_call()
def create_department(payload: schemas.DepartmentCreate):
    _check_window(payload.work_start, payload.work_end)
    d = repo.department_put(payload.name, camera_id=payload.camera_id, camera_name=_camera_name(payload.camera_id),
                            work_start=payload.work_start, work_end=payload.work_end)
    _refresh_engine()
    return _dept_out(d)


@departments_router.put("/{department_id}", response_model=schemas.DepartmentOut)
@log_call()
def update_department(department_id: int, payload: schemas.DepartmentUpdate):
    fields = payload.model_dump(exclude_unset=True)
    if fields.get("camera_id") is not None:
        fields["camera_name"] = _camera_name(int(fields["camera_id"])) or ""
    d = repo.department_update(department_id, **fields)
    if d is None:
        raise HTTPException(status_code=404, detail="department not found")
    _check_window(d["work_start"], d["work_end"])
    _refresh_engine()
    return _dept_out(d)


@departments_router.delete("/{department_id}")
@log_call()
def delete_department(department_id: int):
    if not repo.department_delete(department_id):
        raise HTTPException(status_code=404, detail="department not found")
    _refresh_engine()
    return {"ok": True}
