# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""STARTER EVAL SET — a behavioral spec for the camera agent.

Unit tests check *plumbing*; this file checks *behavior* on representative user
phrasings, so a refactor that quietly breaks tool routing or grounding gets
caught. It's the deterministic layer (routing, grounding gate, noise, end-to-end
on synthetic frames) that runs in CI with no LLM. The LLM-dependent cases (does
the model actually pick create_alarm for "alarm me if…") live in the live-eval
harness — see AGENT_DESIGN.md.

Grow this matrix as you add capabilities — it's the agent's regression net.
"""
from __future__ import annotations

import asyncio

import pytest

import camera_agent as ca
from camera_agent import AppConfig, CameraAgentRuntime
from context import CameraSpec


# ── Eval 1: forced-grounding routes the question to the RIGHT tool ──────
# (describe/attribute/activity → caption/VQA ; presence/count → detector)

_ROUTING_CASES = [
    # describe / attribute / activity → describe_camera (VQA)
    ("what is the person wearing", "describe_camera"),
    ("what is he doing", "describe_camera"),
    ("describe the scene at the gate", "describe_camera"),
    ("what's happening on the porch", "describe_camera"),
    ("what colour is the car", "describe_camera"),
    ("what does it look like outside", "describe_camera"),
    ("what do you see", "describe_camera"),
    # presence / count → detect_objects
    ("how many people are at the door", "detect_objects"),
    ("is anyone in the kitchen", "detect_objects"),
    ("count the cars in the driveway", "detect_objects"),
    ("are there any dogs in the yard", "detect_objects"),
    ("is there a package on the porch", "detect_objects"),
]


@pytest.mark.parametrize("query,expected", _ROUTING_CASES)
def test_eval_forced_tool_routing(query, expected):
    assert ca._pick_forced_tool(query) == expected, query


# ── Eval 2: grounding fires on camera questions, not on chit-chat/config ─

_SHOULD_GROUND = [
    "what's at the back gate", "is anyone there", "how many cars",
    "what is he wearing", "did a person walk past",
]
_SHOULD_NOT_GROUND = [
    "hi", "hello there", "thanks, that's all", "how are you", "good morning",
]
_CONFIG_QUESTIONS = [
    "how many cameras are configured", "which cameras do you have",
    "list the cameras",
]


@pytest.mark.parametrize("q", _SHOULD_GROUND)
def test_eval_camera_questions_ground(q):
    assert ca._looks_like_camera_question(q) is True, q


@pytest.mark.parametrize("q", _SHOULD_NOT_GROUND)
def test_eval_chitchat_does_not_ground(q):
    assert ca._looks_like_camera_question(q) is False, q


@pytest.mark.parametrize("q", _CONFIG_QUESTIONS)
def test_eval_config_questions_skip_grounding(q):
    assert ca._is_config_question(q) is True, q


# ── Eval 2b: after small talk, only a claimed SIGHTING forces a look ─────
# Replies are the ones small local models gave in the live eval
# (MODELS_AND_LATENCY.md, "Measured"): qwen2.5:1.5b answered "thanks" with
# an offer to monitor the cameras, the guard forced a look, and the user's
# goodbye came back as "I can't see anything right now".

_SMALL_TALK_OFFERS = [
    ("thanks, that's all", "You're welcome! Let's check the front door camera."),
    ("thanks, that's all", "You're welcome! I'll start by checking the front door camera."),
    ("hi, what can you do?",
     "I can watch the cameras for you, detect objects, count people, and arm alarms."),
    ("hi, what can you do?",
     "I can describe what's visible on one camera, several, or all of them."),
    ("hi, what can you do?",
     "I see... I can help you monitor your cameras, detect objects, and check recent events."),
]
_SMALL_TALK_SIGHTINGS = [
    ("hi, what can you do?", "I see the front door. It's currently dark outside."),
    ("thanks", "There's a person at the front door camera right now."),
]
_STILL_GROUNDED = [
    # STT garbled "camera 2" — the reply check is why this is caught.
    ("what's on hammer 2", "Camera 2 shows an empty driveway."),
    # The question itself is about the scene, whatever the reply says.
    ("is anyone at the door?", "You're welcome!"),
    # One real word outside small talk: not small talk any more.
    ("thanks, and what about the gate camera", "Sure, I can monitor the cameras."),
]


@pytest.mark.parametrize("q,reply", _SMALL_TALK_OFFERS)
def test_eval_small_talk_offer_does_not_force_a_look(q, reply):
    assert ca._is_small_talk(q), q
    assert ca._looks_like_camera_question(reply), "the old guard looked on this reply"
    assert ca._should_force_grounding(q, reply) is False, (q, reply)


@pytest.mark.parametrize("q,reply", _SMALL_TALK_SIGHTINGS)
def test_eval_small_talk_sighting_still_forces_a_look(q, reply):
    assert ca._should_force_grounding(q, reply) is True, (q, reply)


@pytest.mark.parametrize("q,reply", _STILL_GROUNDED)
def test_eval_camera_questions_still_force_a_look(q, reply):
    assert not ca._is_small_talk(q), q
    assert ca._should_force_grounding(q, reply) is True, (q, reply)


class _NoToolOllama:
    """Answers without a tool call, then (after any forced look) composes."""
    def __init__(self, first):
        self.first, self.n = first, 0
    async def chat(self, **kw):
        self.n += 1
        text = self.first if self.n == 1 else "Done."
        return {"message": {"content": text, "tool_calls": []}}
    async def aclose(self):
        return None


@pytest.mark.parametrize("q,reply,reasked", [
    ("hi, what can you do?",
     "I can watch the cameras for you, detect objects, count people, and arm alarms.", False),
    ("hi, what can you do?", "I see the front door. It's currently dark outside.", True),
])
def test_eval_small_talk_never_looks_and_a_claimed_sighting_is_reasked(q, reply, reasked):
    """Small talk runs with no tools (router tier 0, no tool). A reply that
    claims a sighting anyway is a fabrication: the model is asked once
    more, plainly, and never sent to look — a goodbye answered with
    "I can't see right now" was the measured failure."""
    rt = _runtime("synth:people=0")
    rt.ollama = _NoToolOllama(reply)
    out = asyncio.run(ca._run_conversation_turn(rt, [], q, preferred_camera="front_door"))
    by = [s.get("by") for s in rt.last_turn_trace if "by" in s]
    assert "forced" not in by, rt.last_turn_trace
    steps = [s["step"] for s in rt.last_turn_trace]
    assert ("reask" in steps) is reasked, rt.last_turn_trace
    assert out == ("Done." if reasked else reply)


def test_eval_small_talk_that_keeps_claiming_a_sighting_gets_the_fixed_line():
    rt = _runtime("synth:people=0")
    class _AlwaysSighting:
        async def chat(self, **kw):
            return {"message": {"content": "There's a person at the front door right now.", "tool_calls": []}}
        async def aclose(self):
            return None
    rt.ollama = _AlwaysSighting()
    out = asyncio.run(ca._run_conversation_turn(rt, [], "thanks, that's all", preferred_camera="front_door"))
    assert "person" not in out and "camera" in out
    assert "forced" not in [s.get("by") for s in rt.last_turn_trace if "by" in s]


# ── Eval 3: noise/hallucinations are dropped, real questions pass ───────

_NOISE = ["", "you", "Thank you.", "Thanks for watching!", "[music]", "."]
_REAL = ["how many people are there", "is the gate open"]


@pytest.mark.parametrize("t", _NOISE)
def test_eval_noise_dropped(t):
    assert ca.looks_like_noise(t) is True, t


@pytest.mark.parametrize("t", _REAL)
def test_eval_real_questions_pass(t):
    assert ca.looks_like_noise(t) is False, t


# ── Eval 4: end-to-end on synthetic frames (deterministic, no LLM) ──────
# Forces the tool path and checks the spoken answer is correct + natural.

class _FakeOllama:
    """Calls the given tool, then returns empty content (so the turn falls back
    to the humanised tool result — exercises the whole spoken path)."""
    def __init__(self, tool, camera):
        self._r = [
            {"message": {"content": "", "tool_calls": [
                {"id": "1", "type": "function",
                 "function": {"name": tool, "arguments": {"camera_id": camera}}}]}},
            {"message": {"content": "", "tool_calls": []}},
        ]
        self.n = 0
    async def chat(self, **kw):
        r = self._r[min(self.n, len(self._r) - 1)]; self.n += 1; return r
    async def aclose(self):
        return None


def _runtime(spec):
    cfg = AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t",
                    text_mode=True, synthetic_detection=True,
                    cameras=[CameraSpec(camera_id="front_door", frame_url=spec,
                                        role="the front door")])
    return CameraAgentRuntime(cfg)


@pytest.mark.parametrize("spec,tool,expect", [
    ("synth:people=2", "detect_objects", "2 people"),
    ("synth:people=0,cars=1", "detect_objects", "car"),
])
def test_eval_end_to_end_synthetic(spec, tool, expect):
    rt = _runtime(spec)
    rt.ollama = _FakeOllama(tool, "front_door")
    reply = asyncio.run(ca._run_conversation_turn(rt, [], "what's there?",
                                                  preferred_camera="front_door"))
    assert expect in reply.lower()
    # spoken-friendly: no raw id, no colon
    assert "front_door" not in reply and ":" not in reply


def test_eval_the_router_asks_which_camera_without_a_model():
    """Six cameras, a description asked for, none named: the agent asks,
    and no model or tool runs (a caption per camera would be the cost)."""
    cfg = AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t",
                    text_mode=True, synthetic_detection=True,
                    cameras=[CameraSpec(camera_id=f"cam{i}", frame_url="synth:people=0",
                                        role=r) for i, r in enumerate(
                        ("the front door", "the gate", "the yard", "the garage", "the hall", "the roof"), 1)])
    rt = CameraAgentRuntime(cfg)
    rt.ollama = _NoToolOllama("should not be asked")
    out = asyncio.run(ca._run_conversation_turn(rt, [], "what is the person wearing?"))
    assert out.startswith("Which camera do you mean: the front door")
    assert rt.ollama.n == 0
    steps = [s["step"] for s in rt.last_turn_trace]
    assert steps == ["route", "reply"]

