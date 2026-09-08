# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Interruptions like a person's: noise, backchannels and remarks to
someone else don't stop the agent; a real phrase addressed to it does."""
from __future__ import annotations

import pytest

from turns import DEFAULT_BACKCHANNELS, InterruptionGate, addressed_to_agent, strip_backchannels


def test_backchannels_are_stripped_not_counted():
    assert strip_backchannels("yeah okay so which gate".split()) == ["so", "which", "gate"]
    assert strip_backchannels("mm hm".split()) == []
    assert strip_backchannels(["uh-huh", "right"]) == []
    assert "okay" in DEFAULT_BACKCHANNELS


@pytest.mark.parametrize("text,expected", [
    ("no wait, the other gate", True),          # correction
    ("can you show the front door?", True),     # request / question
    ("what did it read on cam one", True),      # question word
    ("stop, that's wrong", True),               # imperative
    ("hey Ada, hold on", True),                 # named
    ("he said the delivery comes at five", False),        # aside, third person
    ("I'll grab a coffee, do you want one", False),       # to someone else
    ("we should probably move the meeting to thursday afternoon then", False),  # long narrative
    ("the plate reader missed one", True),      # site vocabulary
])
def test_addressee_heuristic(text, expected):
    verdict, why = addressed_to_agent(text, agent_name="Ada", site_words=["plate", "front door", "cam1"])
    assert verdict is expected, (text, why)


def test_gate_combines_duration_words_and_addressee():
    g = InterruptionGate(min_ms=300, min_words=2, addressee=True, agent_name="Ada",
                         site_words=("gate", "cam1"))
    assert g.judge("no wait the other gate", 900)[0] is True
    # too short to be speech aimed at anyone
    ok, why = g.judge("no wait the other gate", 120)
    assert ok is False and "too short" in why
    # "yeah" / "okay" mean keep going
    ok, why = g.judge("yeah okay", 700)
    assert ok is False and "backchannel" in why
    # one real word is not a floor-taking attempt
    ok, why = g.judge("okay wait", 700)
    assert ok is False and "1 word" in why
    # a remark to someone else in the room
    ok, why = g.judge("he said we should go to lunch", 1500)
    assert ok is False and "not addressed" in why
    # the agent already silent: addressee no longer required
    ok, why = g.judge("he said we should go to lunch", 1500, require_addressee=False)
    assert ok is True
    # unknown duration (no VAD pair) is not held against it
    assert g.judge("can you repeat that", None)[0] is True
    # the evidence ring
    assert len(g.recent) == 7 and g.recent[-1]["interrupt"] is True


def test_gate_with_addressee_off_is_words_and_duration_only():
    g = InterruptionGate(min_ms=300, min_words=2, addressee=False)
    assert g.judge("he said we should go to lunch", 1500)[0] is True
    assert g.judge("yeah", 1500)[0] is False


def test_config_and_site_words_reach_the_gate():
    import camera_agent as ca
    from context import CameraSpec

    cfg = ca.AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t", agent_name="Ada",
                       interrupt_min_ms=450, interrupt_min_words=3, interrupt_addressee=False,
                       cameras=[CameraSpec(camera_id="cam1", frame_url="http://x", role="loading dock")])
    g = ca.interruption_gate(cfg)
    assert (g.min_ms, g.min_words, g.addressee, g.agent_name) == (450.0, 3, False, "Ada")
    assert "loading dock" in g.site_words and "cam1" in g.site_words


def test_config_loads_interruption_knobs(tmp_path):
    import camera_agent as ca

    (tmp_path / "c.yml").write_text(
        "kaic_url: http://k\nkaic_api_key: x\nsystem_prompt: t\n"
        "interruptions: eager\ninterrupt_min_ms: 500\ninterrupt_min_words: 3\ninterrupt_addressee: false\n")
    cfg = ca.load_config(str(tmp_path / "c.yml"))
    assert cfg.interruptions == "eager" and cfg.interrupt_min_ms == 500.0
    assert cfg.interrupt_min_words == 3 and cfg.interrupt_addressee is False
    # defaults
    (tmp_path / "d.yml").write_text("kaic_url: http://k\nkaic_api_key: x\nsystem_prompt: t\n")
    cfg = ca.load_config(str(tmp_path / "d.yml"))
    assert cfg.interruptions == "gated" and cfg.interrupt_addressee is True


def test_interruptions_endpoint_reports_the_gate_and_decisions():
    from fastapi.testclient import TestClient

    import camera_agent as ca
    from context import CameraSpec

    cfg = ca.AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t",
                       cameras=[CameraSpec(camera_id="c", frame_url="http://x", role="f")])
    rt = ca.CameraAgentRuntime(cfg)
    rt.interruption_log.append({"ts": 1, "text": "yeah", "interrupt": False, "reason": "backchannel only"})
    body = TestClient(ca.build_app(rt)).get("/interruptions").json()
    assert body["mode"] == "gated" and body["min_words"] == 2
    assert body["recent"][-1]["reason"] == "backchannel only"


# ── the real Pipecat objects ───────────────────────────────────────────

pipecat = pytest.importorskip("pipecat")


def _cfg(**over):
    import camera_agent as ca
    from context import CameraSpec

    return ca.AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t", agent_name="Ada",
                        cameras=[CameraSpec(camera_id="cam1", frame_url="http://x", role="gate")], **over)


def test_strategy_selection_by_mode():
    import camera_agent as ca
    from pipecat.turns.user_start import VADUserTurnStartStrategy

    gated = ca.build_interruption_strategy(_cfg())
    assert type(gated).__name__ == "GatedInterruptionStartStrategy"
    off = ca.build_interruption_strategy(_cfg(interruptions="off"))
    assert isinstance(off, VADUserTurnStartStrategy) and off._enable_interruptions is False
    eager = ca.build_interruption_strategy(_cfg(interruptions="eager"))
    assert isinstance(eager, VADUserTurnStartStrategy) and eager._enable_interruptions is True
    params = ca.build_user_turn_params(_cfg())
    assert type(params.user_turn_strategies.start[0]).__name__ == "GatedInterruptionStartStrategy"


@pytest.mark.asyncio
async def test_strategy_decisions_with_the_real_frames():
    """Drive the strategy with Pipecat frames directly: silent agent →
    VAD opens the turn at once; speaking agent → a backchannel and an
    aside are dropped (aggregation reset, no turn), a real addressed
    phrase opens the turn WITH an interruption."""
    import time

    import camera_agent as ca
    from pipecat.frames.frames import (
        BotStartedSpeakingFrame, BotStoppedSpeakingFrame, TranscriptionFrame,
        VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame,
    )

    rt = ca.CameraAgentRuntime(_cfg())
    strat = ca.build_interruption_strategy(_cfg(), runtime=rt)
    started, resets = [], []

    async def on_started(s, params):
        started.append(params.enable_interruptions)
        await strat.handle_user_turn_started()

    async def on_reset(s):
        resets.append(True)

    strat.add_event_handler("on_user_turn_started", on_started)
    strat.add_event_handler("on_reset_aggregation", on_reset)

    async def speech(text, secs, *, bot):
        t0 = time.time()
        await strat.process_frame(VADUserStartedSpeakingFrame(timestamp=t0))
        await strat.process_frame(VADUserStoppedSpeakingFrame(stop_secs=0.2, timestamp=t0 + secs + 0.2))
        await strat.process_frame(TranscriptionFrame(text=text, user_id="u", timestamp=""))

    # agent silent: VAD start opens the turn immediately, no interruption flag
    await strat.process_frame(VADUserStartedSpeakingFrame(timestamp=time.time()))
    assert started == [False]
    await strat.handle_user_turn_stopped()

    # agent speaking
    await strat.process_frame(BotStartedSpeakingFrame())
    await speech("yeah okay", 0.6, bot=True)                   # backchannel → ignored
    await speech("he said lunch is at one", 1.4, bot=True)     # aside → ignored
    await speech("no", 0.15, bot=True)                         # too short → ignored
    assert started == [False] and len(resets) == 3
    await speech("no wait, show the gate camera", 1.1, bot=True)   # addressed → interrupts
    assert started == [False, True]
    await strat.handle_user_turn_stopped()
    await strat.process_frame(BotStoppedSpeakingFrame())

    log = list(rt.interruption_log)
    assert [e["interrupt"] for e in log] == [False, False, False, True]
    assert "backchannel" in log[0]["reason"] and "not addressed" in log[1]["reason"]
    assert "too short" in log[2]["reason"] and log[3]["speech_ms"] >= 1000


@pytest.mark.asyncio
async def test_real_pipeline_ignores_backchannel_and_yields_to_an_addressed_phrase():
    """Through the real 1.8 processors (STT → user aggregator → LLM → TTS)
    with the agent marked as speaking: 'yeah okay' produces no
    interruption and no LLM turn; 'no wait, show the gate camera' produces
    an InterruptionFrame and a turn."""
    import math
    import struct
    import time
    import types

    import camera_agent as ca
    from pipecat.frames.frames import (
        BotStartedSpeakingFrame, InputAudioRawFrame, InterruptionFrame,
        VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame,
    )
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.task import PipelineParams
    from pipecat.tests.utils import SleepFrame, run_test

    def tone(seconds, rate=16000):
        n = int(seconds * rate)
        return struct.pack(f"<{n}h", *(int(8000 * math.sin(2 * math.pi * 220 * i / rate)) for i in range(n)))

    class Whisper:
        def __init__(self):
            self.answers = ["yeah okay", "no wait, show the gate camera"]
            self.calls = 0

        async def transcribe(self, audio):
            self.calls += 1
            return self.answers[min(self.calls - 1, len(self.answers) - 1)]

    class Ollama:
        def __init__(self):
            self.calls = []

        async def chat(self, *, messages, tools, temperature, max_tokens):
            self.calls.append([dict(m) for m in messages])
            return {"message": {"content": "Switching to the gate camera."}}

    class Piper:
        async def synthesize(self, text):
            n = int(0.05 * 22050)
            pcm = struct.pack(f"<{n}h", *([0] * n))
            import io
            import wave
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(22050)
                w.writeframes(pcm)
            return buf.getvalue()

    cfg = _cfg()
    cfg.turn_stop_secs = 0.3
    cfg.turn_stop_timeout_secs = 2.0
    rt = types.SimpleNamespace(cfg=cfg, whisper=Whisper(), ollama=Ollama(), piper=Piper(),
                               tool_definitions=[], tool_handlers={},
                               build_system_prompt=lambda: "You are the camera agent.",
                               interruption_log=__import__("collections").deque(maxlen=50))
    core, _context = ca.build_core_processors(rt)

    def utterance(seconds):
        t0 = time.time()
        out = [VADUserStartedSpeakingFrame(timestamp=t0)]
        for _ in range(int(seconds / 0.2)):
            out.append(InputAudioRawFrame(audio=tone(0.2), sample_rate=16000, num_channels=1))
            out.append(SleepFrame(sleep=0.02))
        out.append(VADUserStoppedSpeakingFrame(stop_secs=0.2, timestamp=time.time() + seconds + 0.2))
        out.append(SleepFrame(sleep=1.0))   # STT + gate
        return out

    frames = [BotStartedSpeakingFrame(), SleepFrame(sleep=0.05)]
    frames += utterance(0.8)     # "yeah okay"
    frames += utterance(1.2)     # "no wait, show the gate camera"
    frames.append(SleepFrame(sleep=2.5))

    down, up = await run_test(
        Pipeline(core), frames_to_send=frames,
        pipeline_params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=22050),
        start_timeout=10.0,
    )
    assert rt.whisper.calls >= 2
    log = list(rt.interruption_log)
    assert [e["interrupt"] for e in log] == [False, True], log
    interruptions = [f for f in list(down) + list(up) if isinstance(f, InterruptionFrame)]
    assert interruptions, "the addressed phrase should have interrupted the speaking agent"
    # exactly one LLM turn, and it was the addressed phrase — the
    # backchannel never became a user message
    assert len(rt.ollama.calls) == 1
    user_msgs = [m["content"] for m in rt.ollama.calls[0] if m.get("role") == "user"]
    assert user_msgs and "gate camera" in user_msgs[-1] and "yeah okay" not in " ".join(user_msgs)
