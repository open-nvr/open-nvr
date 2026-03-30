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

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from core.auth import get_current_superuser
from core.database import get_db
from models import Integration
from schemas import IntegrationCreate, IntegrationRead, IntegrationUpdate
from services.integration_service import IntegrationService

router = APIRouter(
    prefix="/integrations",
    tags=["integrations"],
    dependencies=[Depends(get_current_superuser)],
)

logger = logging.getLogger(__name__)


@router.get("", response_model=list[IntegrationRead])
async def get_integrations(
    skip: int = 0, limit: int = 100, db: Session = Depends(get_db)
):
    """List all configured integrations."""
    return db.query(Integration).offset(skip).limit(limit).all()


@router.post("", response_model=IntegrationRead)
async def create_integration(
    integration: IntegrationCreate, db: Session = Depends(get_db)
):
    """Create a new integration."""
    db_integration = Integration(
        name=integration.name,
        type=integration.type,
        enabled=integration.enabled,
        config=integration.config,
    )
    db.add(db_integration)
    db.commit()
    db.refresh(db_integration)
    return db_integration


@router.get("/{integration_id}", response_model=IntegrationRead)
async def get_integration(integration_id: int, db: Session = Depends(get_db)):
    """Get specific integration details."""
    db_integration = (
        db.query(Integration).filter(Integration.id == integration_id).first()
    )
    if not db_integration:
        raise HTTPException(status_code=404, detail="Integration not found")
    return db_integration


@router.put("/{integration_id}", response_model=IntegrationRead)
async def update_integration(
    integration_id: int, integration: IntegrationUpdate, db: Session = Depends(get_db)
):
    """Update an integration."""
    db_integration = (
        db.query(Integration).filter(Integration.id == integration_id).first()
    )
    if not db_integration:
        raise HTTPException(status_code=404, detail="Integration not found")

    if integration.name is not None:
        db_integration.name = integration.name
    if integration.enabled is not None:
        db_integration.enabled = integration.enabled
    if integration.config is not None:
        # Deep merge or replace? For simplicity, we assume full config replacement or careful partial update by client.
        # But we'll just replace the whole dict usually.
        # If we really want to merge, we need to implement it.
        # Here we substitute.
        db_integration.config = integration.config

    db.commit()
    db.refresh(db_integration)
    return db_integration


@router.delete("/{integration_id}")
async def delete_integration(integration_id: int, db: Session = Depends(get_db)):
    """Delete an integration."""
    db_integration = (
        db.query(Integration).filter(Integration.id == integration_id).first()
    )
    if not db_integration:
        raise HTTPException(status_code=404, detail="Integration not found")

    db.delete(db_integration)
    db.commit()
    return {"status": "ok"}


@router.post("/{integration_id}/test")
async def test_integration(integration_id: int, db: Session = Depends(get_db)):
    """Test an integration by sending a dummy event."""
    integration = db.query(Integration).filter(Integration.id == integration_id).first()
    if not integration:
        raise HTTPException(status_code=404, detail="Integration not found")

    if not integration.enabled:
        raise HTTPException(status_code=400, detail="Integration is disabled")

    result = await IntegrationService.test_integration(integration)

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])

    return {"status": "ok", "message": result["message"]}
