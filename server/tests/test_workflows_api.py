# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The workflows API (#773): files, versions, state, templates and runs."""

from __future__ import annotations

import os
import secrets
import sys
import types as _types
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_workflows_api_test.db")
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

import routers.workflows as wf_router  # noqa: E402
from core.auth import get_current_active_user  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import (  # noqa: E402
    InstalledApp,
    WorkflowRun,
    WorkflowRunStep,
    WorkflowToolCall,
)
from workflows import catalog  # noqa: E402

LOG = """\
workflow: v1
name: Log plates
when:
  event: { subject: plate.read.v1, cameras: [gate] }
then:
  - log: "plate {plate}"
"""

GATE = next(t for t in catalog.templates() if t["id"] == "gate_concierge")["yaml"]


class _User:
    def __init__(self, uid: int, perms: set[str]):
        self.id = uid
        self.is_superuser = False
        self.role = _types.SimpleNamespace(
            permissions=[_types.SimpleNamespace(name=p) for p in perms])


OPERATOR = _User(2, {"workflows.view", "workflows.manage", "workflows.approve"})
VIEWER = _User(3, {"workflows.view"})
ADMIN = _User(1, {"full_access"})


@pytest.fixture()
def env(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(InstalledApp(id="gate-controller", name="Gate", version="1", url="http://g",
                        manifest_json={"actions": [{"name": "open_now", "confirm": True},
                                                   {"name": "release_hold"}]},
                        config_json={}))
    for app_id in ("license-plate-recognition", "ppe-check", "intrusion-detection"):
        db.add(InstalledApp(id=app_id, name=app_id, version="1", url=f"http://{app_id}",
                            manifest_json={"actions": []}, config_json={}))
    db.commit()
    audits = []
    monkeypatch.setattr(wf_router, "audit_request",
                        lambda db, request, **kw: audits.append(kw))
    app = FastAPI()
    app.include_router(wf_router.router, prefix="/api/v1")
    who = {"user": OPERATOR}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_active_user] = lambda: who["user"]
    client = TestClient(app)
    yield _types.SimpleNamespace(client=client, db=db, who=who, audits=audits)
    db.close()
    eng.dispose()


def post(env, text=LOG):
    return env.client.post("/api/v1/workflows", json={"yaml": text})


# ── saving ─────────────────────────────────────────────────────────────


def test_a_new_workflow_is_saved_in_test_as_version_1(env):
    r = post(env)
    assert r.status_code == 200, r.text
    w = r.json()
    assert (w["name"], w["state"], w["version"], w["yaml"]) == ("Log plates", "test", 1, LOG)
    assert (w["needs"], w["moves_things"], w["last_run"], w["warnings"]) == (
        ["camera:gate"], False, None, [])
    assert env.audits[-1]["action"] == "workflow.create"
    assert [x["name"] for x in env.client.get("/api/v1/workflows").json()] == ["Log plates"]


def test_a_file_that_cannot_run_is_422_with_each_error_at_its_key(env):
    bad = LOG.replace("workflow: v1", "workflow: v9").replace("cameras: [gate]", "cameras: [7]")
    r = post(env, bad)
    assert r.status_code == 422
    paths = [e["path"] for e in r.json()["errors"]]
    assert paths == ["workflow", "when.event.cameras"]
    assert env.client.get("/api/v1/workflows").json() == []


def test_names_are_checked_against_this_site(env):
    text = GATE.replace("gate-controller", "boom-barrier")
    r = post(env, text)
    assert r.status_code == 422
    assert any("'boom-barrier' is not installed on this site" in e["message"]
               for e in r.json()["errors"])


def test_every_save_is_a_new_version(env):
    wid = post(env).json()["id"]
    r = env.client.put(f"/api/v1/workflows/{wid}",
                       json={"yaml": LOG.replace("Log plates", "Log every plate")})
    w = r.json()
    assert (w["version"], w["name"]) == (2, "Log every plate")
    assert env.audits[-1]["details"]["version"] == 2
    assert env.client.put("/api/v1/workflows/999", json={"yaml": LOG}).status_code == 404


# ── state ──────────────────────────────────────────────────────────────


def test_off_test_on(env):
    wid = post(env).json()["id"]
    for state in ("on", "off", "test"):
        r = env.client.post(f"/api/v1/workflows/{wid}/state", json={"state": state})
        assert r.json()["state"] == state
    assert env.client.post(f"/api/v1/workflows/{wid}/state",
                           json={"state": "paused"}).status_code == 422
    assert env.audits[-1]["details"] == {"from": "off", "to": "test"}


def test_a_workflow_that_moves_things_is_turned_on_by_an_admin_only(env):
    w = post(env, GATE).json()
    assert w["moves_things"] is True
    r = env.client.post(f"/api/v1/workflows/{w['id']}/state", json={"state": "on"})
    assert r.status_code == 403 and "an admin turns it On" in r.json()["detail"]
    env.who["user"] = ADMIN
    assert env.client.post(f"/api/v1/workflows/{w['id']}/state",
                           json={"state": "on"}).json()["state"] == "on"


def test_a_changed_ai_step_goes_back_to_test(env):
    env.who["user"] = ADMIN
    w = post(env, GATE).json()
    env.client.post(f"/api/v1/workflows/{w['id']}/state", json={"state": "on"})
    same = env.client.put(f"/api/v1/workflows/{w['id']}",
                          json={"yaml": GATE.replace("timeout: 2m", "timeout: 3m")}).json()
    assert same["state"] == "on"                       # the AI step is as it was
    changed = env.client.put(f"/api/v1/workflows/{w['id']}",
                             json={"yaml": GATE.replace("[delivery, cab, visitor]",
                                                        "[delivery, visitor]")
                                   .replace("      cab: []\n", "")}).json()
    assert changed["state"] == "test" and env.audits[-1]["details"]["back_to_test"] is True


def test_an_operator_adding_a_moving_action_to_an_on_workflow_puts_it_back_in_test(env):
    wid = post(env).json()["id"]
    env.client.post(f"/api/v1/workflows/{wid}/state", json={"state": "on"})
    moving = LOG.replace('  - log: "plate {plate}"',
                         '  - if: { camera: [gate] }\n    then:\n'
                         '      - app: { id: gate-controller, action: open_now }')
    r = env.client.put(f"/api/v1/workflows/{wid}", json={"yaml": moving}).json()
    assert (r["moves_things"], r["state"]) == (True, "test")


# ── permissions ────────────────────────────────────────────────────────


def test_a_viewer_reads_and_cannot_change(env):
    wid = post(env).json()["id"]
    env.who["user"] = VIEWER
    assert env.client.get("/api/v1/workflows").status_code == 200
    assert env.client.get("/api/v1/workflows/templates").status_code == 200
    assert post(env).status_code == 403
    assert env.client.put(f"/api/v1/workflows/{wid}", json={"yaml": LOG}).status_code == 403
    assert env.client.post(f"/api/v1/workflows/{wid}/state",
                           json={"state": "on"}).status_code == 403
    env.who["user"] = _User(4, set())
    assert env.client.get("/api/v1/workflows").status_code == 403


# ── templates and runs ─────────────────────────────────────────────────


def test_the_templates_are_valid_and_say_what_they_need():
    ts = {t["id"]: t for t in catalog.templates()}
    assert set(ts) == {"gate_concierge", "safety_gear_check", "night_intrusion_triage"}
    assert "app:gate-controller" in ts["gate_concierge"]["needs"]
    assert ts["gate_concierge"]["yaml"].startswith("workflow: v1")   # no licence header
    assert all(t["name"] and t["description"] for t in ts.values())


def test_a_template_saves_as_it_is(env):
    env.who["user"] = ADMIN
    for t in env.client.get("/api/v1/workflows/templates").json():
        assert post(env, t["yaml"]).status_code == 200, t["id"]


def test_runs_list_newest_first_and_a_run_reads_step_by_step(env):
    wid = post(env).json()["id"]
    db = env.db
    runs = []
    for i in range(3):
        run = WorkflowRun(workflow_id=wid, workflow_version=1, trigger={"plate": f"P{i}"},
                          status="done")
        db.add(run)
        db.commit()
        runs.append(run)
    step = WorkflowRunStep(run_id=runs[-1].id, seq=0, step_path="then[0]", kind="ai",
                           mode="look", status="done", outcome="delivery", reason="a DHL van",
                           confidence=1.0, rule=None, fields={"company": "dhl"})
    db.add(step)
    db.commit()
    db.add(WorkflowToolCall(id="t1", run_step_id=step.id, seq=0, kind="llm", name="qwen",
                            status="ok", result="{}", ms=900,
                            started_at=runs[-1].started_at))
    db.commit()

    listed = env.client.get(f"/api/v1/workflows/{wid}/runs?limit=2").json()
    assert [r["id"] for r in listed] == [runs[2].id, runs[1].id]
    assert listed[0]["outcome"] == "delivery"
    older = env.client.get(f"/api/v1/workflows/{wid}/runs?before={runs[1].id}").json()
    assert [r["id"] for r in older] == [runs[0].id]

    run = env.client.get(f"/api/v1/workflows/runs/{runs[2].id}").json()
    (s,) = run["steps"]
    assert (s["kind"], s["outcome"], s["reason"], s["fields"]) == (
        "ai", "delivery", "a DHL van", {"company": "dhl"})
    assert s["tool_calls"][0]["ms"] == 900
    assert env.client.get("/api/v1/workflows").json()[0]["last_run"]["id"] == runs[2].id
    assert env.client.get("/api/v1/workflows/runs/999").status_code == 404
    assert env.client.get("/api/v1/workflows/999/runs").status_code == 404
