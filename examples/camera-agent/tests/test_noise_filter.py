# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""STT noise/hallucination guard: Whisper emits phantom phrases ("Thank you.",
"you", "Thanks for watching") on silence/background noise. The voice loop must
ignore these so ambient noise doesn't trigger a turn — but keep real questions."""
from __future__ import annotations

import asyncio

import pytest

from camera_agent import AppConfig, CameraAgentRuntime, build_app, looks_like_noise
from context import CameraSpec
from fastapi.testclient import TestClient


@pytest.mark.parametrize("text", [
    "", "  ", ".", "...", "you", "You.", "thank you", "Thank you.",
    "Thanks for watching!", "Bye.", "um", "Music", "[music]",
    "subtitles by the amara.org community", "a", "🙂",
])
def test_noise_is_filtered(text):
    assert looks_like_noise(text) is True


@pytest.mark.parametrize("text", [
    # Repetition hallucinations: Whisper looping a phrase over sustained
    # background noise (observed live: each one cost a full LLM+VLM turn).
    "Yes, I will do it. Okay. So, you have to do this. Yes, I will do it. "
    "You come here. I will do it. You come here. I will do it. I will do it. "
    "I will not go to bed. I will go to sleep. I will go to sleep. "
    "I will go to sleep.",
    "I will go to sleep. I will go to sleep. I will go to sleep.",
    "okay okay okay okay okay okay okay okay",
])
def test_repetition_hallucinations_are_filtered(text):
    assert looks_like_noise(text) is True


@pytest.mark.parametrize("text", [
    "how many people are at the door",
    "is anyone in the kitchen?",
    "sound a fire alarm if you see smoke",
    "what's happening on the driveway",
    "count the cars",
    # Long real sentences with some repeated words must NOT be dropped.
    "check the front door camera and the back door camera and tell me if "
    "there is a person at either door right now",
])
def test_real_questions_pass(text):
    assert looks_like_noise(text) is False


def _runtime(noise_filter=True):
    cfg = AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t",
                    stt_noise_filter=noise_filter,
                    cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="r")])
    return CameraAgentRuntime(cfg)


def test_converse_drops_noise_without_calling_llm(monkeypatch):
    rt = _runtime()

    async def fake_transcribe(_wav):
        return "Thank you."          # classic silence hallucination
    monkeypatch.setattr(rt.whisper, "transcribe", fake_transcribe)
    # transcode is called first; stub it so we don't need ffmpeg
    import camera_agent as ca
    monkeypatch.setattr(ca, "_transcode_to_wav16k", lambda b: b"RIFFwav")

    called = {"llm": False}
    async def boom(*a, **k):
        called["llm"] = True
        return "should not happen"
    monkeypatch.setattr(ca, "_run_conversation_turn", boom)

    client = TestClient(build_app(rt))
    r = client.post("/converse?camera=all", content=b"fakeaudiobytes",
                    headers={"Content-Type": "application/octet-stream"})
    data = r.json()
    assert data["transcript"] == "" and data["reply"] == ""
    assert data.get("noise") is True
    assert called["llm"] is False     # LLM never invoked on noise


def test_converse_noise_filter_off_lets_it_through(monkeypatch):
    rt = _runtime(noise_filter=False)

    async def fake_transcribe(_wav):
        return "Thank you."
    monkeypatch.setattr(rt.whisper, "transcribe", fake_transcribe)
    import camera_agent as ca
    monkeypatch.setattr(ca, "_transcode_to_wav16k", lambda b: b"RIFFwav")

    async def fake_turn(*a, **k):
        return "you're welcome"
    monkeypatch.setattr(ca, "_run_conversation_turn", fake_turn)

    async def fake_synth(_t):
        return b""
    monkeypatch.setattr(rt.piper, "synthesize", fake_synth)

    client = TestClient(build_app(rt))
    data = client.post("/converse?camera=all", content=b"fakeaudiobytes",
                       headers={"Content-Type": "application/octet-stream"}).json()
    assert data["transcript"] == "Thank you."   # not filtered when disabled
