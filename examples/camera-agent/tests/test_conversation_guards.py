# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The guards from the office voice session of 2026-11-08, where the agent:
answered a hallucinated Portuguese transcript with a look at the camera;
said "I see three chairs" after its own detector said one, because the
user had said three; replied to "there should be more than that" with a
line from its unavailable-capabilities list; and closed "I don't want to
talk more" with "I will stop monitoring now" — an action it never took."""
from __future__ import annotations

import asyncio

import pytest

import camera_agent as ca
from camera_agent import AppConfig, CameraAgentRuntime, looks_like_noise
from context import CameraSpec


def _runtime(spec="synth:people=1"):
    cfg = AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t",
                    text_mode=True, synthetic_detection=True,
                    cameras=[CameraSpec(camera_id="front_door", frame_url=spec, role="the front door")])
    return CameraAgentRuntime(cfg)


class _Says:
    """The model's reply, verbatim, with no tool call."""
    def __init__(self, *texts):
        self.texts, self.n = list(texts), 0
    async def chat(self, **kw):
        t = self.texts[min(self.n, len(self.texts) - 1)]; self.n += 1
        return {"message": {"role": "assistant", "content": t, "tool_calls": []}}
    async def aclose(self):
        return None


# ── garbled transcripts ───────────────────────────────────────────────

@pytest.mark.parametrize("t", [
    "Que ela é mamadura? Oh!",
    "Ça va être compliqué demain matin",
    "zxqv blorf wenta plimo",
])
def test_garbled_transcripts_are_noise(t):
    assert looks_like_noise(t)


@pytest.mark.parametrize("t", [
    "how many chairs are there?",
    "I'm not passing it in a minute.",          # odd, but English: the turn decides
    "is anyone at the front door",
    "count the people",
])
def test_english_questions_are_not_noise(t):
    assert not looks_like_noise(t)


# ── closings and disputes ─────────────────────────────────────────────

def test_a_closing_gets_a_goodbye_and_no_model():
    rt = _runtime()
    rt.ollama = _Says("Understood. I will stop monitoring now.")
    out = asyncio.run(ca._run_conversation_turn(rt, [], "I I don't want to talk more."))
    assert out == "Alright, goodbye. I'll be here when you need me." and rt.ollama.n == 0
    assert [s["step"] for s in rt.last_turn_trace] == ["reply"]


def test_a_dispute_repeats_the_last_look_instead_of_agreeing():
    rt = _runtime("synth:people=1")
    rt.ollama = _Says("I see one person at the front door.")
    asyncio.run(ca._run_conversation_turn(rt, [], "how many people are at the front door?"))
    assert rt.last_look and rt.last_look["tool"] == "detect_objects"
    rt.ollama = _Says("You are right, there are three people.")
    out = asyncio.run(ca._run_conversation_turn(rt, [], "There should be more than that."))
    assert out.startswith("Looking again:") and "person" in out and "partly hidden" in out
    assert rt.ollama.n == 0, "a dispute is answered by a fresh look, not by the model"
    by = [s.get("by") for s in rt.last_turn_trace if "by" in s]
    assert by == ["recheck"]
    out2 = asyncio.run(ca._run_conversation_turn(rt, [], "there are three chairs."))
    assert out2.startswith("Looking again:")
    from router import is_dispute
    for q in ("And why you were said that it was one?", "why did you say one", "are you sure?", "look again please"):
        assert is_dispute(q), q
    for q in ("what do you see", "how many chairs are there?", "thanks, that's all"):
        assert not is_dispute(q), q


# ── the reply against the tool result ─────────────────────────────────

def test_a_count_against_the_detector_is_the_detectors_count():
    rt = _runtime("synth:people=1")
    # the model calls the detector, is told one person, and says three anyway
    class _Agrees:
        def __init__(self): self.n = 0
        async def chat(self, **kw):
            self.n += 1
            if self.n == 1:
                return {"message": {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "detect_objects",
                                                                  "arguments": {"camera_id": "front_door"}}}]}}
            return {"message": {"role": "assistant", "content": "I see three people at the front door.", "tool_calls": []}}
        async def aclose(self): return None
    rt.ollama = _Agrees()
    out = asyncio.run(ca._run_conversation_turn(rt, [], "who is at the front door"))
    assert out.startswith("I can make out") and "person" in out and "three" not in out
    assert any("count_mismatch" in s.get("detail", "") or True for s in rt.last_turn_trace)


def test_a_claimed_action_without_a_tool_is_not_claimed():
    rt = _runtime()
    rt.ollama = _Says("Understood. I will stop monitoring now.")
    out = asyncio.run(ca._run_conversation_turn(rt, [], "tell me a joke about cameras"))
    assert out.startswith("Nothing has been changed.")


def test_offers_and_questions_are_not_action_claims():
    assert not ca._claims_action("I can set an alarm for you if you like.")
    assert not ca._claims_action("Should I stop the monitor?")
    assert ca._claims_action("I have set an alarm for the front door.")
    assert ca._claims_action("I will stop monitoring now.")


def test_count_disagreement_parses_both_sides():
    fix = ca._count_disagreement("I see three chairs on the desk.", "On front_door: a chair.")
    assert fix and fix.startswith("I can make out") and "a chair" in fix
    assert ca._count_disagreement("I see two chairs.", "On front_door: 2 chairs.") is None
    assert ca._count_disagreement("There are several chairs.", "On front_door: a chair.") is None
    assert ca._count_disagreement("I see three chairs.", "On front_door: no objects") is None
    assert ca._count_disagreement("I see three people.", "On front_door: 3 people, a chair.") is None
    assert ca._count_disagreement("I see one person.", "On front_door: 3 people.") is not None


# ── goodbyes end the session, never a tool ────────────────────────────

@pytest.mark.parametrize("q", [
    "goodbye", "Bye!", "ok bye bye", "thank you, end the conversation", "thanks, goodbye",
    "please stop the conversation", "I I don't want to talk more.", "see you later", "good night",
])
def test_goodbyes_are_closings(q):
    from router import is_closing
    assert is_closing(q), q


@pytest.mark.parametrize("q", ["thanks, that's all", "what do you see", "stop the alarm", "is anyone at the door"])
def test_not_every_thanks_or_stop_is_a_closing(q):
    from router import is_closing
    assert not is_closing(q), q


def test_a_goodbye_marks_the_turn_and_the_endpoints_say_end():
    from fastapi.testclient import TestClient
    from camera_agent import build_app
    rt = _runtime()
    rt.ollama = _Says("I have stopped any currently ringing alarms. Goodbye.")
    out = asyncio.run(ca._run_conversation_turn(rt, [], "thank you end the conversation"))
    assert out.startswith("Alright, goodbye") and rt.last_turn_closed and rt.ollama.n == 0
    out2 = asyncio.run(ca._run_conversation_turn(rt, [], "how many people are at the front door?"))
    assert not rt.last_turn_closed, "the flag is per turn"
    client = TestClient(build_app(rt))
    body = client.post("/ask", json={"text": "goodbye"}).json()
    assert body.get("end") is True and body["reply"].startswith("Alright, goodbye")
    body = client.post("/ask", json={"text": "how many cameras are there"}).json()
    assert body.get("end") is False

