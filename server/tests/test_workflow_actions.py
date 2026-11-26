# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The last Phase 1 actions: save_clip, ptz_preset, webhook (Telegram too) and home_assistant."""

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
    assert step.outcome == "failed" and "no enabled webhook, Slack, Teams or Telegram" in step.reason
    assert hooks == []


def test_the_destinations_are_checked_when_the_file_is_saved(env, hooks):
    ok = catalog.check(flow("webhook: { to: ticketing }"), env.db)
    assert ok.ok, ok.errors
    bad = catalog.check(flow("webhook: { to: old }"), env.db)
    assert [str(e) for e in bad.errors] == [
        "then[0].webhook.to: 'old' is not an approved destination on this site"]


# ── Telegram ──────────────────────────────────────────────────────────


@pytest.fixture()
def telegram(env, monkeypatch):
    from services.integration_service import IntegrationService
    env.db.add(Integration(name="guards", type="telegram", enabled=True,
                           config={"token": "123:abc", "chat_id": "-100"}))
    env.db.commit()
    sent = []

    async def send(integration, text, **kw):
        sent.append((integration.name, text))
        return {"success": True, "message": "Telegram message sent"}
    monkeypatch.setattr(IntegrationService, "send_telegram", staticmethod(send))
    return sent


def test_a_workflow_webhook_can_go_to_telegram_as_text(env, telegram):
    (step,) = run(env, flow('webhook: { to: guards, body: { gate: "{camera}" } }'))
    assert (step.outcome, step.reason) == ("done", "Telegram message sent")
    ((name, text),) = telegram
    assert name == "guards" and text.startswith("Yard:") and text.endswith("gate: yard")
    assert catalog.check(flow("webhook: { to: guards }"), env.db).ok


def tg(handler):
    import httpx
    return httpx.MockTransport(handler)


def test_telegram_send_and_test_call_the_bot_api():
    import json as _json

    from services.integration_service import IntegrationService
    calls = []

    def ok(request):
        calls.append((str(request.url), _json.loads(request.content or b"{}")))
        method = str(request.url).rsplit("/", 1)[-1]
        result = {"getMe": {"username": "gate_bot"}, "getChat": {"id": -100}}.get(method, {})
        return __import__("httpx").Response(200, json={"ok": True, "result": result})
    integ = Integration(name="guards", type="telegram", enabled=True,
                        config={"token": "123:abc", "chat_id": "-100"})
    out = asyncio.run(IntegrationService.send_telegram(integ, "x" * 5000, silent=True,
                                                       transport=tg(ok)))
    assert out["success"] is True
    url, body = calls[-1]
    assert url == "https://api.telegram.org/bot123:abc/sendMessage"
    assert body == {"chat_id": "-100", "text": "x" * 4096, "disable_notification": True}

    test = asyncio.run(IntegrationService._test_telegram(integ, transport=tg(ok)))
    assert test == {"success": True, "message": "@gate_bot can post to that chat"}
    assert [u.rsplit("/", 1)[-1] for u, _ in calls[-2:]] == ["getMe", "getChat"]


def test_telegram_errors_say_why_and_never_show_the_token():
    import httpx

    from services.integration_service import IntegrationService
    integ = Integration(name="guards", type="telegram", enabled=True,
                        config={"token": "123:secret", "chat_id": "-100"})
    refused = asyncio.run(IntegrationService.send_telegram(
        integ, "hi", transport=tg(lambda r: httpx.Response(
            401, json={"ok": False, "description": "Unauthorized"}))))
    assert refused == {"success": False, "message": "Telegram refused the bot token"}

    def kicked(request):
        method = str(request.url).rsplit("/", 1)[-1]
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "b"}})
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})
    test = asyncio.run(IntegrationService._test_telegram(integ, transport=tg(kicked)))
    assert test["success"] is False and "chat not found" in test["message"]

    def down(request):
        raise httpx.ConnectError("boom")
    gone = asyncio.run(IntegrationService.send_telegram(integ, "hi", transport=tg(down)))
    assert gone["success"] is False and "secret" not in gone["message"]


# ── home_assistant ────────────────────────────────────────────────────


def ha_flow(spec: str) -> str:
    """A home_assistant action where it is allowed: under a plain rule."""
    return flow(f"if: {{ site_mode: [home] }}\n    then:\n      - home_assistant: {spec}")


@pytest.fixture()
def house(env, monkeypatch):
    from services import site_mode
    from services.integration_service import IntegrationService
    monkeypatch.setattr(site_mode, "current_mode", lambda db: "home")
    env.db.add(Integration(name="house", type="home_assistant", enabled=True,
                           config={"url": "http://ha.local:8123", "token": "t"}))
    env.db.add(Integration(name="ticketing", type="webhook", enabled=True,
                           config={"url": "https://tickets.example/hook"}))
    env.db.commit()
    calls = []

    async def call(integration, service, data):
        calls.append((integration.name, service, data))
        return {"success": True, "message": f"{service} called (HTTP 200)"}
    monkeypatch.setattr(IntegrationService, "call_home_assistant", staticmethod(call))
    return calls


def test_home_assistant_calls_the_service_with_its_data(env, house):
    steps = run(env, ha_flow('{ service: lock.unlock, data: { entity_id: lock.gate, '
                             'note: "{camera}" } }'))
    assert (steps[-1].kind, steps[-1].outcome) == ("home_assistant", "done")
    assert house == [("house", "lock.unlock", {"entity_id": "lock.gate", "note": "yard"})]


def test_home_assistant_by_name_and_when_there_is_none(env, house):
    steps = run(env, ha_flow("{ service: lock.unlock, to: ticketing }"))
    assert steps[-1].outcome == "failed"
    assert "no enabled Home Assistant integration named 'ticketing'" in steps[-1].reason
    assert house == []


def test_home_assistant_with_two_must_name_one(env, house):
    env.db.add(Integration(name="cabin", type="home_assistant", enabled=True,
                           config={"url": "http://cabin:8123", "token": "t"}))
    env.db.commit()
    steps = run(env, ha_flow("{ service: light.turn_on }"))
    assert steps[-1].outcome == "failed" and "name one with to: cabin, house" in steps[-1].reason
    steps = run(env, ha_flow("{ service: light.turn_on, to: cabin }"))
    assert steps[-1].outcome == "done" and house[-1][0] == "cabin"


def test_home_assistant_rest_call(monkeypatch):
    import httpx

    from services.integration_service import IntegrationService
    seen = []

    def handler(request):
        seen.append((request.method, str(request.url), request.headers["authorization"],
                     request.content))
        return httpx.Response(200, json=[])
    integ = Integration(name="house", type="home_assistant", enabled=True,
                        config={"url": "http://ha.local:8123/", "token": "abc"})
    out = asyncio.run(IntegrationService.call_home_assistant(
        integ, "lock.unlock", {"entity_id": "lock.gate"},
        transport=httpx.MockTransport(handler)))
    assert out == {"success": True, "message": "lock.unlock called (HTTP 200)"}
    assert seen == [("POST", "http://ha.local:8123/api/services/lock/unlock", "Bearer abc",
                     b'{"entity_id":"lock.gate"}')]

    refused = asyncio.run(IntegrationService.call_home_assistant(
        integ, "lock.unlock", {}, transport=httpx.MockTransport(lambda r: httpx.Response(401))))
    assert refused == {"success": False,
                       "message": "Home Assistant returned 401 (the token was refused)"}
    test = asyncio.run(IntegrationService._test_home_assistant(
        integ, transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"message": "API running."}))))
    assert test["success"] is True


# ── Test mode ─────────────────────────────────────────────────────────


def test_test_mode_records_them_and_does_nothing(env, hooks, ptz):
    clip(env, 0)
    steps = run(env, flow('save_clip: { camera: "{camera}" }',
                          'ptz_preset: { camera: "{camera}", preset: Gate }',
                          "webhook: { to: ticketing }"), state="test")
    assert [s.outcome for s in steps] == ["would_have"] * 3
    assert env.db.query(Recording).filter_by(is_flagged=True).count() == 0
    assert hooks == [] and ptz.calls == [] and env.later == []
