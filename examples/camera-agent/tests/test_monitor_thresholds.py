# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""create_monitor thresholds: "tell me when more than 3 people gather".

The occupancy rule behind kind='count' has always had max/min thresholds
with edge-triggered alerts (MonitorHost); the tool never exposed them, so
the request could not be expressed, and small models filled `target` with
the camera instead (MODELS_AND_LATENCY.md, "Measured")."""
from __future__ import annotations

import asyncio
import json

import pytest

import camera_agent as ca
from camera_agent import AppConfig, CameraAgentRuntime
from context import CameraSpec

_TWO_PEOPLE = [{"label": "person", "bbox": {"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.2}},
               {"label": "person", "bbox": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}}]


def _runtime(detections=(), state_path=None):
    cfg = AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t",
        state_path=str(state_path) if state_path else None,
        cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="front"),
                 CameraSpec(camera_id="cam2", frame_url="http://x/2.jpg", role="gate")],
    )
    rt = CameraAgentRuntime(cfg)

    async def fake_get_frame(cam, **_kw):
        return b"\xff\xd8\xff"

    async def fake_infer(*, frame_jpeg, **kw):
        return {"result": {"detections": list(detections)}}

    rt.context.get_frame = fake_get_frame
    rt.detection_client.infer = fake_infer
    return rt


async def _wait_for(predicate, *, timeout=1.5, step=0.02):
    for _ in range(int(timeout / step)):
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


def test_the_tool_offers_thresholds_and_says_target_is_not_a_camera():
    tool = ca._create_monitor_tool(["cam1", "cam2"])["function"]
    props = tool["parameters"]["properties"]
    assert props["max_count"]["type"] == "integer"
    assert props["min_count"]["type"] == "integer"
    assert "camera" in props["target"]["description"].lower()
    assert "max_count=3" in tool["description"]


async def test_more_than_three_people_arms_a_count_watch_with_a_ceiling():
    rt = _runtime()
    try:
        reply = await rt._handle_create_monitor(
            {"kind": "count", "target": "person", "camera_id": "cam1", "max_count": 3})
        (mon,) = rt.monitors.list()
        assert (mon["kind"], mon["target"], mon["max_count"]) == ("count", "person", 3)
        assert "more than 3" in reply
    finally:
        rt.monitors.stop_all()


async def test_a_threshold_turns_notify_into_count():
    rt = _runtime()
    try:
        await rt._handle_create_monitor(
            {"kind": "notify", "target": "person", "camera_id": "cam1", "max_count": "3"})
        (mon,) = rt.monitors.list()
        assert mon["kind"] == "count" and mon["max_count"] == 3
    finally:
        rt.monitors.stop_all()


@pytest.mark.parametrize("args,expect", [
    # A real line plus a threshold: ambiguous, so ask.
    ({"kind": "crossing", "line": [0, 0, 1, 1], "max_count": 3}, "threshold"),
    ({"kind": "count", "max_count": "many"}, "whole number"),
    ({"kind": "count", "min_count": -1}, "negative"),
])
async def test_a_bad_threshold_asks_back_and_creates_nothing(args, expect):
    rt = _runtime()
    reply = await rt._handle_create_monitor({"target": "person", "camera_id": "cam1", **args})
    assert expect in reply
    assert rt.monitors.list() == []


async def test_the_ceiling_alerts_once_when_crossed():
    rt = _runtime(detections=_TWO_PEOPLE)
    rt.monitors._default_interval = 0.02
    try:
        await rt._handle_create_monitor(
            {"kind": "count", "target": "person", "camera_id": "cam1", "max_count": 1})
        assert await _wait_for(lambda: rt.monitors.notifications())
        assert "Over-occupancy" in rt.monitors.notifications()[0]["text"]
        n = len(rt.monitors.notifications())
        await asyncio.sleep(0.15)
        assert len(rt.monitors.notifications()) == n, "edge-triggered, not every poll"
    finally:
        rt.monitors.stop_all()


async def test_a_plain_count_stays_silent():
    rt = _runtime(detections=_TWO_PEOPLE)
    rt.monitors._default_interval = 0.02
    try:
        await rt._handle_create_monitor({"kind": "count", "target": "person", "camera_id": "cam1"})
        await asyncio.sleep(0.15)
        assert rt.monitors.notifications() == []
    finally:
        rt.monitors.stop_all()


async def test_a_threshold_is_its_own_watch_but_the_same_one_is_a_duplicate():
    rt = _runtime()
    try:
        base = {"kind": "count", "target": "person", "camera_id": "cam1"}
        await rt._handle_create_monitor(base)
        await rt._handle_create_monitor({**base, "max_count": 3})
        assert len(rt.monitors.list()) == 2
        again = await rt._handle_create_monitor({**base, "max_count": 3})
        assert "already covers" in again and len(rt.monitors.list()) == 2
    finally:
        rt.monitors.stop_all()


def test_thresholds_survive_a_restart(tmp_path):
    state = tmp_path / "state.json"

    async def arm():
        rt = _runtime(state_path=state)
        await rt._handle_create_monitor(
            {"kind": "count", "target": "person", "camera_id": "cam2",
             "max_count": 3, "min_count": 1})
        rt.monitors.stop_all()
    asyncio.run(arm())
    saved = json.loads(state.read_text())["monitors"][0]
    assert (saved["max_count"], saved["min_count"]) == (3, 1)

    async def restart():
        rt = _runtime(state_path=state)
        rt.load_state()
        try:
            (mon,) = rt.monitors.list()
            return mon
        finally:
            rt.monitors.stop_all()
    mon = asyncio.run(restart())
    assert (mon["max_count"], mon["min_count"]) == (3, 1)


# ── The question as a backstop: small models drop max_count ──────────────

@pytest.mark.parametrize("text,want", [
    ("notify me when more than 3 people gather on cam1", {"max_count": 3}),
    ("tell me if there are over five cars in the lot", {"max_count": 5}),
    ("alert when fewer than 2 guards are at the gate", {"min_count": 2}),
    ("count cars over 10 minutes", {}),
    ("notify me when you see a person after 6pm", {}),
    ("watch the door over the next hour", {}),
])
def test_count_thresholds_from_text(text, want):
    assert ca._count_thresholds_from_text(text) == want


async def test_a_dropped_threshold_is_taken_from_the_question():
    """qwen2.5:1.5b: kind='count', target='person', no max_count — then
    replied "watching for more than 3 people" over a silent tally."""
    rt = _runtime()
    rt.tools.current_question = "notify me when more than 3 people gather on cam1"
    try:
        reply = await rt._handle_create_monitor(
            {"kind": "count", "target": "person", "camera_id": "cam1"})
        (mon,) = rt.monitors.list()
        assert mon["max_count"] == 3 and "more than 3" in reply
    finally:
        rt.monitors.stop_all()


async def test_a_crossing_with_a_threshold_and_no_line_is_a_count():
    """qwen2.5:1.5b also tried kind='crossing', max_count=3, a junk line."""
    rt = _runtime()
    try:
        await rt._handle_create_monitor(
            {"kind": "crossing", "target": "person", "camera_id": "cam1",
             "max_count": 3, "line": "[0.5, 0.5, 0.5, 0.5]"})
        (mon,) = rt.monitors.list()
        assert (mon["kind"], mon["max_count"]) == ("count", 3)
    finally:
        rt.monitors.stop_all()


async def test_a_plain_count_question_stays_a_plain_count():
    rt = _runtime()
    rt.tools.current_question = "count people on cam1 over 10 minutes"
    try:
        await rt._handle_create_monitor({"kind": "count", "target": "person", "camera_id": "cam1"})
        (mon,) = rt.monitors.list()
        assert "max_count" not in mon
    finally:
        rt.monitors.stop_all()
