# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""An AI step inside a workflow run: from the YAML to the stored rows."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import types as _types
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_workflow_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

_lm = _types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules.setdefault("core.logging_config", _lm)

from opennvr_app_sdk.agent import (  # noqa: E402
    ChatResult,
    LLMUnreachableError,
    Tool,
    ToolCall,
)
from opennvr_app_sdk.workflows import AIStep  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.database import Base  # noqa: E402
from models import WorkflowRun, WorkflowRunStep, WorkflowToolCall  # noqa: E402
from workflows import ai_steps  # noqa: E402
from workflows.ai_steps import (  # noqa: E402
    hardware_class,
    resume_ai_step_recorded,
    run_ai_step_recorded,
)

GATE = """
name: Gate concierge
then:
  - ai:
      mode: look
      prompt: Who is at the gate?
      decision: [delivery, cab, visitor]
      fields: { company: [dhl, ups, other, none] }
      delivery: [ notify: { who: role:guard, message: "{company} at the gate" } ]
      cab: []
      visitor: [ ask_person: { who: role:guard, message: "{reason}" } ]
      unsure: [ log: "{reason}" ]
"""

NIGHT = """
mode: investigate
goal: Decide whether the person in the yard is a threat.
tools: [describe_camera, recognize_faces]
decision: [ignore, alert]
fields: { who: [staff, intruder, unknown] }
rules:
  - { when: { who: intruder }, then: alert }
  - { when: { who: staff }, then: ignore }
ignore: []
alert: [ alert: { severity: medium, title: "Intruder on {camera}" } ]
unsure: []
"""


class Script:
    """A model that answers from a list: a dict as JSON, a list as tool
    calls, an exception raised."""

    def __init__(self, *turns):
        self.turns = list(turns)

    async def chat(self, *, messages, tools=None, format=None, temperature=0.2, max_tokens=256):
        t = self.turns.pop(0)
        if isinstance(t, Exception):
            raise t
        if isinstance(t, list):
            calls = [ToolCall(name=n, arguments=a, id=f"c{i}") for i, (n, a) in enumerate(t)]
            return ChatResult(content="", tool_calls=calls,
                              message={"role": "assistant", "content": ""})
        content = t if isinstance(t, str) else json.dumps(t)
        return ChatResult(content=content, prompt_tokens=40, completion_tokens=8,
                          message={"role": "assistant", "content": content})


def tool(name, mode="auto"):
    async def handler(args):
        return "yard: a guard in a vest"
    return Tool(name=name, description=name, handler=handler, mode=mode,
                effect="ask_person" if mode == "ask" else "read",
                parameters={"type": "object", "properties": {"camera_id": {"type": "string"}}})


@pytest.fixture()
def sessions(tmp_path):
    # A file, not :memory:, so a second session sees the first one's rows
    # the way a restarted engine does.
    eng = create_engine(f"sqlite:///{tmp_path / 'wf.db'}", poolclass=StaticPool)
    Base.metadata.create_all(eng)
    yield sessionmaker(bind=eng)
    eng.dispose()


@pytest.fixture()
def db(sessions):
    s = sessions()
    yield s
    s.close()


def new_run(db, **kw) -> WorkflowRun:
    run = WorkflowRun(trigger={"type": "plate.read.v1", "plate": "AB12 CDE"}, **kw)
    db.add(run)
    db.commit()
    return run


def look_step() -> AIStep:
    return AIStep.from_dict(yaml.safe_load(GATE)["then"][0]["ai"])


def test_a_look_step_from_the_yaml_to_its_rows(db):
    run = new_run(db)
    llm = Script({"reason": "a DHL van", "company": "dhl", "decision": "delivery",
                  "confidence": 0.9})
    row, result = asyncio.run(run_ai_step_recorded(
        db, run, look_step(), llm, step_path="then[0]", inputs={"plate": "AB12 CDE"},
        images=[b"jpeg"], hardware="cpu", model_name="qwen3:1.7b"))

    assert result.outcome == "delivery"
    db.expire_all()
    stored = db.get(WorkflowRunStep, row.id)
    assert (stored.kind, stored.mode, stored.status, stored.outcome, stored.fields) == (
        "ai", "look", "done", "delivery", {"company": "dhl"})
    assert (stored.step_path, stored.seq, stored.inputs) == ("then[0]", 0, {"plate": "AB12 CDE"})
    assert (stored.model, stored.state, stored.unsure_reason) == ("qwen3:1.7b", None, None)
    assert stored.usage["model_calls"] == 1 and stored.finished_at is not None
    (call,) = stored.tool_calls
    assert (call.kind, call.name, call.status, call.tokens_in) == ("llm", "qwen3:1.7b", "ok", 40)
    assert db.get(WorkflowRun, run.id).status == "running"   # the engine goes on


def test_steps_of_a_run_are_numbered_in_order(db):
    run = new_run(db)
    answer = {"reason": "r", "company": "none", "decision": "cab", "confidence": 1}
    for i in range(2):
        asyncio.run(run_ai_step_recorded(db, run, look_step(), Script(answer),
                                         step_path=f"then[{i}]", hardware="cpu"))
    assert [s.seq for s in db.get(WorkflowRun, run.id).steps] == [0, 1]


def test_a_model_that_is_down_is_the_unsure_path_on_the_row(db):
    run = new_run(db)
    row, result = asyncio.run(run_ai_step_recorded(
        db, run, look_step(), Script(LLMUnreachableError("down")), step_path="then[0]",
        hardware="cpu"))
    assert (result.outcome, result.actions) == ("unsure", [{"log": "{reason}"}])
    assert (row.status, row.outcome, row.unsure_reason) == ("done", "unsure", "llm_down")
    assert [c.status for c in row.tool_calls] == ["failed"]


def test_the_row_is_written_before_the_model_answers(db):
    run = new_run(db)
    seen = {}

    async def slow():
        seen["rows"] = [(s.status, s.outcome) for s in db.query(WorkflowRunStep).all()]
        return ChatResult(content=json.dumps({"reason": "r", "company": "dhl",
                                              "decision": "delivery", "confidence": 1}))

    class Watching(Script):
        async def chat(self, **kw):
            return await slow()

    asyncio.run(run_ai_step_recorded(db, run, look_step(), Watching(), step_path="then[0]",
                                     hardware="cpu"))
    assert seen["rows"] == [("running", None)]


def test_an_ask_tool_waits_on_the_row_and_resumes_after_a_restart(sessions):
    step = AIStep.from_dict(yaml.safe_load(NIGHT))
    tools = [tool("describe_camera"), tool("recognize_faces", mode="ask")]

    db = sessions()
    run = new_run(db)
    llm = Script([("describe_camera", {"camera_id": "yard"})],
                 [("recognize_faces", {"camera_id": "yard"})])
    row, result = asyncio.run(run_ai_step_recorded(
        db, run, step, llm, step_path="then[0]", tools=tools, hardware="cpu",
        model_name="granite4:micro"))
    assert (result.outcome, row.status, row.outcome) == (None, "waiting", None)
    assert db.get(WorkflowRun, run.id).status == "waiting"
    assert row.state["pending"]["name"] == "recognize_faces"
    assert [c.status for c in row.tool_calls] == ["ok", "ok", "ok", "suspended"]
    row_id = row.id
    db.close()

    db = sessions()                                     # the engine restarted
    waiting = db.get(WorkflowRunStep, row_id)
    llm = Script("done", {"reason": "the guard says it is Sam", "who": "staff",
                          "confidence": 1})
    row, result = asyncio.run(resume_ai_step_recorded(db, waiting, step, llm,
                                                      "Yes, that is Sam", tools=tools))
    db.expire_all()
    row = db.get(WorkflowRunStep, row_id)
    assert (row.status, row.outcome, row.rule, row.state) == ("done", "ignore", 2, None)
    assert row.model == "granite4:micro"
    statuses = [c.status for c in row.tool_calls]
    assert statuses[:4] == ["ok", "ok", "ok", "suspended"] and len(statuses) == 7
    assert len({c.id for c in row.tool_calls}) == 7          # rewritten, never doubled
    assert db.get(WorkflowRun, run.id).status == "running"
    db.close()


def test_only_a_waiting_step_can_be_resumed(db):
    run = new_run(db)
    row, _ = asyncio.run(run_ai_step_recorded(
        db, run, look_step(), Script(LLMUnreachableError("down")), step_path="then[0]",
        hardware="cpu"))
    with pytest.raises(ValueError, match="not waiting"):
        asyncio.run(resume_ai_step_recorded(db, row, look_step(), Script(), "yes"))


def test_deleting_a_run_deletes_its_steps_and_calls(db):
    run = new_run(db)
    asyncio.run(run_ai_step_recorded(
        db, run, look_step(), Script(LLMUnreachableError("down")), step_path="then[0]",
        hardware="cpu"))
    db.delete(run)
    db.commit()
    assert db.query(WorkflowRunStep).count() == 0 and db.query(WorkflowToolCall).count() == 0


@pytest.mark.parametrize("accel, want", [("cuda", "gpu"), ("cpu", "cpu"), ("vaapi", "cpu")])
def test_the_hardware_class_follows_the_box(monkeypatch, accel, want):
    from core.config import settings
    monkeypatch.setattr(settings, "detect_hwaccel", accel, raising=False)
    assert hardware_class() == want


def test_no_hardware_profile_means_cpu(monkeypatch):
    import services.hardware_profile as hp

    def boom():
        raise RuntimeError("no profile")
    monkeypatch.setattr(hp, "host_profile", boom)
    assert ai_steps.hardware_class() == "cpu"
