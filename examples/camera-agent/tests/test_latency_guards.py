# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Latency guards: background detection loops yield to interactive turns, and
persisted watches/alarms/reports collapse duplicates on restore so a testing
pile-up doesn't keep hammering the shared local models."""
from __future__ import annotations

import asyncio

import pytest

from camera_agent import AppConfig, CameraAgentRuntime
from context import CameraSpec


def _runtime():
    cfg = AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t",
        cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="front")],
    )
    rt = CameraAgentRuntime(cfg)

    async def fake_get_frame(cam, **_kw):
        return b"\xff\xd8\xff"

    async def fake_infer(*, frame_jpeg, **kw):
        return {"result": {"detections": [{"label": "fire"}]}}

    rt.context.get_frame = fake_get_frame
    rt.detection_client.infer = fake_infer
    return rt


# ── interactive-turn gate ──────────────────────────────────────────────


def test_interactive_turn_counter_nests():
    rt = _runtime()
    assert not rt.interactive_busy()
    with rt.interactive_turn():
        assert rt.interactive_busy()
        with rt.interactive_turn():          # concurrent turns
            assert rt.interactive_busy()
        assert rt.interactive_busy()         # one still outstanding
    assert not rt.interactive_busy()


@pytest.mark.asyncio
async def test_alarm_loop_yields_during_interactive_turn():
    rt = _runtime()
    rt.alarms._interval = 0.02
    calls = {"n": 0}

    async def counting_infer(*, frame_jpeg, **kw):
        calls["n"] += 1
        return {"result": {"detections": [{"label": "fire"}]}}

    rt.detection_client.infer = counting_infer

    # A turn is in flight before the alarm arms: the first cycles yield…
    rt._interactive_turns = 1
    rt.alarms.create(name="a", target="fire", camera_ids=["cam1"], ring="chime")
    await asyncio.sleep(0.05)                 # < _MAX_BUSY_SKIPS cycles
    assert calls["n"] == 0                    # nothing inferred while busy

    # …but the yield is BOUNDED: a safety check must not wait out a long
    # conversation. Past the skip cap the loop polls even mid-turn.
    await asyncio.sleep(0.2)                  # well past the cap
    assert calls["n"] > 0

    # And with no turn in flight it keeps polling normally.
    rt._interactive_turns = 0
    before = calls["n"]
    await asyncio.sleep(0.1)
    assert calls["n"] > before
    rt.alarms.stop_all()


@pytest.mark.asyncio
async def test_monitor_loop_yields_during_interactive_turn():
    rt = _runtime()
    rt.monitors._default_interval = 0.02
    calls = {"n": 0}

    async def counting_infer(*, frame_jpeg, **kw):
        calls["n"] += 1
        return {"result": {"detections": []}}

    rt.detection_client.infer = counting_infer

    rt._interactive_turns = 1
    rt.monitors.create(kind="notify", camera_ids=["cam1"], target="person",
                       interval_s=0.02)
    await asyncio.sleep(0.15)
    assert calls["n"] == 0

    rt._interactive_turns = 0
    await asyncio.sleep(0.15)
    assert calls["n"] > 0
    rt.monitors.stop_all()


# ── restore dedup ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restore_dedupes_alarms():
    rt = _runtime()
    rt.alarms.restore([
        {"name": "Fire", "target": "fire", "camera_ids": ["cam1"]},
        {"name": "Fire", "target": "fire", "camera_ids": ["cam1"]},   # dup
        {"name": "fire", "target": "fire", "camera_ids": ["cam1"]},   # dup (case)
        {"name": "Gate", "target": "person", "camera_ids": ["cam1"]}, # distinct
    ])
    armed = rt.alarms.export()
    assert len(armed) == 2
    names = sorted(a["name"].lower() for a in armed)
    assert names == ["fire", "gate"]
    rt.alarms.stop_all()


@pytest.mark.asyncio
async def test_restore_dedupes_monitors():
    rt = _runtime()
    rt.monitors.restore([
        {"kind": "notify", "target": "person", "camera_ids": ["cam1"]},
        {"kind": "notify", "target": "person", "camera_ids": ["cam1"]},  # dup
        {"kind": "notify", "target": "person", "camera_ids": ["cam1", "cam1"]},  # dup (sorted)
        {"kind": "count", "target": "person", "camera_ids": ["cam1"]},   # distinct kind
    ])
    assert len(rt.monitors.export()) == 2
    rt.monitors.stop_all()


def test_restore_dedupes_reports():
    rt = _runtime()
    rt.reports.restore([
        {"name": "AM", "query": "overnight summary", "every_minutes": 60},
        {"name": "AM", "query": "overnight summary", "every_minutes": 60},  # dup
        {"name": "PM", "query": "overnight summary", "every_minutes": 60},  # distinct
    ])
    assert len(rt.reports.export()) == 2


# ── the middleware brackets a real request end-to-end ──────────────────


def test_ask_request_is_bracketed_as_interactive(monkeypatch):
    """During an /ask turn the runtime reports busy (so background loops
    yield); after the response it's released."""
    import camera_agent as ca
    from fastapi.testclient import TestClient

    rt = _runtime()
    seen: dict[str, bool] = {}

    async def fake_turn(runtime, history, text, **kw):
        seen["busy_during"] = runtime.interactive_busy()
        return "ok"

    monkeypatch.setattr(ca, "_run_conversation_turn", fake_turn)
    app = ca.build_app(rt)
    with TestClient(app) as client:
        r = client.post("/ask", json={"text": "hello"})
    assert r.status_code == 200
    assert seen["busy_during"] is True          # loops would yield mid-turn
    assert rt.interactive_busy() is False        # released after the response
