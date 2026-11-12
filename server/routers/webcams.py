# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""USB webcams on the OpenNVR machine, as the Add Camera dialog lists them.

``GET /api/v1/webcams/local`` answers ``available: false`` on an install
where MediaMTX cannot see USB (macOS, Windows, a Linux install without
``WEBCAMS=on``); the dialog then offers the host agent instead."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from core.auth import get_current_active_user
from services.local_webcams import available, list_local_webcams

router = APIRouter(prefix="/webcams", tags=["webcams"])


@router.get("/local")
def local_webcams(_user=Depends(get_current_active_user)) -> dict:
    ok = available()
    return {"available": ok, "webcams": [w.to_wire() for w in list_local_webcams()] if ok else []}
