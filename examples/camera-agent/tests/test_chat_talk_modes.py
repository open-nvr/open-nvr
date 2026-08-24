# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The unified input bar: chat-with-dictation by default, Talk as a mode.

POST /transcribe is STT-only (mic → words in the text box — the
Claude/ChatGPT dictation pattern): no LLM turn, no TTS, same transcode +
noise filter as /converse so the paths can't drift. /agent advertises
stt_available so text installs (no Whisper) never show a dead mic. The
demo page swaps the install-time voice/text split for a runtime
Chat|Talk toggle — ending a Talk session (Stop or idle) must always land
the UI back on Chat so the mic is never secretly live.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import camera_agent as ca
from camera_agent import AppConfig, CameraAgentRuntime, build_app
from context import CameraSpec


def _runtime(**over):
    cfg = AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t",
                    text_mode=over.pop("text_mode", False),
                    cameras=[CameraSpec(camera_id="cam1",
                                        frame_url="http://x/1.jpg", role="r")],
                    **over)
    return CameraAgentRuntime(cfg)


class _FakeWhisper:
    def __init__(self, text=" hello camera one ", fail=False):
        self._text, self._fail = text, fail

    async def transcribe(self, wav):
        if self._fail:
            raise RuntimeError("adapter down")
        return self._text


# ── /transcribe ─────────────────────────────────────────────────────────

def test_transcribe_returns_text(monkeypatch):
    rt = _runtime()
    rt.whisper = _FakeWhisper()
    monkeypatch.setattr(ca, "_transcode_to_wav16k", lambda b: b)
    r = TestClient(build_app(rt)).post("/transcribe", content=b"audio-bytes")
    assert r.status_code == 200
    assert r.json() == {"text": "hello camera one"}


def test_transcribe_empty_body_400():
    rt = _runtime()
    r = TestClient(build_app(rt)).post("/transcribe", content=b"")
    assert r.status_code == 400


def test_transcribe_stt_failure_502(monkeypatch):
    rt = _runtime()
    rt.whisper = _FakeWhisper(fail=True)
    monkeypatch.setattr(ca, "_transcode_to_wav16k", lambda b: b)
    r = TestClient(build_app(rt)).post("/transcribe", content=b"x")
    assert r.status_code == 502
    assert "Whisper" in r.json()["error"]


def test_transcribe_filters_noise_hallucinations(monkeypatch):
    # Whisper's classic silence hallucination must not land in the user's
    # input box.
    rt = _runtime()
    rt.whisper = _FakeWhisper(text="Thank you.")
    monkeypatch.setattr(ca, "_transcode_to_wav16k", lambda b: b)
    body = TestClient(build_app(rt)).post("/transcribe", content=b"x").json()
    assert body["text"] == ""
    assert body.get("note")


def test_transcribe_bad_audio_400(monkeypatch):
    rt = _runtime()

    def _boom(b):
        raise ValueError("not audio")

    monkeypatch.setattr(ca, "_transcode_to_wav16k", _boom)
    r = TestClient(build_app(rt)).post("/transcribe", content=b"junk")
    assert r.status_code == 400


# ── /agent: stt_available ───────────────────────────────────────────────

def test_agent_advertises_stt_on_voice_installs():
    a = TestClient(build_app(_runtime())).get("/agent").json()
    assert a["stt_available"] is True


def test_agent_hides_stt_on_text_installs():
    a = TestClient(build_app(_runtime(text_mode=True))).get("/agent").json()
    assert a["stt_available"] is False


# ── demo page: unified bar wiring ───────────────────────────────────────

_HTML = (Path(__file__).resolve().parents[1] / "demo" / "index.html").read_text()


def test_demo_has_mode_toggle_and_dictate_mic():
    for needle in ('id="modeSeg"', 'id="modeChat"', 'id="modeTalk"',
                   'id="dictate"', 'fetch("/transcribe"'):
        assert needle in _HTML, needle


def test_demo_voice_installs_keep_the_text_box():
    # The old install-time split hid the ask row on voice installs; the
    # unified bar must never do that again.
    assert "body.mode-voice .askrow" not in _HTML


def test_demo_talk_end_always_returns_to_chat():
    # Both session endings — explicit Stop and the idle timeout — must land
    # the UI back on Chat (ui-talk off), so the mic is never secretly live.
    import re
    stop = re.search(r"function stopSession\(\)\{.*?\}", _HTML, re.S).group(0)
    idle = re.search(r"function pauseForIdle\(\)\{.*?syncSegChat\(\);\s*\}",
                     _HTML, re.S)
    assert "syncSegChat()" in stop
    assert idle is not None
    assert 'classList.remove("ui-talk")' in _HTML


def test_demo_text_installs_hide_all_voice_controls():
    assert "body.mode-text #modeSeg, body.mode-text #dictate" in _HTML
