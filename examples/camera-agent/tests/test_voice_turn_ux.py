# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Voice turn lifecycle UX: cancellable turns and honest TTS failure.

The demo's Stop button must be able to abandon a 25 s LLM+VLM turn
(``/converse/cancel``), and a dead Piper adapter must be REPORTED
(``tts_error``) instead of silently degrading every reply to text-only —
the field complaint was "the text comes back but no speech, and nothing
says why"."""
from __future__ import annotations

import pytest

from camera_agent import AppConfig, CameraAgentRuntime, build_app
from context import CameraSpec
from fastapi.testclient import TestClient


def _runtime():
    cfg = AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t",
                    wake_word_required=False,
                    cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="r")])
    return CameraAgentRuntime(cfg)


def _stub_stt(monkeypatch, rt, text="what do you see"):
    import camera_agent as ca

    async def fake_transcribe(_wav):
        return text
    monkeypatch.setattr(rt.whisper, "transcribe", fake_transcribe)
    monkeypatch.setattr(ca, "_transcode_to_wav16k", lambda b: b"RIFFwav")


def test_cancel_with_nothing_in_flight_is_a_noop():
    client = TestClient(build_app(_runtime()))
    assert client.post("/converse/cancel").json() == {"cancelled": False}


def test_tts_failure_is_reported_not_silent(monkeypatch):
    """Piper down on every attempt → reply text intact + tts_error=True."""
    rt = _runtime()
    _stub_stt(monkeypatch, rt)
    import camera_agent as ca

    async def fake_turn(*a, **k):
        return "a person at the door"
    monkeypatch.setattr(ca, "_run_conversation_turn", fake_turn)

    calls = {"n": 0}

    async def broken_synth(_t):
        calls["n"] += 1
        raise RuntimeError("503 from piper")
    monkeypatch.setattr(rt.piper, "synthesize", broken_synth)

    data = TestClient(build_app(rt)).post(
        "/converse?camera=all&wake=0", content=b"fakeaudio",
        headers={"Content-Type": "application/octet-stream"}).json()
    assert data["reply"] == "a person at the door"   # text still delivered
    assert data["audio_b64"] is None
    assert data["tts_error"] is True                 # ...and the UI is told why
    assert calls["n"] == 2                           # one retry before giving up


def test_tts_transient_failure_recovers_on_retry(monkeypatch):
    rt = _runtime()
    _stub_stt(monkeypatch, rt)
    import camera_agent as ca

    async def fake_turn(*a, **k):
        return "all quiet"
    monkeypatch.setattr(ca, "_run_conversation_turn", fake_turn)

    calls = {"n": 0}

    async def flaky_synth(_t):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient 503")
        return b"RIFFfakewav"
    monkeypatch.setattr(rt.piper, "synthesize", flaky_synth)

    data = TestClient(build_app(rt)).post(
        "/converse?camera=all&wake=0", content=b"fakeaudio",
        headers={"Content-Type": "application/octet-stream"}).json()
    assert data["audio_b64"]                          # second attempt delivered audio
    assert data["tts_error"] is False


def test_cancelled_turn_returns_cancelled_payload(monkeypatch):
    """A turn cancelled mid-LLM returns {cancelled: true}, no reply, no TTS."""
    rt = _runtime()
    _stub_stt(monkeypatch, rt)
    import asyncio

    import camera_agent as ca

    async def slow_turn(*a, **k):
        # Cancel ourselves the way /converse/cancel would: cancel the current
        # task while it is the registered in-flight turn.
        asyncio.current_task().cancel()
        await asyncio.sleep(30)
        return "never"
    monkeypatch.setattr(ca, "_run_conversation_turn", slow_turn)

    synth = {"called": False}

    async def fake_synth(_t):
        synth["called"] = True
        return b"wav"
    monkeypatch.setattr(rt.piper, "synthesize", fake_synth)

    data = TestClient(build_app(rt)).post(
        "/converse?camera=all&wake=0", content=b"fakeaudio",
        headers={"Content-Type": "application/octet-stream"}).json()
    assert data.get("cancelled") is True
    assert data["reply"] == ""
    assert synth["called"] is False                   # no TTS for a dead turn
