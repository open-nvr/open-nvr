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
Media Source settings router.

Stores app-level media source configuration (MediaMTX) under SecuritySetting key 'media_source'.
Superuser-only.
"""

import json

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from core.auth import get_current_superuser
from core.config import settings
from core.database import get_db
from models import SecuritySetting
from schemas import MediaSourceSettings, MediaSourceSettingsUpdate
from services.audit_service import write_audit_log

router = APIRouter(prefix="/media-source", tags=["media-source"])


def _defaults_dict() -> dict:
    # start with runtime config as defaults
    base = MediaSourceSettings(
        mediamtx_base_url=settings.mediamtx_base_url,
        mediamtx_token=settings.mediamtx_token,
        mediamtx_stream_prefix=settings.mediamtx_stream_prefix,
        mediamtx_path_mode=(settings.mediamtx_path_mode or "id").lower(),
        mediamtx_admin_api=settings.mediamtx_admin_api,
        mediamtx_admin_token=settings.mediamtx_admin_token,
        # UI toggles default
        hls_enabled=True,
        ll_hls_enabled=False,
    )
    return base.model_dump()


def _get_row(db: Session) -> SecuritySetting:
    row = (
        db.query(SecuritySetting).filter(SecuritySetting.key == "media_source").first()
    )
    if not row:
        row = SecuritySetting(
            key="media_source", json_value=json.dumps(_defaults_dict())
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


@router.get("/settings")
async def get_media_source_settings(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
):
    row = _get_row(db)
    try:
        val = json.loads(row.json_value or "{}")
    except Exception:
        val = {}
    merged = {**_defaults_dict(), **val}
    # validate
    obj = MediaSourceSettings(**merged)
    return obj.model_dump()


@router.put("/settings")
async def update_media_source_settings(
    payload: MediaSourceSettingsUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    row = _get_row(db)
    try:
        current_val = json.loads(row.json_value or "{}")
    except Exception:
        current_val = {}

    base = {**_defaults_dict(), **current_val}
    update = payload.model_dump(exclude_unset=True)

    def deep_merge(a, b):
        if isinstance(a, dict) and isinstance(b, dict):
            out = dict(a)
            for k, v in b.items():
                out[k] = deep_merge(out.get(k), v)
            return out
        return b

    merged = deep_merge(base, update)
    obj = MediaSourceSettings(**merged)
    row.json_value = json.dumps(obj.model_dump())
    db.commit()
    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="media_source",
            entity_id="media_source",
            details=payload.model_dump(exclude_unset=True),
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return obj.model_dump()


@router.post("/settings/upload")
async def upload_media_source_settings(
    cert_file: UploadFile | None = File(None, description="TLS certificate PEM file"),
    key_file: UploadFile | None = File(None, description="TLS private key PEM file"),
    ca_bundle_file: UploadFile | None = File(
        None, description="CA bundle PEM file (optional)"
    ),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    """Accept PEM files and persist their text content into media_source settings.

    This enables uploading BYOK materials directly rather than pasting PEM text.
    """
    row = _get_row(db)
    try:
        current_val = json.loads(row.json_value or "{}")
    except Exception:
        current_val = {}

    base = {**_defaults_dict(), **current_val}

    try:
        if cert_file is not None:
            cert_text = (await cert_file.read()).decode("utf-8", errors="ignore")
            base["tls_cert_pem"] = cert_text or None
        if key_file is not None:
            key_text = (await key_file.read()).decode("utf-8", errors="ignore")
            base["tls_key_pem"] = key_text or None
        if ca_bundle_file is not None:
            ca_text = (await ca_bundle_file.read()).decode("utf-8", errors="ignore")
            base["tls_ca_bundle_pem"] = ca_text or None
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"Failed to read uploaded files: {e}"
        )

    obj = MediaSourceSettings(**base)
    row.json_value = json.dumps(obj.model_dump())
    db.commit()
    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="media_source",
            entity_id="media_source",
            details={
                "tls_cert_pem": bool(cert_file),
                "tls_key_pem": bool(key_file),
                "tls_ca_bundle_pem": bool(ca_bundle_file),
            },
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return obj.model_dump()
