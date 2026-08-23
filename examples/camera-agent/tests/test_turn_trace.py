# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-turn pipeline trace: every LLM iteration and tool call a turn runs is
recorded in order with latency, so the UI can show the flow (llm →
describe_camera → llm) and a degraded describe names its fallback path."""
from __future__ import annotations

import asyncio

import camera_agent as ca
from camera_agent import AppConfig, CameraAgentRuntime
from context import CameraSpec


class _ScriptedLLM:
    """First call: ask for a tool. Second call: compose the answer."""
    def __init__(self):
        self.calls = 0

    async def chat(self, **kw):
        self.calls += 1
        if self.calls == 1:
            return {"message": {"content": "", "tool_calls": [{
                "id": "t1", "type": "function",
                "function": {"name": "describe_camera",
                             "arguments": {"camera_id": "cam1"}}}]}}
        return {"message": {"content": "There is a person at the door.",
                            "tool_calls": []}}


def _runtime():
    cfg = AppConfig(kaic_url="http://k", kaic_api_key="key", system_prompt="t",
                    cameras=[CameraSpec("cam1", "http://x/f.jpg", "front")])
    rt = CameraAgentRuntime(cfg)
    rt.ollama = _ScriptedLLM()

    class _Src:
        def fetch(self):
            return b"\xff\xd8jpeg"
    rt.context.register_frame_source("cam1", _Src())

    class _Caption:
        async def infer(self, **kw):
            return {"result": {"caption": "a person at the door"}}
    rt.tools._caption = _Caption()
    return rt


def test_trace_records_llm_and_tool_steps_in_order():
    rt = _runtime()
    reply = asyncio.run(ca._run_conversation_turn(rt, [], "what do you see on cam1?"))
    assert "person" in reply.lower()
    steps = [(t["step"], t["detail"]) for t in rt.last_turn_trace]
    assert steps[0][0] == "llm"
    assert steps[1][0] == "describe_camera"
    assert "cam1" in steps[1][1] and "vlm" in steps[1][1]   # names the path
    assert steps[2][0] == "llm"
    assert all(t["ms"] >= 0 for t in rt.last_turn_trace)


def test_trace_marks_detector_fallback_when_vision_down():
    rt = _runtime()

    class _Raising:
        async def infer(self, **kw):
            raise RuntimeError("403 Forbidden")

    class _Detect:
        async def infer(self, **kw):
            return {"result": {"detections": [{"label": "person", "score": 0.9}]}}
    rt.tools._caption = _Raising()
    rt.tools._detect = _Detect()
    asyncio.run(ca._run_conversation_turn(rt, [], "what do you see on cam1?"))
    tool_steps = [t for t in rt.last_turn_trace if t["step"] == "describe_camera"]
    assert tool_steps and "detector-fallback" in tool_steps[0]["detail"]


def test_trace_resets_each_turn():
    rt = _runtime()
    asyncio.run(ca._run_conversation_turn(rt, [], "what do you see on cam1?"))
    first = list(rt.last_turn_trace)
    rt.ollama = _ScriptedLLM()               # fresh script for turn 2
    asyncio.run(ca._run_conversation_turn(rt, [], "what do you see on cam1?"))
    assert rt.last_turn_trace is not first   # new list, not accumulation
