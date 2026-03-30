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
Cloud Providers Router: Manages cloud provider credentials and model configurations.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.auth import get_current_user
from core.config import get_settings
from core.database import get_db
from models import CloudProviderCredential, CloudProviderModel, User
from schemas import (
    CloudProviderCredentialCreate,
    CloudProviderCredentialResponse,
    CloudProviderModelCreate,
    CloudProviderModelResponse,
    TenantQuotaResponse,
    TenantQuotaUpdate,
)
from services.credential_vault_service import CredentialVaultService
from services.tenant_quota_service import TenantQuotaService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/cloud-providers", tags=["Cloud Providers"])


# Dependency injection
def get_credential_service():
    settings = get_settings()
    return CredentialVaultService(settings)


def get_quota_service():
    return TenantQuotaService()


@router.post(
    "/credentials",
    response_model=CloudProviderCredentialResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_credential(
    credential_data: CloudProviderCredentialCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    credential_service: CredentialVaultService = Depends(get_credential_service),
):
    """
    Store a new cloud provider credential (encrypted at rest).

    - **provider**: Provider name (e.g., 'huggingface')
    - **token**: API token (will be encrypted before storage)
    - **account_info**: Optional metadata (e.g., account ID, email)
    """
    try:
        credential = credential_service.store_credential(
            db=db,
            user_id=current_user.id,
            provider=credential_data.provider,
            plaintext_token=credential_data.token,
            account_info=credential_data.account_info,
        )

        logger.info(f"User {current_user.id} created credential {credential.id}")
        return credential

    except Exception as e:
        logger.error(f"Failed to create credential: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create credential: {e!s}",
        )


@router.get("/credentials", response_model=list[CloudProviderCredentialResponse])
async def list_credentials(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    """List all cloud provider credentials for the current user (tokens not returned)."""
    stmt = (
        select(CloudProviderCredential)
        .where(CloudProviderCredential.user_id == current_user.id)
        .order_by(CloudProviderCredential.created_at.desc())
    )

    credentials = db.scalars(stmt).all()
    return credentials


@router.delete("/credentials/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credential(
    credential_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    credential_service: CredentialVaultService = Depends(get_credential_service),
):
    """Delete a cloud provider credential."""
    success = credential_service.delete_credential(
        db=db, credential_id=credential_id, user_id=current_user.id
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Credential {credential_id} not found",
        )

    logger.info(f"User {current_user.id} deleted credential {credential_id}")
    return None


@router.post(
    "/models",
    response_model=CloudProviderModelResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_model_config(
    model_data: CloudProviderModelCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Configure a cloud provider model for use.

    - **name**: Friendly name for the model
    - **provider**: Provider name (must match credential provider)
    - **credential_id**: UUID of the credential to use
    - **model_id**: Model identifier (e.g., 'Salesforce/blip-image-captioning-base')
    - **task**: Task type (e.g., 'image-classification', 'object-detection')
    - **config**: Optional model configuration as JSON string
    - **enabled**: Whether the model is enabled (default: true)
    """
    # Verify credential exists and belongs to user
    stmt = select(CloudProviderCredential).where(
        CloudProviderCredential.id == model_data.credential_id,
        CloudProviderCredential.user_id == current_user.id,
    )
    credential = db.scalar(stmt)

    if not credential:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Credential {model_data.credential_id} not found",
        )

    if credential.provider != model_data.provider:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Credential provider {credential.provider} does not match model provider {model_data.provider}",
        )

    # Create model configuration
    model = CloudProviderModel(
        user_id=current_user.id,
        name=model_data.name,
        provider=model_data.provider,
        credential_id=model_data.credential_id,
        model_id=model_data.model_id,
        task=model_data.task,
        config=model_data.config,
        enabled=model_data.enabled if model_data.enabled is not None else True,
    )

    db.add(model)
    db.commit()
    db.refresh(model)

    logger.info(f"User {current_user.id} created model config {model.id}")
    return model


@router.get("/models", response_model=list[CloudProviderModelResponse])
async def list_model_configs(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    """List all configured cloud provider models for the current user."""
    stmt = (
        select(CloudProviderModel)
        .where(CloudProviderModel.user_id == current_user.id)
        .order_by(CloudProviderModel.created_at.desc())
    )

    models = db.scalars(stmt).all()
    return models


@router.get("/models/{model_id}", response_model=CloudProviderModelResponse)
async def get_model_config(
    model_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get details of a specific model configuration."""
    stmt = select(CloudProviderModel).where(
        CloudProviderModel.id == model_id, CloudProviderModel.user_id == current_user.id
    )
    model = db.scalar(stmt)

    if not model:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Model {model_id} not found"
        )

    return model


@router.delete("/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model_config(
    model_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a model configuration."""
    stmt = select(CloudProviderModel).where(
        CloudProviderModel.id == model_id, CloudProviderModel.user_id == current_user.id
    )
    model = db.scalar(stmt)

    if not model:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Model {model_id} not found"
        )

    db.delete(model)
    db.commit()

    logger.info(f"User {current_user.id} deleted model config {model_id}")
    return None


@router.get("/quotas/{provider}", response_model=TenantQuotaResponse)
async def get_quota_usage(
    provider: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    quota_service: TenantQuotaService = Depends(get_quota_service),
):
    """Get current quota usage and limits for a provider."""
    stats = quota_service.get_usage_stats(
        db=db, user_id=current_user.id, provider=provider
    )

    if not stats:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No quota found for provider {provider}",
        )

    return stats


@router.patch("/quotas/{provider}", response_model=TenantQuotaResponse)
async def update_quota(
    provider: str,
    quota_update: TenantQuotaUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    quota_service: TenantQuotaService = Depends(get_quota_service),
):
    """
    Update quota limits for a provider.

    Requires admin privileges.
    """
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required"
        )

    quota = quota_service.update_quotas(
        db=db,
        user_id=current_user.id,
        provider=provider,
        daily_quota=quota_update.daily_quota,
        monthly_quota=quota_update.monthly_quota,
        concurrent_limit=quota_update.concurrent_limit,
    )

    return quota_service.get_usage_stats(db, current_user.id, provider)
