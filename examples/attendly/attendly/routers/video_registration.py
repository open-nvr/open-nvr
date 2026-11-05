"""Webcam enrolment over a WebSocket: the browser streams frames, the
server keeps the ones with a face (checked through the platform's face
detection), then enrols them one by one and only then writes the student."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from typing import Dict, List, Optional

from fastapi import APIRouter, Form, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from ..config import get_settings
from ..face.engine import FaceEngine, FaceRejected, FaceServiceError
from ..gate import ws_user_allowed
from ..storage import repo
from ..storage.repo import count_students, generate_student_code, get_student, get_student_by_email, put_student
from ..utils.logging_utils import log_call
from .students import check_for_duplicate_face_sync

logger = logging.getLogger(__name__)
router = APIRouter()


async def check_for_duplicate_face(image_bytes: bytes, current_student_code: str) -> Optional[dict]:
    return await asyncio.to_thread(check_for_duplicate_face_sync, image_bytes, current_student_code)


class VideoRegistrationSession:
    def __init__(self, session_id: str, websocket: WebSocket, full_name: str, class_name: Optional[str],
                 email: Optional[str] = None):
        self.session_id = session_id
        self.websocket = websocket
        self.full_name = full_name
        self.class_name = class_name or ""
        self.email = email or None
        self.course_code: Optional[str] = None
        self.section: Optional[str] = None
        self.residence: Optional[str] = None
        self.hostel_block: Optional[str] = None
        self.student_code: Optional[str] = None
        self.collected_frames: List[bytes] = []
        self.start_time = time.time()
        self.frame_count = 0
        self.target_frames = 30
        self.capture_duration = 12
        self.is_capturing = False

    def get_progress(self) -> float:
        if self.is_capturing:
            return min(100.0, (len(self.collected_frames) / self.target_frames) * 100)
        return min(100.0, ((time.time() - self.start_time) / self.capture_duration) * 100)

    def should_capture_frame(self) -> bool:
        return len(self.collected_frames) < self.target_frames and self.is_capturing

    def is_complete(self) -> bool:
        return len(self.collected_frames) >= self.target_frames


active_sessions: Dict[str, VideoRegistrationSession] = {}


def detect_face_in_frame(image_bytes: bytes) -> bool:
    """Exactly one face in the frame? Through the platform's detector; when
    it cannot be asked the frame is kept and the enrolment step decides."""
    try:
        faces = FaceEngine.get_shared().detect_faces(image_bytes)
        return len(faces) == 1
    except Exception as e:
        logger.warning(f"face detection unavailable during capture: {e}")
        return True


@router.post("/start_video_registration")
@log_call()
async def start_video_registration(
    full_name: str = Form(...), course_code: Optional[str] = Form(None), section: Optional[str] = Form(None),
    email: str = Form(...),
):
    settings = get_settings()
    try:
        if count_students() >= settings.max_registered_students:
            raise HTTPException(status_code=403, detail=f"Registration limit reached ({settings.max_registered_students}).")
    except HTTPException:
        raise
    except Exception:
        logger.warning("Failed to count students; proceeding with start_video_registration")
    session_id = uuid.uuid4().hex
    if email and get_student_by_email(str(email)) is not None:
        return JSONResponse(status_code=409, content={"message": "Email already registered", "error": "duplicate_email"})
    return JSONResponse({
        "message": "Video registration session ready", "session_id": session_id,
        "websocket_url": f"/video_registration/ws/{session_id}", "full_name": full_name, "class_name": "",
        "course_code": course_code or "", "section": section or "", "email": email or "",
    })


@router.websocket("/ws/{session_id}")
async def video_registration_websocket(websocket: WebSocket, session_id: str):
    token = websocket.query_params.get("token")
    if not ws_user_allowed(token, "operator"):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    full_name, class_name, course_code_str, section_str, email = "", "", None, None, None
    residence, hostel_block = None, None
    try:
        init_msg = await asyncio.wait_for(websocket.receive_text(), timeout=3.0)
        try:
            init = json.loads(init_msg)
            if isinstance(init, dict) and init.get("type") == "init":
                full_name = str(init.get("full_name") or "")
                class_name = str(init.get("class_name") or "")
                course_code_str = str(init.get("course_code") or "") or None
                section_str = str(init.get("section") or "") or None
                email = str(init.get("email") or "") or None
                residence = str(init.get("residence") or "") or None
                hostel_block = str(init.get("hostel_block") or "") or None
        except Exception:
            pass
    except Exception:
        pass

    session = VideoRegistrationSession(session_id, websocket, full_name, class_name, email)
    session.course_code = course_code_str
    session.section = section_str
    session.residence = residence
    session.hostel_block = hostel_block
    active_sessions[session_id] = session
    try:
        await websocket.send_json({
            "type": "start_capture", "progress": 0,
            "message": "Get ready! Face the camera and move naturally. We'll capture frames automatically.",
            "duration": session.capture_duration, "target_frames": session.target_frames,
        })
        while True:
            try:
                frame_data = json.loads(await websocket.receive_text())
                kind = frame_data.get("type")
                if kind == "frame":
                    try:
                        image_data = base64.b64decode(frame_data["image"].split(",")[1])
                    except Exception as e:
                        logger.error(f"Error decoding image data: {e}")
                        continue
                    has_face = await asyncio.to_thread(detect_face_in_frame, image_data)
                    response = {"type": "feedback", "has_face": has_face, "frames_collected": len(session.collected_frames),
                                "target_frames": session.target_frames, "progress": session.get_progress()}
                    if session.should_capture_frame() and has_face:
                        session.collected_frames.append(image_data)
                        session.frame_count += 1
                        response["message"] = f"Capturing frames... {len(session.collected_frames)}/{session.target_frames}"
                        response["frame_captured"] = True
                        if session.is_complete():
                            response["message"] = "Capture complete! Processing frames..."
                            response["type"] = "capture_complete"
                    elif not has_face:
                        response["message"] = "Please ensure exactly one face is visible in the camera"
                        response["frame_captured"] = False
                    else:
                        response["message"] = f"Ready to capture... {len(session.collected_frames)}/{session.target_frames} frames collected"
                        response["frame_captured"] = False
                    await websocket.send_json(response)
                    if session.is_complete() and response.get("type") == "capture_complete":
                        await process_video_registration(session)
                        break
                elif kind == "start_capture":
                    session.is_capturing = True
                    session.start_time = time.time()
                    await websocket.send_json({"type": "capture_started", "progress": 0,
                                               "message": "Capture started! Move naturally and look at the camera."})
                elif kind == "complete":
                    if session.collected_frames:
                        await process_video_registration(session)
                    break
            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.error(f"Error processing frame for session {session_id}: {e}")
                try:
                    await websocket.send_json({"type": "error", "message": "Error processing frame"})
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket error for session {session_id}: {e}")
    finally:
        active_sessions.pop(session_id, None)


async def process_video_registration(session: VideoRegistrationSession) -> None:
    try:
        engine = FaceEngine.get_shared()
        frames = session.collected_frames[:30]
        await session.websocket.send_json({"type": "processing", "message": "Checking for duplicate registrations...", "progress": 90})
        duplicate = None
        for frame_bytes in frames[:5]:
            duplicate = await check_for_duplicate_face(frame_bytes, session.student_code or "__pending__")
            if duplicate:
                break
        if duplicate:
            await session.websocket.send_json({
                "type": "duplicate_detected", "duplicate_student": duplicate, "progress": 100,
                "message": f"Face already registered for {duplicate['full_name']} (ID: {duplicate['student_code']})",
            })
            return
        allocated = None
        for _ in range(5):
            candidate = generate_student_code()
            if get_student(candidate) is None:
                allocated = candidate
                break
        if not allocated:
            await session.websocket.send_json({"type": "error", "message": "Failed to allocate student ID"})
            return
        session.student_code = allocated
        await session.websocket.send_json({"type": "student_code_assigned", "student_code": allocated,
                                           "message": f"Assigned student ID {allocated}", "progress": 95})
        await session.websocket.send_json({"type": "processing", "progress": 95,
                                           "message": f"No duplicates found. Processing {len(frames)} frames..."})
        registered = failed = 0
        for i, frame_bytes in enumerate(frames):
            try:
                fid = await asyncio.to_thread(engine.index_face, frame_bytes, allocated, name=session.full_name)
                if fid:
                    registered += 1
                else:
                    failed += 1
            except FaceRejected:
                failed += 1
            except FaceServiceError as e:
                logger.error(f"Face service error on frame {i}: {e}")
                failed += 1
            await session.websocket.send_json({"type": "processing", "progress": 95 + (i + 1) / len(frames) * 5,
                                               "message": f"Registering face {i + 1}/{len(frames)}..."})
        if registered == 0:
            try:
                await asyncio.to_thread(engine.delete_person, allocated)
            except Exception:
                pass
            await session.websocket.send_json({
                "type": "registration_complete", "registered_faces": 0, "failed_faces": failed, "total_frames": len(frames),
                "success": False, "student_code": allocated, "progress": 100,
                "message": f"Registration failed! All {failed} frames were rejected by the face model.",
            })
            return
        if session.email and get_student_by_email(session.email):
            try:
                await asyncio.to_thread(engine.delete_person, allocated)
            except Exception:
                pass
            await session.websocket.send_json({"type": "error", "message": "Email already registered", "progress": 100})
            return
        try:
            if session.course_code and not repo.planner_course_get(session.course_code):
                repo.planner_course_put(code=session.course_code, students=0, sections=0)
        except Exception:
            pass
        try:
            put_student(allocated, session.full_name or "", session.class_name or "", session.email,
                        course_code=session.course_code or None, section=session.section or None,
                        residence=session.residence, hostel_block=session.hostel_block)
        except Exception as e:
            logger.error(f"Failed to persist student {allocated}: {e}")
            try:
                await asyncio.to_thread(engine.delete_person, allocated)
            except Exception:
                pass
            await session.websocket.send_json({"type": "error", "message": "Failed to save student after registering faces", "progress": 100})
            return
        message = f"Registration complete! {registered} face samples enrolled."
        if failed:
            message += f" {failed} frames were rejected by the face model."
        await session.websocket.send_json({
            "type": "registration_complete", "registered_faces": registered, "failed_faces": failed,
            "total_frames": len(frames), "success": True, "student_code": allocated, "message": message, "progress": 100,
        })
    except Exception as e:
        logger.error(f"Error processing video registration: {e}")
        try:
            await session.websocket.send_json({"type": "error", "message": "Failed to complete registration"})
        except Exception:
            pass


@router.get("/session_status/{session_id}")
@log_call()
async def get_session_status(session_id: str):
    session = active_sessions.get(session_id)
    if session is None:
        return {"active": False}
    return {"active": True, "progress": session.get_progress(), "frames_collected": len(session.collected_frames),
            "target_frames": session.target_frames, "is_capturing": session.is_capturing, "student_code": session.student_code}


@router.get("/verify_registration/{student_code}")
@log_call()
async def verify_registration(student_code: str):
    try:
        faces = FaceEngine.get_shared().get_faces_by_external_id(student_code)
    except FaceServiceError as e:
        raise HTTPException(status_code=502, detail=f"Failed to verify registration: {e}")
    return {"registered": len(faces) > 0, "face_count": len(faces), "student_code": student_code,
            "faces": [{"face_id": f.get("FaceId"), "confidence": f.get("Confidence")} for f in faces]}
