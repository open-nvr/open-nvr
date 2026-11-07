# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""prompt_budget.py — the prompt inside a window the runtime cannot grow.

The Gemma bundle on the Ventuno Q's NPU holds 4,096 tokens and refuses a
longer prompt (bench/ventuno-q-npu-agent-llm-2026-11-07.md: thirteen of
the agent's fourteen schemas fit beside the system prompt, seven beside
eight turns of history). With ``llm_window_tokens`` set a turn sends the
schemas the question most likely needs and the newest history; unset,
nothing changes and Ollama's cached prefix survives.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import camera_agent as ca
import prompt_budget as pb
from camera_agent import AppConfig, CameraAgentRuntime
from context import CameraSpec


def _tool(name: str, chars: int = 400) -> dict:
    """A schema of about ``chars`` characters."""
    return {"type": "function", "function": {
        "name": name, "description": "x" * max(0, chars - 90 - len(name)),
        "parameters": {"type": "object", "properties": {}}}}


TOOLS = [_tool(n) for n in (
    "describe_camera", "detect_objects", "search_footage", "search_history",
    "describe_event", "describe_window", "recent_events", "create_monitor",
    "stop_monitor", "create_alarm", "stop_alarm", "list_apps", "app_status",
    "recent_app_alerts")]
NAMES = [t["function"]["name"] for t in TOOLS]
SYSTEM = "s" * 3200          # ~800 tokens, the agent's real size


def _names(tools):
    return [t["function"]["name"] for t in tools]


def test_no_window_changes_nothing():
    hist = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}] * 8
    f = pb.fit(system=SYSTEM, history=hist, user_turn="q", tools=TOOLS, window=0, max_tokens=128)
    assert f.tools == TOOLS and f.history == hist and not f.trimmed


def test_everything_fits_when_the_window_is_large():
    f = pb.fit(system=SYSTEM, history=[], user_turn="q", tools=TOOLS, window=16000, max_tokens=256)
    assert _names(f.tools) == NAMES and not f.trimmed


@pytest.mark.parametrize("question,must_keep", [
    ("set an alarm if a person is seen on the front door after 6pm", "create_alarm"),
    ("notify me when more than 3 people gather at the gate", "create_monitor"),
    ("did a white van come to the gate yesterday?", "search_history"),
    ("which apps are running?", "list_apps"),
    ("who came to the door this morning?", "search_history"),
    ("is anyone on the front door camera right now?", "detect_objects"),
])
def test_a_small_window_keeps_the_tools_the_question_points_at(question, must_keep):
    # Room for the system prompt and about four 400-character schemas.
    f = pb.fit(system=SYSTEM, history=[], user_turn=question, tools=TOOLS,
               window=2000, max_tokens=64, user_text=question)
    assert f.trimmed and len(f.tools) < len(TOOLS)
    assert must_keep in _names(f.tools), (question, _names(f.tools))
    # Membership changes, the advertised order does not: the model reads
    # the list as a menu.
    assert _names(f.tools) == [n for n in NAMES if n in _names(f.tools)]
    assert set(f.dropped_tools) | set(_names(f.tools)) == set(NAMES)


def test_the_hint_is_kept_first_whatever_the_words():
    f = pb.fit(system=SYSTEM, history=[], user_turn="hmm", tools=TOOLS,
               window=1500, max_tokens=64, user_text="hmm", hint="recent_app_alerts", min_tools=1)
    assert "recent_app_alerts" in _names(f.tools)


def test_the_priority_order_decides_when_the_question_says_nothing():
    f = pb.fit(system=SYSTEM, history=[], user_turn="hmm", tools=TOOLS,
               window=2200, max_tokens=64, user_text="hmm")
    kept = _names(f.tools)
    assert kept[:2] == ["describe_camera", "detect_objects"]
    assert "search_history" in kept and "recent_events" in kept


def test_at_least_min_tools_even_when_they_do_not_fit():
    f = pb.fit(system=SYSTEM, history=[], user_turn="q", tools=TOOLS,
               window=1000, max_tokens=64, user_text="q", min_tools=2)
    assert len(f.tools) == 2


def test_history_goes_oldest_first_in_whole_pairs():
    hist = []
    for i in range(8):
        hist.append({"role": "user", "content": f"question {i} " + "w" * 200})
        hist.append({"role": "assistant", "content": f"answer {i} " + "w" * 300})
    f = pb.fit(system=SYSTEM, history=hist, user_turn="and now?", tools=TOOLS[:3],
               window=2400, max_tokens=64, user_text="and now?")
    assert f.trimmed and f.dropped_history > 0
    assert f.history == hist[-len(f.history):]            # the newest, in order
    assert f.history[0]["role"] == "user"                 # never a reply without its question
    assert f.history[-1] is hist[-1]


def test_the_newest_two_turns_survive_a_crowd_of_tools():
    hist = []
    for i in range(6):
        hist.append({"role": "user", "content": f"question {i}"})
        hist.append({"role": "assistant", "content": f"answer {i}"})
    f = pb.fit(system=SYSTEM, history=hist, user_turn="and the gate?", tools=TOOLS,
               window=4096, max_tokens=128, user_text="and the gate?")
    assert len(f.history) >= 4, f.history
    assert f.history[-4:] == hist[-4:]


def test_the_estimate_matches_the_measured_window():
    """What the Ventuno Q bundle accepted and refused (bench, 2026-11-07):
    the real system prompt (3,282 chars) with 13 schemas (11,912 chars)
    fitted in 4,096 tokens, all 14 (12,749 chars) did not. The estimate
    must put 14 over the budget and leave 7 plus eight turns inside."""
    system = "s" * 3282
    fourteen = [_tool(n, chars) for n, chars in zip(NAMES, (
        926, 766, 770, 2089, 566, 907, 507, 1684, 232, 1884, 416, 600, 565, 837))]
    f = pb.fit(system=system, history=[], user_turn="is anyone there?", tools=fourteen,
               window=4096, max_tokens=96, user_text="is anyone there?")
    assert f.trimmed and len(f.tools) < 14
    seven = [t for t in fourteen if t["function"]["name"] in (
        "describe_camera", "detect_objects", "search_history", "create_alarm",
        "create_monitor", "recent_events", "list_apps")]
    hist = []
    for i in range(8):
        hist.append({"role": "user", "content": f"turn {i}: what happened at the front door around {8 + i}:00 this morning? give me the full picture"})
        hist.append({"role": "assistant", "content": "Nothing unusual: one person came to the door, stayed about a minute, and left. No vehicles, no packages, no alarms. " * 2})
    f = pb.fit(system=system, history=hist, user_turn="and now?", tools=seven,
               window=4096, max_tokens=96, user_text="and now?")
    assert len(f.tools) == 7 and len(f.history) >= 4


def test_shrink_for_retry_drops_history_then_tools_then_gives_up():
    hist = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    messages = [{"role": "system", "content": "s"}, *hist, {"role": "user", "content": "q"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
                {"role": "tool", "tool_call_id": "1", "content": "r" * 1200}]
    m2, t2, what = pb.shrink_for_retry(messages, TOOLS, history_len=2, user_text="q")
    assert what == "history dropped" and m2 == [messages[0]] + messages[3:] and t2 is TOOLS
    m3, t3, what = pb.shrink_for_retry(m2, t2, history_len=0, user_text="set an alarm")
    assert what == "tools cut to three" and len(t3) == 3 and "create_alarm" in _names(t3)
    assert pb.shrink_for_retry(m3, t3, history_len=0) is None


# ── through a turn ────────────────────────────────────────────────

class _Recorder:
    """Answers without a tool call; keeps every request's tools and messages."""
    def __init__(self, fail_first_with: str | None = None):
        self.calls: list[dict] = []
        self._fail = fail_first_with
    async def chat(self, **kw):
        # Snapshots: the turn mutates the list it sent (retry, tool results).
        self.calls.append(dict(kw, messages=list(kw.get("messages") or []),
                               tools=list(kw["tools"]) if kw.get("tools") else kw.get("tools")))
        if self._fail and len(self.calls) == 1:
            code, self._fail = self._fail, None
            req = httpx.Request("POST", "http://npu/v1/chat/completions")
            raise httpx.HTTPStatusError("400", request=req, response=httpx.Response(
                400, request=req, json={"error": {"code": code, "message": "too long"}}))
        return {"message": {"content": "Done.", "tool_calls": []}}
    async def aclose(self):
        return None


def _runtime(window: int) -> CameraAgentRuntime:
    cfg = AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t",
                    text_mode=True, synthetic_detection=True, llm_window_tokens=window,
                    cameras=[CameraSpec(camera_id="front_door", frame_url="synth:people=0",
                                        role="the front door")])
    return CameraAgentRuntime(cfg)


def test_turn_sends_the_shortlist_when_a_window_is_set():
    rt = _runtime(1800)
    rt.ollama = rec = _Recorder()
    hist = [{"role": "user", "content": "earlier " * 50}, {"role": "assistant", "content": "ok " * 50}] * 4
    asyncio.run(ca._run_conversation_turn(
        rt, list(hist), "set an alarm if a person is seen on the front door after 6pm"))
    first = rec.calls[0]
    assert len(first["tools"]) < len(rt.tool_definitions)
    assert "create_alarm" in _names(first["tools"])
    assert len(first["messages"]) < 2 + len(hist)
    steps = [s for s in rt.last_turn_trace if s["step"] == "prompt"]
    assert steps and "window 1800" in steps[0]["detail"], rt.last_turn_trace


def test_turn_without_a_window_sends_everything():
    rt = _runtime(0)
    rt.ollama = rec = _Recorder()
    hist = [{"role": "user", "content": "earlier " * 50}, {"role": "assistant", "content": "ok " * 50}] * 4
    asyncio.run(ca._run_conversation_turn(
        rt, list(hist), "set an alarm if a person is seen on the front door after 6pm"))
    first = rec.calls[0]
    assert _names(first["tools"]) == _names(rt.tool_definitions)
    assert len(first["messages"]) == 2 + len(hist)
    assert not [s for s in rt.last_turn_trace if s["step"] == "prompt"]


def test_turn_retries_without_history_when_the_runtime_refuses_the_prompt():
    rt = _runtime(4096)
    rt.ollama = rec = _Recorder(fail_first_with="context_length_exceeded")
    hist = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "ok"}]
    out = asyncio.run(ca._run_conversation_turn(
        rt, list(hist), "set an alarm if a person is seen on the front door after 6pm"))
    assert len(rec.calls) >= 2
    assert len(rec.calls[0]["messages"]) == 4 and len(rec.calls[1]["messages"]) == 2
    assert any("too long" in s.get("detail", "") for s in rt.last_turn_trace)
    assert out


def test_any_other_400_still_fails_the_turn():
    rt = _runtime(4096)
    rt.ollama = _Recorder(fail_first_with="invalid_request")
    with pytest.raises(ca.LLMTurnError):
        asyncio.run(ca._run_conversation_turn(
            rt, [], "set an alarm if a person is seen on the front door after 6pm"))


def test_prewarm_sends_what_a_turn_would():
    rt = _runtime(1800)
    rt.ollama = rec = _Recorder()
    asyncio.run(rt._prewarm_llm())
    assert rec.calls and len(rec.calls[0]["tools"]) < len(rt.tool_definitions)
    rt0 = _runtime(0)
    rt0.ollama = rec0 = _Recorder()
    asyncio.run(rt0._prewarm_llm())
    assert len(rec0.calls[0]["tools"]) == len(rt0.tool_definitions)


def test_openai_client_models_url():
    from adapter_clients import OpenAILLMClient
    for base in ("http://caption-adapter:9006", "http://caption-adapter:9006/v1",
                 "http://caption-adapter:9006/v1/chat/completions"):
        c = OpenAILLMClient(base_url=base, api_key=None, model="m")
        assert c.models_url == "http://caption-adapter:9006/v1/models"
