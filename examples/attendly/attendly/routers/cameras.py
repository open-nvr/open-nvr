from __future__ import annotations

from typing import List

from fastapi import APIRouter, HTTPException

from .. import schemas
from ..cameras import CameraSource
from ..utils.logging_utils import log_call

router = APIRouter()


@router.get("/", response_model=List[schemas.CameraOut])
@log_call()
def list_cameras():
    """OpenNVR's cameras, as rooms can be bound to them."""
    try:
        return [schemas.CameraOut(**c) for c in CameraSource.get_shared().list_cameras()]
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not list OpenNVR cameras: {e}")
