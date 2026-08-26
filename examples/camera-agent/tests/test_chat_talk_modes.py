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


def test_demo_has_docked_bar_with_dictate_and_single_talk():
    # ChatGPT-shape bar: mic (dictate) + input + Send + one compact Talk.
    for needle in ('id="dictate"', 'fetch("/transcribe"', 'id="talk"',
                   'class="send"'):
        assert needle in _HTML, needle
    # The Chat|Talk segmented toggle is gone — Talk is a single button.
    assert 'id="modeSeg"' not in _HTML
    assert 'id="modeChat"' not in _HTML


def test_demo_chat_log_sits_above_the_input_bar():
    # The conversation fills the card; the input bar is docked BELOW it —
    # on the camera screen too (one global bar; the camAsk pill is gone).
    assert _HTML.index('<div class="log" id="log">') \
        < _HTML.index('<div class="row askrow">')
    assert 'id="camAsk"' not in _HTML


def test_demo_voice_installs_keep_the_text_box():
    # The old install-time split hid the ask row on voice installs; the
    # unified bar must never do that again.
    assert "body.mode-voice .askrow" not in _HTML


def test_demo_talk_end_always_returns_to_chat():
    # Both session endings — explicit Stop and the idle timeout — must clear
    # the hands-free surface (ui-talk), so the mic is never secretly live;
    # startSession is what raises it.
    import re
    stop = re.search(r"function stopSession\(\)\{.*?\}", _HTML, re.S).group(0)
    idle = re.search(r"function pauseForIdle\(\)\{.*?syncSegChat\(\);\s*\}",
                     _HTML, re.S)
    assert "syncSegChat()" in stop
    assert idle is not None
    assert 'classList.remove("ui-talk")' in _HTML
    assert "syncSegTalk()" in _HTML


def test_demo_text_installs_hide_all_voice_controls():
    assert "body.mode-text #dictate" in _HTML
    assert "body.mode-text #talk" in _HTML


def test_demo_talk_is_off_by_default():
    # The hands-free session starts ONLY from the Talk button: exactly two
    # occurrences of "startSession()" — its definition and the one call in
    # the talk click handler. Any boot-time auto-start would add a third.
    assert _HTML.count("startSession()") == 2
    # And the page never ships with the hands-free surface pre-raised.
    assert 'class="ui-talk"' not in _HTML


def test_demo_state_animations_survive_the_redesign():
    # The avatar's four states, the ambient speaking glow, the blinking
    # Stop dot, and the state-driven voice pill — all still wired.
    for needle in (".ram.idle", ".ram.listening", ".ram.thinking",
                   ".ram.speaking", ".ambient-speak",
                   ".talk.listening::before",
                   "body:has(#ram.thinking) .voicebar",
                   "body:has(#ram.speaking) .ambient-speak"):
        assert needle in _HTML, needle
    # The voice pill must stay STATE-driven, never mode-gated: gating it on
    # ui-talk silently killed the Thinking… animation for typed asks.
    assert "body:not(.ui-talk) .voicebar" not in _HTML


def test_demo_live_stills_run_at_1fps_with_inflight_guard():
    # Camera-screen still refresh: 1 s cadence (was 2 s), guarded so a slow
    # RTSP grab (keyframe wait can exceed 1 s) degrades to "as fast as the
    # source delivers" instead of piling up requests.
    assert "refreshPlayerLive(); },1000)" in _HTML
    assert "_liveInflight" in _HTML
    assert "},2000)" not in _HTML.split("refreshPlayerLive")[2][:200]


def test_configs_frame_ttl_matches_1fps():
    # At TTL 2.0 every other 1 fps poll returned the same cached JPEG.
    base = Path(__file__).resolve().parents[1]
    for fname in ("config.docker.yml", "config.docker.chat.yml"):
        text = (base / fname).read_text()
        assert "frame_cache_ttl_seconds: 1.0" in text, fname
        assert "frame_cache_ttl_seconds: 2.0" not in text, fname


def test_demo_chat_dock_present_on_camera_screen_too():
    # The log + docked bar live OUTSIDE #mainScreen/#camScreen, after both —
    # so opening a single camera keeps the same chat dock below it.
    cam_screen = _HTML.index('id="camScreen"')
    log = _HTML.index('<div class="log" id="log">')
    bar = _HTML.index('<div class="row askrow">')
    assert cam_screen < log < bar


# ── live dictation: waveform, phrase-by-phrase text, ✓/✕ ────────────────
# The mic used to buffer the whole utterance, transcribe it once at the
# end, and drop the text in as a lump — with no sign the mic was hearing
# anything and no way to reject the result but selecting it and deleting
# it. These guard the three things that replaced that.


def _fn(name: str) -> str:
    """Source of one top-level ``function name(``, to its matching close."""
    start = _HTML.index(f"function {name}(")
    depth = 0
    for j in range(_HTML.index("{", start), len(_HTML)):
        if _HTML[j] == "{":
            depth += 1
        elif _HTML[j] == "}":
            depth -= 1
            if depth == 0:
                return _HTML[start:j + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def test_dictation_shows_what_the_mic_is_hearing():
    # A muted mic and a silent room used to look identical: both said
    # "Listening…" and nothing else.
    for needle in ('id="dictbar"', 'id="dictwave"', 'id="dictnote"'):
        assert needle in _HTML, needle
    assert "body.dictating .dictbar" in _HTML
    # The meter is driven by the SAME rms the segmenter decides on, not by
    # a decorative animation — otherwise it can show bars while the VAD
    # hears nothing.
    tick = _fn("dictVadTick")
    assert "dictWavePush(level)" in tick
    assert "dictRms()" in tick


def test_dictation_transcribes_each_phrase_once():
    """The CPU guard, and the reason this is phrase-by-phrase rather than
    word-by-word. faster-whisper has no streaming endpoint, so the only
    other way to get live text is re-transcribing the growing buffer every
    couple of seconds — 4-5x the STT work for one utterance, on a box
    where Whisper shares a CPU with Ollama and the detect pipeline.
    Cutting on silence sends each second of speech exactly once."""
    tick = _fn("dictVadTick")
    # Segments are cut on a SILENCE gate, not on a fixed interval.
    assert "SILENCE_MS" in tick and "dictSegStop()" in tick
    assert "setInterval" not in _fn("dictSend"), "no periodic re-send of the buffer"
    # One request at a time: keeps phrases in spoken order AND stops a fast
    # talker stacking Whisper calls.
    send = _fn("dictSend")
    assert "dictQueue=dictQueue.then(" in send


def test_dictation_reuses_the_tuned_vad_gates():
    # The Talk VAD already adapts to the room's noise floor; dictation must
    # not invent a second set of thresholds that drift from it.
    tick = _fn("dictVadTick")
    for gate in ("START_RMS", "SILENCE_RMS", "START_FRAMES",
                 "START_MARGIN", "SILENCE_MARGIN", "FLOOR_EMA"):
        assert gate in tick, gate


def test_dictation_has_an_explicit_keep_and_discard():
    assert 'id="dictDone"' in _HTML and 'id="dictCancel"' in _HTML
    assert "dictStop(true)" in _HTML and "dictStop(false)" in _HTML
    stop = _fn("dictStop")
    # ✕ puts the box back exactly as it was found — including anything that
    # was still in flight when the button was pressed.
    assert "inp.value=dictBase" in stop
    assert "dictQueue=dictQueue.then(" in stop


def test_stopping_dictation_flushes_the_phrase_in_progress():
    # Tearing the mic down first would lose the last thing said.
    stop = _fn("dictStop")
    assert stop.index("dictRec.stop()") < stop.index("getTracks()"), \
        "the mic is released before the phrase in progress is flushed"


def test_dictation_releases_everything_it_opened():
    stop = _fn("dictStop")
    for needle in ("clearInterval(dictTick)", "clearTimeout(dictTimer)",
                   "getTracks().forEach", "dictCtx.close()"):
        assert needle in stop, needle
    assert 'classList.remove("dictating")' in stop


def test_dictation_still_works_without_an_analyser():
    # No Web Audio (older browser) means no level gate, so capture would
    # never start — fall back to one segment for the session instead.
    tog = _fn("toggleDictate")
    assert "if(!dictAnalyser) dictSegStart();" in tog
