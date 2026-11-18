# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Try a workflow on recent events, before it runs on new ones (RFC #620).

"Pick up to 20 events from the last days and see each decision, reason
and trace." A trial replays alerts from the inbox that the workflow's
trigger matches, newest first, through the engine exactly as a Test run
would go: AI steps run for real, every action is "would have", and Ask a
person takes its default. The file's ``if:`` guards are not checked: they
read the site's mode and the clock now, not when the alert fired, and the
point of a trial is to see the steps' decisions.

A trial is not a run. Its rows carry no workflow while it works and are
deleted once its results are read off, so it never appears among the
workflow's runs and never counts against its cooldowns or budgets. AI
steps on CPU take tens of seconds each, so a trial goes on in the
background: :meth:`Trials.start` returns at once and the UI polls
:meth:`Trials.get`. Trials live in memory; a restart forgets them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from models import AppAlert, WorkflowRun
from workflows.engine import Engine, context_from_alert, matches_alert

log = logging.getLogger(__name__)

MAX_EVENTS = 20
#: Finished trials kept for reading; older ones are forgotten.
KEEP = 20


def envelope_from_inbox(row: AppAlert) -> dict[str, Any]:
    """An inbox row as the alert envelope the engine was given."""
    def loads(text: str | None) -> Any:
        try:
            return json.loads(text) if text else None
        except ValueError:
            return None

    evidence = loads(row.evidence)
    evidence = dict(evidence) if isinstance(evidence, dict) else {}
    images = loads(row.images)
    if isinstance(images, dict) and images:
        evidence["images"] = images
    return {"alert_id": row.alert_id, "title": row.title, "severity": row.severity,
            "source": {"kind": row.source_kind, "name": row.source_name},
            "camera_id": row.camera_id, "alert_type": row.alert_type, "evidence": evidence,
            "fired_at": row.fired_at.isoformat() if row.fired_at else None}


def matching_alerts(db: Session, doc: dict[str, Any], *, days: int, limit: int,
                    now: datetime) -> list[dict[str, Any]]:
    """The inbox's alerts this workflow's trigger matches, newest first.
    Alerts a workflow raised are left out, as the engine leaves them out."""
    trig = (doc.get("when") or {}).get("alert")
    if not isinstance(trig, dict):
        return []
    q = (db.query(AppAlert).filter(AppAlert.source_name == trig.get("app"),
                                   AppAlert.fired_at >= now - timedelta(days=days))
         .filter((AppAlert.source_kind != "workflow") | (AppAlert.source_kind.is_(None))))
    if trig.get("type"):
        q = q.filter(AppAlert.alert_type == trig["type"])
    out = []
    for row in q.order_by(AppAlert.fired_at.desc()).limit(limit * 10).all():
        envelope = envelope_from_inbox(row)
        if matches_alert(doc, envelope, context_from_alert(db, envelope)):
            out.append(envelope)
            if len(out) >= limit:
                break
    return out


class Trials:
    """The process's trials: started, read, and forgotten after ``KEEP``."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.trials: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def start(self, workflow_id: int, version: int | None, doc: dict[str, Any],
              envelopes: list[dict[str, Any]]) -> dict[str, Any]:
        trial = {"id": uuid.uuid4().hex, "workflow_id": workflow_id, "version": version,
                 "state": "running" if envelopes else "done", "total": len(envelopes),
                 "done": 0, "results": [],
                 "started_at": self.engine.now().isoformat()}
        self.trials[trial["id"]] = trial
        while len(self.trials) > KEEP:
            self.trials.popitem(last=False)
        if envelopes:
            task = asyncio.get_event_loop().create_task(self._run(trial, doc, envelopes))
            self.engine.tasks.add(task)
            task.add_done_callback(self.engine.tasks.discard)
        return trial

    def get(self, trial_id: str) -> dict[str, Any] | None:
        return self.trials.get(trial_id)

    async def _run(self, trial: dict[str, Any], doc: dict[str, Any],
                   envelopes: list[dict[str, Any]]) -> None:
        try:
            for envelope in envelopes:
                trial["results"].append(await self.one(doc, envelope))
                trial["done"] += 1
        except Exception:   # a trial that breaks says so; it must not take the engine down
            log.exception("workflow trial %s failed", trial["id"])
            trial["error"] = "the trial stopped on an error; the server log has it"
        trial["state"] = "done"

    async def one(self, doc: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        """One alert through the workflow as a Test run, then the run's rows
        deleted: what the steps decided, with the AI steps' traces."""
        from routers.workflows import run_detail

        engine = self.engine
        db = engine.session()
        try:
            ctx = context_from_alert(db, envelope)
            run = WorkflowRun(workflow_id=None, workflow_version=None, test=True,
                              status="running", started_at=engine.now(),
                              trigger={"alert_id": envelope.get("alert_id"), "_trial": True})
            db.add(run)
            db.commit()
            run_id = run.id
        finally:
            db.close()
        await engine.execute(run_id, doc, ctx)
        db = engine.session()
        try:
            run = db.get(WorkflowRun, run_id)
            detail = run_detail(run)
            db.delete(run)
            db.commit()
        finally:
            db.close()
        return {"alert": {"alert_id": envelope.get("alert_id"), "title": envelope.get("title"),
                          "camera": ctx.values.get("camera"),
                          "fired_at": envelope.get("fired_at")},
                "status": detail["status"], "outcome": detail["outcome"],
                "steps": detail["steps"]}


_trials: Trials | None = None


def trials() -> Trials:
    global _trials
    if _trials is None:
        from workflows.consumer import engine
        _trials = Trials(engine())
    return _trials
