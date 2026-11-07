# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Approvals: a workflow's Ask a person, answered by a person (RFC #620).

Their own records with their own permission, ``workflows.approve``: the
inbox's acknowledge-all never touches them, and a viewer cannot answer.
Answering also needs the run's camera, the same rule as seeing it. A
reply carries the waiting run on down that reply's path (#773).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.database import get_db
from core.permissions import RequirePermission
from models import User, Workflow, WorkflowApproval, WorkflowRun
from services.audit_service import audit_request

router = APIRouter(prefix="/approvals", tags=["workflows"])

_approve = RequirePermission("workflows.approve")


class ReplyIn(BaseModel):
    reply: str = Field(..., min_length=1, max_length=120)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).isoformat()


def approval_out(db: Session, a: WorkflowApproval) -> dict[str, Any]:
    run = db.get(WorkflowRun, a.run_id)
    wf = db.get(Workflow, run.workflow_id) if run and run.workflow_id else None
    return {"id": a.id, "run_id": a.run_id, "workflow_name": wf.name if wf else None,
            "question": a.question, "who": a.who, "replies": list(a.replies or []),
            "created_at": _iso(a.created_at), "timeout_at": _iso(a.timeout_at),
            "default_reply": a.default_reply, "state": a.state, "reply": a.reply,
            "answered_at": _iso(a.answered_at),
            "camera": (run.trigger or {}).get("camera") if run else None}


def _may_see_run_camera(db: Session, user: User, run: WorkflowRun | None) -> bool:
    from services.camera_scope import camera_id_from_handle, can_view_camera
    cam = camera_id_from_handle((run.trigger or {}).get("camera_id")) if run else None
    return cam is None or can_view_camera(db, user, cam)


def get_engine():
    from workflows.consumer import engine
    return engine()


@router.get("")
async def list_approvals(open: bool = Query(True), db: Session = Depends(get_db),
                         user: User = Depends(_approve)):
    q = db.query(WorkflowApproval)
    if open:
        q = q.filter(WorkflowApproval.state == "open")
    rows = q.order_by(WorkflowApproval.id.desc()).limit(200).all()
    return [approval_out(db, a) for a in rows
            if _may_see_run_camera(db, user, db.get(WorkflowRun, a.run_id))]


@router.post("/{approval_id}")
async def answer(approval_id: int, body: ReplyIn, request: Request,
                 db: Session = Depends(get_db), user: User = Depends(_approve)):
    a = db.get(WorkflowApproval, approval_id)
    if a is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    if not _may_see_run_camera(db, user, db.get(WorkflowRun, a.run_id)):
        raise HTTPException(status_code=403, detail="Not permitted for this camera")
    if a.state != "open":
        raise HTTPException(status_code=409, detail=f"Already {a.state.replace('_', ' ')}")
    if body.reply not in (a.replies or []):
        raise HTTPException(status_code=422, detail=f"Reply is one of {a.replies}")
    if not get_engine().answer(a.id, body.reply, user.id):
        raise HTTPException(status_code=409, detail="Already answered")
    audit_request(db, request, action="workflow.approval", user_id=user.id,
                  entity_type="workflow_approval", entity_id=a.id,
                  details={"run": a.run_id, "reply": body.reply})
    db.expire_all()
    return approval_out(db, db.get(WorkflowApproval, approval_id))
