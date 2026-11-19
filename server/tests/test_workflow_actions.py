# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The last Phase 1 actions: save_clip, ptz_preset and webhook."""

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
os.environ.setdefault("DATABASE_URL", "sqlite:///./_workflow_actions_test.db")
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

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.database import Base  # noqa: E402
from models import (  # noqa: E402
    Camera,
    InstalledApp,
    Integration,
    Recording,
    User,
    Workflow,
    WorkflowRunStep,
    WorkflowVersion,
)
from workflows import catalog  # noqa: E402
from workflows.engine import Engine  # noqa: E402

T0 = datetime(2026, 11, 19, 2, 14, 0, tzinfo=UTC)


def flow(*actions: str, state: str = "on") -> str:
    body = "".join(f"  - {a}\n" for a in actions)
    return ("workflow: v1\nname: Yard\nwhen:\n  alert: { app: intrusion-detection }\n"
            "if:\n  - camera: [yard]\nthen:\n" + body)


@pytest.fixture()
def env(monkeypatch):
    sql = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(sql)
    make = sessionmaker(bind=sql)
    db = make()
    db.add(User(id=1, username="owner", email="o@x.test", hashed_password="x", is_active=True,
                role_id=1))
    db.add(Camera(id=3, name="Yard", ip_address="10.0.0.3", owner_id=1, username="admin"))
    db.add(InstalledApp(id="intrusion-detection", name="i", version="1", url="http://i",
                        manifest_json={"actions": []}, config_json={}))
    db.commit()
    later = []
    e = Engine(make, client=lambda: None, now=lambda: T0, hardware="cpu", max_side=0)
    monkeypatch.setattr(e, "later", lambda secs, fn, *a: later.append((secs, fn, a)))
    yield _types.SimpleNamespace(db=db, engine=e, make=make, later=later)
    db.close()
    sql.dispose()


def run(env, text, state="on"):
    wf = Workflow(name="Yard", state=state, current_version=1)
    wf.versions.append(WorkflowVersion(version=1, yaml=text))
    env.db.add(wf)
    env.db.commit()
    alert = {"alert_id": f"a{wf.id}", "title": "Intrusion", "severity": "high",
             "source": {"kind": "app", "name": "intrusion-detection"}, "camera_id": "cam3",
             "alert_type": "intrusion", "evidence": {}}

    async def go():
        ids = env.engine.start_from_alert(alert)
        await asyncio.gather(*env.engine.tasks)
        return ids
    (run_id,) = asyncio.run(go())
    env.db.expire_all()
    return env.db.query(WorkflowRunStep).filter_by(run_id=run_id).order_by(
        WorkflowRunStep.seq).all()


# ── save_clip ─────────────────────────────────────────────────────────


def clip(env, minutes):
    start = T0 + timedelta(minutes=minutes)
    env.db.add(Recording(filename=f"{minutes}.mp4", file_path=f"/r/{minutes}.mp4", camera_id=3,
                         start_time=start, end_time=start + timedelta(minutes=1)))
    env.db.commit()


def test_save_clip_protects_the_moment_and_comes_back_for_the_rest(env):
    for m in (-5, -1, 0, 3):
        clip(env, m)
    (step,) = run(env, flow('save_clip: { camera: "{camera}", before: 1m, after: 1m }'))
    assert step.outcome == "done" and "protected 2 clip(s) of Yard" in step.reason
    flagged = sorted(r.filename for r in env.db.query(Recording).filter_by(is_flagged=True))
    assert flagged == ["-1.mp4", "0.mp4"]                 # overlap [T0-1m, T0+1m), not 3 or -5
    ((secs, fn, args),) = env.later                        # the second pass, once recorded
    assert secs == pytest.approx(60 + 90)
    clip(env, 0.5)                                         # indexed after the run, in the window
    fn(*args)
    assert env.db.query(Recording).filter_by(is_flagged=True).count() == 3


def test_save_clip_on_an_unknown_camera_fails_the_step(env):
    (step,) = run(env, flow("save_clip: { camera: gate }"))
    assert (step.outcome, step.reason) == ("failed", "no camera with the role 'gate'")


# ── ptz_preset ────────────────────────────────────────────────────────


@pytest.fixture()
def ptz(monkeypatch):
    from services import ptz_presets_cache
    from services.ptz_service import PTZService
    calls = []
    cache = {}

    async def presets(**kw):
        calls.append(("presets", kw["camera_id"]))
        return [{"name": "Gate", "token": "2"}, {"name": "Home", "token": "1"}]

    async def goto(**kw):
        calls.append(("goto", kw["camera_id"], kw["preset_token"], kw["username"]))
        return {}
    monkeypatch.setattr(PTZService, "presets", staticmethod(presets))
    monkeypatch.setattr(PTZService, "goto_preset", staticmethod(goto))
    monkeypatch.setattr(ptz_presets_cache, "token_for", lambda cam, name: cache.get(name))
    return _types.SimpleNamespace(calls=calls, cache=cache)


def test_ptz_preset_by_name_from_the_cache_or_the_camera(env, ptz):
    ptz.cache["Gate"] = "2"
    (step,) = run(env, flow('ptz_preset: { camera: "{camera}", preset: Gate }'))
    assert (step.outcome, step.reason) == ("done", "Yard to Gate")
    assert ptz.calls == [("goto", 3, "2", "admin")]
    ptz.cache.clear()
    ptz.calls.clear()
    (step,) = run(env, flow('ptz_preset: { camera: "{camera}", preset: Home }'))
    assert step.outcome == "done" and ptz.calls == [("presets", 3), ("goto", 3, "1", "admin")]


def test_ptz_preset_failures_fail_the_step_not_the_run(env, ptz, monkeypatch):
    (step,) = run(env, flow('ptz_preset: { camera: "{camera}", preset: Roof }'))
    assert (step.outcome, step.reason) == ("failed", "Yard has no preset 'Roof'")
    from services.ptz_service import PTZService

    async def down(**kw):
        raise TimeoutError("camera timed out")
    monkeypatch.setattr(PTZService, "presets", staticmethod(down))
    (step,) = run(env, flow('ptz_preset: { camera: "{camera}", preset: Gate }'))
    assert step.outcome == "failed" and "did not take the preset" in step.reason


# ── webhook ───────────────────────────────────────────────────────────


@pytest.fixture()
def hooks(env, monkeypatch):
    from services.integration_service import IntegrationService
    env.db.add(Integration(name="ticketing", type="webhook", enabled=True,
                           config={"url": "https://tickets.example/hook"}))
    env.db.add(Integration(name="old", type="webhook", enabled=False,
                           config={"url": "https://old.example/hook"}))
    env.db.add(Integration(name="mail", type="email", enabled=True, config={}))
    env.db.commit()
    sent = []

    async def send(integration, payload):
        sent.append((integration.name, payload))
        return {"success": True, "message": "Webhook delivered successfully (HTTP 200)"}
    monkeypatch.setattr(IntegrationService, "_send_webhook", staticmethod(send))
    return sent


def test_webhook_to_an_approved_destination_with_the_run_and_the_body(env, hooks):
    (step,) = run(env, flow('webhook: { to: ticketing, body: { priority: high, cam: "{camera}" } }'))
    assert (step.outcome, step.reason) == ("done", "Webhook delivered successfully (HTTP 200)")
    ((name, payload),) = hooks
    assert name == "ticketing"
    assert (payload["workflow"], payload["camera"], payload["priority"], payload["cam"]) == (
        "Yard", "yard", "high", "yard")
    assert payload["correlation_id"] and payload["text"].startswith("Yard:")


@pytest.mark.parametrize("to", ["old", "mail", "nobody"])
def test_webhook_only_to_an_enabled_webhook_slack_or_teams(env, hooks, to):
    (step,) = run(env, flow(f"webhook: {{ to: {to} }}"))
    assert step.outcome == "failed" and "no enabled webhook, Slack or Teams" in step.reason
    assert hooks == []


def test_the_destinations_are_checked_when_the_file_is_saved(env, hooks):
    ok = catalog.check(flow("webhook: { to: ticketing }"), env.db)
    assert ok.ok, ok.errors
    bad = catalog.check(flow("webhook: { to: old }"), env.db)
    assert [str(e) for e in bad.errors] == [
        "then[0].webhook.to: 'old' is not an approved destination on this site"]


# ── Test mode ─────────────────────────────────────────────────────────


def test_test_mode_records_them_and_does_nothing(env, hooks, ptz):
    clip(env, 0)
    steps = run(env, flow('save_clip: { camera: "{camera}" }',
                          'ptz_preset: { camera: "{camera}", preset: Gate }',
                          "webhook: { to: ticketing }"), state="test")
    assert [s.outcome for s in steps] == ["would_have"] * 3
    assert env.db.query(Recording).filter_by(is_flagged=True).count() == 0
    assert hooks == [] and ptz.calls == [] and env.later == []
