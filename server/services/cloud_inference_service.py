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
Cloud Inference Service: Orchestrates cloud AI inference requests.
"""

import json
import logging
import uuid
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import Settings
from models import (
    AIInferenceJob,
    CloudInferenceResult,
    CloudProviderModel,
)
from services.credential_vault_service import CredentialVaultService
from services.tenant_quota_service import (
    CircuitBreakerOpenException,
    QuotaExceededException,
    TenantQuotaService,
)

logger = logging.getLogger(__name__)


class CloudInferenceService:
    """Service for executing cloud AI inference requests."""

    def __init__(
        self,
        settings: Settings,
        credential_service: CredentialVaultService,
        quota_service: TenantQuotaService,
    ):
        self.settings = settings
        self.credential_service = credential_service
        self.quota_service = quota_service

    async def run_inference(
        self,
        db: Session,
        user_id: int,
        model_id: int,
        inputs: dict[str, Any],
        parameters: dict[str, Any] | None = None,
    ) -> CloudInferenceResult:
        """
        Execute synchronous cloud inference.

        Args:
            db: Database session
            user_id: User making the request
            model_id: CloudProviderModel ID
            inputs: Inference inputs (image URL, text, etc.)
            parameters: Optional inference parameters

        Returns:
            CloudInferenceResult record

        Raises:
            QuotaExceededException: If user exceeds quota
            CircuitBreakerOpenException: If circuit breaker is open
            ValueError: If model not found or unauthorized
        """
        # Get model configuration
        stmt = select(CloudProviderModel).where(
            CloudProviderModel.id == model_id, CloudProviderModel.user_id == user_id
        )
        model_config = db.scalar(stmt)

        if not model_config:
            raise ValueError(f"Model {model_id} not found or unauthorized")

        provider = model_config.provider

        # Check quota and circuit breaker
        try:
            self.quota_service.check_and_increment(db, user_id, provider)
        except (QuotaExceededException, CircuitBreakerOpenException) as e:
            logger.warning(f"Quota check failed for user {user_id}: {e}")
            raise

        # Get decrypted credential
        credential_token = self.credential_service.get_decrypted_credential(
            db, model_config.credential_id, user_id
        )

        if not credential_token:
            self.quota_service.decrement_concurrent(db, user_id, provider)
            raise ValueError(f"Credential {model_config.credential_id} not found")

        # Create result record
        result = CloudInferenceResult(
            id=str(uuid.uuid4()),
            user_id=user_id,
            credential_id=model_config.credential_id,
            model_id=model_id,
            provider=provider,
            model_identifier=model_config.model_id,
            task=model_config.task,
            status="processing",
            result_json="{}",
        )
        db.add(result)
        db.commit()
        db.refresh(result)

        # Execute inference via KAI-C
        start_time = datetime.utcnow()
        try:
            inference_result = await self._call_kai_c(
                provider=provider,
                model_name=model_config.model_id,
                task=model_config.task,
                inputs=inputs,
                parameters=parameters or {},
                credential_token=credential_token,
            )

            end_time = datetime.utcnow()
            latency_ms = int((end_time - start_time).total_seconds() * 1000)

            # Update result
            result.status = "completed"
            result.result_json = json.dumps(inference_result)
            result.latency_ms = latency_ms
            result.executed_at = end_time

            db.commit()
            db.refresh(result)

            # Record success for circuit breaker
            self.quota_service.record_success(db, user_id, provider)

            logger.info(
                f"Inference completed for user {user_id}, model {model_id}, "
                f"latency {latency_ms}ms"
            )

        except Exception as e:
            end_time = datetime.utcnow()
            latency_ms = int((end_time - start_time).total_seconds() * 1000)

            # Update result with error
            result.status = "failed"
            result.error_message = str(e)
            result.latency_ms = latency_ms
            result.executed_at = end_time

            db.commit()
            db.refresh(result)

            # Record failure for circuit breaker
            self.quota_service.record_failure(db, user_id, provider)

            logger.error(
                f"Inference failed for user {user_id}, model {model_id}: {e}",
                exc_info=True,
            )
            raise

        finally:
            # Always decrement concurrent usage
            self.quota_service.decrement_concurrent(db, user_id, provider)

        return result

    async def create_async_job(
        self,
        db: Session,
        user_id: int,
        model_id: int,
        inputs: dict[str, Any],
        parameters: dict[str, Any] | None = None,
    ) -> AIInferenceJob:
        """
        Create an async inference job.

        Args:
            db: Database session
            user_id: User making the request
            model_id: CloudProviderModel ID
            inputs: Inference inputs
            parameters: Optional inference parameters

        Returns:
            AIInferenceJob record in 'queued' state
        """
        # Get model configuration
        stmt = select(CloudProviderModel).where(
            CloudProviderModel.id == model_id, CloudProviderModel.user_id == user_id
        )
        model_config = db.scalar(stmt)

        if not model_config:
            raise ValueError(f"Model {model_id} not found or unauthorized")

        # Create job record
        job = AIInferenceJob(
            id=str(uuid.uuid4()),
            user_id=user_id,
            credential_id=model_config.credential_id,
            provider=model_config.provider,
            model_id=model_config.model_id,
            task=model_config.task,
            inputs_json=json.dumps(inputs),
            options_json=json.dumps(parameters or {}),
            status="queued",
        )

        db.add(job)
        db.commit()
        db.refresh(job)

        logger.info(f"Created async job {job.id} for user {user_id}, model {model_id}")

        return job

    async def process_job(self, db: Session, job_id: str) -> AIInferenceJob:
        """
        Process an async job (called by background worker).

        Args:
            db: Database session
            job_id: Job UUID

        Returns:
            Updated job record
        """
        stmt = select(AIInferenceJob).where(AIInferenceJob.id == job_id)
        job = db.scalar(stmt)

        if not job:
            raise ValueError(f"Job {job_id} not found")

        if job.status != "queued":
            logger.warning(f"Job {job_id} already processed with status {job.status}")
            return job

        # Update status to processing
        job.status = "processing"
        job.started_at = datetime.utcnow()
        db.commit()

        try:
            # Parse inputs and options
            inputs = (
                json.loads(job.inputs_json)
                if isinstance(job.inputs_json, str)
                else job.inputs_json
            )
            parameters = (
                json.loads(job.options_json)
                if isinstance(job.options_json, str)
                else job.options_json or {}
            )

            # Get decrypted credential
            credential_token = self.credential_service.get_decrypted_credential(
                db, job.credential_id, job.user_id
            )

            if not credential_token:
                raise ValueError(f"Credential {job.credential_id} not found")

            # Create result record
            result = CloudInferenceResult(
                id=str(uuid.uuid4()),
                user_id=job.user_id,
                credential_id=job.credential_id,
                model_id=None,  # No FK reference for async jobs
                provider=job.provider,
                model_identifier=job.model_id,
                task=job.task,
                status="processing",
                result_json="{}",
            )
            db.add(result)
            db.commit()
            db.refresh(result)

            # Execute inference via KAI-C
            start_time = datetime.utcnow()
            inference_result = await self._call_kai_c(
                provider=job.provider,
                model_name=job.model_id,
                task=job.task,
                inputs=inputs,
                parameters=parameters,
                credential_token=credential_token,
            )

            end_time = datetime.utcnow()
            latency_ms = int((end_time - start_time).total_seconds() * 1000)

            # Update result
            result.status = "completed"
            result.result_json = json.dumps(inference_result)
            result.latency_ms = latency_ms
            result.executed_at = end_time
            db.commit()

            # Update job with result
            job.status = "completed"
            job.result_id = result.id
            job.completed_at = datetime.utcnow()

            db.commit()
            db.refresh(job)

            logger.info(f"Job {job_id} completed with result {result.id}")

        except Exception as e:
            job.status = "failed"
            job.error_message = str(e)
            job.completed_at = datetime.utcnow()

            db.commit()
            db.refresh(job)

            logger.error(f"Job {job_id} failed: {e}", exc_info=True)

        return job

    async def _call_kai_c(
        self,
        provider: str,
        model_name: str,
        task: str,
        inputs: dict[str, Any],
        parameters: dict[str, Any],
        credential_token: str,
    ) -> dict[str, Any]:
        """
        Call KAI-C orchestrator for inference.

        Args:
            provider: Provider name (e.g., 'huggingface')
            model_name: Model identifier
            task: Task type
            inputs: Inference inputs
            parameters: Inference parameters
            credential_token: Decrypted API token

        Returns:
            Inference result as JSON dict

        Raises:
            httpx.HTTPError: If request fails
        """
        url = f"{self.settings.kai_c_url}/infer/cloud"

        payload = {
            "provider": provider,
            "model_name": model_name,
            "task": task,
            "inputs": inputs,
            "parameters": parameters,
            "credential_token": credential_token,
        }

        headers = {"X-Internal-API-Key": self.settings.internal_api_key or ""}

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()
