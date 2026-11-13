# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""GET /turn-stats: what turns cost by tier and by tool, from the traces."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

import camera_agent as ca
from camera_agent import AppConfig, CameraAgentRuntime, build_app
from context import CameraSpec
from turn_stats import TurnStats


def _trace_tier0(ms_tool=600, ms_llm=400):
    return [{"step": "route", "detail": "tier0 detect_objects", "ms": 0},
            {"step": "detect_objects", "detail": "cam1", "by": "router", "ok": True, "ms": ms_tool},
            {"step": "llm", "detail": "compose", "ms": ms_llm},
            {"step": "reply", "detail": "llm"}]


def _trace_tier1():
    return [{"step": "route", "detail": "tier1 hint=create_alarm", "ms": 0},
            {"step": "prompt", "detail": "window 4096: 8 of 8 tools", "ms": 0},
            {"step": "llm", "detail": "iter 1", "ms": 1500},
            {"step": "create_alarm", "detail": "", "by": "model", "ok": True, "ms": 5},
            {"step": "llm", "detail": "iter 2", "ms": 1200},
            {"step": "reply", "detail": "llm"}]


def _trace_tier2_failed_tool():
    return [{"step": "route", "detail": "tier2", "ms": 0},
            {"step": "llm", "detail": "iter 1", "ms": 1900},
            {"step": "recent_events", "detail": "cam1", "by": "model", "ok": False, "ms": 0},
            {"step": "llm", "detail": "iter 2", "ms": 1250},
            {"step": "reply", "detail": "llm"}]


def test_summarise_reads_tier_tools_and_times_off_a_trace():
    row = TurnStats.summarise(_trace_tier1(), 2800, "voice")
    assert row["tier"] == 1 and row["hint"] == "create_alarm" and row["routed_tool"] is None
    assert [t["tool"] for t in row["tools"]] == ["create_alarm"]
    assert row["llm_ms"] == 2700 and row["llm_calls"] == 2 and row["tool_ms"] == 5
    assert row["total_ms"] == 2800 and row["source"] == "voice" and row["reply_source"] == "llm"
    row0 = TurnStats.summarise(_trace_tier0(), 1100, None)
    assert row0["tier"] == 0 and row0["routed_tool"] == "detect_objects" and row0["source"] == "background"


def test_summary_aggregates_by_tier_and_tool():
    s = TurnStats()
    for ms in (900, 1100, 1300):
        s.record(_trace_tier0(ms_tool=ms - 400), ms, "text")
    s.record(_trace_tier1(), 2800, "voice")
    s.record(_trace_tier2_failed_tool(), 3200, "voice")
    out = s.summary(recent=2)
    assert out["turns"] == 5
    assert out["by_tier"]["tier0"]["turns"] == 3
    assert out["by_tier"]["tier0"]["total_ms_p50"] == 1100
    assert out["by_tier"]["tier1"]["llm_ms_p50"] == 2700
    assert out["by_tool"]["detect_objects"] == {"calls": 3, "failed": 0, "by_router": 3, "by_model": 0,
                                                "ms_p50": 700, "ms_p95": 900}
    assert out["by_tool"]["recent_events"]["failed"] == 1
    assert out["sources"] == {"text": 3, "voice": 2}
    assert out["reply_sources"] == {"llm": 5}
    assert len(out["recent"]) == 2 and out["recent"][-1]["tools"] == ["recent_events"]


def test_the_ring_is_bounded():
    s = TurnStats(maxlen=3)
    for i in range(10):
        s.record(_trace_tier0(), i, "text")
    assert s.summary()["turns"] == 3


def _runtime():
    return CameraAgentRuntime(AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t", text_mode=True,
        synthetic_detection=True,
        cameras=[CameraSpec(camera_id="cam1", frame_url="synth:people=1", role="the gate")]))


class _NoToolOllama:
    def __init__(self):
        self.n = 0

    async def chat(self, **kw):
        self.n += 1
        return {"message": {"content": "Someone is at the gate.", "tool_calls": []}}

    async def aclose(self):
        return None


def test_a_turn_is_recorded_and_served_by_the_endpoint():
    rt = _runtime()
    rt.ollama = _NoToolOllama()
    asyncio.run(ca._run_conversation_turn(rt, [], "is anyone at the gate", preferred_camera="cam1"))
    body = TestClient(build_app(rt)).get("/turn-stats?recent=5").json()
    assert body["turns"] == 1
    (row,) = body["recent"]
    assert row["tier"] == 0 and row["routed_tool"] == "detect_objects"
    assert "detect_objects" in body["by_tool"] and body["by_tool"]["detect_objects"]["by_router"] == 1
    assert row["total_ms"] >= 0 and body["by_tier"]["tier0"]["turns"] == 1
