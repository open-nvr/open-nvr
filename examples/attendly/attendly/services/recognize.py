"""Detect → crop → recognise, one frame in, student codes out. Shared by
the class worker (engine.py) and the manual snapshot / probe routes."""
from __future__ import annotations

import logging
from typing import List, Optional

from ..config import get_settings
from ..storage.repo import get_student
from ..utils.image_io import save_detected_face

logger = logging.getLogger(__name__)


def crop_boxes(frame, faces: List[dict]) -> List[tuple]:
    """``[(index, crop)]`` for each detected face, clamped to the frame."""
    h, w = frame.shape[:2]
    out = []
    for i, fd in enumerate(faces):
        px = (fd or {}).get("bbox_px")
        if isinstance(px, (list, tuple)) and len(px) >= 4:
            x1, y1, x2, y2 = [int(v) for v in px[:4]]
        else:
            bbox = (fd or {}).get("BoundingBox") or {}
            left = float(bbox.get("Left", 0.0))
            top = float(bbox.get("Top", 0.0))
            bw = float(bbox.get("Width", 0.0))
            bh = float(bbox.get("Height", 0.0))
            x1 = int(round(left * w))
            y1 = int(round(top * h))
            x2 = int(round((left + bw) * w))
            y2 = int(round((top + bh) * h))
        # A little margin helps the recognition model see the whole face.
        mx = int(0.15 * max(1, x2 - x1))
        my = int(0.15 * max(1, y2 - y1))
        x1 = max(0, min(w - 1, x1 - mx))
        y1 = max(0, min(h - 1, y1 - my))
        x2 = max(0, min(w, x2 + mx))
        y2 = max(0, min(h, y2 + my))
        if x2 <= x1 or y2 <= y1:
            continue
        crop = frame[y1:y2, x1:x2]
        if crop is None or crop.size == 0:
            continue
        out.append((i, crop))
    return out


def recognize_faces_in_frame(
    engine, frame, *, threshold: Optional[float] = None, tag: str = "frame", largest_only: bool = False,
) -> List[dict]:
    """Every recognised face in ``frame`` as ``{code, similarity, area,
    bbox_px}``, unfiltered — the caller decides whether a code is a
    student, a staff member or nobody it knows. ``largest_only`` keeps the
    face nearest the camera (the person at a barrier) and returns at most
    one entry, with ``code`` None when it was not recognised."""
    if engine is None or frame is None:
        return []
    import cv2

    settings = get_settings()
    thr = float(threshold if threshold is not None else settings.recognition_threshold)
    ok_full, full_buf = cv2.imencode(".jpg", frame)
    if not ok_full:
        return []
    try:
        faces = engine.detect_faces(full_buf.tobytes())
    except Exception as exc:
        logger.warning("[%s] face detection failed: %s", tag, exc)
        return []
    if not faces:
        return []
    boxes = crop_boxes(frame, faces)
    if largest_only:
        boxes.sort(key=lambda ic: -(ic[1].shape[0] * ic[1].shape[1]))
        boxes = boxes[:1]
    out: List[dict] = []
    for i, crop in boxes:
        ok_c, buf_c = cv2.imencode(".jpg", crop)
        if not ok_c:
            continue
        entry = {"code": None, "similarity": 0.0, "area": int(crop.shape[0] * crop.shape[1]),
                 "bbox_px": list((faces[i] or {}).get("bbox_px") or [])}
        try:
            matches = engine.search_faces(buf_c.tobytes(), threshold=thr, max_faces=1)
        except Exception as exc:
            logger.warning("[%s] face %d: recognition failed: %s", tag, i, exc)
            matches = []
        if matches:
            entry["code"], entry["similarity"] = str(matches[0][0]), float(matches[0][1])
        out.append(entry)
    return out


def recognize_codes_in_frame(
    engine, frame, *, threshold: Optional[float] = None, course_code: Optional[str] = None,
    section: Optional[str] = None, tag: str = "attempt",
) -> List[str]:
    """Student codes recognised in ``frame``, deduplicated, limited to
    students that exist (and to the class's course/section when given)."""
    if engine is None or frame is None:
        return []
    import cv2

    settings = get_settings()
    thr = float(threshold if threshold is not None else settings.recognition_threshold)
    ok_full, full_buf = cv2.imencode(".jpg", frame)
    if not ok_full:
        return []
    try:
        faces = engine.detect_faces(full_buf.tobytes())
    except Exception as exc:
        logger.warning("[%s] face detection failed: %s", tag, exc)
        return []
    logger.info("[%s] detected %d faces", tag, len(faces))
    if not faces:
        return []
    found: List[str] = []
    for i, crop in crop_boxes(frame, faces):
        save_detected_face(crop, tag=f"{tag}_i{i}")
        ok_c, buf_c = cv2.imencode(".jpg", crop)
        if not ok_c:
            continue
        try:
            matches = engine.search_faces(buf_c.tobytes(), threshold=thr, max_faces=settings.top_k)
        except Exception as exc:
            logger.warning("[%s] face %d: recognition failed: %s", tag, i, exc)
            continue
        for code, sim in matches:
            student = get_student(code)
            if student is None:
                continue
            if course_code and str(student.get("course_code")) != str(course_code):
                continue
            if section and str(student.get("section")) != str(section):
                continue
            logger.debug("[%s] face %d: %s (%.3f)", tag, i, code, sim)
            found.append(code)
    return list(dict.fromkeys(found))
