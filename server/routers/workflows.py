# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Workflows: the files, their versions, their state and their runs (RFC #620).

The contract the Workflows UI builds against (#773). A workflow is saved
as YAML (``workflow: v1``) and checked by the SDK's validator against
this site, the same check the builder and outside tools run; a file that
cannot run is refused with ``422 {errors: [{path, message}]}``, each at
the key at fault so the editor can put it on a line. Every save is a new
version, so a run says which version decided it.

States are Off, Test and On, and new workflows start in Test (actions
are recorded as "would have"). Turning On a workflow with an action that
moves things is an admin's decision; a new version whose AI steps
changed goes back to Test, as does one that gains such an action while
On under a non-admin (RFC #620, section 7). Seeing workflows needs
``workflows.view``; changing them ``workflows.manage``.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.database import get_db
from core.permissions import RequirePermission, user_has_permission
from models import User, Workflow, WorkflowRun, WorkflowVersion
from services.audit_service import audit_request
from workflows import catalog

router = APIRouter(prefix="/workflows", tags=["workflows"])

_view = RequirePermission("workflows.view")
_manage = RequirePermission("workflows.manage")

#: The largest workflow file accepted, so a paste cannot fill the table.
MAX_YAML = 64_000


class WorkflowIn(BaseModel):
    yaml: str = Field(..., min_length=1, max_length=MAX_YAML)


class StateIn(BaseModel):
    state: Literal["off", "test", "on"]


# ── shapes (#773) ──────────────────────────────────────────────────────


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).isoformat()


def _run_outcome(run: WorkflowRun) -> str | None:
    """The last outcome a step of the run reached, or None."""
    for step in reversed(run.steps):
        if step.outcome:
            return step.outcome
    return None


def run_summary(run: WorkflowRun) -> dict[str, Any]:
    return {"id": run.id, "workflow_id": run.workflow_id, "version": run.workflow_version,
            "test": run.test, "status": run.status, "trigger": run.trigger or {},
            "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at),
            "outcome": _run_outcome(run)}


def run_detail(run: WorkflowRun) -> dict[str, Any]:
    steps = []
    for s in run.steps:
        step: dict[str, Any] = {
            "seq": s.seq, "step_path": s.step_path, "kind": s.kind, "status": s.status,
            "outcome": s.outcome, "started_at": _iso(s.started_at),
            "finished_at": _iso(s.finished_at)}
        if s.kind == "ai":
            step.update(mode=s.mode, reason=s.reason, confidence=s.confidence,
                        unsure_reason=s.unsure_reason, rule=s.rule, fields=s.fields or {},
                        tool_calls=[{"id": c.id, "kind": c.kind, "name": c.name,
                                     "status": c.status, "args_asked": c.args_asked,
                                     "result": c.result, "ms": c.ms} for c in s.tool_calls])
        steps.append(step)
    return {**run_summary(run), "steps": steps}


def workflow_out(db: Session, wf: Workflow, warnings: list[Any] | None = None) -> dict[str, Any]:
    version = next((v for v in wf.versions if v.version == wf.current_version), None)
    last = (db.query(WorkflowRun).filter(WorkflowRun.workflow_id == wf.id)
            .order_by(WorkflowRun.id.desc()).first())
    out = {"id": wf.id, "name": wf.name, "state": wf.state,
           "needs_attention": wf.needs_attention, "version": wf.current_version,
           "yaml": version.yaml if version else "", "updated_at": _iso(wf.updated_at),
           "last_run": run_summary(last) if last else None,
           "needs": (version.needs or []) if version else [], "moves_things": wf.moves_things}
    if warnings is not None:
        out["warnings"] = [{"path": w.path, "message": w.message} for w in warnings]
    return out


def _refused(report) -> JSONResponse:
    return JSONResponse(status_code=422, content={
        "errors": [{"path": e.path, "message": e.message} for e in report.errors],
        "warnings": [{"path": w.path, "message": w.message} for w in report.warnings]})


def _workflow_or_404(db: Session, workflow_id: int) -> Workflow:
    wf = db.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return wf


def _ai_steps(doc: Any) -> list[Any]:
    """Every ai: step in a parsed file, wherever it is, in order."""
    found: list[Any] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "ai" in node:
                found.append(node["ai"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(doc)
    return found


def _is_admin(user: User) -> bool:
    return user_has_permission(user, "full_access")


# ── routes ─────────────────────────────────────────────────────────────


@router.get("")
async def list_workflows(db: Session = Depends(get_db), _user: User = Depends(_view)):
    return [workflow_out(db, wf) for wf in db.query(Workflow).order_by(Workflow.name).all()]


@router.get("/templates")
async def list_templates(_user: User = Depends(_view)):
    return list(catalog.templates())


@router.get("/runs/{run_id}")
async def get_run(run_id: int, db: Session = Depends(get_db), _user: User = Depends(_view)):
    run = db.get(WorkflowRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run_detail(run)


@router.post("")
async def create_workflow(body: WorkflowIn, request: Request, db: Session = Depends(get_db),
                          user: User = Depends(_manage)):
    report = catalog.check(body.yaml, db)
    if not report.ok:
        return _refused(report)
    wf = Workflow(name=str(report.doc["name"]).strip()[:200], state="test", current_version=1,
                  moves_things=report.moves_things, created_by=user.id)
    wf.versions.append(WorkflowVersion(version=1, yaml=body.yaml, created_by=user.id,
                                       needs=catalog.needs_list(report)))
    db.add(wf)
    db.commit()
    db.refresh(wf)
    audit_request(db, request, action="workflow.create", user_id=user.id,
                  entity_type="workflow", entity_id=wf.id, details={"name": wf.name})
    return workflow_out(db, wf, report.warnings)


@router.put("/{workflow_id}")
async def update_workflow(workflow_id: int, body: WorkflowIn, request: Request,
                          db: Session = Depends(get_db), user: User = Depends(_manage)):
    wf = _workflow_or_404(db, workflow_id)
    report = catalog.check(body.yaml, db)
    if not report.ok:
        return _refused(report)
    old = next((v for v in wf.versions if v.version == wf.current_version), None)
    ai_changed = old is None or _ai_steps(yaml.safe_load(old.yaml)) != _ai_steps(report.doc)
    version = wf.current_version + 1
    wf.versions.append(WorkflowVersion(version=version, yaml=body.yaml, created_by=user.id,
                                       needs=catalog.needs_list(report)))
    wf.current_version = version
    wf.name = str(report.doc["name"]).strip()[:200]
    wf.moves_things = report.moves_things
    back_to_test = wf.state == "on" and (
        ai_changed or (report.moves_things and not _is_admin(user)))
    if back_to_test:
        wf.state = "test"
    db.commit()
    db.refresh(wf)
    audit_request(db, request, action="workflow.update", user_id=user.id,
                  entity_type="workflow", entity_id=wf.id,
                  details={"version": version, "back_to_test": back_to_test})
    return workflow_out(db, wf, report.warnings)


@router.post("/{workflow_id}/state")
async def set_state(workflow_id: int, body: StateIn, request: Request,
                    db: Session = Depends(get_db), user: User = Depends(_manage)):
    wf = _workflow_or_404(db, workflow_id)
    if body.state == "on" and wf.moves_things and not _is_admin(user):
        raise HTTPException(status_code=403, detail=(
            "This workflow has an action that moves things; an admin turns it On"))
    before = wf.state
    wf.state = body.state
    db.commit()
    db.refresh(wf)
    audit_request(db, request, action="workflow.state", user_id=user.id,
                  entity_type="workflow", entity_id=wf.id,
                  details={"from": before, "to": body.state})
    return workflow_out(db, wf)


@router.get("/{workflow_id}/runs")
async def list_runs(workflow_id: int, limit: int = Query(50, ge=1, le=200),
                    before: int | None = Query(None, description="a run id: older runs only"),
                    db: Session = Depends(get_db), _user: User = Depends(_view)):
    _workflow_or_404(db, workflow_id)
    q = db.query(WorkflowRun).filter(WorkflowRun.workflow_id == workflow_id)
    if before is not None:
        q = q.filter(WorkflowRun.id < before)
    return [run_summary(r) for r in q.order_by(WorkflowRun.id.desc()).limit(limit).all()]
