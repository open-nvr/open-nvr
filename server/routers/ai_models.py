# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
# 
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""
AI Models Router - Endpoints for managing AI models and KAI-C integration

This router provides endpoints for:
- Managing AI model configurations
- Testing AI Adapter connections
- Fetching available tasks from adapters
- Running inference on camera streams
"""

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.database import get_db
from models import AIDetectionResult, User
from services.audit_service import write_audit_log
from services.kai_c_service import get_kai_c_service

router = APIRouter(prefix="/ai-models", tags=["ai-models"])


# Request/Response schemas
class KaiCHealthResponse(BaseModel):
    kai_c_status: str
    adapters: dict[str, Any]
    message: str | None = None


class CapabilitiesResponse(BaseModel):
    kai_c: dict[str, Any]
    adapters: dict[str, Any]


class InferenceRequest(BaseModel):
    camera_id: int
    rtsp_url: str
    model_name: str
    task: str
    options: dict[str, Any] | None = None
    model_id: int | None = None  # Optional: link to AIModel for result storage

    class Config:
        protected_namespaces = ()


class RecordingInferenceRequest(BaseModel):
    camera_id: int
    session_id: str | None = None  # Session ID from recording-sessions endpoint
    recording_path: str | None = None  # Legacy: single recording file path
    segments: list[str] | None = None  # List of segment paths for session
    model_name: str
    task: str
    frame_interval: int = 30  # Process every Nth frame
    start_time: str | None = None  # ISO format: start of time range to analyze
    end_time: str | None = None  # ISO format: end of time range to analyze
    options: dict[str, Any] | None = None
    model_id: int | None = None  # Optional: link to AIModel for result storage

    class Config:
        protected_namespaces = ()


class InferenceResponse(BaseModel):
    status: str
    camera_id: int
    model_used: str
    task: str
    response: dict[str, Any] | None = None
    message: str | None = None

    class Config:
        protected_namespaces = ()


@router.get("/health", response_model=KaiCHealthResponse)
async def check_kai_c_health(
    current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)
):
    """
    Check if KAI-C and its configured AI Adapters are healthy.

    Users only need to know about KAI-C, not individual adapter URLs.

    Requires authenticated user.
    """
    try:
        kai_c_service = get_kai_c_service()
        health_status = await kai_c_service.check_kai_c_health()

        return KaiCHealthResponse(
            kai_c_status=health_status.get("kai_c_status", "unknown"),
            adapters=health_status.get("adapters", {}),
            message=health_status.get("message"),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to check KAI-C health: {e!s}",
        )


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def get_capabilities(
    current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)
):
    """
    Fetch all available capabilities from KAI-C.

    KAI-C queries its configured adapters and returns combined capabilities.
    Users never need to provide adapter URLs.

    Requires authenticated user.
    """
    try:
        kai_c_service = get_kai_c_service()
        capabilities = await kai_c_service.get_capabilities()

        return CapabilitiesResponse(
            kai_c=capabilities.get("kai_c", {}),
            adapters=capabilities.get("adapters", {}),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch capabilities: {e!s}",
        )


@router.post("/inference", response_model=InferenceResponse)
async def run_inference(
    request: InferenceRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Run AI inference on a camera stream.

    This endpoint:
    1. Captures a frame from the RTSP stream
    2. Sends it to KAI-C (which routes to correct adapter based on model_name)
    3. Returns the inference results

    Users never provide adapter URLs - KAI-C handles routing internally.

    Requires authenticated user.
    """
    try:
        kai_c_service = get_kai_c_service()

        result = await kai_c_service.process_inference(
            camera_id=request.camera_id,
            rtsp_url=request.rtsp_url,
            model_name=request.model_name,
            task=request.task,
            options=request.options,
        )

        if result.get("status") == "error":
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=result.get("message", "Inference failed"),
            )

        # Save detection result to database if model_id is provided
        if request.model_id:
            try:
                response_data = result.get("response", {})

                # Create detection result record
                detection_result = AIDetectionResult(
                    model_id=request.model_id,
                    camera_id=request.camera_id,
                    task=request.task,
                    label=response_data.get("label"),
                    confidence=response_data.get("confidence"),
                    bbox_x=response_data.get("bbox", [None])[0]
                    if response_data.get("bbox")
                    and len(response_data.get("bbox", [])) > 0
                    else None,
                    bbox_y=response_data.get("bbox", [None, None])[1]
                    if response_data.get("bbox")
                    and len(response_data.get("bbox", [])) > 1
                    else None,
                    bbox_width=response_data.get("bbox", [None, None, None])[2]
                    if response_data.get("bbox")
                    and len(response_data.get("bbox", [])) > 2
                    else None,
                    bbox_height=response_data.get("bbox", [None, None, None, None])[3]
                    if response_data.get("bbox")
                    and len(response_data.get("bbox", [])) > 3
                    else None,
                    count=response_data.get("count"),
                    caption=response_data.get("caption")
                    or response_data.get("description"),
                    latency_ms=response_data.get("latency_ms"),
                    annotated_image_uri=response_data.get("annotated_image_uri"),
                    executed_at=datetime.fromtimestamp(
                        response_data.get("executed_at") / 1000.0
                    )
                    if response_data.get("executed_at")
                    else None,
                )

                db.add(detection_result)
                db.commit()
            except Exception as e:
                # Don't fail inference if db save fails
                db.rollback()
                print(f"Failed to save detection result to database: {e}")

        # Log inference request
        try:
            write_audit_log(
                db,
                action="ai.inference",
                user_id=current_user.id,
                entity_type="camera",
                entity_id=request.camera_id,
                details={"model": request.model_name, "task": request.task},
            )
        except Exception:
            pass  # Don't fail if audit logging fails

        return InferenceResponse(
            status=result.get("status", "success"),
            camera_id=result.get("camera_id", request.camera_id),
            model_used=result.get("model_used", request.model_name),
            task=result.get("task", request.task),
            response=result.get("response"),
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Inference failed: {e!s}",
        )


@router.get("/schema")
async def get_task_schema(
    task: str | None = None,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Get schema documentation for tasks from KAI-C.

    KAI-C queries its configured adapters for schemas.

    Requires authenticated user.
    """
    try:
        kai_c_service = get_kai_c_service()
        schema = await kai_c_service.get_task_schema(task)
        return schema

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch schema: {e!s}",
        )


@router.post("/inference/recording")
async def run_recording_inference(
    request: RecordingInferenceRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Run AI inference on recorded video(s).

    Supports:
    - Single recording file (legacy: recording_path)
    - Recording session (session_id + segments)
    - Time-range selection (start_time, end_time)

    Results stream to database in real-time (similar to live inference).

    Requires authenticated user.
    """
    try:
        from datetime import datetime
        from pathlib import Path

        import cv2

        from services.inference_manager import get_inference_manager
        from services.storage_service import get_effective_recordings_base_path

        recordings_base = get_effective_recordings_base_path(db)
        inference_manager = get_inference_manager()

        # Check if already running
        if request.model_id and inference_manager.is_running(request.model_id):
            return {
                "status": "already_running",
                "message": "Inference already running for this model. Stop it first or wait for completion.",
            }

        # Determine which recording(s) to process
        segments_to_process = []

        if request.segments:
            # Session mode: multiple segments provided
            segments_to_process = request.segments
        elif request.recording_path:
            # Legacy mode: single recording path
            segments_to_process = [request.recording_path]
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Either 'recording_path' or 'segments' must be provided",
            )

        # Validate all segments exist
        for seg_path in segments_to_process:
            video_path = Path(recordings_base) / seg_path
            if not video_path.exists():
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Recording not found: {seg_path}",
                )

        # Parse time range if provided
        start_time_dt = None
        end_time_dt = None
        if request.start_time:
            start_time_dt = datetime.fromisoformat(
                request.start_time.replace("Z", "+00:00")
            )
        if request.end_time:
            end_time_dt = datetime.fromisoformat(
                request.end_time.replace("Z", "+00:00")
            )

        # Calculate total frames and time estimate
        total_frames = 0
        frames_to_analyze = 0

        for seg_path in segments_to_process:
            video_path = Path(recordings_base) / seg_path
            cap = cv2.VideoCapture(str(video_path))
            seg_total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = cap.get(cv2.CAP_PROP_FPS)
            cap.release()

            total_frames += seg_total_frames
            frames_to_analyze += len(
                range(0, seg_total_frames, request.frame_interval or 30)
            )

        # Parse config options
        options = request.options or {}
        config_str = json.dumps(options) if options else None

        # Start managed background inference
        if request.model_id:
            await inference_manager.start_recording_inference(
                model_id=request.model_id,
                camera_id=request.camera_id,
                recording_paths=segments_to_process,
                model_name=request.model_name,
                task=request.task,
                frame_interval=request.frame_interval or 30,
                config=config_str,
            )

        # Audit log for start
        try:
            write_audit_log(
                db,
                action="ai_inference.recording_started",
                user_id=current_user.id,
                entity_type="recording",
                entity_id=0,
                details={
                    "camera_id": request.camera_id,
                    "session_id": request.session_id,
                    "segments_count": len(segments_to_process),
                    "model": request.model_name,
                    "task": request.task,
                    "frames_to_analyze": frames_to_analyze,
                    "time_range": f"{request.start_time} - {request.end_time}"
                    if request.start_time
                    else None,
                },
            )
        except Exception:
            pass

        return {
            "status": "processing",
            "message": "Recording analysis started - results will appear in real-time on AI Detection Results page",
            "camera_id": request.camera_id,
            "session_id": request.session_id,
            "segments_count": len(segments_to_process),
            "model_used": request.model_name,
            "task": request.task,
            "total_frames": total_frames,
            "frames_to_analyze": frames_to_analyze,
            "estimated_time_seconds": frames_to_analyze
            * 2,  # Rough estimate: 2s per frame
            "time_range": {"start": request.start_time, "end": request.end_time}
            if request.start_time
            else None,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Recording inference failed: {e!s}",
        )
