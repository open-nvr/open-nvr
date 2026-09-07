# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The real Pipecat 1.8 pipeline, end to end, without a WebSocket or a
single adapter: fake Whisper / Ollama / Piper clients behind the real
services, the real user aggregator with Silero VAD + Smart Turn v3, the
real assistant aggregator. Audio in → transcript → tool call → answer →
TTS audio out, and the context carries the whole exchange.

Skipped when Pipecat is not importable (the static-check environment)."""
from __future__ import annotations

import importlib
import json
import math
import struct
import time
import types

import pytest


def _pipecat_available() -> bool:
    try:
        importlib.import_module("pipecat.tests.utils")
        importlib.import_module("pipecat.audio.turn.smart_turn.local_smart_turn_v3")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pipecat_available(), reason="pipecat-ai 1.8+ not installed")


def _wav(pcm: bytes, rate: int = 22050) -> bytes:
    n = len(pcm)
    return (b"RIFF" + struct.pack("<I", 36 + n) + b"WAVE" + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", n) + pcm)


def _tone(seconds: float, rate: int = 16000, hz: float = 220.0, amp: int = 12000) -> bytes:
    n = int(seconds * rate)
    return struct.pack(f"<{n}h", *(int(amp * math.sin(2 * math.pi * hz * i / rate)) for i in range(n)))


class _Whisper:
    def __init__(self):
        self.calls = []

    async def transcribe(self, audio: bytes) -> str:
        self.calls.append(len(audio))
        return "what is on camera one"


class _Ollama:
    """First call: a tool call; second: the spoken answer."""

    def __init__(self):
        self.calls = []

    async def chat(self, *, messages, tools, temperature, max_tokens):
        self.calls.append([dict(m) for m in messages])
        if not any(m.get("role") == "tool" for m in messages):
            return {"message": {"content": "", "tool_calls": [
                {"id": "call-1", "function": {"name": "describe_camera",
                                              "arguments": json.dumps({"camera": "one"})}}]}}
        return {"message": {"content": "Camera one shows a parked car by the gate."}}


class _Piper:
    def __init__(self):
        self.texts = []

    async def synthesize(self, text: str) -> bytes:
        self.texts.append(text)
        return _wav(_tone(0.1, rate=22050))


@pytest.fixture
def runtime():
    import camera_agent as ca

    cfg = ca.AppConfig(kaic_url="http://x", kaic_api_key="k")
    cfg.turn_stop_secs = 0.3               # keep the test quick: fallback stop after 0.3 s of silence
    cfg.turn_stop_timeout_secs = 2.0
    describe_calls = []

    async def describe_camera(args):
        describe_calls.append(args)
        return "A parked car by the gate."

    rt = types.SimpleNamespace(
        cfg=cfg, whisper=_Whisper(), ollama=_Ollama(), piper=_Piper(),
        tool_definitions=[{"type": "function", "function": {"name": "describe_camera", "parameters": {}}}],
        tool_handlers={"describe_camera": describe_camera},
        build_system_prompt=lambda: "You are the camera agent.",
    )
    rt.describe_calls = describe_calls
    return rt


@pytest.mark.asyncio
async def test_audio_in_to_speech_out_through_the_real_pipeline(runtime):
    import camera_agent as ca
    from pipecat.frames.frames import (
        InputAudioRawFrame, LLMContextFrame, LLMTextFrame, TranscriptionFrame, TTSAudioRawFrame,
        TTSStartedFrame, TTSStoppedFrame, UserStartedSpeakingFrame, UserStoppedSpeakingFrame,
        VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame,
    )
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.task import PipelineParams
    from pipecat.tests.utils import SleepFrame, run_test

    core, context = ca.build_core_processors(runtime)
    pipeline = Pipeline(core)

    # One utterance: the VAD announces a start, ~1 s of "speech", the
    # VAD announces a stop. Smart Turn judges synthetic audio however it
    # likes; the strategy's stop_secs fallback closes the turn either way.
    frames = [VADUserStartedSpeakingFrame()]
    for _ in range(5):
        frames.append(InputAudioRawFrame(audio=_tone(0.2), sample_rate=16000, num_channels=1))
        frames.append(SleepFrame(sleep=0.05))
    frames.append(VADUserStoppedSpeakingFrame(stop_secs=0.2, timestamp=time.time()))
    frames.append(SleepFrame(sleep=2.5))

    down, up = await run_test(
        pipeline, frames_to_send=frames,
        pipeline_params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=22050),
        start_timeout=10.0,
    )
    kinds = [type(f).__name__ for f in down]

    # The exchange happened, in order.
    assert runtime.whisper.calls, f"STT was never asked; frames out: {kinds}"
    assert runtime.describe_calls == [{"camera": "one"}]
    assert [len(c) for c in runtime.ollama.calls] == [2, 4]        # system+user, then +assistant+tool
    assert runtime.piper.texts == ["Camera one shows a parked car by the gate."]

    def first(cls):
        idx = [i for i, f in enumerate(down) if isinstance(f, cls)]
        assert idx, f"no {cls.__name__} downstream; got {kinds}"
        return idx[0]
    # (The user aggregator consumes TranscriptionFrames into the context and
    # the TTS consumes the LLM text frames, so neither reaches the sink —
    # what does is the turn envelope, the speech, and the closed context.)
    assert first(UserStartedSpeakingFrame) < first(UserStoppedSpeakingFrame) < first(TTSStartedFrame)
    assert first(TTSStartedFrame) < first(TTSAudioRawFrame) < first(TTSStoppedFrame)
    assert not any(isinstance(f, (TranscriptionFrame, LLMTextFrame)) for f in down)
    assert first(TTSStoppedFrame) < first(LLMContextFrame)
    audio_out = [f for f in down if isinstance(f, TTSAudioRawFrame)]
    assert audio_out and audio_out[0].sample_rate == 22050 and audio_out[0].audio[:4] != b"RIFF"

    # The shared context carries the whole turn, tool round trip included.
    roles = [m["role"] for m in context.get_messages()]
    assert roles[:2] == ["system", "user"] and "tool" in roles and roles[-1] == "assistant"
    assert context.get_messages()[-1]["content"] == "Camera one shows a parked car by the gate."


@pytest.mark.asyncio
async def test_noise_that_whisper_calls_you_never_reaches_the_llm(runtime):
    """The hallucination filter still gates the LLM: a burst Whisper
    transcribes as 'You' produces no turn text, no tool call, no speech."""
    import camera_agent as ca
    from pipecat.frames.frames import (
        InputAudioRawFrame, VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame,
    )
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.task import PipelineParams
    from pipecat.tests.utils import SleepFrame, run_test

    async def you(audio):
        return "You"
    runtime.whisper.transcribe = you
    core, context = ca.build_core_processors(runtime)
    frames = [VADUserStartedSpeakingFrame(),
              InputAudioRawFrame(audio=_tone(0.6), sample_rate=16000, num_channels=1),
              VADUserStoppedSpeakingFrame(stop_secs=0.2, timestamp=time.time()),
              SleepFrame(sleep=2.5)]
    await run_test(Pipeline(core), frames_to_send=frames,
                   pipeline_params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=22050),
                   start_timeout=10.0)
    assert runtime.ollama.calls == [] and runtime.piper.texts == []
    assert [m["role"] for m in context.get_messages()] == ["system"]
