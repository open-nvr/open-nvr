"""Detect → crop → recognise, one frame in, student codes out. Shared by
the class worker (engine.py) and the manual snapshot / probe routes."""
from __future__ import annotations

import logging
from typing import List, Optional

from ..config import get_settings
from ..storage.repo import get_student, student_enrolled_in


class RecognitionUnavailable(RuntimeError):
    """Detection could not be run on this frame (KAI-C/adapter down): the
    look did not happen, as opposed to happening and finding nobody."""
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


def _identify_entries(result: dict, *, margin: float, largest_only: bool) -> List[dict]:
    """``face_identify``'s faces as this module's entries. A hit whose lead
    over the runner-up is under ``margin`` is kept as a face but not as a
    person: two look-alikes must not swap marks silently."""
    out: List[dict] = []
    for f in result.get("faces") or []:
        if not isinstance(f, dict):
            continue
        bbox = f.get("bbox") or []
        if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
            continue
        x1, y1, x2, y2 = [int(v) for v in bbox[:4]]
        entry = {"code": None, "similarity": float(f.get("similarity") or 0.0), "area": max(0, x2 - x1) * max(0, y2 - y1),
                 "bbox_px": [x1, y1, x2, y2], "landmarks": f.get("landmarks"), "ambiguous": False}
        if f.get("recognized") and f.get("person_id"):
            second = f.get("second_similarity")
            if isinstance(second, (int, float)) and entry["similarity"] - float(second) < margin:
                entry["ambiguous"] = True
            else:
                entry["code"] = str(f["person_id"])
        out.append(entry)
    if largest_only:
        out.sort(key=lambda e: -e["area"])
        out = out[:1]
    return out


def identify_or_none(engine, jpeg: bytes, *, threshold: float, tag: str) -> Optional[dict]:
    """The adapter's ``face_identify`` answer, or None when it has no such
    task (the caller then detects and crops). An outage raises
    :class:`RecognitionUnavailable`, as detection would."""
    fn = getattr(engine, "identify_faces", None)
    if fn is None:
        return None
    try:
        return fn(jpeg, threshold=threshold)
    except Exception as exc:
        logger.warning("[%s] face_identify failed: %s", tag, exc)
        raise RecognitionUnavailable(str(exc)) from exc


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
    jpeg = full_buf.tobytes()
    identified = identify_or_none(engine, jpeg, threshold=thr, tag=tag)
    if identified is not None:
        entries = _identify_entries(identified, margin=settings.identify_margin, largest_only=largest_only)
        logger.info("[%s] face_identify: %d faces, %d recognised", tag, len(entries), sum(1 for e in entries if e["code"]))
        return entries
    try:
        faces = engine.detect_faces(jpeg)
    except Exception as exc:
        logger.warning("[%s] face detection failed: %s", tag, exc)
        raise RecognitionUnavailable(str(exc)) from exc
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
    jpeg = full_buf.tobytes()
    identified = identify_or_none(engine, jpeg, threshold=thr, tag=tag)
    if identified is not None:
        found = []
        for e in _identify_entries(identified, margin=settings.identify_margin, largest_only=False):
            code = e["code"]
            if not code:
                continue
            student = get_student(code)
            if student is None or not student_enrolled_in(student, course_code, section):
                continue
            found.append(code)
        logger.info("[%s] face_identify: %d faces, %d of this class", tag, len(identified.get("faces") or []), len(found))
        return list(dict.fromkeys(found))
    try:
        faces = engine.detect_faces(jpeg)
    except Exception as exc:
        logger.warning("[%s] face detection failed: %s", tag, exc)
        raise RecognitionUnavailable(str(exc)) from exc
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
            if not student_enrolled_in(student, course_code, section):
                continue
            logger.debug("[%s] face %d: %s (%.3f)", tag, i, code, sim)
            found.append(code)
    return list(dict.fromkeys(found))
