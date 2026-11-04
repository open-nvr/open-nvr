from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, File, HTTPException, UploadFile

from .. import schemas
from ..config import get_settings
from ..face.engine import FaceEngine, FaceServiceError
from ..storage.repo import get_student, put_attendance
from ..utils.logging_utils import log_call

logger = logging.getLogger(__name__)
router = APIRouter()

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
MAX_FILE_SIZE = 10 * 1024 * 1024


async def validate_image_upload(file: UploadFile) -> bytes:
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    ext = "." + file.filename.lower().rsplit(".", 1)[-1] if "." in file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Invalid file type. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}")
    contents = await file.read()
    if len(contents) == 0:
        raise HTTPException(status_code=400, detail="Empty file uploaded")
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail=f"File too large. Maximum size: {MAX_FILE_SIZE // (1024 * 1024)}MB")
    return contents


@router.post("/identify", response_model=schemas.RecognitionResult)
@log_call()
async def identify(image: UploadFile = File(...), camera_name: Optional[str] = None):
    start = time.time()
    data = await validate_image_upload(image)
    settings = get_settings()
    try:
        matches = FaceEngine.get_shared().search_faces(data, threshold=settings.recognition_threshold, max_faces=settings.top_k)
    except FaceServiceError as e:
        raise HTTPException(status_code=502, detail=f"Recognition service error: {e}")
    matched = None
    top_sim = 0.0
    if matches:
        best_code, best_sim = matches[0]
        s = get_student(best_code)
        if s:
            matched = schemas.student_out(s)
            top_sim = float(best_sim)
            put_attendance(student_code=best_code, camera_name=camera_name, confidence=top_sim)
    logger.info("identify: %s in %.2fs", matched.student_code if matched else "no match", time.time() - start)
    return schemas.RecognitionResult(student=matched, confidence=top_sim, face_count=1 if matched else 0)


@router.post("/detect_only")
@log_call()
async def detect_only(image: UploadFile = File(...)):
    data = await validate_image_upload(image)
    try:
        faces = FaceEngine.get_shared().detect_faces(data)
    except FaceServiceError as e:
        raise HTTPException(status_code=502, detail=f"Detection service error: {e}")
    return {"face_count": len(faces), "boxes": [f.get("bbox_px") for f in faces]}
