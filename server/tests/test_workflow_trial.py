# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Try a workflow on recent events (RFC #620): results only, never runs."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import time
import types as _types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_workflow_trial_test.db")
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

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from opennvr_app_sdk.agent import ChatResult  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import routers.workflows as wf_router  # noqa: E402
import workflows.trial as trial_mod  # noqa: E402
from core.auth import get_current_active_user  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import (  # noqa: E402
    AppAlert,
    Camera,
    InstalledApp,
    User,
    Workflow,
    WorkflowRun,
    WorkflowRunStep,
    WorkflowToolCall,
    WorkflowVersion,
)
from workflows.engine import Engine  # noqa: E402
from workflows.trial import Trials, envelope_from_inbox, matching_alerts  # noqa: E402

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)

YARD = """\
workflow: v1
name: Yard check
when:
  alert: { app: intrusion-detection, type: intrusion, cameras: [yard] }
if:
  - site_mode: [armed_away]
then:
  - ai:
      mode: look
      prompt: Who is in the yard?
      decision: [ignore, alert]
      fields: { who: [guard, stranger] }
      rules:
        - { when: { who: stranger }, then: alert }
        - { when: { who: guard }, then: ignore }
      ignore: [ log: "a guard" ]
      alert:
        - ask_person: { who: role:guard, message: "Stranger?", replies: ["Yes", "No"],
                        default: "No" }
          "Yes": [ alert: { severity: medium, title: "Stranger on {camera}" } ]
      unsure: [ log: "unsure" ]
"""


class Script:
    def __init__(self, *answers):
        self.answers = list(answers)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def chat(self, *, messages, tools=None, format=None, temperature=0.2, max_tokens=256):
        return ChatResult(content=json.dumps(self.answers.pop(0)))


def who(w):
    return {"reason": f"a {w}", "who": w, "confidence": 1}


class _User:
    def __init__(self, uid, perms):
        self.id = uid
        self.is_superuser = False
        self.role = _types.SimpleNamespace(
            permissions=[_types.SimpleNamespace(name=p) for p in perms])


@pytest.fixture()
def env(monkeypatch):
    sql = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(sql)
    make = sessionmaker(bind=sql)
    db = make()
    db.add(User(id=1, username="o", email="o@x.test", hashed_password="x", is_active=True,
                role_id=1))
    db.add(Camera(id=3, name="Yard", ip_address="10.0.0.3", owner_id=1))
    db.add(Camera(id=4, name="Gate", ip_address="10.0.0.4", owner_id=1))
    db.add(InstalledApp(id="intrusion-detection", name="i", version="1", url="http://i",
                        manifest_json={"actions": []}, config_json={}))
    wf = Workflow(name="Yard check", state="off", current_version=1)
    wf.versions.append(WorkflowVersion(version=1, yaml=YARD))
    db.add(wf)
    db.commit()
    model = {"client": Script()}
    engine = Engine(make, client=lambda: model["client"], now=lambda: NOW, hardware="cpu",
                    max_side=0)
    t = Trials(engine)
    monkeypatch.setattr(trial_mod, "_trials", t)
    yield _types.SimpleNamespace(db=db, engine=engine, trials=t, model=model, wf=wf, make=make)
    db.close()
    sql.dispose()


def inbox(db, n, *, camera="cam3", app="intrusion-detection", type_="intrusion",
          minutes_ago=10, kind="app", images=None):
    row = AppAlert(alert_id=f"a{n}", fired_at=NOW - timedelta(minutes=minutes_ago),
                   severity="high", title=f"Intrusion {n}", source_kind=kind, source_name=app,
                   camera_id=camera, alert_type=type_,
                   evidence=json.dumps({"track_id": 7}),
                   images=json.dumps(images) if images else None)
    db.add(row)
    db.commit()
    return row


def test_an_inbox_row_is_the_envelope_the_engine_was_given(env):
    row = inbox(env.db, 1, images={"best": "2026/11/a.jpg"})
    e = envelope_from_inbox(row)
    assert e["source"] == {"kind": "app", "name": "intrusion-detection"}
    assert e["evidence"] == {"track_id": 7, "images": {"best": "2026/11/a.jpg"}}
    assert (e["alert_type"], e["camera_id"]) == ("intrusion", "cam3")


def test_only_the_alerts_the_trigger_matches_newest_first(env):
    import yaml
    doc = yaml.safe_load(YARD)
    inbox(env.db, 1, minutes_ago=30)
    inbox(env.db, 2, minutes_ago=5)
    inbox(env.db, 3, camera="cam4")                          # not the yard
    inbox(env.db, 4, type_="loitering")                      # not this type
    inbox(env.db, 5, app="ppe-check")                        # not this app
    inbox(env.db, 6, minutes_ago=60 * 24 * 9)                # older than the days asked
    inbox(env.db, 7, kind="workflow")                        # a workflow's own alert
    got = matching_alerts(env.db, doc, days=7, limit=10, now=NOW)
    assert [e["alert_id"] for e in got] == ["a2", "a1"]
    assert [e["alert_id"] for e in matching_alerts(env.db, doc, days=7, limit=1, now=NOW)] == [
        "a2"]


def test_a_trial_runs_the_steps_as_test_and_leaves_no_run(env):
    inbox(env.db, 1, minutes_ago=5)
    inbox(env.db, 2, minutes_ago=6)
    env.model["client"] = Script(who("stranger"), who("guard"))
    import yaml
    doc = yaml.safe_load(YARD)

    async def go():
        t = env.trials.start(env.wf.id, 1, doc,
                             matching_alerts(env.db, doc, days=7, limit=10, now=NOW))
        await asyncio.gather(*env.engine.tasks)
        return t
    t = asyncio.run(go())
    assert (t["state"], t["total"], t["done"]) == ("done", 2, 2)
    first, second = t["results"]
    assert (first["alert"]["alert_id"], first["alert"]["camera"]) == ("a1", "yard")
    assert [(s["step_path"], s["kind"], s["outcome"]) for s in first["steps"]] == [
        ("then[0]", "ai", "alert"),
        ("then[0].ai.alert[0]", "ask_person", "No"),            # the default, as in Test
    ]
    assert first["steps"][0]["fields"] == {"who": "stranger"} and first["steps"][0]["rule"] == 1
    assert len(first["steps"][0]["tool_calls"]) == 1
    assert [(s["kind"], s["outcome"]) for s in second["steps"]] == [
        ("ai", "ignore"), ("log", "would_have")]
    # Nothing left behind: no run, no step, no call, no alert raised.
    env.db.expire_all()
    for model in (WorkflowRun, WorkflowRunStep, WorkflowToolCall):
        assert env.db.query(model).count() == 0
    assert env.db.query(AppAlert).count() == 2


def test_a_trial_with_nothing_to_try_is_done_at_once(env):
    t = env.trials.start(env.wf.id, 1, {"when": {"alert": {"app": "x"}}}, [])
    assert (t["state"], t["total"], t["results"]) == ("done", 0, [])
    assert env.trials.get(t["id"]) is t and env.trials.get("nope") is None


# ── the API ────────────────────────────────────────────────────────────


@pytest.fixture()
def api(env, monkeypatch):
    monkeypatch.setattr(wf_router, "audit_request", lambda db, request, **kw: None)
    # The fixture's alerts are dated from NOW; on the real clock they age out
    # of /try's seven-day window a week after NOW was written.
    monkeypatch.setattr(wf_router, "_now", lambda: NOW)
    app = FastAPI()
    app.include_router(wf_router.router, prefix="/api/v1")
    user = {"u": _User(1, {"workflows.view", "workflows.manage"})}
    app.dependency_overrides[get_db] = lambda: env.db
    app.dependency_overrides[get_current_active_user] = lambda: user["u"]
    env.client = TestClient(app)
    env.user = user
    return env


def poll(api, try_id):
    for _ in range(100):
        t = api.client.get(f"/api/v1/workflows/tries/{try_id}").json()
        if t["state"] == "done":
            return t
        time.sleep(0.02)
    raise AssertionError("trial did not finish")


def test_try_the_saved_version_and_read_the_results(api):
    inbox(api.db, 1)
    api.model["client"] = Script(who("guard"))
    r = api.client.post(f"/api/v1/workflows/{api.wf.id}/try", json={"limit": 5})
    assert r.status_code == 200 and r.json()["total"] == 1
    t = poll(api, r.json()["try_id"])
    (result,) = t["results"]
    assert (result["status"], t["version"]) == ("done", 1)
    assert [(s["kind"], s["outcome"]) for s in result["steps"]] == [
        ("ai", "ignore"), ("log", "would_have")]
    assert api.client.get("/api/v1/workflows/tries/nope").status_code == 404


def test_try_a_draft_and_what_is_refused(api):
    inbox(api.db, 1)
    api.model["client"] = Script(who("stranger"))
    draft = YARD.replace("prompt: Who is in the yard?", "prompt: Who is it?")
    r = api.client.post(f"/api/v1/workflows/{api.wf.id}/try", json={"yaml": draft})
    t = poll(api, r.json()["try_id"])
    assert t["version"] is None and t["results"][0]["steps"][0]["outcome"] == "alert"
    bad = api.client.post(f"/api/v1/workflows/{api.wf.id}/try",
                          json={"yaml": YARD.replace("workflow: v1", "workflow: v9")})
    assert bad.status_code == 422 and bad.json()["errors"][0]["path"] == "workflow"
    sched = YARD.replace("  alert: { app: intrusion-detection, type: intrusion, cameras: [yard] }",
                         '  schedule: { at: "07:00" }')
    r = api.client.post(f"/api/v1/workflows/{api.wf.id}/try", json={"yaml": sched})
    assert r.status_code == 422 and "started by an app's alert" in r.json()["detail"]
    assert api.client.post(f"/api/v1/workflows/{api.wf.id}/try",
                           json={"limit": 50}).status_code == 422
    assert api.client.post("/api/v1/workflows/999/try", json={}).status_code == 404
    api.user["u"] = _User(2, {"workflows.view"})
    assert api.client.post(f"/api/v1/workflows/{api.wf.id}/try", json={}).status_code == 403
