# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The streaming path (/ws) answers with the agent's own turn and tells
the page what is happening beside the audio."""
from __future__ import annotations

import importlib
import json
import types

import asyncio

import pytest


def _pipecat_available() -> bool:
    try:
        importlib.import_module("pipecat.tests.utils")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pipecat_available(), reason="pipecat-ai not installed")


def test_history_and_question_takes_text_turns_only():
    from services import history_and_question
    history, text = history_and_question([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "is anyone there?"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "On cam1: a person"},
        {"role": "assistant", "content": "One person at the door."},
        {"role": "user", "content": "and now?"},
    ])
    assert text == "and now?"
    assert history == [{"role": "user", "content": "is anyone there?"},
                       {"role": "assistant", "content": ""},
                       {"role": "assistant", "content": "One person at the door."}]
    assert history_and_question([{"role": "assistant", "content": "x"}]) == ([{"role": "assistant", "content": "x"}], "")


@pytest.mark.asyncio
async def test_message_frames_serialize_as_json_text():
    from pipecat.frames.frames import OutputTransportMessageFrame, OutputTransportMessageUrgentFrame
    from serializer import RawPcmSerializer
    s = RawPcmSerializer()
    out = await s.serialize(OutputTransportMessageFrame(message={"type": "transcript", "text": "hi"}))
    assert isinstance(out, str) and json.loads(out) == {"type": "transcript", "text": "hi"}
    out = await s.serialize(OutputTransportMessageUrgentFrame(message={"type": "interrupted"}))
    assert json.loads(out) == {"type": "interrupted"}


@pytest.mark.asyncio
async def test_stream_events_announce_the_turn_beside_the_frames():
    from pipecat.frames.frames import (
        InterruptionFrame, OutputTransportMessageFrame, OutputTransportMessageUrgentFrame,
        TranscriptionFrame, UserStartedSpeakingFrame, UserStoppedSpeakingFrame,
    )
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.tests.utils import run_test
    from services import StreamEvents
    frames = [UserStartedSpeakingFrame(), TranscriptionFrame(text="is anyone there", user_id="u", timestamp="t"),
              UserStoppedSpeakingFrame(), InterruptionFrame()]
    down, _ = await run_test(Pipeline([StreamEvents()]), frames_to_send=frames, start_timeout=10.0)
    msgs = [f.message for f in down if isinstance(f, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame))]
    # System frames overtake data frames in the pipeline, so only membership
    # is pinned here; the user-start / user-stop order is.
    assert sorted(m["type"] for m in msgs) == ["interrupted", "transcript", "user_started", "user_stopped"]
    assert {"type": "transcript", "text": "is anyone there"} in msgs
    types_ = [m["type"] for m in msgs]
    assert types_.index("user_started") < types_.index("user_stopped")
    assert any(isinstance(f, TranscriptionFrame) for f in down), "the original frames still pass through"
    assert any(isinstance(f, OutputTransportMessageUrgentFrame) and f.message == {"type": "interrupted"} for f in down)


@pytest.mark.asyncio
async def test_the_llm_service_answers_with_the_agents_turn_when_given_one():
    from pipecat.frames.frames import LLMContextFrame, LLMTextFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.tests.utils import run_test
    from services import OpenNvrOllamaLLM
    seen = {}

    async def runner(history, text):
        seen["history"], seen["text"] = history, text
        return "Nobody is at the door right now."

    class _NeverCalled:
        async def chat(self, **kw):
            raise AssertionError("the inline loop must not run when a turn runner is given")

    llm = OpenNvrOllamaLLM(client=_NeverCalled(), tools=[], tool_handlers={}, turn_runner=runner)
    context = LLMContext(messages=[
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "is anyone at the door?"},
    ])
    down, _ = await run_test(Pipeline([llm]), frames_to_send=[LLMContextFrame(context=context)], start_timeout=10.0)
    texts = [f.text for f in down if isinstance(f, LLMTextFrame)]
    assert texts == ["Nobody is at the door right now."]
    assert seen["text"] == "is anyone at the door?"
    # The page cannot see the TranscriptionFrame (the aggregator eats it),
    # so the service tells it what was heard, and when the turn is over —
    # without these the page showed no user text and merged every answer
    # into one bubble (2026-11-09).
    from pipecat.frames.frames import OutputTransportMessageFrame
    msgs = [f.message for f in down if isinstance(f, OutputTransportMessageFrame)]
    assert msgs == [{"type": "transcript", "text": "is anyone at the door?"}, {"type": "turn_end"}]
    assert seen["history"] == [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]


def test_the_real_runtime_gets_a_turn_runner_and_a_bare_one_does_not():
    import camera_agent as ca
    from context import CameraSpec
    cfg = ca.AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t", text_mode=True,
                       synthetic_detection=True,
                       cameras=[CameraSpec(camera_id="c", frame_url="synth:people=0", role="r")])
    rt = ca.CameraAgentRuntime(cfg)
    core, _ = ca.build_core_processors(rt)
    assert core[2]._turn_runner is not None
    bare = types.SimpleNamespace(cfg=cfg, whisper=None, ollama=None, piper=None, tool_definitions=[],
                                 tool_handlers={}, build_system_prompt=lambda clock=True: "s")
    core, _ = ca.build_core_processors(bare)
    assert core[2]._turn_runner is None
    assert ca.AppConfig(kaic_url="http://k", kaic_api_key="x").turn_stop_secs == 1.0


@pytest.mark.asyncio
async def test_a_goodbye_over_the_stream_ends_the_session_after_the_reply():
    from pipecat.frames.frames import LLMContextFrame, LLMTextFrame, OutputTransportMessageFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.tests.utils import run_test
    from services import OpenNvrOllamaLLM

    async def runner(history, text):
        return "Alright, goodbye.", {"end": True}

    llm = OpenNvrOllamaLLM(client=None, tools=[], tool_handlers={}, turn_runner=runner)
    context = LLMContext(messages=[{"role": "user", "content": "goodbye"}])
    down, _ = await run_test(Pipeline([llm]), frames_to_send=[LLMContextFrame(context=context)], start_timeout=10.0)
    kinds = [(type(f).__name__, getattr(f, "text", None) or getattr(f, "message", None)) for f in down
             if isinstance(f, (LLMTextFrame, OutputTransportMessageFrame))]
    assert kinds == [("OutputTransportMessageFrame", {"type": "transcript", "text": "goodbye"}),
                     ("LLMTextFrame", "Alright, goodbye."),
                     ("OutputTransportMessageFrame", {"type": "turn_end"}),
                     ("OutputTransportMessageFrame", {"type": "session_end"})]



# ── a sentence cut by a pause is answered once, whole ──────────────────

def test_unfinished_transcripts_are_recognised():
    from services import join_transcripts, looks_unfinished

    assert looks_unfinished("I remove that alarm for")
    assert looks_unfinished("Okay, you can remove the")
    assert looks_unfinished("Yeah, that's okay, but I see that")
    assert looks_unfinished("Remove")               # a lone verb needs its object
    assert not looks_unfinished("Yeah.")             # a lone answer stands
    assert not looks_unfinished("Thanks")
    assert not looks_unfinished("I remove that alarm for cam 24 person.")
    assert not looks_unfinished("is anyone at the door?")
    assert not looks_unfinished("")
    assert join_transcripts("I remove that alarm.", "For cam 24 person.") == "I remove that alarm for cam 24 person."
    assert join_transcripts("Okay, you can remove the", "alarm for person") == "Okay, you can remove the alarm for person"
    assert join_transcripts("show me", "CTPL camera") == "show me CTPL camera", "an acronym keeps its capitals"


def _llm_with_runner():
    from services import OpenNvrOllamaLLM
    calls: list[str] = []
    pushed: list = []

    async def runner(history, text):
        calls.append(text)
        return f"done: {text}"

    llm = OpenNvrOllamaLLM(client=None, tools=[], tool_handlers={}, turn_runner=runner)

    async def push(frame, direction=None):
        pushed.append(frame)
    llm.push_frame = push  # type: ignore[assignment]
    return llm, calls, pushed


def _ctx(*user_texts):
    from pipecat.processors.aggregators.llm_context import LLMContext
    msgs = [{"role": "system", "content": "sys"}]
    for i, t in enumerate(user_texts):
        msgs.append({"role": "user", "content": t})
        if i < len(user_texts) - 1:
            msgs.append({"role": "assistant", "content": "…"})
    return LLMContext(messages=msgs)


@pytest.mark.asyncio
async def test_a_pause_mid_sentence_joins_the_two_halves_into_one_turn():
    """"I remove that alarm." then "for cam 24 person." 1.2 s later were
    two turns, the first answered before the second arrived (Ventuno Q,
    2026-11-10). The first half is held; the second joins it."""
    from pipecat.frames.frames import LLMTextFrame, OutputTransportMessageFrame
    llm, calls, pushed = _llm_with_runner()
    llm.continue_secs = 5.0
    await llm._handle_context(_ctx("Okay, you can remove the"))
    assert calls == [], "held: the sentence is not over"
    await llm._handle_context(_ctx("Okay, you can remove the", "alarm for person"))
    assert calls == ["Okay, you can remove the alarm for person"]
    msgs = [f.message for f in pushed if isinstance(f, OutputTransportMessageFrame)]
    assert msgs == [{"type": "transcript", "text": "Okay, you can remove the alarm for person"}, {"type": "turn_end"}]
    assert [f.text for f in pushed if isinstance(f, LLMTextFrame)] == ["done: Okay, you can remove the alarm for person"]
    assert llm._pending is None


@pytest.mark.asyncio
async def test_an_unfinished_sentence_is_answered_as_it_stands_after_the_wait():
    llm, calls, pushed = _llm_with_runner()
    llm.continue_secs = 0.15
    await llm._handle_context(_ctx("show me the"))
    assert calls == []
    await asyncio.sleep(0.4)
    assert calls == ["show me the"]
    assert llm._pending is None and llm._pending_timer is None


@pytest.mark.asyncio
async def test_a_complete_sentence_is_answered_at_once():
    llm, calls, _ = _llm_with_runner()
    llm.continue_secs = 5.0
    await llm._handle_context(_ctx("is anyone at the door?"))
    assert calls == ["is anyone at the door?"]
