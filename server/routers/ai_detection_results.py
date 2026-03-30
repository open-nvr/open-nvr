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
AI Detection Results Router - Store and retrieve detection results
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.database import get_db
from models import AIDetectionResult, AIModel, User

router = APIRouter(prefix="/ai-detection-results", tags=["ai-detection-results"])


# Pydantic schemas
class AIDetectionResultCreate(BaseModel):
    model_id: int
    camera_id: int | None = None
    task: str
    label: str | None = None
    confidence: float | None = None
    bbox_x: int | None = None
    bbox_y: int | None = None
    bbox_width: int | None = None
    bbox_height: int | None = None
    count: int | None = None
    caption: str | None = None
    latency_ms: int | None = None
    annotated_image_uri: str | None = None
    executed_at: datetime | None = None


class AIDetectionResultResponse(BaseModel):
    id: int
    model_id: int
    camera_id: int | None = None
    task: str
    label: str | None = None
    confidence: float | None = None
    bbox_x: int | None = None
    bbox_y: int | None = None
    bbox_width: int | None = None
    bbox_height: int | None = None
    count: int | None = None
    caption: str | None = None
    latency_ms: int | None = None
    annotated_image_uri: str | None = None
    executed_at: datetime | None = None
    created_at: datetime

    # Include model name for display
    model_name: str | None = None

    class Config:
        from_attributes = True


# Create Detection Result
@router.post("", response_model=AIDetectionResultResponse)
async def create_detection_result(
    result_data: AIDetectionResultCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Store a detection result."""
    try:
        # Verify model exists
        ai_model = db.query(AIModel).filter(AIModel.id == result_data.model_id).first()
        if not ai_model:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"AI model with ID {result_data.model_id} not found",
            )

        # Create detection result
        detection_result = AIDetectionResult(**result_data.dict())

        db.add(detection_result)
        db.commit()
        db.refresh(detection_result)

        # Add model name to response
        response = AIDetectionResultResponse.from_orm(detection_result)
        response.model_name = ai_model.name

        return response

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create detection result: {e!s}",
        )


# Get Detection Results with filters
@router.get("", response_model=list[AIDetectionResultResponse])
async def get_detection_results(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    model_id: int | None = None,
    camera_id: int | None = None,
    task: str | None = None,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Get detection results with optional filters."""
    query = db.query(AIDetectionResult).join(AIModel)

    if model_id:
        query = query.filter(AIDetectionResult.model_id == model_id)
    if camera_id:
        query = query.filter(AIDetectionResult.camera_id == camera_id)
    if task:
        query = query.filter(AIDetectionResult.task == task)

    results = (
        query.order_by(AIDetectionResult.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )

    # Add model names to responses
    response_list = []
    for result in results:
        response = AIDetectionResultResponse.from_orm(result)
        response.model_name = result.model.name if result.model else None
        response_list.append(response)

    return response_list


# Get Detection Result by ID
@router.get("/{result_id}", response_model=AIDetectionResultResponse)
async def get_detection_result(
    result_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Get a specific detection result by ID."""
    result = (
        db.query(AIDetectionResult).filter(AIDetectionResult.id == result_id).first()
    )

    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Detection result with ID {result_id} not found",
        )

    response = AIDetectionResultResponse.from_orm(result)
    response.model_name = result.model.name if result.model else None

    return response


# Delete Detection Result
@router.delete("/{result_id}")
async def delete_detection_result(
    result_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Delete a detection result."""
    result = (
        db.query(AIDetectionResult).filter(AIDetectionResult.id == result_id).first()
    )

    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Detection result with ID {result_id} not found",
        )

    try:
        db.delete(result)
        db.commit()

        return {"message": "Detection result deleted successfully"}

    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete detection result: {e!s}",
        )


# Bulk delete old results
@router.delete("/bulk/older-than/{days}")
async def delete_old_results(
    days: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Delete detection results older than specified days."""
    try:
        from datetime import timedelta

        cutoff_date = datetime.utcnow() - timedelta(days=days)

        deleted_count = (
            db.query(AIDetectionResult)
            .filter(AIDetectionResult.created_at < cutoff_date)
            .delete()
        )

        db.commit()

        return {"message": f"Deleted {deleted_count} old detection results"}

    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete old results: {e!s}",
        )
