# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""HA-118: site mode (arming only).

* a site that never set a mode is armed_away: today's behaviour;
* changing it needs settings.manage (a token: that scope), is audited and
  pushed as a site-wide event that reaches camera-scoped subscribers;
* disarmed stops alarm ACTIONS (calls, SMS, webhook) and nothing else; the
  test button (force) still runs them;
* the v2 snapshot carries the mode for sockets entitled to it.
"""

from __future__ import annotations

import asyncio
import json

from services import event_bus_service as ebs
from tests.test_api_tokens import _as, _mint, env  # noqa: F401 - shared fixture


def test_default_is_armed_away_and_changes_are_audited(env, monkeypatch):  # noqa: F811
    sent = []

    async def fake(event):
        sent.append(event)

    bus = ebs.EventBus()
    monkeypatch.setattr(ebs, "_event_bus_instance", bus)
    monkeypatch.setattr(bus, "publish", fake)
    A = env.jwt("admin")
    assert env.client.get("/api/v1/site-mode", headers=A).json()["mode"] == "armed_away"
    r = env.client.put("/api/v1/site-mode", headers=A, json={"mode": "disarmed", "reason": "home"})
    assert r.status_code == 200 and r.json()["changed_by"] == "user:admin"
    assert env.client.get("/api/v1/site-mode", headers=A).json()["mode"] == "disarmed"
    assert env.client.put("/api/v1/site-mode", headers=A, json={"mode": "party"}).status_code == 422
    s = env.Session()
    row = s.query(env.models.AuditLog).filter_by(action="site_mode.set").one()
    s.close()
    assert json.loads(row.details) == {"from": "armed_away", "to": "disarmed", "reason": "home"}
    assert sent[0]["event_type"] == "site_mode" and sent[0]["site_wide"] is True
    assert sent[0]["payload"]["mode"] == "disarmed"


def test_who_may_arm(env):  # noqa: F811
    # vera has settings.view but not settings.manage.
    assert env.client.get("/api/v1/site-mode", headers=env.jwt("vera")).status_code == 200
    assert env.client.put("/api/v1/site-mode", headers=env.jwt("vera"),
                          json={"mode": "disarmed"}).status_code == 403
    ro = _mint(env, scopes=["settings.view"])["token"]
    assert env.client.get("/api/v1/site-mode", headers=_as(ro)).status_code == 200
    assert env.client.put("/api/v1/site-mode", headers=_as(ro),
                          json={"mode": "disarmed"}).status_code == 403
    rw = _mint(env, name="panel", scopes=["settings.view", "settings.manage"])["token"]
    r = env.client.put("/api/v1/site-mode", headers=_as(rw), json={"mode": "armed_home"})
    assert r.status_code == 200 and r.json()["changed_by"] == "token:panel"
    # settings.manage on a token reaches ONLY this route.
    assert env.client.get("/api/v1/audit-logs/", headers=_as(rw)).status_code == 403


def test_disarmed_pauses_alarm_actions_only(env, monkeypatch):  # noqa: F811
    from services import alarm_actions, site_mode

    calls = []
    monkeypatch.setattr(alarm_actions, "load_action_config", lambda db: {
        "min_severity": "low", "twilio": {"enabled": False},
        "webhook": {"enabled": True}})
    monkeypatch.setattr(alarm_actions, "_dispatch_webhook",
                        lambda wh, alert: calls.append(alert["title"]) or {"action": "webhook", "ok": True})
    alert = {"severity": "high", "title": "intruder"}
    assert len(alarm_actions.dispatch_alarm_actions(alert)) == 1
    s = env.Session()
    site_mode.set_mode(s, "disarmed", "test")
    s.close()
    assert alarm_actions.dispatch_alarm_actions(alert) == []
    assert len(alarm_actions.dispatch_alarm_actions(alert, force=True)) == 1  # test button
    s = env.Session()
    site_mode.set_mode(s, "armed_home", "test")
    s.close()
    assert len(alarm_actions.dispatch_alarm_actions(alert)) == 1
    assert calls == ["intruder", "intruder", "intruder"]


def test_site_wide_events_reach_camera_scoped_subscribers():
    bus = ebs.EventBus()

    async def run():
        async with bus.subscribe(allowed_camera_ids={1}, camera_id=1) as sub:
            await bus.publish({"event_type": "site_mode", "site_wide": True, "payload": {}})
            await bus.publish({"event_type": "system_alert", "payload": {}})  # no camera
            return [sub.queue.get_nowait()["event_type"] for _ in range(sub.queue.qsize())]

    assert asyncio.run(run()) == ["site_mode"]


def test_v2_snapshot_carries_the_mode(env, monkeypatch):  # noqa: F811
    import services.live_state as ls_mod

    monkeypatch.setattr(ebs, "_event_bus_instance", ebs.EventBus())
    monkeypatch.setattr(ls_mod, "_instance", ls_mod.LiveState())

    def snap(headers):
        t = env.client.post("/api/v1/events/ws-ticket", headers=headers).json()["ticket"]
        with env.client.websocket_connect(f"/api/v1/events/ws?ticket={t}&v=2") as ws:
            ws.receive_json()
            return ws.receive_json()

    assert snap(env.jwt("admin"))["site_mode"]["mode"] == "armed_away"
    no_settings = _mint(env, scopes=["cameras.view"])["token"]
    assert snap(_as(no_settings))["site_mode"] is None
