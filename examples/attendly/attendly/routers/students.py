from __future__ import annotations

import io
import logging
from typing import List, Optional, Union

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, ImageEnhance

from .. import schemas
from ..config import get_settings
from ..face.engine import FaceEngine, FaceRejected, FaceServiceError
from ..storage import repo
from ..people import find_person
from ..pictures import drop_picture, has_picture, picture_path, save_picture
from ..storage.repo import (
    count_students,
    erase_student,
    generate_student_code,
    get_student,
    get_student_by_email,
    move_student_to_deleted,
    normalise_residence,
    put_student,
    update_student as repo_update_student,
)
from ..utils.logging_utils import log_call

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_IMAGE_BYTES = 10 * 1024 * 1024


CONSENT_NEEDED = "Consent must be recorded before a face is enrolled"


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _check_limit(settings) -> None:
    """``ATTENDLY_MAX_REGISTERED_STUDENTS``; 0 (the default) is no limit."""
    limit = int(settings.max_registered_students or 0)
    if limit <= 0:
        return
    try:
        n = count_students()
    except Exception:
        logger.warning("Failed to count students; proceeding")
        return
    if n >= limit:
        raise HTTPException(status_code=403, detail=f"Registration limit reached ({limit}).")


@log_call()
def check_for_duplicate_face_sync(image_bytes: bytes, current_student_code: str) -> Optional[dict]:
    """Is the face in this image already enrolled for someone else?"""
    try:
        engine = FaceEngine.get_shared()
        matches = engine.search_faces(image_bytes, threshold=get_settings().duplicate_threshold, max_faces=1)
        if matches:
            matched_code, similarity = matches[0]
            if matched_code != current_student_code:
                person = find_person(matched_code)
                if person:
                    info = person["record"]
                    return {
                        "student_code": matched_code, "full_name": info.get("full_name", "Unknown"),
                        "class_name": info.get("class_name", ""), "email": info.get("email"),
                        "course_code": info.get("course_code"), "section": info.get("section"),
                        "kind": person["kind"], "similarity": similarity * 100,
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
    angle_list: Optional[List[str]], validation_errors: List[str], category: Optional[str] = None,
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
                fid = engine.index_face(enhanced, student_code, name=full_name, category=category)
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
    _check_limit(settings)
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
                               course_code=payload.course_code, section=payload.section,
                               residence=payload.residence, hostel_block=payload.hostel_block, consent=payload.consent)
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
            residence=payload.residence, hostel_block=payload.hostel_block, consent=payload.consent,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="student not found")
    if payload.consent is False:
        _forget_faces(student_code)
    if payload.enrolments is not None:
        repo.set_student_enrolments(student_code, [e.model_dump() for e in payload.enrolments])
        updated = get_student(student_code) or updated
    return schemas.student_out(updated)


@router.get("/{student_code}/enrolments", response_model=List[schemas.Enrolment])
@log_call()
def get_enrolments(student_code: str):
    if get_student(student_code) is None:
        raise HTTPException(status_code=404, detail="student not found")
    return repo.list_student_enrolments(student_code)


@router.put("/{student_code}/enrolments", response_model=List[schemas.Enrolment])
@log_call()
def put_enrolments(student_code: str, payload: List[schemas.Enrolment]):
    """The extra course/section pairs the student attends (the primary
    pair stays on the student). A class of any of them marks them."""
    try:
        return repo.set_student_enrolments(student_code, [e.model_dump() for e in payload])
    except KeyError:
        raise HTTPException(status_code=404, detail="student not found")


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
    if not student_data.get("consent_at"):
        raise HTTPException(status_code=403, detail=CONSENT_NEEDED)
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
    residence: Optional[str] = Form(None),
    hostel_block: Optional[str] = Form(None),
    consent: Optional[str] = Form(None),
):
    """Enrol the photos first; the student row is written only when at
    least one face was accepted by the face model. Consent first."""
    if not _truthy(consent):
        raise HTTPException(status_code=403, detail=CONSENT_NEEDED)
    if normalise_residence(residence) == "hostler" and not (hostel_block or "").strip():
        raise HTTPException(status_code=400, detail="A hostler needs a hostel block")
    if len(images) == 0:
        raise HTTPException(status_code=400, detail="At least one image is required")
    if len(images) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 images allowed")
    settings = get_settings()
    _check_limit(settings)
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
        item = put_student(code_candidate, full_name, None, email, course_code=course_code, section=section,
                           residence=residence, hostel_block=hostel_block, consent=True)
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


# ── Consent, erasure, the picture ───────────────────────────────────


def _forget_faces(student_code: str) -> int:
    """The faces and the picture go together."""
    deleted = 0
    try:
        engine = FaceEngine.get_shared()
        faces = engine.get_faces_by_external_id(student_code)
        if faces and engine.delete_person(student_code):
            deleted = len(faces)
    except FaceServiceError as e:
        logger.warning("could not remove faces of %s: %s", student_code, e)
    drop_picture(student_code)
    return deleted


@router.post("/{student_code}/consent", response_model=schemas.StudentOut)
@log_call()
def record_consent(student_code: str):
    try:
        item = repo_update_student(student_code, consent=True)
    except KeyError:
        raise HTTPException(status_code=404, detail="student not found")
    return schemas.student_out(item)


@router.post("/{student_code}/consent/withdraw", response_model=schemas.StudentOut)
@log_call()
def withdraw_consent(student_code: str):
    """Consent withdrawn: the faces are deleted first, then the record says so."""
    if get_student(student_code) is None:
        raise HTTPException(status_code=404, detail="student not found")
    _forget_faces(student_code)
    item = repo_update_student(student_code, consent=False)
    return schemas.student_out(item)


@router.delete("/{student_code}")
@log_call()
def delete_student(student_code: str, erase: bool = Query(False)):
    """Remove a student: faces and picture go, the row retires (attendance
    history keeps the name). ``erase=true`` also blanks name, e-mail and
    block and keeps only the counts (DPDP/GDPR) — also for someone
    already removed."""
    current = get_student(student_code)
    if current is None and not erase:
        raise HTTPException(status_code=404, detail="student not found")
    deleted = _forget_faces(student_code)
    if erase:
        item = erase_student(student_code)
        if item is None:
            raise HTTPException(status_code=404, detail="student not found")
    else:
        item = move_student_to_deleted(student_code)
    return {"ok": True, "student_code": student_code, "faces_deleted": deleted, "erased": bool(erase)}


@router.put("/{student_code}/picture")
@log_call()
def put_picture(student_code: str, image: UploadFile = File(...)):
    if get_student(student_code) is None:
        raise HTTPException(status_code=404, detail="student not found")
    data = image.file.read()
    if not data or len(data) > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="a JPEG up to 2 MB")
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
    except Exception:
        raise HTTPException(status_code=400, detail="not an image")
    save_picture(student_code, data)
    return {"ok": True, "student_code": student_code}


@router.get("/{student_code}/picture")
@log_call()
def get_picture(student_code: str):
    p = picture_path(student_code)
    if not p or not p.is_file():
        raise HTTPException(status_code=404, detail="no picture")
    return FileResponse(str(p), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


# ── The webcam's guidance: where the face is, nothing kept ──────────


@router.post("/check_frame")
@log_call()
def check_frame(image: UploadFile = File(...)):
    """For the enrolling page, about once a second: every face's box (and
    its five landmarks when the adapter gives them), the frame's size, and
    whether faces were dropped for being too small. Nobody is looked up;
    nothing is stored."""
    data = image.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
    except Exception:
        raise HTTPException(status_code=400, detail="not an image")
    engine = FaceEngine.get_shared()
    settings = get_settings()
    faces: List[dict] = []
    too_small = False
    try:
        ident = engine.identify_faces(data, match=False) if hasattr(engine, "identify_faces") else None
    except FaceServiceError as e:
        raise HTTPException(status_code=502, detail=f"Face service unavailable: {e}")
    if ident is not None:
        for f in ident.get("faces") or []:
            if isinstance(f, dict) and isinstance(f.get("bbox"), (list, tuple)) and len(f["bbox"]) >= 4:
                faces.append({"bbox": [int(v) for v in f["bbox"][:4]], "confidence": f.get("confidence"),
                              "landmarks": f.get("landmarks")})
        dropped = ident.get("dropped") or {}
        too_small = int(dropped.get("too_small") or 0) > 0
    else:
        try:
            detected = engine.detect_faces(data)
        except FaceServiceError as e:
            raise HTTPException(status_code=502, detail=f"Face service unavailable: {e}")
        for f in detected:
            x1, y1, x2, y2 = [int(v) for v in (f.get("bbox_px") or [0, 0, 0, 0])]
            if min(x2 - x1, y2 - y1) < int(settings.min_face_px):
                too_small = True
                continue
            faces.append({"bbox": [x1, y1, x2, y2], "confidence": (f.get("Confidence") or 0) / 100.0, "landmarks": None})
    return {"faces": faces, "frame": {"w": w, "h": h}, "too_small": too_small and not faces}


@router.post("/check_duplicate")
@log_call()
def check_duplicate(image: UploadFile = File(...), exclude_code: Optional[str] = Form(None)):
    """Is this face already enrolled for somebody else? Asked of each
    webcam photo as it is taken, so a person registered twice is told at
    the first photo rather than after the whole set."""
    data = image.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    dup = check_for_duplicate_face_sync(enhance_image_for_recognition(data), exclude_code or "__pending__")
    return {"duplicate": dup}
