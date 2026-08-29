# Copyright (c) 2026 OpenNVR
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""RFC-0002 Phase 1: ``GET /api/v1/skills`` — the registry as an index.

The derivation itself lives in ``services/skills_registry.py`` (pure);
this router owns fetching the inputs and a short TTL cache so the view
stays cheap enough to poll from UIs without turning KAI-C probes into
load. The cache is per-process and advisory — 15s of staleness on an
*index* is fine; anything needing fresher truth (the enable gate, the
infer path) already talks to the source directly, which is exactly the
index-never-broker rule (decision 1).
"""

from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.database import get_db
from core.logging_config import main_logger
from models import InstalledApp, User
from routers.ai_models import _load_tasks_registry
from services import skill_assignments
from services.kai_c_service import get_kai_c_service
from services.skills_registry import derive_skills

router = APIRouter(prefix="/skills", tags=["skills"])

_CACHE_TTL_SECONDS = 15.0
_cache: dict[str, Any] = {"at": 0.0, "health": None, "caps": None}


async def _kai_c_view() -> tuple[Optional[dict], Optional[dict]]:
    """(adapters_health, adapters_caps) from KAI-C, each None on failure,
    TTL-cached together. Failures are logged once per fetch, not raised —
    the registry reports an unreachable source instead of 500ing."""
    now = time.monotonic()
    if now - _cache["at"] < _CACHE_TTL_SECONDS:
        return _cache["health"], _cache["caps"]
    service = get_kai_c_service()
    health: Optional[dict] = None
    caps: Optional[dict] = None
    try:
        raw = await service.check_kai_c_health()
        if raw.get("kai_c_status") == "ok" and isinstance(raw.get("adapters"), dict):
            health = raw["adapters"]
    except Exception:  # noqa: BLE001
        main_logger.debug("skills registry: adapter health fetch failed",
                          exc_info=True)
    try:
        raw = await service.get_capabilities()
        if isinstance(raw.get("adapters"), dict):
            caps = raw["adapters"]
    except Exception:  # noqa: BLE001
        main_logger.debug("skills registry: capabilities fetch failed",
                          exc_info=True)
    _cache.update({"at": now, "health": health, "caps": caps})
    return health, caps


@router.get("")
async def list_skills(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Every skill the platform knows, with live status — RFC-0002
    decision 2's view over the four sources. Consumers: the camera
    agent's skills panel, the App Catalog, and anything else that today
    reinvents this derivation privately."""
    health, caps = await _kai_c_view()
    apps_rows = db.query(InstalledApp).all()
    return derive_skills(
        tasks_registry=_load_tasks_registry(),
        adapters_health=health,
        adapters_caps=caps,
        apps_rows=apps_rows,
        assignments=skill_assignments.assignments_by_skill(db),
    )


# ── RFC-0002 Phase 2: the declarative camera-assignment surface ────
# One table, union semantics (decision 8). GET shows the union WITH the
# per-consumer claims, so a release is never a surprise; PUT/DELETE act
# on exactly one (skill, camera, consumer) claim. Releasing the last
# claim makes the skill dormant — visible immediately in GET /skills.


@router.get("/{skill_id}/cameras")
async def get_skill_cameras(
    skill_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict[str, Any]:
    return skill_assignments.skill_view(db, skill_id)


@router.put("/{skill_id}/cameras/{camera_id}")
async def declare_skill_camera(
    skill_id: str,
    camera_id: int,
    consumer: str = Body(..., embed=True),
    params: dict[str, Any] | None = Body(None, embed=True),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Declare one consumer's claim (desired state; idempotent upsert).

    The vocabulary stays open — an assignment may name a skill whose
    capability isn't installed yet (annotate-never-gate, the
    per-camera-assignment design rule); GET /skills is where its status
    shows as missing-dependency.
    """
    try:
        skill_assignments.declare(
            db, skill=skill_id, camera_id=camera_id,
            consumer=consumer, params=params,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    db.commit()
    return skill_assignments.skill_view(db, skill_id)


@router.delete("/{skill_id}/cameras/{camera_id}")
async def release_skill_camera(
    skill_id: str,
    camera_id: int,
    consumer: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Release one consumer's claim. The union shrinks by exactly this
    claim; other consumers' assignments survive (decision 8)."""
    if not skill_assignments.release(
            db, skill=skill_id, camera_id=camera_id, consumer=consumer):
        raise HTTPException(
            status_code=404,
            detail=f"no claim by {consumer!r} on camera {camera_id} "
                   f"for skill {skill_id!r}")
    db.commit()
    return skill_assignments.skill_view(db, skill_id)
