# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The workflow engine: an alert starts a run, and the run walks its steps."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import types as _types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_workflow_engine_test.db")
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

from opennvr_app_sdk.agent import ChatResult  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.database import Base  # noqa: E402
from models import (  # noqa: E402
    AppAlert,
    Camera,
    User,
    Workflow,
    WorkflowRun,
    WorkflowRunStep,
    WorkflowVersion,
)
from workflows import engine as eng_mod  # noqa: E402
from workflows.engine import Engine, camera_role, holds  # noqa: E402

T0 = datetime(2026, 11, 3, 23, 0, tzinfo=UTC)

YARD = """
workflow: v1
name: Yard check
limits: { cooldown: 2m, one_at_a_time: camera, per_hour: 5 }
when:
  alert: { app: intrusion-detection, type: intrusion, cameras: [yard] }
then:
  - ai:
      mode: look
      prompt: Who is in the yard?
      decision: [ignore, alert]
      fields: { who: [guard, stranger] }
      rules:
        - { when: { who: stranger }, then: alert }
        - { when: { who: guard }, then: ignore }
      ignore: [ log: "a guard: {reason}" ]
      alert: [ alert: { severity: high, title: "Stranger on {camera}" } ]
      unsure: [ log: "unsure: {reason}" ]
always: [ log: "checked {camera}" ]
"""

RULES_ONLY = """
workflow: v1
name: Arm on intrusion
when:
  alert: { app: intrusion-detection }
if:
  - camera: [yard]
then:
  - if: { value: { of: plate, in: [KA01] } }
    then: [ log: "known plate {plate}" ]
    else: [ site_mode: armed_away ]
  - for_each: { cameras: [yard, gate] }
    do: [ log: "look at {camera}" ]
  - notify: { who: role:guard, message: hi }
"""


class Script:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def chat(self, *, messages, tools=None, format=None, temperature=0.2, max_tokens=256):
        self.calls.append(messages)
        a = self.answers.pop(0)
        return ChatResult(content=json.dumps(a))


@pytest.fixture()
def env(monkeypatch, tmp_path):
    sql = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(sql)
    make = sessionmaker(bind=sql)
    db = make()
    db.add(User(id=1, username="owner", email="o@x.test", hashed_password="x",
                is_active=True, role_id=1))
    db.add(Camera(id=3, name="Yard", ip_address="10.0.0.3", owner_id=1))
    db.add(Camera(id=4, name="Front Gate", ip_address="10.0.0.4", owner_id=1))
    db.commit()
    clock = {"now": T0}
    model = {"client": None}
    mode = {"mode": "disarmed"}
    from services import site_mode
    monkeypatch.setattr(site_mode, "current_mode", lambda db: mode["mode"])
    monkeypatch.setattr(site_mode, "set_mode",
                        lambda db, m, actor: mode.update(mode=m, actor=actor))
    pushed = []

    async def push(**kw):
        pushed.append(kw)
    import services.event_bus_service as bus
    monkeypatch.setattr(bus, "publish_app_alert", push)
    import services.alarm_actions as alarms
    monkeypatch.setattr(alarms, "dispatch_in_background", lambda stored: None)
    e = Engine(make, client=lambda: model["client"], now=lambda: clock["now"], hardware="cpu",
               max_side=0)
    yield _types.SimpleNamespace(db=db, engine=e, clock=clock, model=model, mode=mode,
                                 pushed=pushed, make=make)
    db.close()
    sql.dispose()


def add(env, text, state="on"):
    import yaml
    wf = Workflow(name=yaml.safe_load(text)["name"], state=state, current_version=1)
    wf.versions.append(WorkflowVersion(version=1, yaml=text))
    env.db.add(wf)
    env.db.commit()
    return wf


def alert(camera="cam3", app="intrusion-detection", type_="intrusion", **evidence):
    return {"alert_id": f"a-{datetime.now().timestamp()}", "title": "Intrusion",
            "severity": "high", "source": {"kind": "app", "name": app}, "camera_id": camera,
            "alert_type": type_, "evidence": evidence}


def fire(env, envelope):
    async def go():
        ids = env.engine.start_from_alert(envelope)
        await asyncio.gather(*env.engine.tasks)
        return ids
    return asyncio.run(go())


def steps(env, run_id):
    env.db.expire_all()
    return [(s.step_path, s.kind, s.outcome) for s in
            env.db.query(WorkflowRunStep).filter(WorkflowRunStep.run_id == run_id)
            .order_by(WorkflowRunStep.seq).all()]


def answer(who):
    return {"reason": f"a {who} by the fence", "who": who, "confidence": 0.9}


# ── matching ───────────────────────────────────────────────────────────


def test_camera_roles_come_from_names():
    assert camera_role("Front Gate") == "front-gate" and camera_role("Yard") == "yard"


def test_an_alert_starts_only_the_workflows_it_triggers(env):
    add(env, YARD)
    add(env, YARD.replace("Yard check", "Off one"), state="off")
    env.model["client"] = Script(answer("guard"))
    assert len(fire(env, alert())) == 1
    assert fire(env, alert(camera="cam4")) == []                    # not the yard
    assert fire(env, alert(type_="loitering")) == []                # not this type
    assert fire(env, alert(app="ppe-check")) == []                  # not this app
    wf_alert = alert()
    wf_alert["source"] = {"kind": "workflow", "name": "Yard check"}
    assert fire(env, wf_alert) == []                                # never its own kind


# ── the AI step and its paths ──────────────────────────────────────────


def test_a_look_step_picks_the_path_and_the_trace_is_recorded(env):
    add(env, YARD)
    env.model["client"] = Script(answer("stranger"))
    (run_id,) = fire(env, alert(track_id=7))
    assert steps(env, run_id) == [
        ("then[0]", "ai", "alert"), ("then[0].ai.alert[0]", "alert", "done"),
        ("always[0]", "log", "done")]
    run = env.db.get(WorkflowRun, run_id)
    assert (run.status, run.test, run.trigger["camera"]) == ("done", False, "yard")
    ai = env.db.query(WorkflowRunStep).filter_by(run_id=run_id, kind="ai").one()
    assert (ai.rule, ai.fields, len(ai.tool_calls)) == (1, {"who": "stranger"}, 1)
    # The event reached the model as data.
    assert '"camera": "yard"' in env.model["client"].calls[0][1]["content"]


def test_the_ai_decided_high_alert_is_capped_and_lands_in_the_inbox(env):
    add(env, YARD)
    env.model["client"] = Script(answer("stranger"))
    (run_id,) = fire(env, alert())
    row = env.db.query(AppAlert).one()
    assert (row.title, row.severity, row.source_kind, row.source_name) == (
        "Stranger on yard", "medium", "workflow", "Yard check")
    run = env.db.get(WorkflowRun, run_id)
    assert row.correlation_id == run.correlation_id
    step = env.db.query(WorkflowRunStep).filter_by(run_id=run_id, kind="alert").one()
    assert step.reason == "capped at medium: AI-decided"
    assert env.pushed[0]["severity"] == "medium"


def test_placeholders_fill_from_the_ai_step(env):
    add(env, YARD)
    env.model["client"] = Script(answer("guard"))
    (run_id,) = fire(env, alert())
    logged = [s.reason for s in env.db.query(WorkflowRunStep).filter_by(run_id=run_id,
                                                                          kind="log")]
    assert logged == ["a guard: a guard by the fence", "checked yard"]


def test_no_model_is_the_unsure_path(env):
    add(env, YARD)
    (run_id,) = fire(env, alert())
    assert steps(env, run_id)[:2] == [("then[0]", "ai", "unsure"),
                                      ("then[0].ai.unsure[0]", "log", "done")]
    ai = env.db.query(WorkflowRunStep).filter_by(run_id=run_id, kind="ai").one()
    assert ai.unsure_reason == "llm_down"


# ── Test mode ──────────────────────────────────────────────────────────


def test_test_mode_runs_the_ai_and_only_records_the_actions(env):
    add(env, YARD, state="test")
    env.model["client"] = Script(answer("stranger"))
    (run_id,) = fire(env, alert())
    assert steps(env, run_id) == [
        ("then[0]", "ai", "alert"), ("then[0].ai.alert[0]", "alert", "would_have"),
        ("always[0]", "log", "would_have")]
    assert env.db.query(AppAlert).count() == 0
    assert env.db.get(WorkflowRun, run_id).test is True


# ── rules, guards and actions ──────────────────────────────────────────


def test_guards_if_for_each_and_site_mode(env):
    add(env, RULES_ONLY)
    assert fire(env, alert(camera="cam4", type_="x")) == []         # the guard: not the yard
    (run_id,) = fire(env, alert(plate="ZZ99"))
    assert steps(env, run_id) == [
        ("then[0]", "if", "else"), ("then[0].else[0]", "site_mode", "done"),
        ("then[1]", "for_each", "done"), ("then[1].do[0]", "log", "done"),
        ("then[1].do[0]", "log", "done"), ("then[2]", "notify", "skipped")]
    assert env.mode == {"mode": "armed_away", "actor": "workflow:1"}
    logs = [s.reason for s in env.db.query(WorkflowRunStep).filter_by(run_id=run_id,
                                                                        kind="log")]
    assert logs == ["look at yard", "look at gate"]
    notify = env.db.query(WorkflowRunStep).filter_by(run_id=run_id, kind="notify").one()
    assert "not available yet" in notify.reason


def test_conditions():
    ctx = eng_mod.Context(values={"camera": "yard", "plate": "KA01"}, camera_handle="cam3")
    late = datetime(2026, 11, 3, 23, 30).astimezone()
    noon = datetime(2026, 11, 3, 12, 0).astimezone()
    night = {"time": {"from": "22:00", "to": "06:00"}}
    assert holds(None, night, ctx, now=late) and not holds(None, night, ctx, now=noon)
    assert holds(None, {"time": {"from": "09:00", "to": "17:00"}}, ctx, now=noon)
    assert holds(None, [{"camera": ["yard"]}, {"value": {"of": "plate", "in": ["KA01"]}}], ctx)
    assert not holds(None, {"list": {"of": "plate", "in": "staff"}}, ctx)


# ── limits ─────────────────────────────────────────────────────────────


def test_cooldown_one_at_a_time_and_per_hour(env):
    add(env, YARD)
    env.model["client"] = Script(*[answer("guard")] * 10)
    assert len(fire(env, alert())) == 1
    env.clock["now"] = T0 + timedelta(seconds=60)
    assert fire(env, alert()) == []                          # cooldown 2m, same camera
    env.clock["now"] = T0 + timedelta(minutes=3)
    assert len(fire(env, alert())) == 1
    for i in range(3):
        env.clock["now"] = T0 + timedelta(minutes=6 + 3 * i)
        assert len(fire(env, alert())) == 1
    env.clock["now"] = T0 + timedelta(minutes=20)
    assert fire(env, alert()) == []                          # 5 runs this hour already


def test_a_run_still_going_holds_its_camera(env):
    wf = add(env, YARD)
    env.db.add(WorkflowRun(workflow_id=wf.id, workflow_version=1, status="waiting",
                           trigger={"_key": "camera:yard"},
                           started_at=T0 - timedelta(minutes=30)))
    env.db.commit()
    assert fire(env, alert()) == []


def test_the_ai_budget_sends_the_step_unsure(env):
    add(env, YARD.replace("per_hour: 5 }", "per_hour: 5, ai_per_hour: 1 }")
           .replace("cooldown: 2m", "cooldown: 1s"))
    env.model["client"] = Script(answer("guard"), answer("guard"))
    fire(env, alert())
    env.clock["now"] = T0 + timedelta(minutes=1)
    (run_id,) = fire(env, alert())
    ai = env.db.query(WorkflowRunStep).filter_by(run_id=run_id, kind="ai").one()
    assert (ai.outcome, ai.unsure_reason) == ("unsure", "over_budget")


# ── restarts ───────────────────────────────────────────────────────────


def test_a_run_a_stop_left_half_done_is_failed_not_redone(env):
    wf = add(env, YARD)
    env.db.add(WorkflowRun(workflow_id=wf.id, workflow_version=1, status="running",
                           started_at=T0))
    env.db.add(WorkflowRun(workflow_id=wf.id, workflow_version=1, status="done",
                           started_at=T0))
    env.db.commit()
    assert env.engine.close_interrupted() == 1
    env.db.expire_all()
    assert sorted(r.status for r in env.db.query(WorkflowRun)) == ["done", "failed"]


def test_a_failing_step_fails_the_run_not_the_engine(env, monkeypatch):
    add(env, RULES_ONLY)

    def boom(*a, **k):
        raise RuntimeError("site mode store down")
    from services import site_mode
    monkeypatch.setattr(site_mode, "set_mode", boom)
    (run_id,) = fire(env, alert())
    env.db.expire_all()
    run = env.db.get(WorkflowRun, run_id)
    assert run.status == "failed" and run.finished_at is not None


def test_warm_up_loads_the_model_once_and_never_raises(env):
    assert asyncio.run(env.engine.warm_up()) is False          # no model set: nothing to do
    llm = Script({"reason": "", "confidence": 1})
    env.model["client"] = llm
    assert asyncio.run(env.engine.warm_up()) is True and len(llm.calls) == 1

    class Down(Script):
        async def chat(self, **kw):
            raise OSError("connection refused")
    env.model["client"] = Down()
    assert asyncio.run(env.engine.warm_up()) is False
