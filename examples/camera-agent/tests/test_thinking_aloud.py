# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Thinking aloud: the agent says what it is about to do — from the tool
call's own arguments, no LLM — only when the wait is worth a sentence."""
from __future__ import annotations

import asyncio
import json

import pytest

from fillers import (
    DEFAULT_TOOL_MS, ThinkingAloud, humanize_camera, spell_plate, template_line, time_window,
)


class _Cam:
    def __init__(self, camera_id, role):
        self.camera_id, self.role = camera_id, role


def test_slots_read_like_a_person():
    cams = [_Cam("cam1", "front door"), _Cam("gate", "main gate camera")]
    assert humanize_camera("cam1", cams) == "the front door camera"
    assert humanize_camera("gate", cams) == "the main gate camera"
    assert humanize_camera("cam2") == "camera two"
    assert humanize_camera("all") == "all cameras"
    assert spell_plate("66HH07") == "6 6 H H 0 7"
    assert time_window({"start_time": "2026-09-08T14:00:00+05:30", "end_time": "2026-09-08T15:30:00+05:30"}) == \
        " between 2 pm and 3:30 pm"
    assert time_window({"window_seconds": 1800}) == " in the last 30 minutes"
    assert time_window({"window_seconds": 86400}) == " in the last 24 hours"
    assert time_window({"within_minutes": 60}) == " in the last hour"
    assert time_window({}) == ""


def test_one_sentence_shape_per_tool_with_the_calls_context():
    cams = [_Cam("cam1", "front door"), _Cam("gate", "gate")]
    assert template_line("search_history", {"camera_id": "gate", "label": "vehicle",
                         "start_time": "2026-09-08T14:00:00", "end_time": "2026-09-08T15:00:00"}, cams) == \
        "Let me check the gate camera for vehicles between 2 pm and 3 pm."
    assert template_line("describe_camera", {"camera_id": "cam1", "question": "Is the door open?"}, cams) == \
        "Let me take a look at the front door camera — is the door open."
    assert template_line("recent_plates", {"plate": "66HH07", "window_seconds": 3600}) == \
        "Let me check the plate reads for 6 6 H H 0 7 in the last hour."
    assert template_line("search_footage", {"keywords": ["red truck"], "within_minutes": 30}) == \
        "Let me search the footage for red truck in the last 30 minutes."
    assert template_line("app_status", {"app_id": "occupancy-counting"}) == "Let me ask the occupancy counting app."
    assert template_line("describe_camera", {"camera_ids": ["cam1", "gate"]}, cams, opener="Sure — I'll") == \
        "Sure — I'll take a look at the front door camera and the gate camera."
    # instant lookups and control verbs get no line
    assert template_line("recent_events", {"camera_id": "cam1"}) is None
    assert template_line("create_alarm", {"target": "person"}) is None


def test_gate_speaks_only_when_the_wait_is_worth_it_and_once_per_turn():
    t = ThinkingAloud(min_ms=1500)
    t.new_turn()
    # defaults: vision is slow → a line; a ring lookup is not → silence
    assert t.line_for("describe_camera", {"camera_id": "cam1"}) is not None
    assert t.line_for("search_history", {"camera_id": "cam1"}) is None      # second tool this turn: latch
    t.new_turn()
    assert t.line_for("recent_events", {"camera_id": "cam1"}) is None
    assert t.recent[-1]["why"] == "no template for this tool"     # instant lookup: never announced
    # this site's own numbers win: a fast vision box needs no filler
    t.record_stages([{"step": "describe_camera", "ms": 300}] * 5 + [{"step": "llm", "ms": 200}] * 5,
                    {"tts": 100})
    t.new_turn()
    assert t.expected_wait_ms("describe_camera") == 600
    assert t.line_for("describe_camera", {"camera_id": "cam1"}) is None
    assert "expected 600 ms < 1500" in t.recent[-1]["why"]
    # and a slow history search on this site does get one
    t.record_stages([{"step": "search_history", "ms": 2500}] * 5)
    t.new_turn()
    line = t.line_for("search_history", {"camera_id": "cam1", "label": "person"})
    assert line and "camera one for persons" in line
    # disabled → never
    t.enabled = False
    t.new_turn()
    assert t.line_for("describe_camera", {"camera_id": "cam1"}) is None


def test_openers_rotate():
    t = ThinkingAloud(min_ms=0)
    seen = set()
    for _ in range(12):
        t.new_turn()
        seen.add(t.line_for("describe_camera", {"camera_id": "cam1"}).split(" take")[0])
    assert len(seen) >= 2
    # never the same opener twice in a row
    t.new_turn()
    a = t.line_for("describe_camera", {"camera_id": "cam1"})
    t.new_turn()
    b = t.line_for("describe_camera", {"camera_id": "cam1"})
    assert a.split(" take")[0] != b.split(" take")[0]


def test_model_line_is_used_only_when_it_is_a_usable_acknowledgement():
    t = ThinkingAloud(min_ms=0, source="model")
    t.new_turn()
    assert t.line_for("describe_camera", {"camera_id": "cam1"}, model_line="Let me look at the gate camera for you") == \
        "Let me look at the gate camera for you."
    # too long, an answer, or empty → the template
    for bad in ("", "I see a person standing by the gate with a red bag near the car",
                "There are 3 people", "Yes, the gate is open."):
        t.new_turn()
        line = t.line_for("describe_camera", {"camera_id": "cam1"}, model_line=bad)
        assert "take a look at camera one" in line, (bad, line)
    # template mode ignores the model line entirely
    t2 = ThinkingAloud(min_ms=0, source="template")
    t2.new_turn()
    assert "take a look at camera one" in t2.line_for("describe_camera", {"camera_id": "cam1"}, model_line="Let me look at the gate.")


@pytest.mark.asyncio
async def test_turn_publishes_a_working_line_before_the_slow_tool_and_only_for_interactive_turns():
    """Through _run_conversation_turn with a fake LLM that calls
    describe_camera: a voice turn publishes the line WITH audio while the
    tool runs; a typed turn publishes text only; background work nothing."""
    import camera_agent as ca
    from context import CameraSpec

    cfg = ca.AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t",
                       cameras=[CameraSpec(camera_id="cam1", frame_url="http://x", role="front door")])
    rt = ca.CameraAgentRuntime(cfg)
    order: list[str] = []

    class _Ollama:
        def __init__(self):
            self.n = 0

        async def chat(self, *, messages, tools, temperature, max_tokens):
            self.n += 1
            if self.n % 2 == 1:
                return {"message": {"content": "", "tool_calls": [
                    {"id": "c1", "function": {"name": "describe_camera",
                                              "arguments": json.dumps({"camera_id": "cam1"})}}]}}
            return {"message": {"content": "A parked car by the door."}}

    class _Piper:
        async def synthesize(self, text):
            order.append(f"tts:{text}")
            return b"RIFFfakewav"

    async def describe(args):
        await asyncio.sleep(0.05)
        order.append("tool")
        return "A parked car."

    rt.ollama = _Ollama()
    rt.piper = _Piper()
    rt.tool_handlers = {"describe_camera": describe}
    rt.tool_definitions = [{"type": "function", "function": {"name": "describe_camera", "parameters": {}}}]
    inbox = rt.subscribe_updates()

    reply = await ca._run_conversation_turn(rt, [], "what is at the door", speak_progress="voice")
    await asyncio.sleep(0.1)
    assert "parked car" in reply.lower()
    msg = inbox.get_nowait()
    assert msg["working"]["text"].endswith("the front door camera.") and msg["working"]["audio_b64"]
    assert order[0].startswith("tts:") and order[1] == "tool" or order == ["tool", f"tts:{msg['working']['text']}"]
    assert rt.thinking.recent[-1]["said"] == msg["working"]["text"]

    # typed: text only, no Piper
    order.clear()
    rt.ollama = _Ollama()
    await ca._run_conversation_turn(rt, [], "what is at the door", speak_progress="text")
    await asyncio.sleep(0.05)
    msg = inbox.get_nowait()
    assert msg["working"]["audio_b64"] is None and "tool" in order and not any(o.startswith("tts") for o in order)

    # background: nothing published
    rt.ollama = _Ollama()
    await ca._run_conversation_turn(rt, [], "what is at the door")
    await asyncio.sleep(0.05)
    assert inbox.empty()
    rt.unsubscribe_updates(inbox)


def test_thinking_aloud_endpoint_and_page_wiring():
    from pathlib import Path

    from fastapi.testclient import TestClient

    import camera_agent as ca
    from context import CameraSpec

    cfg = ca.AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t",
                       cameras=[CameraSpec(camera_id="c", frame_url="http://x", role="f")])
    body = TestClient(ca.build_app(ca.CameraAgentRuntime(cfg))).get("/thinking-aloud").json()
    assert body["enabled"] is True and body["min_ms"] == 1500 and body["source"] == "template"
    assert body["expected_ms"]["describe_camera"] == DEFAULT_TOOL_MS["describe_camera"] + 1500 + 600

    html = (Path(__file__).resolve().parents[1] / "demo" / "index.html").read_text()
    assert "if(d.working) onWorking(d.working);" in html
    assert "cutFiller();" in html                      # the answer outranks the filler
    assert "if(!w.audio_b64||!sessionActive||speaking) return;" in html   # typed turn: status only


def test_model_source_adds_the_prompt_hint_only_when_on():
    import camera_agent as ca
    from context import CameraSpec

    cams = [CameraSpec(camera_id="c", frame_url="http://x", role="f")]
    off = ca.CameraAgentRuntime(ca.AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t", cameras=cams))
    assert "what you are about to check" not in off.build_system_prompt()
    on = ca.CameraAgentRuntime(ca.AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t", cameras=cams,
                                            filler_source="model"))
    assert "what you are about to check" in on.build_system_prompt()
