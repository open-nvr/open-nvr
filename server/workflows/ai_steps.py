# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Run an AI step inside a workflow run, and write what it did.

RFC #620, §8: "Each step's inputs, result and timing are written before
the next step starts. A restart resumes runs that were waiting." So:

* :func:`run_ai_step_recorded` writes the step row with its inputs and
  ``running`` first, runs the SDK's :func:`run_ai_step`, then writes the
  outcome and every model and tool call. A crash in between leaves a
  ``running`` row the engine can see, never a missing one.
* An ``ask`` tool leaves the step ``waiting`` with the run ``waiting``
  and the SDK's state on the row; :func:`resume_ai_step_recorded` carries
  on from that row after a restart, with the person's answer.

The step's trace is rewritten whole each time (``StepResult.trace_rows()``
is the whole trace so far), so a resumed step never doubles its rows.

The AI step never raises (the SDK's rule), and neither does this: a
failure is the ``unsure`` path with the reason on the row.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from opennvr_app_sdk.agent import LLMClient, Tool, ToolResult
from opennvr_app_sdk.workflows import AIStep, StepResult, resume_ai_step, run_ai_step
from sqlalchemy.orm import Session

from models import WorkflowRun, WorkflowRunStep, WorkflowToolCall

log = logging.getLogger(__name__)

#: The AI step's row columns, as ``StepResult.row()`` names them.
_RESULT_COLUMNS = ("mode", "outcome", "fields", "reason", "confidence", "confidence_source",
                   "self_reported", "unsure_reason", "proposed", "rule", "evidence",
                   "model", "usage", "state")


def hardware_class() -> str:
    """``"gpu"`` when the box has an accelerator the server knows of, else
    ``"cpu"``: the limits and draws an AI step gets when it sets none
    (RFC #620, corrections, 4). The default on #620's open question 2."""
    try:
        from services.hardware_profile import host_profile
        return "gpu" if host_profile().has_gpu else "cpu"
    except Exception:          # the safe side: CPU limits are the longer ones
        log.exception("hardware profile unavailable; using CPU limits")
        return "cpu"


async def run_ai_step_recorded(db: Session, run: WorkflowRun, step: AIStep, client: LLMClient, *,
                               step_path: str, inputs: Mapping[str, Any] | None = None,
                               images: Sequence[bytes | str] = (),
                               tools: Mapping[str, Tool] | Sequence[Tool] = (),
                               policy: Mapping[str, str] | None = None, site_facts: str = "",
                               hardware: str | None = None,
                               model_name: str = "llm") -> tuple[WorkflowRunStep, StepResult]:
    """Run ``step`` as the next step of ``run`` and record it."""
    row = WorkflowRunStep(run_id=run.id, seq=_next_seq(db, run), step_path=step_path, kind="ai",
                          mode=step.mode, status="running", inputs=dict(inputs or {}),
                          model=model_name)
    db.add(row)
    db.commit()
    result = await run_ai_step(step, client, inputs=inputs, images=images, tools=tools,
                               policy=policy, site_facts=site_facts,
                               hardware=hardware or hardware_class(), model_name=model_name)
    _write(db, run, row, result)
    return row, result


async def resume_ai_step_recorded(db: Session, row: WorkflowRunStep, step: AIStep,
                                  client: LLMClient, answer: ToolResult | str, *,
                                  tools: Mapping[str, Tool] | Sequence[Tool] = ()
                                  ) -> tuple[WorkflowRunStep, StepResult]:
    """Carry on a ``waiting`` AI step with the person's answer."""
    if row.status != "waiting" or not row.state:
        raise ValueError(f"step {row.id} is not waiting for a person")
    result = await resume_ai_step(step, client, row.state, answer, tools=tools)
    _write(db, row.run, row, result)
    return row, result


def _next_seq(db: Session, run: WorkflowRun) -> int:
    last = (db.query(WorkflowRunStep.seq).filter(WorkflowRunStep.run_id == run.id)
            .order_by(WorkflowRunStep.seq.desc()).first())
    return 0 if last is None else last[0] + 1


def _write(db: Session, run: WorkflowRun, row: WorkflowRunStep, result: StepResult) -> None:
    for k, v in result.row().items():
        if k in _RESULT_COLUMNS:
            setattr(row, k, v)
    waiting = result.suspended is not None
    row.status = "waiting" if waiting else "done"
    row.finished_at = None if waiting else datetime.now(UTC)
    run.status = "waiting" if waiting else "running"
    db.query(WorkflowToolCall).filter(WorkflowToolCall.run_step_id == row.id).delete()
    for seq, t in enumerate(result.trace_rows()):
        db.add(WorkflowToolCall(
            id=t["id"], run_step_id=row.id, seq=seq, kind=t["kind"], name=t["name"],
            status=t["status"], mode=t.get("mode"), args_asked=t.get("args_asked"),
            result=t.get("result"), ms=int(t.get("ms") or 0),
            started_at=datetime.fromtimestamp(float(t["started_at"]), UTC),
            tokens_in=t.get("tokens_in"), tokens_out=t.get("tokens_out")))
    db.commit()
