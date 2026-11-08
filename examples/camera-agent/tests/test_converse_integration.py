# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""End-to-end HTTP integration tests for the camera-agent server.

Drives the real FastAPI request path through a TestClient — including the
real ffmpeg transcode in /converse — with the model clients (Whisper STT,
Ollama LLM, Piper TTS) and the vision tools stubbed. This exercises the
production turn: audio in → transcode → STT → tool-calling loop (incl.
camera hint + forced grounding) → TTS → JSON out.

Requires the ``ffmpeg`` binary (shipped in the camera-agent image).
"""
from __future__ import annotations

import base64
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

import camera_agent as ca
from camera_agent import AppConfig, CameraAgentRuntime, build_app
from context import CameraSpec

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not available"
)


def _wav_blob(seconds: float = 0.4) -> bytes:
    """A real (silent-ish) WAV so the actual ffmpeg transcode path runs."""
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}",
         "-ac", "1", "-ar", "44100", "-f", "wav", "pipe:1"],
        capture_output=True,
    )
    assert proc.returncode == 0 and proc.stdout, proc.stderr
    return proc.stdout


@pytest.fixture
def harness(monkeypatch):
    cfg = AppConfig(
        kaic_url="http://kaic", kaic_api_key="k",
        whisper_url="http://w", ollama_url="http://o", piper_url="http://p",
        system_prompt="test",
        # These tests exercise the STT→LLM→tool→TTS pipeline, not the wake-word
        # gate (which has its own tests). Turn it off so a plain transcript is
        # answered; the gate is tested explicitly with ?wake=1 below.
        wake_word_required=False,
        cameras=[
            CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="front door"),
            CameraSpec(camera_id="cam2", frame_url="http://x/2.jpg", role="back yard"),
        ],
    )
    rt = CameraAgentRuntime(cfg)

    state = {"transcript": "what do you see", "detect_args": None,
             "chat": None}

    async def fake_transcribe(_wav_bytes):
        return state["transcript"]

    async def fake_synth(_text):
        return b"FAKEWAVBYTES"

    async def fake_detect(args):
        state["detect_args"] = args
        return "1 person"

    monkeypatch.setattr(rt.whisper, "transcribe", fake_transcribe)
    monkeypatch.setattr(rt.piper, "synthesize", fake_synth)
    rt.tool_handlers["detect_objects"] = fake_detect

    def set_chat(fn):
        monkeypatch.setattr(rt.ollama, "chat", fn)
    state["set_chat"] = set_chat
    state["rt"] = rt

    # No context manager → skip startup() (NATS + LLM prewarm) which we
    # don't need for endpoint tests.
    client = TestClient(build_app(rt))
    return client, state


# ── basic endpoints ───────────────────────────────────────────────────


def test_health_ok(harness):
    client, _ = harness
    assert client.get("/health").status_code == 200


def test_cameras_lists_configured(harness):
    client, _ = harness
    body = client.get("/cameras").json()
    ids = [c["camera_id"] for c in body["cameras"]]
    assert ids == ["cam1", "cam2"]
    assert body["cameras"][0]["role"] == "front door"


def test_reset_ok(harness):
    client, _ = harness
    assert client.post("/reset").json()["status"] == "ok"


def test_converse_empty_body_400(harness):
    client, _ = harness
    assert client.post("/converse", content=b"").status_code == 400


# ── full turn: model invokes a tool, then answers ──────────────────────


def test_converse_full_tool_turn(harness):
    client, state = harness
    # A question the router leaves to the model (it asks WHO), so the
    # model's own tool call is what gets exercised.
    state["transcript"] = "who is on cam1"

    async def chat(*, messages, tools=None, temperature=0.4, max_tokens=256, **kw):
        # Second pass (tool result present) → final answer.
        if any(m.get("role") == "tool" for m in messages):
            return {"message": {"role": "assistant", "content": "I see one person."}}
        # First pass → invoke detect_objects on cam1.
        return {"message": {"role": "assistant", "content": "",
                            "tool_calls": [{"id": "c1", "type": "function",
                                            "function": {"name": "detect_objects",
                                                         "arguments": {"camera_id": "cam1"}}}]}}
    state["set_chat"](chat)

    resp = client.post("/converse", content=_wav_blob())
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["transcript"] == "who is on cam1"
    assert data["reply"] == "I see one person."
    assert data["audio_b64"] == base64.b64encode(b"FAKEWAVBYTES").decode()
    assert state["detect_args"] == {"camera_id": "cam1"}
    # per-phase timing breakdown is reported for the latency harness
    assert set(data["timings_ms"]) >= {"transcode", "stt", "llm", "tts", "total"}


def test_converse_logs_timings_breakdown(harness, caplog):
    """The per-stage breakdown is logged so a slow turn can be diagnosed from
    the agent logs (grep 'converse: timings_ms'), not only the browser."""
    import logging

    client, state = harness

    async def chat(*, messages, tools=None, temperature=0.4, max_tokens=256, **kw):
        return {"message": {"role": "assistant", "content": "All clear."}}
    state["set_chat"](chat)

    with caplog.at_level(logging.INFO):
        resp = client.post("/converse", content=_wav_blob())
    assert resp.status_code == 200, resp.text
    line = next((r.getMessage() for r in caplog.records
                 if "converse: timings_ms" in r.getMessage()), None)
    assert line is not None
    for stage in ("transcode", "stt", "llm", "tts", "total"):
        assert stage in line


# ── camera dropdown hint drives the forced-grounding default ───────────


def test_converse_camera_hint_selects_camera(harness):
    client, state = harness
    # A detection-style question so forced grounding picks detect_objects
    # (vs describe_camera for open-ended "what do you see").
    state["transcript"] = "how many people are there"

    # Model answers WITHOUT calling a tool → anti-fabrication forces a
    # detection. With ?camera=cam2 the forced detection must target cam2.
    async def chat(*, messages, tools=None, temperature=0.4, max_tokens=256, **kw):
        if any(m.get("role") == "tool" for m in messages):
            return {"message": {"role": "assistant", "content": "Grounded answer."}}
        return {"message": {"role": "assistant", "content": "There is a dog."}}
    state["set_chat"](chat)

    resp = client.post("/converse?camera=cam2", content=_wav_blob())
    assert resp.status_code == 200, resp.text
    assert state["detect_args"] == {"camera_id": "cam2"}


def test_converse_bogus_camera_hint_falls_back(harness):
    client, state = harness
    state["transcript"] = "how many people are there"

    async def chat(*, messages, tools=None, temperature=0.4, max_tokens=256, **kw):
        if any(m.get("role") == "tool" for m in messages):
            return {"message": {"role": "assistant", "content": "Grounded."}}
        return {"message": {"role": "assistant", "content": "There is a cat."}}
    state["set_chat"](chat)

    # Unknown camera id is ignored → with no camera named, every camera
    # (the first camera used to stand in for the fleet).
    resp = client.post("/converse?camera=does-not-exist", content=_wav_blob())
    assert resp.status_code == 200, resp.text
    assert state["detect_args"] == {"camera_id": "all"}


# ── empty transcript short-circuits before the LLM ─────────────────────


def test_converse_empty_transcript_returns_blank(harness):
    client, state = harness
    state["transcript"] = ""   # Whisper found no speech

    called = {"chat": False}

    async def chat(**kw):
        called["chat"] = True
        return {"message": {"role": "assistant", "content": "should not run"}}
    state["set_chat"](chat)

    data = client.post("/converse", content=_wav_blob()).json()
    assert data["transcript"] == "" and data["reply"] == "" and data["audio_b64"] is None
    assert called["chat"] is False


# ── wake-word gate (voice): only answer when addressed by name ─────────


def test_converse_wake_gate_ignores_unaddressed(harness):
    client, state = harness
    state["transcript"] = "what do you see"   # no wake word

    called = {"chat": False}

    async def chat(**kw):
        called["chat"] = True
        return {"message": {"role": "assistant", "content": "should not run"}}
    state["set_chat"](chat)

    # ?wake=1 forces the gate on for this request (harness default is off).
    data = client.post("/converse?wake=1", content=_wav_blob()).json()
    assert data["invoked"] is False
    assert data["reply"] == "" and data["audio_b64"] is None
    assert data["transcript"] == "what do you see"   # still echoed for the UI
    assert called["chat"] is False                   # LLM never spent


def test_converse_wake_gate_answers_when_addressed(harness):
    client, state = harness
    # The wake gate matches the agent's name (default "Camera Agent").
    state["transcript"] = "hey Camera Agent, what do you see"

    async def chat(*, messages, tools=None, temperature=0.4, max_tokens=256, **kw):
        # The wake phrase must be stripped before the model sees the question.
        user = [m for m in messages if m.get("role") == "user"][-1]["content"].lower()
        # The user turn carries the clock above the user's words (see
        # _user_turn); the words themselves are the last paragraph.
        words = user.strip().split("\n\n")[-1].strip()
        assert "camera agent" not in user and words == "what do you see"
        return {"message": {"role": "assistant", "content": "I see the front door."}}
    state["set_chat"](chat)

    data = client.post("/converse?wake=1", content=_wav_blob()).json()
    assert data["invoked"] is True
    assert "front door" in data["reply"]


def test_converse_bare_wake_word_acks_without_llm(harness):
    client, state = harness
    state["transcript"] = "Hey Camera Agent"   # just the name, no question

    called = {"chat": False}

    async def chat(**kw):
        called["chat"] = True
        return {"message": {"role": "assistant", "content": "x"}}
    state["set_chat"](chat)

    data = client.post("/converse?wake=1", content=_wav_blob()).json()
    assert data["invoked"] is True
    assert data["armed"] is True         # bare wake word arms her (Hey-Siri style)
    assert data["reply"]                 # a spoken acknowledgement ("Yes?")
    assert called["chat"] is False       # but no LLM spend


def test_a_bare_wake_word_records_no_stale_stages(harness):
    """An armed turn runs no LLM, so it must not feed the PREVIOUS turn's
    llm/tool latencies into the thinking-aloud medians a second time."""
    client, state = harness
    rt = state["rt"]
    rt.last_turn_trace = [{"step": "llm", "detail": "iter 1", "ms": 9999}]
    state["transcript"] = "Hey Camera Agent"

    async def chat(**kw):
        return {"message": {"role": "assistant", "content": "x"}}
    state["set_chat"](chat)

    data = client.post("/converse?wake=1", content=_wav_blob()).json()
    assert data["armed"] is True
    assert data["trace"] == []
    assert 9999.0 not in rt.thinking.stage_ms.get("llm", ())


# ── talking-avatar clips are served (whitelisted) ─────────────────────


def test_avatar_clip_served(harness):
    client, _ = harness
    # All three states, both formats, are whitelisted and shipped.
    for name in ("idle.webm", "idle.mp4", "speaking.webm", "speaking.mp4",
                 "thinking.webm", "thinking.mp4"):
        r = client.get("/demo/avatar/" + name)
        assert r.status_code == 200, name
        assert r.headers["content-type"].startswith("video/"), name


def test_avatar_unknown_name_404(harness):
    client, _ = harness
    assert client.get("/demo/avatar/evil.sh").status_code == 404
    assert client.get("/demo/avatar/secrets.yml").status_code == 404


def test_agent_reports_avatar_video(harness):
    client, _ = harness
    # Default on; the UI reads this to decide video-avatar vs SVG-face.
    assert client.get("/agent").json()["avatar_video"] is True


# ── skills catalogue (Skills panel) ───────────────────────────────────


def test_skills_lists_core_capabilities(harness):
    client, _ = harness
    skills = client.get("/skills").json()["skills"]
    by_id = {s["id"]: s for s in skills}
    # Core vision + control skills are always available in the default harness.
    for sid in ("see", "count", "alarm", "watch", "report", "task"):
        assert by_id[sid]["available"] is True, sid
        assert by_id[sid]["example"] and by_id[sid]["name"]


def test_skills_gate_unconfigured_backends(harness):
    client, _ = harness
    by_id = {s["id"]: s for s in client.get("/skills").json()["skills"]}
    # No faces_url / NATS / footage index in the harness → these are off, with a hint.
    for sid in ("faces", "events", "footage"):
        assert by_id[sid]["available"] is False, sid
        assert by_id[sid]["hint"], sid  # tells the user how to enable it


def test_skills_turn_on_when_configured():
    # Wiring faces_url + a NATS url flips those skills to available (no hint).
    cfg = AppConfig(
        kaic_url="http://kaic", kaic_api_key="k", system_prompt="t",
        faces_url="http://faces", nats_inference_url="nats://127.0.0.1:4222",
        cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="r")],
    )
    client = TestClient(build_app(CameraAgentRuntime(cfg)))
    by_id = {s["id"]: s for s in client.get("/skills").json()["skills"]}
    assert by_id["faces"]["available"] is True and by_id["faces"]["hint"] == ""
    assert by_id["events"]["available"] is True and by_id["events"]["hint"] == ""


def _skill_runtime():
    cfg = AppConfig(
        kaic_url="http://kaic", kaic_api_key="k", system_prompt="t",
        cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="r")],
    )
    rt = CameraAgentRuntime(cfg)
    return rt, TestClient(build_app(rt))


def test_skill_toggle_reconfigures_live_toolset():
    rt, client = _skill_runtime()
    advertised = lambda: {t["function"]["name"] for t in rt.tool_definitions}
    assert "detect_objects" in advertised()
    # Remove the "count" skill → its tool drops from the advertised set.
    r = client.post("/skills/count/disable")
    assert r.status_code == 200
    assert "detect_objects" not in advertised()
    assert {s["id"]: s for s in r.json()["skills"]}["count"]["enabled"] is False
    # Add it back → tool returns.
    r = client.post("/skills/count/enable")
    assert r.status_code == 200
    assert "detect_objects" in advertised()


def test_skill_enable_unconfigured_returns_409_with_hint():
    _, client = _skill_runtime()   # no faces_url/nats/footage
    r = client.post("/skills/faces/enable")
    assert r.status_code == 409
    assert r.json()["hint"]        # tells the operator how to enable it


def test_skill_unknown_404_and_bad_action_400():
    _, client = _skill_runtime()
    assert client.post("/skills/not-a-skill/disable").status_code == 404
    assert client.post("/skills/count/frobnicate").status_code == 400


# ── alarm ringing must track ACTIVE alarms only ───────────────────────


def test_alarm_ringing_only_when_active_and_triggered():
    from camera_agent import Alarm
    rt, client = _skill_runtime()
    assert client.get("/alarms").json()["ringing"] is False   # nothing armed
    # Inject an armed, triggered alarm (bypass create() to avoid the async loop).
    a = Alarm(id=1, name="Fire", target="fire", camera_ids=["cam1"], triggered=True)
    rt.alarms._alarms[1] = a
    rt.alarms._order.append(1)
    assert client.get("/alarms").json()["ringing"] is True
    # A stale 'triggered' flag on a DISARMED alarm must NOT keep the siren up —
    # this was the bug: banner rang while the panel showed "No alarms armed".
    a.active = False
    assert client.get("/alarms").json()["ringing"] is False


# ── roster/config questions skip the LLM (latency) ────────────────────


def test_config_question_short_circuits_before_llm():
    import asyncio
    rt, _ = _skill_runtime()

    async def boom(**kw):   # the LLM must NOT be called for a roster question
        raise AssertionError("LLM was called for a config question")
    rt.ollama.chat = boom

    reply = asyncio.run(ca._run_conversation_turn(
        rt, [], "how many cameras are configured right now?"))
    assert "camera" in reply.lower()   # deterministic roster answer, no LLM spend


def test_converse_routed_turn_makes_one_compose_call_and_no_tool_loop(harness):
    """The router (router.py): the UI is on cam1, the question is a count.
    Every slot resolves, so the tool runs first and the model is asked
    only to say the answer — one LLM call, no tool schemas."""
    client, state = harness
    state["transcript"] = "how many people are there"
    calls = []

    async def chat(*, messages, tools=None, temperature=0.4, max_tokens=256, **kw):
        calls.append({"tools": tools, "roles": [m.get("role") for m in messages]})
        if any(m.get("role") == "tool" for m in messages):
            return {"message": {"role": "assistant", "content": "One person is at the front door."}}
        return {"message": {"role": "assistant", "content": "",
                            "tool_calls": [{"id": "c1", "type": "function",
                                            "function": {"name": "detect_objects",
                                                         "arguments": {"camera_id": "cam2"}}}]}}
    state["set_chat"](chat)
    data = client.post("/converse?camera=cam1", content=_wav_blob()).json()
    assert state["detect_args"] == {"camera_id": "cam1"}, "the router picked the camera the UI is on"
    assert data["reply"] == "One person is at the front door."
    assert len(calls) == 1, "one compose call; the tool-calling iteration was skipped"
    assert calls[0]["tools"] is None and "tool" in calls[0]["roles"]
    assert [t["step"] for t in data["trace"]][:3] == ["route", "detect_objects", "compose"]
