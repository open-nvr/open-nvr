"""The enrolled-faces registry (was the AWS admin page's backend): what the
face adapter holds for attendly's students, joined with the student rows."""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ..config import get_settings
from ..pictures import has_picture as _has_picture
from ..face.engine import FaceEngine, FaceServiceError
from ..storage.repo import get_student, get_students_batch, list_students, move_student_to_deleted
from ..utils.logging_utils import log_call

logger = logging.getLogger(__name__)
router = APIRouter()


def _engine() -> FaceEngine:
    return FaceEngine.get_shared()


def _service_error(e: Exception, what: str):
    logger.error(f"{what}: {e}")
    return HTTPException(status_code=502, detail=f"{what}: {e}")


@router.get("/collection/info")
@log_call()
def get_collection_info():
    try:
        info = _engine().describe_collection()
    except FaceServiceError as e:
        raise _service_error(e, "Failed to get collection info")
    return {"collection_id": info.get("CollectionId"), "face_count": info.get("FaceCount", 0),
            "person_count": info.get("PersonCount", 0), "face_model_version": info.get("FaceModelVersion"),
            "creation_timestamp": info.get("CreationTimestamp"), "collection_arn": None}


@router.get("/faces/list")
@log_call()
def list_all_faces(
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
    student_code: Optional[str] = Query(None), include_student_info: bool = Query(True),
):
    start = time.time()
    try:
        faces = _engine().list_faces()
    except FaceServiceError as e:
        raise _service_error(e, "Failed to list faces")
    if student_code:
        faces = [f for f in faces if f.get("ExternalImageId") == student_code]
    total = len(faces)
    s, e = (page - 1) * page_size, (page - 1) * page_size + page_size
    page_faces = faces[s:e]
    students = get_students_batch([f.get("ExternalImageId") for f in page_faces]) if include_student_info else {}
    out = []
    for f in page_faces:
        pid = f.get("ExternalImageId")
        row = {"face_id": f.get("FaceId"), "external_image_id": pid, "student_code": pid,
               "confidence": f.get("Confidence"), "image_id": f.get("ImageId")}
        if include_student_info:
            info = students.get(pid)
            row.update({"student_name": info.get("full_name") if info else f.get("Name") or "Unknown",
                        "student_class": info.get("class_name") if info else None,
                        "course_code": info.get("course_code") if info else None,
                        "section": info.get("section") if info else None,
                        "student_email": info.get("email") if info else None})
        out.append(row)
    return {"total_faces": total, "page": page, "page_size": page_size, "total_pages": (total + page_size - 1) // page_size,
            "faces": out, "processing_time_seconds": round(time.time() - start, 2),
            "has_next_page": e < total, "has_previous_page": page > 1}


@router.get("/student/{student_code}/faces")
@log_call()
def get_student_faces(student_code: str):
    info = get_student(student_code)
    if not info:
        raise HTTPException(status_code=404, detail="Student not found")
    try:
        faces = _engine().get_faces_by_external_id(student_code)
    except FaceServiceError as e:
        raise _service_error(e, "Failed to get student faces")
    return {"student_code": student_code, "student_name": info.get("full_name"), "student_class": info.get("class_name"),
            "course_code": info.get("course_code"), "section": info.get("section"), "total_faces": len(faces),
            "faces": [{"face_id": f.get("FaceId"), "confidence": f.get("Confidence"), "image_id": None, "bounding_box": None} for f in faces]}


@router.delete("/student/{student_code}/faces")
@log_call()
def delete_student_faces(student_code: str):
    try:
        faces = _engine().get_faces_by_external_id(student_code)
        if not faces:
            raise HTTPException(status_code=404, detail="No faces found for this student")
        ok = _engine().delete_person(student_code)
    except FaceServiceError as e:
        raise _service_error(e, "Failed to delete student faces")
    return {"student_code": student_code, "deleted_faces": len(faces) if ok else 0, "total_faces_found": len(faces), "success": bool(ok)}


@router.post("/student/{student_code}/soft_delete")
@log_call()
def soft_delete_student(student_code: str):
    """Remove the student's faces from the adapter and retire the row."""
    deleted = 0
    try:
        faces = _engine().get_faces_by_external_id(student_code)
        if faces and _engine().delete_person(student_code):
            deleted = len(faces)
    except FaceServiceError as e:
        raise _service_error(e, "Failed to delete student faces")
    if move_student_to_deleted(student_code) is None:
        raise HTTPException(status_code=404, detail="Student not found")
    from ..pictures import drop_picture

    drop_picture(student_code)
    return {"student_code": student_code, "deleted_faces": deleted, "moved_to": "deleted_students", "success": True}


@router.get("/stats")
@log_call()
def get_registry_stats(
    include_face_ids: bool = Query(False), include_student_info: bool = Query(True), include_students: bool = Query(False),
):
    start = time.time()
    try:
        persons = _engine().list_persons()
    except FaceServiceError as e:
        raise _service_error(e, "Failed to get registry stats")
    live = {s["student_code"]: s for s in list_students()}
    samples_by_code = {str(p.get("person_id")): int(p.get("samples") or 0) for p in persons if p.get("person_id")}
    # Students whose faces are enrolled AND whose row is live; a retired
    # student's leftover samples (adapter restored from backup, say) are
    # not shown as a student.
    with_faces = {code: n for code, n in samples_by_code.items() if code in live and n > 0}
    distribution = defaultdict(int)
    for n in with_faces.values():
        distribution[str(n) if n <= 10 else "10+"] += 1
    total_faces = sum(with_faces.values())
    response = {
        "collection": {"id": _engine().adapter_name, "total_faces": sum(samples_by_code.values()),
                       "face_model_version": "insightface", "created_at": None},
        "statistics": {
            "total_students_with_faces": len(with_faces), "total_faces": total_faces,
            "average_faces_per_student": round(total_faces / len(with_faces), 2) if with_faces else 0,
            "students_by_face_count": dict(distribution), "processing_time_seconds": round(time.time() - start, 2),
            "max_students": int(get_settings().max_registered_students or 0),
        },
    }
    if include_students:
        rows = []
        for code, n in with_faces.items():
            info = live.get(code) or {}
            row = {"student_code": code, "face_count": n, "max_confidence": None}
            if include_face_ids:
                row["face_ids"] = [f"{code}#{i}" for i in range(n)]
            if include_student_info:
                row.update({"student_name": info.get("full_name"), "student_class": info.get("class_name"),
                            "course_code": info.get("course_code"), "section": info.get("section"),
                            "student_email": info.get("email"), "residence": info.get("residence"),
                            "hostel_block": info.get("hostel_block"), "enrolments": info.get("enrolments") or [],
                            "consent_at": info.get("consent_at"), "has_picture": _has_picture(code)})
            rows.append(row)
        rows.sort(key=lambda x: x["face_count"], reverse=True)
        response["students_with_faces"] = rows
    return response


@router.get("/dashboard")
@log_call()
def get_dashboard_data():
    start = time.time()
    try:
        info = _engine().describe_collection()
    except FaceServiceError as e:
        raise _service_error(e, "Failed to get dashboard data")
    return {"collection": {"id": info.get("CollectionId"), "total_faces": info.get("FaceCount", 0),
                           "face_model_version": info.get("FaceModelVersion"), "created_at": None},
            "quick_stats": {"total_faces": info.get("FaceCount", 0), "processing_time_seconds": round(time.time() - start, 2)}}


@router.get("/engine_status")
@log_call()
def engine_status():
    from ..services.engine import StreamEngine

    return {"ok": True, "workers": StreamEngine.get_shared().status()}
