# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The prompt prefix Ollama caches — system text + tool schemas — and the
things that used to break it between the pre-warm and the first question.

Measured on a CPU host (Ventuno Q, 2026-11-01): the pre-warm cached 4,958
tokens; a voice turn with a camera selected in the UI reused 1,267 of them
and spent 288 s of all eight cores re-prefilling; a turn by an admin with
no camera selected reused 4,952. The difference was one system message —
"the user is currently viewing camera X" — which Ollama collates into the
leading system block, in front of the tool schemas. Nothing logged it.

What these tests defend:
  * the selected camera rides in the USER turn, never as a second system
    message, and the router's hint joins it rather than replacing it;
  * the pre-warm records the prefix it cached, and a turn whose prefix
    differs says which part changed (log + trace);
  * rebuilding the tools into something different re-warms the prefix;
  * the voice service puts the clock in the user turn, not the system prompt.
"""
from __future__ import annotations

import asyncio
import logging

import camera_agent as ca
from camera_agent import AppConfig, CameraAgentRuntime, _prefix_fingerprint
from context import CameraSpec
from services import with_clock_in_user_turn


def _runtime(spec: str) -> CameraAgentRuntime:
    cfg = AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t",
                    text_mode=True, synthetic_detection=True,
                    cameras=[CameraSpec(camera_id="front_door", frame_url=spec,
                                        role="the front door")])
    return CameraAgentRuntime(cfg)


class _NoToolOllama:
    """Answers without a tool call, then composes."""
    def __init__(self, first):
        self.first, self.n = first, 0
    async def chat(self, **kw):
        self.n += 1
        return {"message": {"content": self.first if self.n == 1 else "Done.", "tool_calls": []}}
    async def aclose(self):
        return None


class _Capture:
    """Answers plainly and keeps every request's messages and tools."""
    def __init__(self):
        self.calls = []
    async def chat(self, **kw):
        self.calls.append(kw)
        return {"message": {"content": "Nobody at the door.", "tool_calls": []}}
    async def aclose(self):
        return None


def test_the_selected_camera_rides_in_the_user_turn_not_a_second_system_message():
    rt = _runtime("synth:people=0")
    rt.ollama = _Capture()
    # Small talk is not routed, so the LLM sees the full assembly.
    asyncio.run(ca._run_conversation_turn(rt, [], "hi, what can you do?", preferred_camera="front_door"))
    msgs = rt.ollama.calls[0]["messages"]
    assert [m["role"] for m in msgs if m["role"] == "system"] == ["system"], msgs
    assert msgs[-1]["role"] == "user"
    assert "currently viewing camera 'front_door'" in msgs[-1]["content"]
    assert "hi, what can you do?" in msgs[-1]["content"]
    assert "currently viewing" not in msgs[0]["content"]


def test_the_router_hint_joins_the_camera_hint_instead_of_replacing_it():
    """A past-tense question gets the router's tool hint in the same user
    turn; the camera hint must survive it."""
    rt = _runtime("synth:people=0")
    rt.ollama = _Capture()
    asyncio.run(ca._run_conversation_turn(
        rt, [], "did anyone come by this morning?", preferred_camera="front_door"))
    first = rt.ollama.calls[0]["messages"]
    user = first[-1]["content"]
    if "most likely needs" in user:          # the router hinted
        assert "currently viewing camera 'front_door'" in user
    assert sum(1 for m in first if m["role"] == "system") == 1


def test_prefix_fingerprint_names_the_part_that_changed():
    a = _prefix_fingerprint("sys", [{"function": {"name": "x"}}])
    assert a == _prefix_fingerprint("sys", [{"function": {"name": "x"}}])
    assert _prefix_fingerprint("sys2", [{"function": {"name": "x"}}])[0] != a[0]
    assert _prefix_fingerprint("sys2", [{"function": {"name": "x"}}])[1] == a[1]
    assert _prefix_fingerprint("sys", [])[1] != a[1]


def test_a_turn_with_a_different_prefix_says_so(caplog):
    rt = _runtime("synth:people=0")
    rt.ollama = _NoToolOllama("Nobody there.")
    # warmed with the real prefix: quiet
    rt._warm_prefix = _prefix_fingerprint(rt.build_system_prompt(clock=False), rt.tool_definitions)
    trace: list = []
    with caplog.at_level(logging.WARNING):
        asyncio.run(ca._run_conversation_turn(rt, [], "is anyone there?", trace=trace))
    assert "differ from the pre-warmed prefix" not in caplog.text
    assert not [s for s in trace if s.get("step") == "prefix"]
    # warmed with other tool schemas: the turn names the part
    caplog.clear()
    rt.ollama = _NoToolOllama("Nobody there.")
    rt._warm_prefix = _prefix_fingerprint(rt.build_system_prompt(clock=False), [])
    trace = []
    with caplog.at_level(logging.WARNING):
        asyncio.run(ca._run_conversation_turn(rt, [], "is anyone there?", trace=trace))
    assert "tool schemas differ from the pre-warmed prefix" in caplog.text
    assert [s["detail"] for s in trace if s.get("step") == "prefix"] == ["changed: tool schemas"]


def test_rebuilding_the_tools_rewarms_a_changed_prefix():
    rt = _runtime("synth:people=0")
    warmed = []

    async def fake_prewarm():
        warmed.append(True)
    rt._prewarm_llm = fake_prewarm

    async def go():
        rt._warm_prefix = _prefix_fingerprint(rt.build_system_prompt(clock=False), rt.tool_definitions)
        rt._configure_tools()                       # same tools: nothing
        await asyncio.sleep(0)
        assert warmed == []
        rt.cfg.enabled_tools = ["detect_objects"]   # a different toolset
        rt._configure_tools()
        await asyncio.sleep(0)
        assert warmed == [True]
    asyncio.run(go())


def test_no_warm_prefix_means_no_rewarm_and_no_warning(caplog):
    """Before the first pre-warm there is nothing to compare with."""
    rt = _runtime("synth:people=0")
    rt._configure_tools()
    rt.ollama = _NoToolOllama("Nobody there.")
    with caplog.at_level(logging.WARNING):
        asyncio.run(ca._run_conversation_turn(rt, [], "is anyone there?"))
    assert "pre-warmed prefix" not in caplog.text


def test_voice_service_puts_the_clock_in_the_user_turn():
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hello"}]
    out = with_clock_in_user_turn(msgs, lambda: "The current date and time is X")
    assert out[0] == {"role": "system", "content": "sys"}
    assert out[-1]["content"].startswith("[The current date and time is X]\n\n")
    assert out[-1]["content"].endswith("hello")
    assert msgs[-1]["content"] == "hello"               # the context's list is untouched
    # an assistant turn last (a tool loop in flight) or no clock: unchanged
    tail = [{"role": "assistant", "content": "…"}]
    assert with_clock_in_user_turn(tail, lambda: "X") == tail
    assert with_clock_in_user_turn(msgs, None) == msgs
