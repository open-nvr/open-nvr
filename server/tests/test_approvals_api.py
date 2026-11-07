# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The approvals API (#773): a workflow's Ask a person, answered."""

from __future__ import annotations

import asyncio
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
os.environ.setdefault("DATABASE_URL", "sqlite:///./_approvals_api_test.db")
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
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import routers.approvals as ap_router  # noqa: E402
from core.auth import get_current_active_user  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import (  # noqa: E402
    Workflow,
    WorkflowApproval,
    WorkflowRun,
    WorkflowRunStep,
    WorkflowVersion,
)
from workflows.engine import Engine  # noqa: E402

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
FLOW = """
workflow: v1
name: Ask
when:
  alert: { app: x }
then:
  - ask_person: { who: role:guard, message: "ok?", replies: ["Yes", "No"] }
    "Yes": [ log: "yes" ]
"""


class _User:
    def __init__(self, uid: int, perms: set[str]):
        self.id = uid
        self.username = f"u{uid}"
        self.is_superuser = False
        self.role = _types.SimpleNamespace(
            permissions=[_types.SimpleNamespace(name=p) for p in perms])


GUARD = _User(2, {"workflows.approve", "workflows.view"})
VIEWER = _User(3, {"workflows.view"})


@pytest.fixture()
def env(monkeypatch):
    sql = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(sql)
    make = sessionmaker(bind=sql)
    db = make()
    wf = Workflow(name="Ask", state="on", current_version=1)
    wf.versions.append(WorkflowVersion(version=1, yaml=FLOW))
    db.add(wf)
    db.commit()
    run = WorkflowRun(workflow_id=wf.id, workflow_version=1, status="waiting", test=False,
                      trigger={"camera": "gate", "camera_id": "cam4"}, started_at=T0)
    db.add(run)
    db.commit()
    step = WorkflowRunStep(run_id=run.id, seq=0, step_path="then[0]", kind="ask_person",
                           status="waiting")
    db.add(step)
    db.commit()
    db.add(WorkflowApproval(run_id=run.id, step_id=step.id, step_path="then[0]",
                            question="ok?", who="role:guard", replies=["Yes", "No"],
                            state="open", created_at=T0, timeout_at=T0 + timedelta(minutes=5),
                            resume={"tokens": ["then", 0],
                                    "frames": [{"p": "then", "tokens": ["then"], "next": 1,
                                                "ai_decided": False}],
                                    "values": {"camera": "gate"}}))
    db.commit()
    engine = Engine(make, client=lambda: None, now=lambda: T0)
    monkeypatch.setattr(ap_router, "get_engine", lambda: engine)
    monkeypatch.setattr(ap_router, "audit_request", lambda db, request, **kw: None)
    scope = {"visible": None}
    import services.camera_scope as cs
    monkeypatch.setattr(cs, "can_view_camera",
                        lambda db, user, cam: scope["visible"] is None or cam in scope["visible"])
    app = FastAPI()
    app.include_router(ap_router.router, prefix="/api/v1")
    who = {"user": GUARD}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_active_user] = lambda: who["user"]
    yield _types.SimpleNamespace(client=TestClient(app), db=db, who=who, engine=engine,
                                 scope=scope, run_id=run.id)
    db.close()
    sql.dispose()


def test_open_approvals_are_listed_in_the_773_shape(env):
    (a,) = env.client.get("/api/v1/approvals").json()
    assert {k: a[k] for k in ("workflow_name", "question", "replies", "state", "camera")} == {
        "workflow_name": "Ask", "question": "ok?", "replies": ["Yes", "No"], "state": "open",
        "camera": "gate"}
    assert a["timeout_at"].startswith("2026-11-03T12:05")


def test_an_answer_carries_the_run_on(env):
    a = env.client.get("/api/v1/approvals").json()[0]
    r = env.client.post(f"/api/v1/approvals/{a['id']}", json={"reply": "Yes"})
    assert r.status_code == 200 and r.json()["state"] == "answered"
    # The route hands the answer to the engine, which carries the run on.
    for _ in range(50):
        env.db.expire_all()
        if env.db.get(WorkflowRun, env.run_id).status == "done":
            break
        time.sleep(0.02)
    assert env.db.get(WorkflowRun, env.run_id).status == "done"
    steps = env.db.query(WorkflowRunStep).filter_by(run_id=env.run_id).order_by(
        WorkflowRunStep.seq).all()
    assert [(s.step_path, s.outcome) for s in steps] == [("then[0]", "Yes"),
                                                          ("then[0].Yes[0]", "done")]
    # A second resume of the same run does nothing: it carries on once.
    asyncio.run(env.engine.resume(a["id"]))
    env.db.expire_all()
    assert env.db.query(WorkflowRunStep).filter_by(run_id=env.run_id).count() == 2
    assert env.client.get("/api/v1/approvals").json() == []
    assert env.client.get("/api/v1/approvals?open=false").json()[0]["reply"] == "Yes"


def test_what_is_refused(env):
    a = env.client.get("/api/v1/approvals").json()[0]
    assert env.client.post(f"/api/v1/approvals/{a['id']}",
                           json={"reply": "Maybe"}).status_code == 422
    assert env.client.post("/api/v1/approvals/999", json={"reply": "Yes"}).status_code == 404
    env.scope["visible"] = {1}                                 # not the gate camera
    assert env.client.get("/api/v1/approvals").json() == []
    assert env.client.post(f"/api/v1/approvals/{a['id']}",
                           json={"reply": "Yes"}).status_code == 403
    env.scope["visible"] = None
    env.who["user"] = VIEWER                                   # can see workflows, not answer
    assert env.client.get("/api/v1/approvals").status_code == 403
    assert env.client.post(f"/api/v1/approvals/{a['id']}",
                           json={"reply": "Yes"}).status_code == 403
    env.who["user"] = GUARD
    env.db.query(WorkflowApproval).update({"state": "timed_out"})
    env.db.commit()
    r = env.client.post(f"/api/v1/approvals/{a['id']}", json={"reply": "Yes"})
    assert r.status_code == 409 and r.json()["detail"] == "Already timed out"
