# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Scheduled recurring reports: due() logic, run-now generation + delivery,
voice handlers, and endpoints. The due() math is tested with injected
datetimes so there's no timing flakiness."""
from __future__ import annotations

import asyncio
import datetime

from fastapi.testclient import TestClient

import camera_agent as ca
from camera_agent import AppConfig, CameraAgentRuntime, ReportSchedule, build_app
from context import CameraSpec


def _runtime(webhooks=None):
    cfg = AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t",
        notify_webhooks=webhooks, notify_events=["alarm", "notify", "report"],
        cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="front")],
    )
    return CameraAgentRuntime(cfg)


# ── due() logic (no real waiting) ──────────────────────────────────────


def test_every_minutes_due_when_never_run_then_respects_interval():
    s = ReportSchedule(id=1, name="r", query="q", every_minutes=30)
    assert s.due() is True            # never run → due
    s.last_run = datetime.datetime.now().timestamp()
    assert s.due() is False           # just ran → not due


def test_daily_at_due_only_after_time_and_once_per_day():
    s = ReportSchedule(id=1, name="r", query="q", at_min=7 * 60)  # 07:00
    before = datetime.datetime(2026, 1, 1, 6, 30)
    at = datetime.datetime(2026, 1, 1, 7, 5)
    assert s.due(now=before) is False           # before 07:00
    assert s.due(now=at) is True                # after 07:00, never run
    s.last_run = datetime.datetime(2026, 1, 1, 7, 1).timestamp()
    assert s.due(now=at) is False               # already ran today
    next_day = datetime.datetime(2026, 1, 2, 7, 5)
    assert s.due(now=next_day) is True          # due again next day


# ── run_now: generation + delivery ─────────────────────────────────────


def test_run_now_generates_delivers_and_records(monkeypatch):
    rt = _runtime(webhooks=["http://hook"])
    posts = []

    class _Client:
        async def post(self, url, json=None):
            posts.append((url, json))
            class _R: status_code = 200
            return _R()
    rt.notifier._client = _Client()

    async def fake_turn(runtime, history, query, *, tool_definitions=None, **kw):
        names = [t["function"]["name"] for t in (tool_definitions or [])]
        assert "create_report" not in names      # reports can't schedule reports
        return f"summary for: {query}"

    monkeypatch.setattr(ca, "_run_conversation_turn", fake_turn)

    async def go():
        sched = rt.reports.create(name="Morning", query="overnight activity", at_min=7 * 60)
        result = await rt.reports.run_now(sched.id)
        assert result == "summary for: overnight activity"
        # recorded
        reps = rt.reports.reports()
        assert reps and reps[-1]["text"] == result
        assert rt.reports.get(sched.id).run_count == 1
        # delivered to webhook as type "report"
        await asyncio.sleep(0.05)
        assert posts and posts[-1][1]["type"] == "report"

    asyncio.run(go())


def test_run_now_unknown_returns_none():
    rt = _runtime()
    assert asyncio.run(rt.reports.run_now(999)) is None


# ── voice handlers ─────────────────────────────────────────────────────


def test_create_report_handler_parses_time_and_defaults(monkeypatch):
    rt = _runtime()

    async def go():
        msg = await rt._handle_create_report({"name": "AM", "query": "overnight", "at": "07:00"})
        assert "scheduled report #" in msg.lower()
        assert rt.reports.list()[0]["schedule"] == "daily at 07:00"
        # no time given → defaults to 08:00 daily
        await rt._handle_create_report({"name": "B", "query": "x"})
        assert rt.reports.list()[1]["schedule"] == "daily at 08:00"
        # missing query → asks
        ask = await rt._handle_create_report({"name": "C"})
        assert ask.endswith("?")

    asyncio.run(go())


def test_stop_report_handler():
    rt = _runtime()

    async def go():
        s = rt.reports.create(name="r", query="q", every_minutes=10)
        assert f"#{s.id}" in await rt._handle_stop_report({"report_id": s.id})
        # Cancel now REMOVES the schedule (stop-and-forget parity with
        # alarms/watches) — it must be gone, not parked inactive.
        assert rt.reports.get(s.id) is None
        assert all(r["id"] != s.id for r in rt.reports.list())

    asyncio.run(go())


# ── endpoints ──────────────────────────────────────────────────────────


def test_report_endpoints(monkeypatch):
    rt = _runtime()

    async def fake_turn(*a, **k):
        return "the summary"
    monkeypatch.setattr(ca, "_run_conversation_turn", fake_turn)

    client = TestClient(build_app(rt))
    r = client.post("/reports", json={"name": "AM", "query": "overnight", "at": "07:00"})
    assert r.status_code == 202
    rid = client.get("/reports").json()["schedules"][0]["id"]
    run = client.post(f"/reports/{rid}/run")
    assert run.status_code == 200 and run.json()["result"] == "the summary"
    body = client.get("/reports").json()
    assert body["reports"] and body["reports"][-1]["text"] == "the summary"
    assert client.delete(f"/reports/{rid}").status_code == 200
    assert client.post("/reports/9999/run").status_code == 404
    assert client.post("/reports", json={"name": "x"}).status_code == 400
