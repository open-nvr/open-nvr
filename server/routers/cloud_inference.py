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
Cloud Inference Router: Handles synchronous and asynchronous AI inference requests.
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.auth import get_current_user
from core.config import get_settings
from core.database import get_db
from models import AIInferenceJob, CloudInferenceResult, User
from schemas import (
    AIInferenceJobCreate,
    AIInferenceJobResponse,
    CloudInferenceRequest,
    CloudInferenceResponse,
)
from services.cloud_inference_service import CloudInferenceService
from services.credential_vault_service import CredentialVaultService
from services.tenant_quota_service import (
    CircuitBreakerOpenException,
    QuotaExceededException,
    TenantQuotaService,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/cloud-inference", tags=["Cloud Inference"])


def get_inference_service():
    settings = get_settings()
    credential_service = CredentialVaultService(settings)
    quota_service = TenantQuotaService()
    return CloudInferenceService(settings, credential_service, quota_service)


@router.post("/infer", response_model=CloudInferenceResponse)
async def run_inference(
    request: CloudInferenceRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    inference_service: CloudInferenceService = Depends(get_inference_service),
):
    """
    Execute synchronous cloud AI inference.

    - **model_id**: CloudProviderModel ID to use
    - **inputs**: Inference inputs (e.g., {"image": "https://example.com/image.jpg"})
    - **parameters**: Optional inference parameters (e.g., {"num_beams": 5})

    Returns inference result immediately (blocks until complete).
    """
    try:
        result = await inference_service.run_inference(
            db=db,
            user_id=current_user.id,
            model_id=request.model_id,
            inputs=request.inputs,
            parameters=request.parameters,
        )

        logger.info(
            f"Inference completed for user {current_user.id}, result {result.id}"
        )
        return result

    except QuotaExceededException as e:
        logger.warning(f"Quota exceeded for user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(e)
        )

    except CircuitBreakerOpenException as e:
        logger.warning(f"Circuit breaker open for user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)
        )

    except ValueError as e:
        logger.warning(f"Invalid request from user {current_user.id}: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    except Exception as e:
        logger.error(f"Inference failed for user {current_user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Inference failed: {e!s}",
        )


@router.post(
    "/jobs", response_model=AIInferenceJobResponse, status_code=status.HTTP_201_CREATED
)
async def create_async_job(
    request: AIInferenceJobCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    inference_service: CloudInferenceService = Depends(get_inference_service),
):
    """
    Create an asynchronous inference job.

    Returns immediately with a job ID. Use GET /jobs/{job_id} to check status.
    """
    try:
        job = await inference_service.create_async_job(
            db=db,
            user_id=current_user.id,
            model_id=request.model_id,
            inputs=request.inputs,
            parameters=request.parameters,
        )

        # Process job in background
        background_tasks.add_task(process_job_background, job.id, inference_service)

        logger.info(f"Created async job {job.id} for user {current_user.id}")
        return job

    except ValueError as e:
        logger.warning(f"Invalid job request from user {current_user.id}: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    except Exception as e:
        logger.error(
            f"Failed to create job for user {current_user.id}: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create job: {e!s}",
        )


@router.get("/jobs/{job_id}", response_model=AIInferenceJobResponse)
async def get_job_status(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get the status of an async inference job."""
    stmt = select(AIInferenceJob).where(
        AIInferenceJob.id == job_id, AIInferenceJob.user_id == current_user.id
    )
    job = db.scalar(stmt)

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found"
        )

    return job


@router.get("/jobs", response_model=list[AIInferenceJobResponse])
async def list_jobs(
    status_filter: str = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    List async inference jobs for the current user.

    - **status_filter**: Filter by status (queued, processing, completed, failed)
    - **limit**: Max number of jobs to return (default 50)
    """
    stmt = select(AIInferenceJob).where(AIInferenceJob.user_id == current_user.id)

    if status_filter:
        stmt = stmt.where(AIInferenceJob.status == status_filter)

    stmt = stmt.order_by(AIInferenceJob.created_at.desc()).limit(limit)
    jobs = db.scalars(stmt).all()

    return jobs


@router.get("/results/{result_id}", response_model=CloudInferenceResponse)
async def get_inference_result(
    result_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a specific inference result by ID."""
    stmt = select(CloudInferenceResult).where(
        CloudInferenceResult.id == result_id,
        CloudInferenceResult.user_id == current_user.id,
    )
    result = db.scalar(stmt)

    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Result {result_id} not found",
        )

    return result


@router.get("/results", response_model=list[CloudInferenceResponse])
async def list_inference_results(
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    List inference results for the current user.

    - **limit**: Max number of results to return (default 50)
    """
    stmt = (
        select(CloudInferenceResult)
        .where(CloudInferenceResult.user_id == current_user.id)
        .order_by(CloudInferenceResult.created_at.desc())
        .limit(limit)
    )

    results = db.scalars(stmt).all()
    return results


# Background task helper
async def process_job_background(job_id: str, inference_service: CloudInferenceService):
    """Background task to process async jobs."""
    from core.database import SessionLocal

    db = SessionLocal()
    try:
        await inference_service.process_job(db, job_id)
    except Exception as e:
        logger.error(f"Background job {job_id} processing failed: {e}", exc_info=True)
    finally:
        db.close()
