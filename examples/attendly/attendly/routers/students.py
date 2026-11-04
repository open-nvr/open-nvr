from __future__ import annotations

import io
import logging
from typing import List, Optional, Union

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from PIL import Image, ImageEnhance

from .. import schemas
from ..config import get_settings
from ..face.engine import FaceEngine, FaceRejected, FaceServiceError
from ..storage import repo
from ..storage.repo import (
    count_students,
    generate_student_code,
    get_student,
    get_student_by_email,
    put_student,
    update_student as repo_update_student,
)
from ..utils.logging_utils import log_call

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_IMAGE_BYTES = 10 * 1024 * 1024


@log_call()
def check_for_duplicate_face_sync(image_bytes: bytes, current_student_code: str) -> Optional[dict]:
    """Is the face in this image already enrolled for someone else?"""
    try:
        engine = FaceEngine.get_shared()
        matches = engine.search_faces(image_bytes, threshold=get_settings().duplicate_threshold, max_faces=1)
        if matches:
            matched_code, similarity = matches[0]
            if matched_code != current_student_code:
                info = get_student(matched_code)
                if info:
                    return {
                        "student_code": matched_code, "full_name": info.get("full_name", "Unknown"),
                        "class_name": info.get("class_name", ""), "email": info.get("email"),
                        "course_code": info.get("course_code"), "section": info.get("section"),
                        "similarity": similarity * 100,
                    }
        return None
    except Exception as e:
        logger.error(f"Error checking for duplicate face: {e}")
        return None


@router.post("/validate_image_quality")
@log_call()
def validate_image_quality_endpoint(image: UploadFile = File(..., description="Image to validate")):
    """Basic format check; the InsightFace adapter judges face quality."""
    data = image.file.read()
    image.file.seek(0)
    if len(data) == 0:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="File too large (max 10MB)")
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
    except Exception:
        return {"is_valid": False, "message": "Invalid image format", "quality_score": 0.0,
                "recommendations": ["Please upload a valid image file (JPEG, PNG, etc.)"]}
    return {"is_valid": True, "message": "Image format is valid", "quality_score": 1.0,
            "recommendations": ["Image ready for registration - the face model validates quality"]}


@log_call()
def enhance_image_for_recognition(image_bytes: bytes) -> bytes:
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode != "RGB":
            img = img.convert("RGB")
        width, height = img.size
        max_size = 1024
        if width > max_size or height > max_size:
            ratio = min(max_size / width, max_size / height)
            img = img.resize((int(width * ratio), int(height * ratio)), Image.Resampling.LANCZOS)
        img = ImageEnhance.Sharpness(img).enhance(1.1)
        img = ImageEnhance.Contrast(img).enhance(1.05)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=95, optimize=True)
        return out.getvalue()
    except Exception as e:
        logger.error(f"Error enhancing image: {e}")
        return image_bytes


def _enrol_images(
    engine: FaceEngine, images: List[UploadFile], *, student_code: str, full_name: str,
    angle_list: Optional[List[str]], validation_errors: List[str],
) -> tuple[int, List[str]]:
    """Index every usable image for ``student_code``; returns (count, angles)."""
    registered = 0
    angles: List[str] = []
    for idx, up in enumerate(images):
        try:
            data = up.file.read()
            up.file.seek(0)
            if len(data) > MAX_IMAGE_BYTES:
                validation_errors.append(f"Image {idx + 1}: File too large (max 10MB)")
                continue
            if len(data) == 0:
                validation_errors.append(f"Image {idx + 1}: Empty file")
                continue
            enhanced = enhance_image_for_recognition(data)
            duplicate = check_for_duplicate_face_sync(enhanced, student_code)
            if duplicate:
                msg = (f"Image {idx + 1}: Duplicate face detected - already registered for "
                       f"{duplicate['full_name']} (ID: {duplicate['student_code']}, {duplicate['similarity']:.1f}% similarity)")
                validation_errors.append(msg)
                logger.warning(msg)
                continue
            try:
                fid = engine.index_face(enhanced, student_code, name=full_name)
            except FaceRejected as e:
                validation_errors.append(f"Image {idx + 1}: {e}")
                continue
            except FaceServiceError as e:
                logger.error(f"Face service error for image {idx + 1}: {e}")
                validation_errors.append(f"Image {idx + 1}: Recognition service error")
                continue
            if fid:
                registered += 1
                angles.append(angle_list[idx] if angle_list and idx < len(angle_list) else "unknown")
                logger.info(f"Registered face {idx + 1} for {student_code}: {fid}")
            else:
                validation_errors.append(f"Image {idx + 1}: Failed to register face")
        except Exception as e:
            logger.error(f"Error processing image {idx + 1}: {e}")
            validation_errors.append(f"Image {idx + 1}: Processing error")
    return registered, angles


@router.post("/", response_model=schemas.StudentOut)
@log_call()
def create_student(payload: schemas.StudentCreate):
    settings = get_settings()
    try:
        if count_students() >= settings.max_registered_students:
            raise HTTPException(status_code=403, detail=f"Registration limit reached ({settings.max_registered_students}).")
    except HTTPException:
        raise
    except Exception:
        logger.warning("Failed to count students; proceeding with create_student")
    if payload.email and get_student_by_email(str(payload.email)) is not None:
        raise HTTPException(status_code=409, detail="Email already registered")
    try:
        if not repo.planner_course_get(payload.course_code):
            repo.planner_course_put(code=payload.course_code, students=0, sections=0)
    except Exception:
        pass
    for _ in range(5):
        code = generate_student_code()
        if get_student(code) is None:
            item = put_student(code, payload.full_name, None, str(payload.email) if payload.email else None,
                               course_code=payload.course_code, section=payload.section)
            break
    else:
        raise HTTPException(status_code=500, detail="Failed to generate unique student code")
    return schemas.student_out(item)


@router.get("/", response_model=List[schemas.StudentOut])
@log_call()
def list_students():
    return [schemas.student_out(item) for item in repo.list_students()]


@router.put("/{student_code}", response_model=schemas.StudentOut)
@log_call()
def update_student(student_code: str, payload: schemas.StudentUpdate):
    if payload.email is not None:
        existing = get_student_by_email(str(payload.email))
        if existing is not None and existing.get("student_code") != student_code:
            raise HTTPException(status_code=409, detail="Email already registered")
    try:
        updated = repo_update_student(
            student_code, full_name=payload.full_name, class_name=payload.class_name,
            email=str(payload.email) if payload.email is not None else None,
            course_code=payload.course_code, section=payload.section,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="student not found")
    # Keep the adapter's display name in step with ours (best effort).
    if payload.full_name:
        try:
            FaceEngine.get_shared()._faces_request("PATCH", f"/faces/{student_code}", json={"name": payload.full_name})
        except Exception:
            pass
    return schemas.student_out(updated)


@router.post("/{student_code}/register", response_model=schemas.RegisterFacesResponse)
@log_call()
def register_faces(
    student_code: str,
    images: List[UploadFile] = File(..., description="Multiple face images from different angles"),
    angle_tags: Optional[Union[str, List[str]]] = Form(None),
):
    student_data = get_student(student_code)
    if not student_data:
        raise HTTPException(status_code=404, detail="student not found")
    if len(images) == 0:
        raise HTTPException(status_code=400, detail="At least one image is required")
    if len(images) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 images allowed")
    angle_list = [angle_tags] if isinstance(angle_tags, str) else angle_tags
    validation_errors: List[str] = []
    registered, angles = _enrol_images(
        FaceEngine.get_shared(), images, student_code=student_code, full_name=str(student_data.get("full_name") or ""),
        angle_list=angle_list, validation_errors=validation_errors,
    )
    if registered == 0:
        raise HTTPException(status_code=400, detail={"message": "No faces were successfully registered", "errors": validation_errors})
    return schemas.RegisterFacesResponse(
        student=schemas.student_out(student_data), num_faces_registered=registered, angles=angles,
        validation_errors=validation_errors or None,
    )


@router.post("/register_new", response_model=schemas.RegisterFacesResponse)
@log_call()
def register_and_create_student(
    full_name: str = Form(...),
    course_code: str = Form(...),
    section: str = Form(...),
    email: str = Form(...),
    images: List[UploadFile] = File(..., description="Multiple face images from different angles"),
    angle_tags: Optional[Union[str, List[str]]] = Form(None),
):
    """Enrol the photos first; the student row is written only when at
    least one face was accepted by the face model."""
    if len(images) == 0:
        raise HTTPException(status_code=400, detail="At least one image is required")
    if len(images) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 images allowed")
    settings = get_settings()
    try:
        if count_students() >= settings.max_registered_students:
            raise HTTPException(status_code=403, detail=f"Registration limit reached ({settings.max_registered_students}).")
    except HTTPException:
        raise
    except Exception:
        pass
    angle_list = [angle_tags] if isinstance(angle_tags, str) else angle_tags
    engine = FaceEngine.get_shared()

    validation_errors: List[str] = []
    for idx, up in enumerate(images[:3]):
        try:
            data = up.file.read()
            up.file.seek(0)
            if len(data) == 0:
                continue
            duplicate = check_for_duplicate_face_sync(enhance_image_for_recognition(data), "__pending__")
            if duplicate:
                validation_errors.append(
                    f"Image {idx + 1}: Duplicate face detected - already registered for "
                    f"{duplicate['full_name']} (ID: {duplicate['student_code']}, {duplicate['similarity']:.1f}% similarity)")
                raise HTTPException(status_code=409, detail={"message": "Duplicate face detected", "errors": validation_errors})
        except HTTPException:
            raise
        except Exception:
            continue

    try:
        if not repo.planner_course_get(course_code):
            repo.planner_course_put(code=course_code, students=0, sections=0)
    except Exception:
        pass
    if email and get_student_by_email(email) is not None:
        raise HTTPException(status_code=409, detail="Email already registered")

    code_candidate: Optional[str] = None
    for _ in range(5):
        candidate = generate_student_code()
        if get_student(candidate) is None:
            code_candidate = candidate
            break
    if code_candidate is None:
        raise HTTPException(status_code=500, detail="Failed to allocate student code")

    registered, angles = _enrol_images(
        engine, images, student_code=code_candidate, full_name=full_name, angle_list=angle_list,
        validation_errors=validation_errors,
    )
    if registered == 0:
        try:
            engine.delete_person(code_candidate)
        except Exception:
            pass
        raise HTTPException(status_code=400, detail={"message": "No faces were successfully registered", "errors": validation_errors})

    try:
        item = put_student(code_candidate, full_name, None, email, course_code=course_code, section=section)
    except Exception as e:
        logger.error(f"Failed to persist student {code_candidate}: {e}")
        try:
            engine.delete_person(code_candidate)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail="Failed to save student after registering faces")

    return schemas.RegisterFacesResponse(
        student=schemas.student_out(item), num_faces_registered=registered, angles=angles,
        validation_errors=validation_errors or None,
    )
