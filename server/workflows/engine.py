# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The workflow engine: an alert starts a run, and the run walks its steps.

RFC #620, section 8. An app's alert on ``opennvr.alerts.>`` is matched
against every workflow that is On or in Test; for each that it triggers,
the file's ``if:`` guards and its limits are checked, a run row is
written, and the run walks ``then`` (and ``always``) top to bottom. Each
step is a ``workflow_run_steps`` row, written as it happens, so a run
explains itself in the Runs view.

What runs today:

* **If** and **For each** (over camera roles).
* **AI**, Look and Investigate, through ``workflows.ai_steps`` on the
  model in ``WORKFLOWS_LLM_URL``; a Look step is shown the alert's
  picture, shrunk first (``WORKFLOWS_LOOK_MAX_SIDE``). With no model set,
  an AI step answers ``unsure`` and the run carries on down that path.
  Investigate has no core tools yet, so it decides from the event alone.
* **Ask a person**: an approval record (``workflow_approvals``); the run
  waits, and a reply carries it on down that reply's path, after a
  restart too. A timeout takes the step's ``no_answer`` path, never a
  reply's (nobody chose it); ``inside For each`` it is not supported yet.
* The actions **log**, **alert** (into the inbox, as the workflow, with
  the run's correlation id; an AI-decided ``high`` is capped at
  ``medium``), **notify** (an inbox entry for ``who``), **app** (an
  installed app's action, as the person who answered, audited as the
  workflow's), **site_mode** and **wait**.

The other actions (save_clip, ptz_preset, webhook, home_assistant) are
recorded as "not available yet" and the path carries on.

In **Test** every action is recorded as "would have" and nothing is done;
AI steps run for real, so the trace is genuine, and Ask a person asks
nobody and takes its default (RFC #620, section 2 and corrections, 11).

A workflow never triggers on an alert from a workflow. Camera roles are
camera names, lower-cased with spaces as ``-`` ("Gate" is ``gate``),
until cameras have roles of their own. A run the engine was in the
middle of when it stopped is marked failed at the next start.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import yaml
from opennvr_app_sdk.agent import LLMClient, OllamaChat, OpenAIChat
from opennvr_app_sdk.workflows import AIStep, parse_duration
from sqlalchemy.orm import Session

from models import (
    Camera,
    Workflow,
    WorkflowApproval,
    WorkflowRun,
    WorkflowRunStep,
    WorkflowVersion,
)
from workflows.ai_steps import run_ai_step_recorded

log = logging.getLogger(__name__)

LIMIT_DEFAULTS = {"cooldown": "1m", "one_at_a_time": "camera", "per_hour": 30,
                  "ai_per_hour": 60}
NOT_YET = "not available yet in this engine"
#: How long an Ask a person step waits when it sets no timeout.
DEFAULT_ASK_TIMEOUT = "5m"
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
#: Event values a step may read, besides the alert's own scalar evidence.
_SCALAR = (str, int, float, bool)


def camera_role(name: str | None) -> str:
    """``"Front Gate"`` -> ``"front-gate"``: a camera's role, until cameras
    carry a role of their own."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def camera_num(handle: Any) -> int | None:
    text = str(handle or "").strip().lower()
    if text.startswith("cam"):
        text = text[3:].lstrip("-")
    return int(text) if text.isdigit() else None


@dataclass
class Context:
    """What a run knows: the event's values, then each AI step's."""

    values: dict[str, Any]
    camera_handle: str | None
    images: list[str] = field(default_factory=list)   # evidence store paths

    def expand(self, value: Any) -> Any:
        """``{camera}``, ``{reason}``... filled in, in strings at any depth;
        an unknown name is left as written."""
        if isinstance(value, str):
            return _PLACEHOLDER.sub(lambda m: str(self.values.get(m.group(1), m.group(0))), value)
        if isinstance(value, list):
            return [self.expand(v) for v in value]
        if isinstance(value, dict):
            return {k: self.expand(v) for k, v in value.items()}
        return value


def context_from_alert(db: Session, envelope: dict[str, Any]) -> Context:
    source = envelope.get("source") if isinstance(envelope.get("source"), dict) else {}
    evidence = envelope.get("evidence") if isinstance(envelope.get("evidence"), dict) else {}
    handle = envelope.get("camera_id")
    num = camera_num(handle)
    cam = db.get(Camera, num) if num is not None else None
    values: dict[str, Any] = {k: v for k, v in evidence.items()
                              if isinstance(v, _SCALAR) and not k.endswith("_b64")}
    values.update(app=source.get("name"), type=envelope.get("alert_type"),
                  title=envelope.get("title"), severity=envelope.get("severity"),
                  camera=camera_role(cam.name) if cam else str(handle or ""),
                  camera_name=cam.name if cam else None,
                  camera_id=str(handle) if handle else None)
    images = evidence.get("images") if isinstance(evidence.get("images"), dict) else {}
    return Context(values=values, camera_handle=str(handle) if handle else None,
                   images=[p for p in images.values() if isinstance(p, str)])


def matches_alert(doc: dict[str, Any], envelope: dict[str, Any], ctx: Context) -> bool:
    trig = (doc.get("when") or {}).get("alert")
    if not isinstance(trig, dict):
        return False
    source = envelope.get("source") if isinstance(envelope.get("source"), dict) else {}
    if trig.get("app") != source.get("name"):
        return False
    if trig.get("type") and trig["type"] != envelope.get("alert_type"):
        return False
    cams = trig.get("cameras")
    return not cams or ctx.values.get("camera") in cams


# ── conditions ─────────────────────────────────────────────────────────


def holds(db: Session, cond: Any, ctx: Context, *, now: datetime | None = None) -> bool:
    """One condition, or a list (all must hold)."""
    if isinstance(cond, list):
        return all(holds(db, c, ctx, now=now) for c in cond)
    (kind, v), = cond.items()
    if kind == "site_mode":
        from services import site_mode
        return site_mode.current_mode(db) in v
    if kind == "time":
        now = now or datetime.now().astimezone()
        t = now.strftime("%H:%M")
        start, end = v["from"], v["to"]
        return start <= t < end if start <= end else (t >= start or t < end)
    if kind == "camera":
        return ctx.values.get("camera") in v
    if kind == "value":
        return str(ctx.values.get(v["of"])) in [str(x) for x in v["in"]]
    if kind == "list":
        return False            # site lists come with Site rules; until then, not on one
    return False


# ── limits ─────────────────────────────────────────────────────────────


def limits_of(doc: dict[str, Any]) -> dict[str, Any]:
    return {**LIMIT_DEFAULTS, **(doc.get("limits") or {})}


def _run_key(limits: dict[str, Any], ctx: Context) -> str:
    one = limits["one_at_a_time"]
    if one == "none":
        return ""
    if one in ("track", "plate"):
        key = ctx.values.get("track_id" if one == "track" else "plate")
        if key not in (None, ""):
            return f"{one}:{key}"
    return f"camera:{ctx.values.get('camera')}"


def refused_by_limits(db: Session, wf: Workflow, doc: dict[str, Any], ctx: Context,
                      now: datetime) -> str | None:
    """Why a run may not start now, or None."""
    limits = limits_of(doc)
    key = _run_key(limits, ctx)
    runs = db.query(WorkflowRun).filter(WorkflowRun.workflow_id == wf.id)
    hour = runs.filter(WorkflowRun.started_at >= now - timedelta(hours=1)).count()
    if hour >= limits["per_hour"]:
        return "per_hour"
    if not key:
        return None
    same = [r for r in runs.filter(WorkflowRun.started_at
                                   >= now - timedelta(seconds=max(
                                       parse_duration(limits["cooldown"]), 3600))).all()
            if (r.trigger or {}).get("_key") == key]
    cooldown = timedelta(seconds=parse_duration(limits["cooldown"]))
    if any(r.status in ("running", "waiting") for r in same):
        return "one_at_a_time"
    if any(_aware(r.started_at) >= now - cooldown for r in same):
        return "cooldown"
    return None


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# ── the engine ─────────────────────────────────────────────────────────


def default_client() -> LLMClient | None:
    from core.config import settings
    url = (settings.workflows_llm_url or "").strip()
    if not url:
        return None
    if settings.workflows_llm_api == "openai":
        return OpenAIChat(url, model=settings.workflows_llm_model)
    return OllamaChat(url, model=settings.workflows_llm_model, think=False)


def shrink_jpeg(data: bytes, max_side: int) -> bytes:
    """The picture with its long side at most ``max_side``, through the
    ffmpeg core already ships; as it is when ffmpeg is missing or fails."""
    if not max_side or shutil.which("ffmpeg") is None:
        return data
    scale = f"scale='if(gt(iw,ih),min({max_side},iw),-2)':'if(gt(iw,ih),-2,min({max_side},ih))'"
    try:
        out = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", "pipe:0", "-vf", scale,
             "-frames:v", "1", "-q:v", "3", "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
            input=data, capture_output=True, timeout=20, check=True)
        return out.stdout or data
    except (OSError, subprocess.SubprocessError):
        log.debug("workflow: picture not shrunk", exc_info=True)
        return data


class Engine:
    """Runs workflows. ``session`` opens a database session; ``client``
    makes the AI steps' model client (None: AI steps answer unsure);
    ``now`` is the clock, for tests."""

    def __init__(self, session: Callable[[], Session], *,
                 client: Callable[[], LLMClient | None] = default_client,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC),
                 hardware: str | None = None, max_side: int | None = None) -> None:
        self.session = session
        self.client = client
        self.now = now
        self.hardware = hardware
        self.max_side = max_side
        self.tasks: set[asyncio.Task] = set()

    async def warm_up(self) -> bool:
        """Load the model before the first alert needs it: a cold load on
        CPU takes longer than a step's 120 s, so the first run after a
        start would answer unsure (over_budget). The client keeps the model
        resident once loaded."""
        client = self.client()
        if client is None:
            return False
        try:
            async with client:
                await client.chat(messages=[{"role": "user", "content": "ok"}], max_tokens=1)
            return True
        except Exception:   # an AI step will say llm_down; nothing to stop here
            log.warning("workflows: the model did not load", exc_info=True)
            return False

    # ── starting runs ──

    def start_from_alert(self, envelope: dict[str, Any]) -> list[int]:
        """The runs this alert starts (their ids); the runs themselves go on
        in the background. A workflow's own alerts start nothing."""
        source = envelope.get("source") if isinstance(envelope.get("source"), dict) else {}
        if source.get("kind") == "workflow":
            return []
        db = self.session()
        started: list[tuple[int, dict[str, Any], Context]] = []
        try:
            ctx_base = context_from_alert(db, envelope)
            for wf in db.query(Workflow).filter(Workflow.state.in_(("on", "test"))).all():
                version = (db.query(WorkflowVersion)
                           .filter(WorkflowVersion.workflow_id == wf.id,
                                   WorkflowVersion.version == wf.current_version).first())
                if version is None:
                    continue
                doc = yaml.safe_load(version.yaml)
                ctx = Context(values=dict(ctx_base.values), camera_handle=ctx_base.camera_handle,
                              images=list(ctx_base.images))
                if not matches_alert(doc, envelope, ctx):
                    continue
                if doc.get("if") is not None and not holds(db, doc["if"], ctx):
                    continue
                now = self.now()
                why = refused_by_limits(db, wf, doc, ctx, now)
                if why:
                    log.info("workflow %s: run not started (%s)", wf.name, why)
                    continue
                run = WorkflowRun(workflow_id=wf.id, workflow_version=version.version,
                                  test=wf.state == "test", status="running",
                                  trigger={**{k: v for k, v in ctx.values.items()
                                              if isinstance(v, _SCALAR) or v is None},
                                           "alert_id": envelope.get("alert_id"),
                                           "_key": _run_key(limits_of(doc), ctx)},
                                  started_at=now)
                db.add(run)
                db.commit()
                ctx.values["workflow"] = wf.name
                started.append((run.id, doc, ctx))
        finally:
            db.close()
        for run_id, doc, ctx in started:
            task = asyncio.get_event_loop().create_task(self.execute(run_id, doc, ctx))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return [r for r, _d, _c in started]

    def close_interrupted(self) -> int:
        """At start: runs left running by a stop are failed, never redone
        (an action may already have happened)."""
        db = self.session()
        try:
            stale = db.query(WorkflowRun).filter(WorkflowRun.status == "running").all()
            for run in stale:
                run.status = "failed"
                run.finished_at = self.now()
            db.commit()
            return len(stale)
        finally:
            db.close()

    # ── running ──

    async def execute(self, run_id: int, doc: dict[str, Any], ctx: Context) -> None:
        db = self.session()
        run = db.get(WorkflowRun, run_id)
        walker = _Walker(self, db, run, ctx)
        try:
            await walker.path(doc.get("then") or [], "then", ["then"], ai_decided=False)
            if doc.get("always"):
                await walker.path(doc["always"], "always", ["always"], ai_decided=False)
            run.status = "done"
            run.finished_at = self.now()
        except _WaitingError as wait:
            walker.suspend(wait)
        except Exception:
            log.exception("workflow run %s failed", run_id)
            db.rollback()
            run = db.get(WorkflowRun, run_id)
            run.status = "failed"
            run.finished_at = self.now()
        finally:
            db.commit()
            db.close()

    def answer(self, approval_id: int, reply: str | None, user_id: int | None) -> bool:
        """A person's reply, or None for a timeout: record it and carry the
        run on in the background. False when the approval is not open."""
        db = self.session()
        try:
            approval = db.get(WorkflowApproval, approval_id)
            if approval is None or approval.state != "open":
                return False
            approval.state = "answered" if reply is not None else "timed_out"
            approval.reply = reply
            approval.answered_by = user_id
            approval.answered_at = self.now()
            db.commit()
        finally:
            db.close()
        task = asyncio.get_event_loop().create_task(self.resume(approval_id))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return True

    def expire_due(self) -> list[int]:
        """Time out every open approval past its timeout; their runs take
        the step's no_answer path."""
        db = self.session()
        try:
            due = [a.id for a in db.query(WorkflowApproval)
                   .filter(WorkflowApproval.state == "open").all()
                   if _aware(a.timeout_at) <= self.now()]
        finally:
            db.close()
        return [a for a in due if self.answer(a, None, None)]

    async def resume(self, approval_id: int) -> None:
        db = self.session()
        approval = db.get(WorkflowApproval, approval_id)
        run = db.get(WorkflowRun, approval.run_id)
        state = approval.resume or {}
        ctx = Context(values=dict(state.get("values") or {}),
                      camera_handle=state.get("camera_handle"),
                      images=list(state.get("images") or []))
        if run.status != "waiting":
            # Claimed already: a run carries on once, or an action could happen twice.
            db.close()
            return
        run.status = "running"
        db.commit()
        walker = _Walker(self, db, run, ctx)
        walker.approver_id = approval.answered_by
        frames = [dict(f) for f in state.get("frames") or []]
        try:
            version = (db.query(WorkflowVersion)
                       .filter(WorkflowVersion.workflow_id == run.workflow_id,
                               WorkflowVersion.version == run.workflow_version).one())
            doc = yaml.safe_load(version.yaml)
            run.status = "running"
            branch = approval.reply if approval.reply is not None else "no_answer"
            step = db.get(WorkflowRunStep, approval.step_id) if approval.step_id else None
            if step is not None:
                step.status, step.outcome, step.finished_at = "done", branch, self.now()
                step.reason = (f"answered {branch!r}" if approval.reply is not None
                               else "no answer in time")
            db.commit()
            ctx.values["reply"] = branch
            ask_tokens = state["tokens"]
            ask_step = _resolve(doc, ask_tokens)
            try:
                await walker.path(ask_step.get(branch) or [], f"{approval.step_path}.{branch}",
                                  [*ask_tokens, branch], ai_decided=False)
            except _WaitingError as wait:
                wait.frames.extend(frames)
                raise
            for k, frame in enumerate(frames):
                try:
                    await walker.path(_resolve(doc, frame["tokens"]), frame["p"],
                                      frame["tokens"], ai_decided=frame["ai_decided"],
                                      start=frame["next"])
                except _WaitingError as wait:
                    wait.frames.extend(frames[k + 1:])
                    raise
            if frames and frames[-1]["tokens"] == ["then"] and doc.get("always"):
                await walker.path(doc["always"], "always", ["always"], ai_decided=False)
            run.status = "done"
            run.finished_at = self.now()
        except _WaitingError as wait:
            walker.suspend(wait)
        except Exception:
            log.exception("workflow run %s failed after an answer", run.id)
            db.rollback()
            run = db.get(WorkflowRun, approval.run_id)
            run.status = "failed"
            run.finished_at = self.now()
        finally:
            db.commit()
            db.close()


class _WaitingError(Exception):
    """An Ask a person step is waiting; ``frames`` are the paths to carry
    on with, innermost first, each ``{p, tokens, next, ai_decided}``."""

    def __init__(self, approval_id: int) -> None:
        super().__init__(approval_id)
        self.approval_id = approval_id
        self.frames: list[dict[str, Any]] = []


def _resolve(doc: Any, tokens: list[Any]) -> Any:
    node = doc
    for t in tokens:
        node = node[t]
    return node


class _Walker:
    def __init__(self, engine: Engine, db: Session, run: WorkflowRun, ctx: Context) -> None:
        self.engine = engine
        self.db = db
        self.run = run
        self.ctx = ctx
        self.approver_id: int | None = None
        self.in_for_each = 0

    def suspend(self, wait: _WaitingError) -> None:
        """Park the run: its approval keeps where to carry on."""
        approval = self.db.get(WorkflowApproval, wait.approval_id)
        approval.resume = {**(approval.resume or {}), "frames": wait.frames,
                           "values": {k: v for k, v in self.ctx.values.items()
                                      if isinstance(v, _SCALAR) or v is None},
                           "camera_handle": self.ctx.camera_handle, "images": self.ctx.images}
        self.run.status = "waiting"

    def record(self, path: str, kind: str, *, status: str = "done", outcome: str | None = None,
               inputs: Any = None, reason: str | None = None) -> WorkflowRunStep:
        now = self.engine.now()
        step = WorkflowRunStep(run_id=self.run.id, seq=self._next_seq(), step_path=path,
                               kind=kind, status=status, outcome=outcome, inputs=inputs,
                               reason=reason, started_at=now,
                               finished_at=None if status == "waiting" else now)
        self.db.add(step)
        self.db.commit()
        return step

    def _next_seq(self) -> int:
        last = (self.db.query(WorkflowRunStep.seq).filter(WorkflowRunStep.run_id == self.run.id)
                .order_by(WorkflowRunStep.seq.desc()).first())
        return 0 if last is None else last[0] + 1

    async def path(self, steps: list[Any], p: str, tokens: list[Any], *, ai_decided: bool,
                   start: int = 0) -> None:
        for i in range(start, len(steps)):
            try:
                await self.step(steps[i], f"{p}[{i}]", [*tokens, i], ai_decided=ai_decided)
            except _WaitingError as wait:
                wait.frames.append({"p": p, "tokens": tokens, "next": i + 1,
                                    "ai_decided": ai_decided})
                raise

    async def step(self, step: dict[str, Any], sp: str, tokens: list[Any], *,
                   ai_decided: bool) -> None:
        if "ai" in step:
            await self.ai(step["ai"], sp, tokens)
        elif "if" in step:
            yes = holds(self.db, step["if"], self.ctx)
            branch = "then" if yes else "else"
            self.record(sp, "if", outcome=branch, inputs=step["if"])
            await self.path(step.get(branch) or [], f"{sp}.{branch}", [*tokens, branch],
                            ai_decided=False)
        elif "for_each" in step:
            cams = step["for_each"]["cameras"]
            self.record(sp, "for_each", outcome="done", inputs={"cameras": cams})
            before = self.ctx.values.get("camera")
            self.in_for_each += 1
            try:
                for cam in cams:
                    self.ctx.values["camera"] = cam
                    await self.path(step.get("do") or [], f"{sp}.do", [*tokens, "do"],
                                    ai_decided=ai_decided)
            finally:
                self.in_for_each -= 1
                self.ctx.values["camera"] = before
        elif "ask_person" in step:
            await self.ask_person(step, sp, tokens)
        else:
            kind, spec = next(iter(step.items()))
            await self.action(kind, self.ctx.expand(spec), sp, ai_decided=ai_decided)

    async def ask_person(self, step: dict[str, Any], sp: str, tokens: list[Any]) -> None:
        spec = self.ctx.expand(step["ask_person"])
        default = spec.get("default")
        if self.run.test:
            # Test asks nobody and takes the default (RFC #620, corrections, 11).
            branch = default if default is not None else "no_answer"
            self.record(sp, "ask_person", outcome=branch, inputs=spec,
                        reason=f"would have asked {spec['who']}; took {branch!r} (Test)")
            self.ctx.values["reply"] = branch
            await self.path(step.get(branch) or [], f"{sp}.{branch}", [*tokens, branch],
                            ai_decided=False)
            return
        if self.in_for_each:
            self.record(sp, "ask_person", outcome="skipped", inputs=spec,
                        reason="Ask a person inside For each is not supported yet")
            return
        now = self.engine.now()
        row = self.record(sp, "ask_person", status="waiting", inputs=spec,
                          reason=f"waiting for {spec['who']}")
        timeout = parse_duration(spec.get("timeout") or DEFAULT_ASK_TIMEOUT)
        approval = WorkflowApproval(
            run_id=self.run.id, step_id=row.id, step_path=sp, question=str(spec["message"]),
            who=str(spec["who"]), replies=list(spec["replies"]), default_reply=default,
            state="open", created_at=now, timeout_at=now + timedelta(seconds=timeout),
            resume={"tokens": tokens})
        self.db.add(approval)
        self.db.commit()
        raise _WaitingError(approval.id)

    async def ai(self, raw: dict[str, Any], sp: str, tokens: list[Any]) -> None:
        engine = self.engine
        step = AIStep.from_dict(raw)
        if self._ai_over_budget():
            row = self.record(sp, "ai", outcome="unsure", reason="the workflow's AI budget for "
                              "this hour is spent")
            row.mode, row.unsure_reason = step.mode, "over_budget"
            self.db.commit()
            await self.path(raw.get("unsure") or [], f"{sp}.ai.unsure",
                            [*tokens, "ai", "unsure"], ai_decided=True)
            return
        client = engine.client()
        if client is None:
            row = self.record(sp, "ai", outcome="unsure",
                              reason="no language model is set up (WORKFLOWS_LLM_URL)")
            row.mode, row.unsure_reason = step.mode, "llm_down"
            self.db.commit()
            await self.path(raw.get("unsure") or [], f"{sp}.ai.unsure",
                            [*tokens, "ai", "unsure"], ai_decided=True)
            return
        from core.config import settings
        images = []
        if step.mode == "look" and step.input != "text":
            images = self._pictures(engine.max_side if engine.max_side is not None
                                    else settings.workflows_look_max_side)
        inputs = {k: v for k, v in self.ctx.values.items() if v is not None}
        async with client:
            _row, result = await run_ai_step_recorded(
                self.db, self.run, step, client, step_path=sp, inputs=inputs, images=images,
                hardware=engine.hardware, model_name=settings.workflows_llm_model)
        self.ctx.values.update(result.values())
        outcome = result.outcome or "unsure"
        await self.path(raw.get(outcome) or [], f"{sp}.ai.{outcome}",
                        [*tokens, "ai", outcome], ai_decided=True)

    def _ai_over_budget(self) -> bool:
        wf = self.db.get(Workflow, self.run.workflow_id)
        version = (self.db.query(WorkflowVersion)
                   .filter(WorkflowVersion.workflow_id == wf.id,
                           WorkflowVersion.version == self.run.workflow_version).first())
        budget = limits_of(yaml.safe_load(version.yaml))["ai_per_hour"]
        since = self.engine.now() - timedelta(hours=1)
        used = (self.db.query(WorkflowRunStep).join(WorkflowRun)
                .filter(WorkflowRun.workflow_id == wf.id, WorkflowRunStep.kind == "ai",
                        WorkflowRun.started_at >= since).count())
        return used >= budget

    def _pictures(self, max_side: int) -> list[bytes]:
        from services.evidence_store import resolve_evidence
        out = []
        for rel in self.ctx.images[:3]:
            path = resolve_evidence(rel)
            if path is not None and path.is_file():
                out.append(shrink_jpeg(path.read_bytes(), max_side))
        return out

    async def action(self, kind: str, spec: Any, sp: str, *, ai_decided: bool) -> None:
        if self.run.test:
            if kind == "wait":
                self.record(sp, kind, outcome="would_have", inputs=spec,
                            reason=f"would have waited {spec}")
                return
            self.record(sp, kind, outcome="would_have", inputs=spec,
                        reason=f"would have: {kind} (Test)")
            return
        if kind == "log":
            self.record(sp, kind, outcome="done", inputs=spec, reason=str(spec))
        elif kind == "alert":
            severity = spec.get("severity", "medium")
            capped = ai_decided and severity == "high"
            if capped:
                severity = "medium"    # until the step has earned high (RFC #620, section 7)
            await self._raise_alert(spec, severity)
            self.record(sp, kind, outcome="done", inputs=spec,
                        reason="capped at medium: AI-decided" if capped else None)
        elif kind == "site_mode":
            from services import site_mode
            site_mode.set_mode(self.db, str(spec), actor=f"workflow:{self.run.workflow_id}")
            self.record(sp, kind, outcome="done", inputs=spec)
        elif kind == "wait":
            await asyncio.sleep(parse_duration(spec))
            self.record(sp, kind, outcome="done", inputs=spec)
        elif kind == "notify":
            await self._raise_alert({"title": spec["message"]}, spec.get("severity", "low"),
                                    who=spec.get("who"))
            self.record(sp, kind, outcome="done", inputs=spec)
        elif kind == "app":
            outcome, why = await self._app_action(spec)
            self.record(sp, kind, outcome=outcome, inputs=spec, reason=why)
        else:
            self.record(sp, kind, outcome="skipped", inputs=spec, reason=NOT_YET)

    async def _app_action(self, spec: dict[str, Any]) -> tuple[str, str | None]:
        """An installed app's action, as the person who answered the Ask a
        person step on this path (or the workflow's author), audited as
        the workflow's. ``(outcome, why)``."""
        from fastapi import HTTPException

        from models import InstalledApp, User
        from routers.apps import call_app_action
        from services.audit_service import write_audit_log

        wf = self.db.get(Workflow, self.run.workflow_id)
        app = self.db.get(InstalledApp, spec["id"])
        if app is None or not app.enabled:
            return "failed", f"{spec['id']} is not installed and enabled"
        user_id = self.approver_id or wf.created_by
        user = self.db.get(User, user_id) if user_id else None
        if user is None:
            return "failed", "no person to act as (the approver or the workflow's author)"
        write_audit_log(self.db, action="workflow.app_action", user_id=user.id,
                        entity_type="workflow", entity_id=wf.id,
                        details={"run": self.run.id, "version": self.run.workflow_version,
                                 "app": spec["id"], "action": spec["action"],
                                 "param_keys": sorted((spec.get("args") or {}).keys())})
        try:
            await call_app_action(self.db, app, spec["action"], dict(spec.get("args") or {}),
                                  user)
        except HTTPException as err:
            return "failed", str(err.detail)[:300]
        return "done", f"as {user.username}"

    async def _raise_alert(self, spec: dict[str, Any], severity: str,
                           who: str | None = None) -> None:
        from services.alerts_inbox import apply_alert
        wf = self.db.get(Workflow, self.run.workflow_id)
        envelope = {"alert_id": f"wf-{uuid.uuid4().hex}", "title": spec.get("title"),
                    "description": (f"For {who}. " if who else "")
                    + str(self.ctx.values.get("reason") or ""), "severity": severity,
                    "source": {"kind": "workflow", "name": wf.name},
                    "camera_id": self.ctx.camera_handle, "alert_type": "workflow",
                    "correlation_id": self.run.correlation_id,
                    "evidence": {"images": dict(enumerate(self.ctx.images))} if self.ctx.images
                    else None}
        stored: dict[str, Any] = {}
        status = apply_alert(envelope, self.db, stored)
        if status != "stored":
            return
        try:
            from services.event_bus_service import publish_app_alert
            await publish_app_alert(
                camera_id=camera_num(self.ctx.camera_handle), severity=severity,
                alert_type="workflow",
                payload={"title": stored.get("title"), "alert_id": stored.get("alert_id"),
                         "id": stored.get("id"), "source": {"kind": "workflow", "name": wf.name},
                         "correlation_id": self.run.correlation_id, "images": []})
        except Exception:   # the stored alert stands; the live push is only a nudge
            log.debug("workflow alert: live push failed", exc_info=True)
