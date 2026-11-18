# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The other triggers: contracted events, camera offline, schedule, button."""

from __future__ import annotations

import asyncio
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
os.environ.setdefault("DATABASE_URL", "sqlite:///./_workflow_triggers_test.db")
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

import yaml  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import routers.workflows as wf_router  # noqa: E402
from core.auth import get_current_active_user  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import (  # noqa: E402
    Camera,
    User,
    Workflow,
    WorkflowRun,
    WorkflowRunStep,
    WorkflowVersion,
)
from workflows.engine import Engine  # noqa: E402

T0 = datetime(2026, 11, 18, 7, 0, 20, tzinfo=UTC)


def wf_yaml(name, when, extra=""):
    return (f"workflow: v1\nname: {name}\nlimits: {{ cooldown: 1s }}\nwhen:\n  {when}\n{extra}"
            "then:\n  - log: \"{camera} {plate_text} {pressed_by} {time} {offline_for}\"\n")


@pytest.fixture()
def env(monkeypatch):
    sql = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(sql)
    make = sessionmaker(bind=sql)
    db = make()
    db.add(User(id=1, username="owner", email="o@x.test", hashed_password="x", is_active=True,
                role_id=1))
    db.add(Camera(id=3, name="Gate", ip_address="10.0.0.3", owner_id=1))
    db.add(Camera(id=4, name="Yard", ip_address="10.0.0.4", owner_id=1))
    db.commit()
    clock = {"now": T0}
    mode = {"mode": "disarmed"}
    from services import site_mode
    monkeypatch.setattr(site_mode, "current_mode", lambda db: mode["mode"])
    e = Engine(make, client=lambda: None, now=lambda: clock["now"], hardware="cpu", max_side=0)
    yield _types.SimpleNamespace(db=db, engine=e, clock=clock, mode=mode, make=make)
    db.close()
    sql.dispose()


def add(env, text, state="on"):
    wf = Workflow(name=yaml.safe_load(text)["name"], state=state, current_version=1)
    wf.versions.append(WorkflowVersion(version=1, yaml=text))
    env.db.add(wf)
    env.db.commit()
    env.engine.wanted_schemas(refresh=True)
    return wf


def run_in_loop(fn, *a):
    async def go():
        ids = fn(*a)
        await asyncio.gather(*fn.__self__.tasks)
        return ids
    return asyncio.run(go())


def logged(env, run_id):
    env.db.expire_all()
    return [s.reason for s in env.db.query(WorkflowRunStep).filter_by(run_id=run_id)]


# ── contracted events ─────────────────────────────────────────────────


def plate(camera="cam3", schema="plate.recognized.v1"):
    return {"id": "evt_1", "schema": schema, "camera_id": camera, "ts": "2026-11-18T07:00:00Z",
            "producer": "app:license-plate-recognition", "correlation_id": None,
            "payload": {"plate_text": "KA01AB1234", "confidence": 0.93}}


def test_a_contracted_event_starts_the_workflows_listening_for_its_schema(env):
    add(env, wf_yaml("Plates at the gate",
                     "event: { subject: plate.recognized.v1, cameras: [gate] }"))
    (run_id,) = run_in_loop(env.engine.start_from_event, plate())
    assert logged(env, run_id)[0].startswith("gate KA01AB1234")
    run = env.db.get(WorkflowRun, run_id)
    assert (run.trigger["kind"], run.trigger["schema"], run.trigger["plate_text"]) == (
        "event", "plate.recognized.v1", "KA01AB1234")
    assert run_in_loop(env.engine.start_from_event, plate(camera="cam4")) == []   # the yard


def test_events_no_workflow_listens_for_are_dropped_without_the_database(env):
    add(env, wf_yaml("Plates", "event: { subject: plate.recognized.v1 }"))
    assert env.engine.wanted_schemas() == {"plate.recognized.v1"}

    def no_db():
        raise AssertionError("a detection must not reach the database")
    env.engine.session = no_db
    assert env.engine.start_from_event(plate(schema="detection.observed.v1")) == []


# ── camera offline ────────────────────────────────────────────────────


def test_a_camera_offline_for_long_enough_starts_its_workflow_once_per_outage(env):
    add(env, wf_yaml("Gate down", "camera_offline: { cameras: [gate], for: 2m }"))
    eng = env.engine
    assert run_in_loop(eng.check_offline, {3: False, 4: True}) == []     # down, not for 2m yet
    env.clock["now"] = T0 + timedelta(minutes=1)
    assert run_in_loop(eng.check_offline, {3: False}) == []
    env.clock["now"] = T0 + timedelta(minutes=2, seconds=5)
    (run_id,) = run_in_loop(eng.check_offline, {3: False})
    assert logged(env, run_id)[0].startswith("gate ") and "125" in logged(env, run_id)[0]
    env.clock["now"] = T0 + timedelta(minutes=10)
    assert run_in_loop(eng.check_offline, {3: False}) == []              # same outage: once
    assert run_in_loop(eng.check_offline, {3: True}) == []               # back: forgotten
    env.clock["now"] = T0 + timedelta(minutes=11)
    assert run_in_loop(eng.check_offline, {3: False}) == []
    env.clock["now"] = T0 + timedelta(minutes=13, seconds=1)
    assert len(run_in_loop(eng.check_offline, {3: False})) == 1          # a new outage


def test_unknown_status_and_other_cameras_start_nothing(env):
    add(env, wf_yaml("Gate down", "camera_offline: { cameras: [gate], for: 30s }"))
    env.clock["now"] = T0
    run_in_loop(env.engine.check_offline, {3: None, 4: False})
    env.clock["now"] = T0 + timedelta(minutes=5)
    assert run_in_loop(env.engine.check_offline, {3: None, 4: False}) == []


# ── schedule ──────────────────────────────────────────────────────────


def at(dt):
    return dt.astimezone().strftime("%H:%M")


def test_a_schedule_starts_once_in_its_minute(env):
    day = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[T0.astimezone().weekday()]
    add(env, wf_yaml("Morning report", f'schedule: {{ at: "{at(T0)}", days: [{day}] }}'))
    (run_id,) = run_in_loop(env.engine.check_schedule)
    assert f" {at(T0)} " in logged(env, run_id)[0]          # {time} filled; {camera} has none
    env.clock["now"] = T0 + timedelta(seconds=15)
    assert run_in_loop(env.engine.check_schedule) == []                  # same slot
    restarted = Engine(env.make, client=lambda: None, now=lambda: env.clock["now"])
    assert run_in_loop(restarted.check_schedule) == []                   # even after a restart
    env.clock["now"] = T0 + timedelta(minutes=1)
    assert run_in_loop(env.engine.check_schedule) == []                  # not its minute


def test_a_schedule_on_other_days_or_held_back_by_its_guards_does_not_start(env):
    other = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[(T0.astimezone().weekday() + 1) % 7]
    add(env, wf_yaml("Other day", f'schedule: {{ at: "{at(T0)}", days: [{other}] }}'))
    add(env, wf_yaml("Armed only", f'schedule: {{ at: "{at(T0)}" }}',
                     "if:\n  - site_mode: [armed_away]\n"))
    assert run_in_loop(env.engine.check_schedule) == []
    env.mode["mode"] = "armed_away"
    assert len(run_in_loop(env.engine.check_schedule)) == 1


def test_tick_does_all_of_it(env):
    add(env, wf_yaml("Morning report", f'schedule: {{ at: "{at(T0)}" }}'))

    async def go():
        env.engine.tick({3: True})
        await asyncio.gather(*env.engine.tasks)
    asyncio.run(go())
    assert env.db.query(WorkflowRun).count() == 1


# ── button ────────────────────────────────────────────────────────────


class _User:
    def __init__(self, uid, perms, name="guard"):
        self.id = uid
        self.username = name
        self.is_superuser = False
        self.role = _types.SimpleNamespace(
            permissions=[_types.SimpleNamespace(name=p) for p in perms])


def test_the_button(env, monkeypatch):
    pressed = add(env, wf_yaml("Lock down", 'button: { label: "Lock down" }'))
    other = add(env, wf_yaml("Plates", "event: { subject: plate.recognized.v1 }"))
    off = add(env, wf_yaml("Off one", "button: {}"), state="off")
    import workflows.consumer as consumer
    monkeypatch.setattr(consumer, "_engine", env.engine)
    monkeypatch.setattr(wf_router, "audit_request", lambda db, request, **kw: None)
    app = FastAPI()
    app.include_router(wf_router.router, prefix="/api/v1")
    user = {"u": _User(1, {"workflows.view", "workflows.manage"})}
    app.dependency_overrides[get_db] = lambda: env.db
    app.dependency_overrides[get_current_active_user] = lambda: user["u"]
    client = TestClient(app)

    r = client.post(f"/api/v1/workflows/{pressed.id}/run")
    assert r.status_code == 200 and r.json()["test"] is False
    run_id = r.json()["run_id"]
    for _ in range(50):
        env.db.expire_all()
        if env.db.get(WorkflowRun, run_id).status == "done":
            break
    run = env.db.get(WorkflowRun, run_id)
    assert (run.trigger["kind"], run.trigger["pressed_by"]) == ("button", "guard")
    again = client.post(f"/api/v1/workflows/{pressed.id}/run")   # within its 1 s cooldown
    assert again.status_code == 409 and again.json()["detail"].startswith("Not started")
    assert "not started by a button" in client.post(
        f"/api/v1/workflows/{other.id}/run").json()["detail"]
    assert client.post(f"/api/v1/workflows/{off.id}/run").json()["detail"] == \
        "This workflow is Off"
    user["u"] = _User(2, {"workflows.view"})
    assert client.post(f"/api/v1/workflows/{pressed.id}/run").status_code == 403
