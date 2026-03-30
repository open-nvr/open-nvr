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
AI Model Management Router - CRUD operations for AI models
"""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.database import get_db
from models import AIModel, Camera, CameraConfig, User
from services.audit_service import write_audit_log
from services.inference_manager import get_inference_manager
from services.storage_service import storage_service

router = APIRouter(prefix="/ai-model-management", tags=["ai-model-management"])


# Pydantic schemas
class AIModelCreate(BaseModel):
    name: str
    model_name: str  # yolov8, yolov11, blip, insightface
    task: str
    config: str | None = None
    enabled: bool = True
    source_type: str = "live"  # "live" or "recording"
    assigned_camera_id: int | None = None
    recording_path: str | None = None  # For recording source type
    inference_interval: int | None = 2


class AIModelUpdate(BaseModel):
    name: str | None = None
    model_name: str | None = None
    task: str | None = None
    config: str | None = None
    enabled: bool | None = None
    source_type: str | None = None
    assigned_camera_id: int | None = None
    recording_path: str | None = None
    inference_interval: int | None = None


class AIModelResponse(BaseModel):
    id: int
    name: str
    model_name: str
    task: str
    config: str | None = None
    enabled: bool
    source_type: str
    assigned_camera_id: int | None = None
    recording_path: str | None = None
    inference_interval: int | None = None
    created_at: datetime
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


# Create AI Model
@router.post("", response_model=AIModelResponse)
async def create_ai_model(
    model_data: AIModelCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Create a new AI model configuration."""
    try:
        # Validate JSON config if provided (treat empty string as None)
        config_value = (
            model_data.config
            if model_data.config and model_data.config.strip()
            else None
        )
        if config_value:
            try:
                json.loads(config_value)
            except json.JSONDecodeError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid JSON config",
                )

        # Validate source_type
        if model_data.source_type not in ["live", "recording"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="source_type must be 'live' or 'recording'",
            )

        # Validate source configuration
        if model_data.source_type == "live" and not model_data.assigned_camera_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="assigned_camera_id is required for live source type",
            )

        if model_data.source_type == "recording" and not model_data.recording_path:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="recording_path is required for recording source type",
            )

        # Create model
        ai_model = AIModel(
            name=model_data.name,
            model_name=model_data.model_name,
            task=model_data.task,
            config=config_value,
            enabled=model_data.enabled,
            source_type=model_data.source_type,
            assigned_camera_id=model_data.assigned_camera_id,
            recording_path=model_data.recording_path,
            inference_interval=model_data.inference_interval,
        )

        db.add(ai_model)
        db.commit()
        db.refresh(ai_model)

        # Audit log
        try:
            write_audit_log(
                db,
                action="ai_model.create",
                user_id=current_user.id,
                entity_type="ai_model",
                entity_id=ai_model.id,
                details={"name": ai_model.name, "task": ai_model.task},
            )
        except Exception:
            pass

        return ai_model

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create AI model: {e!s}",
        )


# Get all AI Models
@router.get("", response_model=list[AIModelResponse])
async def get_ai_models(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    enabled_only: bool = False,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Get all AI model configurations."""
    query = db.query(AIModel)

    if enabled_only:
        query = query.filter(AIModel.enabled == True)

    models = query.order_by(AIModel.created_at.desc()).offset(skip).limit(limit).all()
    return models


# List Recording Sessions for AI Processing (MUST be before /{model_id} routes)
@router.get("/recording-sessions")
async def list_recording_sessions_for_ai(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
    camera_id: int | None = Query(None, description="Filter by camera ID"),
    date: str | None = Query(None, description="Filter by date (YYYY-MM-DD)"),
):
    """
    List available recording sessions grouped by camera and date.
    Returns hierarchical structure: camera -> date -> sessions.
    Each session contains one or more continuous recording segments.
    """
    try:
        from services.recording_session_service import get_recording_session_service

        session_service = get_recording_session_service()
        result = session_service.list_recording_sessions(
            db=db, camera_id=camera_id, date=date
        )

        return result

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list recording sessions: {e!s}",
        )


# Legacy endpoint - kept for backward compatibility
@router.get("/recordings")
async def list_recordings_for_ai(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
    camera_id: int | None = Query(None, description="Filter by camera ID"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """
    List available recordings that can be used for AI processing.
    Returns recordings with camera info, duration, and file paths.

    DEPRECATED: Use /recording-sessions for better grouped view.
    """
    try:
        # Get recordings from storage service
        recordings = storage_service.list_recordings(
            db=db, camera_id=camera_id, limit=limit, offset=offset
        )

        # Extract camera IDs from recording paths
        camera_map = {}
        for item in recordings.get("items", []):
            # Camera name format: "cam-XX"
            cam_name = item.get("camera", "")
            if cam_name.startswith("cam-"):
                try:
                    cam_id = int(cam_name.split("-")[1])
                    if cam_id not in camera_map:
                        camera = db.query(Camera).filter(Camera.id == cam_id).first()
                        camera_map[cam_id] = {
                            "id": cam_id,
                            "name": camera.name if camera else f"Camera {cam_id}",
                        }
                except (ValueError, IndexError):
                    pass

        # Enrich recordings with camera info
        for item in recordings.get("items", []):
            cam_name = item.get("camera", "")
            if cam_name.startswith("cam-"):
                try:
                    cam_id = int(cam_name.split("-")[1])
                    item["camera_id"] = cam_id
                    item["camera_info"] = camera_map.get(cam_id)
                except (ValueError, IndexError):
                    pass

        return recordings

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list recordings: {e!s}",
        )


# Get AI Model by ID
@router.get("/{model_id}", response_model=AIModelResponse)
async def get_ai_model(
    model_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Get a specific AI model by ID."""
    ai_model = db.query(AIModel).filter(AIModel.id == model_id).first()

    if not ai_model:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AI model with ID {model_id} not found",
        )

    return ai_model


# Update AI Model
@router.put("/{model_id}", response_model=AIModelResponse)
async def update_ai_model(
    model_id: int,
    model_data: AIModelUpdate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Update an AI model configuration."""
    ai_model = db.query(AIModel).filter(AIModel.id == model_id).first()

    if not ai_model:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AI model with ID {model_id} not found",
        )

    try:
        # Validate JSON config if provided (treat empty string as None)
        if model_data.config is not None:
            config_value = model_data.config if model_data.config.strip() else None
            if config_value:
                try:
                    json.loads(config_value)
                except json.JSONDecodeError:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Invalid JSON config",
                    )
            # Override config with processed value
            model_data.config = config_value

        # Update fields
        update_data = model_data.dict(exclude_unset=True)
        for field, value in update_data.items():
            setattr(ai_model, field, value)

        db.commit()
        db.refresh(ai_model)

        # Audit log
        try:
            write_audit_log(
                db,
                action="ai_model.update",
                user_id=current_user.id,
                entity_type="ai_model",
                entity_id=ai_model.id,
                details=update_data,
            )
        except Exception:
            pass

        return ai_model

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update AI model: {e!s}",
        )


# Delete AI Model
@router.delete("/{model_id}")
async def delete_ai_model(
    model_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Delete an AI model configuration."""
    ai_model = db.query(AIModel).filter(AIModel.id == model_id).first()

    if not ai_model:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AI model with ID {model_id} not found",
        )

    try:
        model_name = ai_model.name

        db.delete(ai_model)
        db.commit()

        # Audit log
        try:
            write_audit_log(
                db,
                action="ai_model.delete",
                user_id=current_user.id,
                entity_type="ai_model",
                entity_id=model_id,
                details={"name": model_name},
            )
        except Exception:
            pass

        return {"message": f"AI model '{model_name}' deleted successfully"}

    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete AI model: {e!s}",
        )


# Start Background Inference
@router.post("/{model_id}/start-inference")
async def start_inference(
    model_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Start background inference for a model."""
    # Get model
    ai_model = db.query(AIModel).filter(AIModel.id == model_id).first()

    if not ai_model:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AI model with ID {model_id} not found",
        )

    if not ai_model.enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Model is disabled"
        )

    if not ai_model.assigned_camera_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No camera assigned to this model",
        )

    # Check if this is a cloud model (format: "cloud:1")
    is_cloud_model = ai_model.model_name.startswith("cloud:")

    if is_cloud_model:
        # Extract cloud model ID
        try:
            cloud_model_id = int(ai_model.model_name.split(":")[1])
        except (IndexError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid cloud model format: {ai_model.model_name}",
            )

        # Get cloud model configuration
        from models import CloudProviderModel

        cloud_model = (
            db.query(CloudProviderModel)
            .filter(
                CloudProviderModel.id == cloud_model_id,
                CloudProviderModel.user_id == current_user.id,
            )
            .first()
        )

        if not cloud_model:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Cloud model {cloud_model_id} not found",
            )

        # Get camera config for RTSP URL
        camera_config = (
            db.query(CameraConfig)
            .filter(CameraConfig.camera_id == ai_model.assigned_camera_id)
            .first()
        )

        if not camera_config or not camera_config.source_url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Camera does not have a source URL configured",
            )

        # Start cloud inference in background
        inference_manager = get_inference_manager()

        if inference_manager.is_running(model_id):
            return {
                "message": "Inference already running for this model",
                "running": True,
            }

        try:
            await inference_manager.start_cloud_inference(
                model_id=model_id,
                camera_id=ai_model.assigned_camera_id,
                rtsp_url=camera_config.source_url,
                cloud_model_id=cloud_model_id,
                interval=ai_model.inference_interval or 2,
                user_id=current_user.id,
            )

            # Audit log
            try:
                write_audit_log(
                    db,
                    action="ai_model.start_inference",
                    user_id=current_user.id,
                    entity_type="ai_model",
                    entity_id=model_id,
                    details={"model": ai_model.name, "type": "cloud"},
                )
            except Exception:
                pass

            return {
                "message": f"Cloud inference started for model '{ai_model.name}'",
                "running": True,
            }

        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to start cloud inference: {e!s}",
            )

    # Get camera and config (for local models)
    camera_config = (
        db.query(CameraConfig)
        .filter(CameraConfig.camera_id == ai_model.assigned_camera_id)
        .first()
    )

    if not camera_config or not camera_config.source_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Camera does not have a source URL configured",
        )

    # Start inference in background
    inference_manager = get_inference_manager()

    if inference_manager.is_running(model_id):
        return {"message": "Inference already running for this model", "running": True}

    try:
        await inference_manager.start_inference(
            model_id=model_id,
            camera_id=ai_model.assigned_camera_id,
            rtsp_url=camera_config.source_url,
            model_name=ai_model.model_name,
            task=ai_model.task,
            interval=ai_model.inference_interval or 2,
            config=ai_model.config,
        )

        # Audit log
        try:
            write_audit_log(
                db,
                action="ai_model.start_inference",
                user_id=current_user.id,
                entity_type="ai_model",
                entity_id=model_id,
                details={"model": ai_model.name},
            )
        except Exception:
            pass

        return {
            "message": f"Inference started for model '{ai_model.name}'",
            "running": True,
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to start inference: {e!s}",
        )


# Stop Background Inference
@router.post("/{model_id}/stop-inference")
async def stop_inference(
    model_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Stop background inference for a model."""
    ai_model = db.query(AIModel).filter(AIModel.id == model_id).first()

    if not ai_model:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AI model with ID {model_id} not found",
        )

    inference_manager = get_inference_manager()

    if not inference_manager.is_running(model_id):
        return {"message": "Inference is not running for this model", "running": False}

    try:
        await inference_manager.stop_inference(model_id)

        # Audit log
        try:
            write_audit_log(
                db,
                action="ai_model.stop_inference",
                user_id=current_user.id,
                entity_type="ai_model",
                entity_id=model_id,
                details={"model": ai_model.name},
            )
        except Exception:
            pass

        return {
            "message": f"Inference stopped for model '{ai_model.name}'",
            "running": False,
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to stop inference: {e!s}",
        )


# Get Inference Status
@router.get("/{model_id}/inference-status")
async def get_inference_status(
    model_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Get inference status for a model."""
    ai_model = db.query(AIModel).filter(AIModel.id == model_id).first()

    if not ai_model:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AI model with ID {model_id} not found",
        )

    inference_manager = get_inference_manager()
    running = inference_manager.is_running(model_id)

    return {"model_id": model_id, "model_name": ai_model.name, "running": running}


# Get All Running Inference Tasks
@router.get("/inference/running")
async def get_running_inference(
    current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)
):
    """Get all models with running inference."""
    inference_manager = get_inference_manager()
    running_model_ids = inference_manager.get_running_models()

    # Get model details
    models = (
        db.query(AIModel).filter(AIModel.id.in_(running_model_ids)).all()
        if running_model_ids
        else []
    )

    return {
        "running_count": len(running_model_ids),
        "models": [
            {
                "id": model.id,
                "name": model.name,
                "model_name": model.model_name,
                "task": model.task,
                "camera_id": model.assigned_camera_id,
                "interval": model.inference_interval,
            }
            for model in models
        ],
    }
