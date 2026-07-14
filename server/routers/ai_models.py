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
AI Models Router - Endpoints for managing AI models and KAI-C integration

This router provides endpoints for:
- Managing AI model configurations
- Testing AI Adapter connections
- Fetching available tasks from adapters
- Running inference on camera streams
"""

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.database import get_db
from core.logging_config import main_logger
from models import AIDetectionResult, User
from services.audit_service import write_audit_log
from services.kai_c_service import get_kai_c_service

router = APIRouter(prefix="/ai-models", tags=["ai-models"])


# Request/Response schemas
class KaiCHealthResponse(BaseModel):
    kai_c_status: str
    adapters: dict[str, Any]
    message: str | None = None


class CapabilitiesResponse(BaseModel):
    kai_c: dict[str, Any]
    adapters: dict[str, Any]


class UseCaseEntry(BaseModel):
    use_case: str
    intent: str | None = None
    needs_capability: str
    also_needs: list[str] = []
    suggested_apps: list[str] = []
    suggested_adapters: list[str] = []


USE_CASE_MAP_PATH = Path(__file__).resolve().parent.parent / "config" / "use_case_map.yml"


@lru_cache(maxsize=1)
def _load_use_case_map() -> list[UseCaseEntry]:
    raw = yaml.safe_load(USE_CASE_MAP_PATH.read_text()) or []
    return [UseCaseEntry(**entry) for entry in raw]


class TaskEntry(BaseModel):
    """One canonical task in the curated taxonomy (server/config/tasks.yml).

    ``task`` is the canonical string an adapter SHOULD advertise in
    ``tasks_advertised`` (contract §4). ``aliases`` are non-canonical
    spellings that mean the same capability — the canonicalizer folds
    them in, and the OpenNVR Agent's skill mapping treats an adapter
    advertising an alias exactly like the canonical. ``agent_skill`` names
    the Agent skill id (see/count/faces/watch) this task backs, or None.
    ``suggested_adapters`` names the reference adapter(s) that provide this
    task (editorial, consistent with ``use_case_map.yml``) — the OpenNVR
    Agent surfaces these to guide an operator to enable one when a skill is
    greyed out.
    """

    task: str
    label: str
    summary: str | None = None
    categories: list[str] = []
    tags: list[str] = []
    agent_skill: str | None = None
    aliases: list[str] = []
    suggested_adapters: list[str] = []
    suggested_apps: list[str] = []


TASKS_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "config" / "tasks.yml"


@lru_cache(maxsize=1)
def _load_tasks_registry() -> list[TaskEntry]:
    raw = yaml.safe_load(TASKS_REGISTRY_PATH.read_text()) or []
    entries = [TaskEntry(**entry) for entry in raw]
    # Fail fast on collisions. tasks.yml is a hand-edited editorial file;
    # without this, a duplicated name/alias would resolve SILENTLY and
    # inconsistently — canonicalize_task iterates in file order (first entry
    # wins) while lint_task_names builds dicts (last entry wins), so the
    # canonicalizer and the advisory log would disagree about the same
    # string. Better to refuse the file at load than map a task to the
    # wrong skill with a contradictory log line.
    canonical_by_key: dict[str, str] = {}
    for e in entries:
        key = e.task.lower()
        if key in canonical_by_key:
            raise ValueError(
                f"tasks.yml: duplicate canonical task '{e.task}'"
            )
        canonical_by_key[key] = e.task
    alias_owner: dict[str, str] = {}
    for e in entries:
        for a in e.aliases:
            key = a.lower()
            if key in canonical_by_key:
                raise ValueError(
                    f"tasks.yml: alias '{a}' of '{e.task}' collides with "
                    f"canonical task '{canonical_by_key[key]}'"
                )
            if key in alias_owner:
                raise ValueError(
                    f"tasks.yml: alias '{a}' is claimed by both "
                    f"'{alias_owner[key]}' and '{e.task}'"
                )
            alias_owner[key] = e.task
    return entries


def canonicalize_task(name: str, registry: list[TaskEntry]) -> str:
    """Map an advertised task string to its canonical name.

    Pure and reusable — no I/O. Returns the canonical ``task`` when
    ``name`` matches a canonical name (case-insensitively) or any of its
    ``aliases``; otherwise returns ``name`` unchanged (an unknown /
    free-text task registers and stays as-is, §15.1). Canonical names
    always win over aliases if the two ever collide.
    """
    key = (name or "").strip().lower()
    if not key:
        return name
    for entry in registry:
        if entry.task.lower() == key:
            return entry.task
    for entry in registry:
        if any(a.lower() == key for a in entry.aliases):
            return entry.task
    return name


def lint_task_names(advertised: list[str], registry: list[TaskEntry]) -> list[str]:
    """Human-readable warnings for a list of advertised task strings.

    Returns one message per string that is neither a canonical task nor a
    canonical name itself:

    * an alias → nudge toward the canonical spelling
      ("'scene_caption' is an alias of 'image_captioning'; prefer the
      canonical name")
    * anything unknown → note it registers but stays uncategorized
      ("'foo_bar' is not a known task — it will register but stay
      uncategorized")

    Canonical strings produce no warning. Purely advisory: nothing here
    blocks registration — free-text tasks are first-class (§15.1).
    """
    canonical = {e.task.lower(): e.task for e in registry}
    alias_to_task = {
        a.lower(): e.task for e in registry for a in e.aliases
    }

    def _display(s: str) -> str:
        # Adapter-supplied strings end up in server logs verbatim; bound the
        # length and escape newlines so a misbehaving adapter can't forge log
        # lines or inflate the log with a megabyte "task name".
        out = (s or "")[:80].replace("\r", "\\r").replace("\n", "\\n")
        return out + ("…" if s and len(s) > 80 else "")

    warnings: list[str] = []
    for raw in advertised or []:
        key = (raw or "").strip().lower()
        if not key or key in canonical:
            continue
        if key in alias_to_task:
            warnings.append(
                f"'{_display(raw)}' is an alias of '{alias_to_task[key]}'; "
                "prefer the canonical name"
            )
        else:
            warnings.append(
                f"'{_display(raw)}' is not a known task — it will register "
                "but stay uncategorized"
            )
    return warnings


# One-time-per-(adapter, taskset) lint dedupe. The /capabilities poll
# runs on a cadence; without this we'd re-emit the same advisory every
# poll. Keyed by (adapter_name, frozenset(tasks_advertised)) so a genuine
# taxonomy change (an adapter's task list drifting) re-lints, but a
# steady-state advertisement warns exactly once per process.
_lint_seen: set[tuple[str, frozenset[str]]] = set()


def lint_and_log_adapter_tasks(
    adapter_name: str,
    tasks_advertised: list[str],
    registry: list[TaskEntry],
) -> None:
    """Advisory-only: run ``lint_task_names`` over one adapter's
    ``tasks_advertised`` and LOG a warning per non-canonical string,
    prefixed with the adapter name so an operator can trace it back.
    Deduped per (adapter, taskset) so it doesn't spam every poll.

    Never raises, never blocks — an adapter advertising free-text or
    alias tasks is fully valid (§15.1); this only nudges toward the
    canonical spelling.
    """
    key = (adapter_name, frozenset(tasks_advertised or []))
    if key in _lint_seen:
        return
    # Bound the dedupe set so a long-running server with churning adapter
    # names / task sets can't grow it without limit. The set only suppresses
    # duplicate log lines, so clearing it just re-warns once — harmless.
    if len(_lint_seen) >= 512:
        _lint_seen.clear()
    _lint_seen.add(key)
    for warning in lint_task_names(tasks_advertised, registry):
        main_logger.warning("adapter %s advertises %s", adapter_name, warning)


class InferenceRequest(BaseModel):
    camera_id: int
    rtsp_url: str
    model_name: str
    task: str
    options: dict[str, Any] | None = None
    model_id: int | None = None  # Optional: link to AIModel for result storage

    class Config:
        protected_namespaces = ()


class RecordingInferenceRequest(BaseModel):
    camera_id: int
    session_id: str | None = None  # Session ID from recording-sessions endpoint
    recording_path: str | None = None  # Legacy: single recording file path
    segments: list[str] | None = None  # List of segment paths for session
    model_name: str
    task: str
    frame_interval: int = 30  # Process every Nth frame
    start_time: str | None = None  # ISO format: start of time range to analyze
    end_time: str | None = None  # ISO format: end of time range to analyze
    options: dict[str, Any] | None = None
    model_id: int | None = None  # Optional: link to AIModel for result storage

    class Config:
        protected_namespaces = ()


class InferenceResponse(BaseModel):
    status: str
    camera_id: int
    model_used: str
    task: str
    response: dict[str, Any] | None = None
    message: str | None = None

    class Config:
        protected_namespaces = ()


@router.get("/health", response_model=KaiCHealthResponse)
async def check_kai_c_health(
    current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)
):
    """
    Check if KAI-C and its configured AI Adapters are healthy.

    Users only need to know about KAI-C, not individual adapter URLs.

    Requires authenticated user.
    """
    try:
        kai_c_service = get_kai_c_service()
        health_status = await kai_c_service.check_kai_c_health()

        return KaiCHealthResponse(
            kai_c_status=health_status.get("kai_c_status", "unknown"),
            adapters=health_status.get("adapters", {}),
            message=health_status.get("message"),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to check KAI-C health: {e!s}",
        )


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def get_capabilities(
    current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)
):
    """
    Fetch all available capabilities from KAI-C.

    KAI-C queries its configured adapters and returns combined capabilities.
    Users never need to provide adapter URLs.

    Requires authenticated user.
    """
    try:
        kai_c_service = get_kai_c_service()
        capabilities = await kai_c_service.get_capabilities()

        # Advisory taxonomy lint (contract §4): make the canonical task
        # taxonomy earn its keep — when the server aggregates adapter
        # capabilities, warn (once per adapter+taskset) about any task an
        # adapter advertises under an alias or a free-text spelling.
        # Purely a log nudge; never blocks or mutates the response.
        try:
            registry = _load_tasks_registry()
            for adapter_name, entry in (capabilities.get("adapters") or {}).items():
                caps = (entry or {}).get("capabilities") or {}
                tasks = caps.get("tasks_advertised") or []
                if tasks:
                    lint_and_log_adapter_tasks(adapter_name, tasks, registry)
        except Exception:
            main_logger.debug("task-name lint skipped", exc_info=True)

        return CapabilitiesResponse(
            kai_c=capabilities.get("kai_c", {}),
            adapters=capabilities.get("adapters", {}),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch capabilities: {e!s}",
        )


@router.get("/adapters-metrics")
async def get_fleet_metrics(
    current_user: User = Depends(get_current_active_user),
):
    """Every adapter's windowed rollup in one call — the AI Adapters
    page's fleet strip (N ok / worst p95 / total rpm) reads this."""
    try:
        return await kai_c_service.get_fleet_metrics()
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="KAI-C unreachable")


@router.get("/adapters/{adapter_name}/metrics")
async def get_adapter_metrics(
    adapter_name: str,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Fetch the windowed per-adapter metrics rollup from KAI-C
    (observability spec §05): p50/p95/p99 latency, outcome counts,
    saturation gauges, and the fingerprint-change timeline.

    KAI-C collects these from each adapter's /metrics on its existing
    60s registry poll; users never talk to adapters directly.

    Requires authenticated user.
    """
    try:
        kai_c_service = get_kai_c_service()
        return await kai_c_service.get_adapter_metrics(adapter_name)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == status.HTTP_404_NOT_FOUND:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown adapter: {adapter_name}",
            )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"KAI-C returned {e.response.status_code} for adapter metrics",
        )
    except Exception as e:
        # KAI-C down / unreachable — a gateway problem, not a server bug.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch adapter metrics from KAI-C: {e!s}",
        )


class PermissionKeysRequest(BaseModel):
    """Body for the grant / revoke permission endpoints."""

    keys: list[str] = []


def _map_kai_c_permission_error(e: Exception, adapter_name: str) -> HTTPException:
    """Shared status mapping for the permission proxy routes — KAI-C's
    404 (unknown adapter) maps to a backend 404; anything else (5xx,
    connect error, timeout) is a gateway problem → 502. Mirrors the
    metrics route."""
    if isinstance(e, httpx.HTTPStatusError):
        if e.response.status_code == status.HTTP_404_NOT_FOUND:
            return HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown adapter: {adapter_name}",
            )
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"KAI-C returned {e.response.status_code} for adapter permissions",
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"Failed to reach KAI-C for adapter permissions: {e!s}",
    )


@router.get("/adapters/{adapter_name}/permissions")
async def get_adapter_permissions(
    adapter_name: str,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Fetch the permission-approval view for one adapter (§8 / §11):
    declared permission keys with labels + sovereignty-conflict flags,
    the granted set, the still-pending set, and the derived
    approval_status. Read-only — no audit log.

    Requires authenticated user.
    """
    kai_c_service = get_kai_c_service()
    try:
        return await kai_c_service.get_adapter_permissions(adapter_name)
    except Exception as e:
        raise _map_kai_c_permission_error(e, adapter_name)


@router.post("/adapters/{adapter_name}/permissions/grant")
async def grant_adapter_permissions(
    adapter_name: str,
    request: PermissionKeysRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Grant permission keys for an adapter (§8 / §11). Governance
    mutation — writes an audit log recording the actor, adapter, keys,
    and the grant_id KAI-C returns.

    Requires authenticated user.
    """
    kai_c_service = get_kai_c_service()
    try:
        result = await kai_c_service.grant_adapter_permissions(
            adapter_name, request.keys, actor=current_user.username
        )
    except Exception as e:
        raise _map_kai_c_permission_error(e, adapter_name)

    try:
        write_audit_log(
            db,
            action="adapter.permission.grant",
            user_id=current_user.id,
            entity_type="adapter",
            entity_id=adapter_name,
            details={"keys": request.keys, "grant_id": (result.get("grant_id") if isinstance(result, dict) else None)},
        )
    except Exception:
        pass  # never fail the mutation on an audit-log hiccup
    return result


@router.post("/adapters/{adapter_name}/permissions/revoke")
async def revoke_adapter_permissions(
    adapter_name: str,
    request: PermissionKeysRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Revoke permission keys for an adapter (§8 / §11). Revoking any
    key flips the adapter back to pending and stops it serving. Writes
    an audit log.

    Requires authenticated user.
    """
    kai_c_service = get_kai_c_service()
    try:
        result = await kai_c_service.revoke_adapter_permissions(
            adapter_name, request.keys, actor=current_user.username
        )
    except Exception as e:
        raise _map_kai_c_permission_error(e, adapter_name)

    try:
        write_audit_log(
            db,
            action="adapter.permission.revoke",
            user_id=current_user.id,
            entity_type="adapter",
            entity_id=adapter_name,
            details={"keys": request.keys, "grant_id": (result.get("grant_id") if isinstance(result, dict) else None)},
        )
    except Exception:
        pass
    return result


@router.post("/adapters/{adapter_name}/permissions/approve-all")
async def approve_all_adapter_permissions(
    adapter_name: str,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Grant every declared permission key for an adapter — the operator
    "approve" button (§8 / §11). Writes an audit log.

    Requires authenticated user.
    """
    kai_c_service = get_kai_c_service()
    try:
        result = await kai_c_service.approve_all_adapter_permissions(
            adapter_name, actor=current_user.username
        )
    except Exception as e:
        raise _map_kai_c_permission_error(e, adapter_name)

    try:
        write_audit_log(
            db,
            action="adapter.permission.approve",
            user_id=current_user.id,
            entity_type="adapter",
            entity_id=adapter_name,
            details={
                "keys": result.get("granted", []) if isinstance(result, dict) else [],
                "grant_id": (result.get("grant_id") if isinstance(result, dict) else None),
            },
        )
    except Exception:
        pass
    return result


@router.get("/use-cases", response_model=list[UseCaseEntry])
async def get_use_cases(
    current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)
):
    """
    The curated intent map: use case -> required capability -> suggested
    apps and adapters. Product-owned editorial content (adapters never
    declare use cases themselves); powers the capability catalog's
    use-case door.
    """
    try:
        return _load_use_case_map()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load use-case map: {e!s}",
        )


@router.get("/tasks", response_model=list[TaskEntry])
async def get_tasks(
    current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)
):
    """
    The canonical task taxonomy: for each converged task, its canonical
    string, label, categories/tags, which OpenNVR Agent skill it backs,
    and the non-canonical aliases that mean the same thing (contract §4).

    Curated + open: an adapter may still advertise any free-text task it
    likes (§15.1); this registry adds canonical names, skill mapping, and
    a lint. Product-owned editorial content, like the use-case map.
    """
    try:
        return _load_tasks_registry()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load task registry: {e!s}",
        )


@router.post("/inference", response_model=InferenceResponse)
async def run_inference(
    request: InferenceRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Run AI inference on a camera stream.

    This endpoint:
    1. Captures a frame from the RTSP stream
    2. Sends it to KAI-C (which routes to correct adapter based on model_name)
    3. Returns the inference results

    Users never provide adapter URLs - KAI-C handles routing internally.

    Requires authenticated user.
    """
    try:
        kai_c_service = get_kai_c_service()

        result = await kai_c_service.process_inference(
            camera_id=request.camera_id,
            rtsp_url=request.rtsp_url,
            model_name=request.model_name,
            task=request.task,
            options=request.options,
        )

        if result.get("status") == "error":
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=result.get("message", "Inference failed"),
            )

        # Save detection result to database if model_id is provided
        if request.model_id:
            try:
                response_data = result.get("response", {})

                # Create detection result record
                detection_result = AIDetectionResult(
                    model_id=request.model_id,
                    camera_id=request.camera_id,
                    task=request.task,
                    label=response_data.get("label"),
                    confidence=response_data.get("confidence"),
                    bbox_x=response_data.get("bbox", [None])[0]
                    if response_data.get("bbox")
                    and len(response_data.get("bbox", [])) > 0
                    else None,
                    bbox_y=response_data.get("bbox", [None, None])[1]
                    if response_data.get("bbox")
                    and len(response_data.get("bbox", [])) > 1
                    else None,
                    bbox_width=response_data.get("bbox", [None, None, None])[2]
                    if response_data.get("bbox")
                    and len(response_data.get("bbox", [])) > 2
                    else None,
                    bbox_height=response_data.get("bbox", [None, None, None, None])[3]
                    if response_data.get("bbox")
                    and len(response_data.get("bbox", [])) > 3
                    else None,
                    count=response_data.get("count"),
                    caption=response_data.get("caption")
                    or response_data.get("description"),
                    latency_ms=response_data.get("latency_ms"),
                    annotated_image_uri=response_data.get("annotated_image_uri"),
                    executed_at=datetime.fromtimestamp(
                        response_data.get("executed_at") / 1000.0
                    )
                    if response_data.get("executed_at")
                    else None,
                )

                db.add(detection_result)
                db.commit()
            except Exception:
                # Don't fail inference if db save fails. Log via the logger
                # rather than print() to stdout (avoids polluting the
                # audit/stdout stream and leaking error detail).
                db.rollback()
                from core.logging_config import main_logger
                main_logger.error(
                    "failed to save detection result to database", exc_info=True
                )

        # Log inference request
        try:
            write_audit_log(
                db,
                action="ai.inference",
                user_id=current_user.id,
                entity_type="camera",
                entity_id=request.camera_id,
                details={"model": request.model_name, "task": request.task},
            )
        except Exception:
            pass  # Don't fail if audit logging fails

        return InferenceResponse(
            status=result.get("status", "success"),
            camera_id=result.get("camera_id", request.camera_id),
            model_used=result.get("model_used", request.model_name),
            task=result.get("task", request.task),
            response=result.get("response"),
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Inference failed: {e!s}",
        )


@router.get("/schema")
async def get_task_schema(
    task: str | None = None,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Get schema documentation for tasks from KAI-C.

    KAI-C queries its configured adapters for schemas.

    Requires authenticated user.
    """
    try:
        kai_c_service = get_kai_c_service()
        schema = await kai_c_service.get_task_schema(task)
        return schema

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch schema: {e!s}",
        )


@router.post("/inference/recording")
async def run_recording_inference(
    request: RecordingInferenceRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Run AI inference on recorded video(s).

    Supports:
    - Single recording file (legacy: recording_path)
    - Recording session (session_id + segments)
    - Time-range selection (start_time, end_time)

    Results stream to database in real-time (similar to live inference).

    Requires authenticated user.
    """
    try:
        from datetime import datetime
        from pathlib import Path

        import cv2

        from services.inference_manager import get_inference_manager
        from services.storage_service import (
            RecordingPathError,
            get_effective_recordings_base_path,
            resolve_under_root,
        )

        recordings_base = get_effective_recordings_base_path(db)
        inference_manager = get_inference_manager()

        # Check if already running
        if request.model_id and inference_manager.is_running(request.model_id):
            return {
                "status": "already_running",
                "message": "Inference already running for this model. Stop it first or wait for completion.",
            }

        # Determine which recording(s) to process
        segments_to_process = []

        if request.segments:
            # Session mode: multiple segments provided
            segments_to_process = request.segments
        elif request.recording_path:
            # Legacy mode: single recording path
            segments_to_process = [request.recording_path]
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Either 'recording_path' or 'segments' must be provided",
            )

        # Validate every segment: it must stay INSIDE the recordings base
        # (V-005) and exist. seg_path is request-supplied — without
        # resolve_under_root an absolute path or a ``..`` segment would escape
        # the recordings folder and let this endpoint read/probe arbitrary
        # files off disk via cv2.VideoCapture below.
        for seg_path in segments_to_process:
            try:
                video_path = resolve_under_root(Path(recordings_base), seg_path)
            except RecordingPathError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid recording path: {seg_path}",
                ) from exc
            if not video_path.exists():
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Recording not found: {seg_path}",
                )

        # Parse time range if provided
        start_time_dt = None
        end_time_dt = None
        if request.start_time:
            start_time_dt = datetime.fromisoformat(
                request.start_time.replace("Z", "+00:00")
            )
        if request.end_time:
            end_time_dt = datetime.fromisoformat(
                request.end_time.replace("Z", "+00:00")
            )

        # Calculate total frames and time estimate
        total_frames = 0
        frames_to_analyze = 0

        for seg_path in segments_to_process:
            video_path = Path(recordings_base) / seg_path
            cap = cv2.VideoCapture(str(video_path))
            seg_total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = cap.get(cv2.CAP_PROP_FPS)
            cap.release()

            total_frames += seg_total_frames
            frames_to_analyze += len(
                range(0, seg_total_frames, request.frame_interval or 30)
            )

        # Parse config options
        options = request.options or {}
        config_str = json.dumps(options) if options else None

        # Start managed background inference
        if request.model_id:
            await inference_manager.start_recording_inference(
                model_id=request.model_id,
                camera_id=request.camera_id,
                recording_paths=segments_to_process,
                model_name=request.model_name,
                task=request.task,
                frame_interval=request.frame_interval or 30,
                config=config_str,
            )

        # Audit log for start
        try:
            write_audit_log(
                db,
                action="ai_inference.recording_started",
                user_id=current_user.id,
                entity_type="recording",
                entity_id=0,
                details={
                    "camera_id": request.camera_id,
                    "session_id": request.session_id,
                    "segments_count": len(segments_to_process),
                    "model": request.model_name,
                    "task": request.task,
                    "frames_to_analyze": frames_to_analyze,
                    "time_range": f"{request.start_time} - {request.end_time}"
                    if request.start_time
                    else None,
                },
            )
        except Exception:
            pass

        return {
            "status": "processing",
            "message": "Recording analysis started - results will appear in real-time on AI Detection Results page",
            "camera_id": request.camera_id,
            "session_id": request.session_id,
            "segments_count": len(segments_to_process),
            "model_used": request.model_name,
            "task": request.task,
            "total_frames": total_frames,
            "frames_to_analyze": frames_to_analyze,
            "estimated_time_seconds": frames_to_analyze
            * 2,  # Rough estimate: 2s per frame
            "time_range": {"start": request.start_time, "end": request.end_time}
            if request.start_time
            else None,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Recording inference failed: {e!s}",
        )
