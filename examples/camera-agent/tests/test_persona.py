# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the agent persona (nameless — "the OpenNVR camera agent"), the
spoken intro, and the background task system."""
from __future__ import annotations

import asyncio
import base64

from fastapi.testclient import TestClient

import camera_agent as ca
from camera_agent import AppConfig, CameraAgentRuntime, build_app
from context import CameraSpec


def _runtime() -> CameraAgentRuntime:
    cfg = AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="be concise",
        cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="front door")],
    )
    return CameraAgentRuntime(cfg)


# ── persona ────────────────────────────────────────────────────────────


def test_system_prompt_identifies_agent_and_describes_tasks():
    prompt = _runtime().build_system_prompt()
    assert "camera agent" in prompt.lower()   # nameless identity, no persona name
    assert "create_background_task" in prompt


def test_agent_name_default_and_configurable():
    assert ca.agent_name_for(None) == "Camera Agent"   # technical default (wake/metadata)
    assert ca.agent_name_for("Nova") == "Nova"         # operator override
    assert "camera agent" in ca.greeting_for().lower()  # greeting is nameless


def test_name_is_independent_of_voice_gender():
    # The voice can be male without affecting the (technical) default name.
    cfg = AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t",
                    voice_gender="male",
                    cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="r")])
    rt = CameraAgentRuntime(cfg)
    assert rt.agent_name == "Camera Agent"
    assert "camera agent" in rt.build_system_prompt().lower()


def test_custom_agent_name_used():
    cfg = AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t",
                    agent_name="Nova",
                    cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="r")])
    assert CameraAgentRuntime(cfg).agent_name == "Nova"


def test_llm_think_false_appends_no_think():
    # Qwen3 default runs non-thinking for snappy tool-calling
    cfg = AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t",
                    llm_think=False,
                    cameras=[CameraSpec(camera_id="c", frame_url="http://x/1.jpg", role="r")])
    assert CameraAgentRuntime(cfg).build_system_prompt().rstrip().endswith("/no_think")


def test_llm_think_default_and_true_do_not_append():
    # default (None) and True must NOT inject /no_think (non-thinking models)
    assert "/no_think" not in _runtime().build_system_prompt()
    cfg = AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t",
                    llm_think=True,
                    cameras=[CameraSpec(camera_id="c", frame_url="http://x/1.jpg", role="r")])
    assert "/no_think" not in CameraAgentRuntime(cfg).build_system_prompt()


# ── tool wiring: create_background_task is foreground-only ─────────────


def test_create_task_tool_foreground_only():
    rt = _runtime()
    fg = [t["function"]["name"] for t in rt.tool_definitions]
    bg = [t["function"]["name"] for t in rt.background_tool_definitions]
    assert "create_background_task" in fg
    assert "create_background_task" not in bg  # a task must not spawn tasks


# ── task manager lifecycle ─────────────────────────────────────────────


def test_task_runs_to_completion(monkeypatch):
    rt = _runtime()

    async def fake_turn(runtime, history, query, *, tool_definitions=None, **kw):
        # background turns must NOT be offered the create_background_task tool
        names = [t["function"]["name"] for t in (tool_definitions or [])]
        assert "create_background_task" not in names
        return f"answer for: {query}"

    monkeypatch.setattr(ca, "_run_conversation_turn", fake_turn)

    async def go():
        task = rt.tasks.create("person in red shirt at 3am two days ago")
        assert task.status in ("queued", "running")
        for _ in range(100):
            if rt.tasks.get(task.id).status in ("done", "error"):
                break
            await asyncio.sleep(0.02)
        t = rt.tasks.get(task.id)
        assert t.status == "done"
        assert t.result == "answer for: person in red shirt at 3am two days ago"

    asyncio.run(go())


def test_create_task_handler_acks_with_id(monkeypatch):
    rt = _runtime()

    async def fake_turn(*a, **k):
        return "x"

    monkeypatch.setattr(ca, "_run_conversation_turn", fake_turn)

    async def go():
        msg = await rt._handle_create_task({"query": "check the back door yesterday"})
        assert "background task #" in msg
        assert len(rt.tasks.list()) == 1

    asyncio.run(go())


def test_create_task_handler_rejects_empty():
    rt = _runtime()
    msg = asyncio.run(rt._handle_create_task({"query": ""}))
    assert "more detail" in msg
    assert rt.tasks.list() == []


# ── endpoints ──────────────────────────────────────────────────────────


def test_intro_endpoint_returns_text_and_audio(monkeypatch):
    rt = _runtime()

    async def fake_synth(_text):
        return b"WAVDATA"

    monkeypatch.setattr(rt.piper, "synthesize", fake_synth)
    client = TestClient(build_app(rt))
    data = client.get("/intro").json()
    assert data["name"] == "Camera Agent"
    assert "camera agent" in data["text"].lower()
    assert data["audio_b64"] == base64.b64encode(b"WAVDATA").decode()


def test_intro_endpoint_text_only_when_tts_down(monkeypatch):
    rt = _runtime()

    async def boom(_text):
        raise RuntimeError("piper down")

    monkeypatch.setattr(rt.piper, "synthesize", boom)
    client = TestClient(build_app(rt))
    data = client.get("/intro").json()
    assert data["audio_b64"] is None
    assert "camera agent" in data["text"].lower()


def test_tasks_endpoints(monkeypatch):
    rt = _runtime()

    async def fake_turn(*a, **k):
        return "result"

    monkeypatch.setattr(ca, "_run_conversation_turn", fake_turn)
    client = TestClient(build_app(rt))

    r = client.post("/tasks", json={"query": "find the red truck earlier"})
    assert r.status_code == 202
    tid = r.json()["id"]

    listed = client.get("/tasks").json()["tasks"]
    assert any(t["id"] == tid for t in listed)

    assert client.post("/tasks", json={}).status_code == 400


def test_say_endpoint_synthesizes_text(monkeypatch):
    rt = _runtime()
    seen = {}

    async def fake_synth(text):
        seen["text"] = text
        return b"SPOKEN"

    monkeypatch.setattr(rt.piper, "synthesize", fake_synth)
    client = TestClient(build_app(rt))

    r = client.post("/say", json={"text": "Done with task #1: found a red truck."})
    assert r.status_code == 200
    assert r.json()["audio_b64"] == base64.b64encode(b"SPOKEN").decode()
    assert "red truck" in seen["text"]

    assert client.post("/say", json={"text": ""}).status_code == 400


def test_say_endpoint_text_only_when_tts_down(monkeypatch):
    rt = _runtime()

    async def boom(_text):
        raise RuntimeError("piper down")

    monkeypatch.setattr(rt.piper, "synthesize", boom)
    client = TestClient(build_app(rt))
    r = client.post("/say", json={"text": "hello"})
    assert r.status_code == 200 and r.json()["audio_b64"] is None


def test_greeting_opens_with_the_time_of_day():
    import camera_agent as ca

    assert ca.time_of_day_salutation(7) == "Good morning"
    assert ca.time_of_day_salutation(11) == "Good morning"
    assert ca.time_of_day_salutation(12) == "Good afternoon"
    assert ca.time_of_day_salutation(16) == "Good afternoon"
    assert ca.time_of_day_salutation(17) == "Good evening"
    assert ca.time_of_day_salutation(21) == "Good evening"
    assert ca.time_of_day_salutation(23) == "Hello"      # nobody means "good morning" at 2 am
    assert ca.time_of_day_salutation(2) == "Hello"
    assert ca.time_of_day_salutation("bogus") in ("Good morning", "Good afternoon", "Good evening", "Hello")
    assert ca.greeting_for(hour=9).startswith("Good morning, I'm the OpenNVR Agent")
    assert ca.greeting_for(hour=None).split(",")[0] in ("Good morning", "Good afternoon", "Good evening", "Hello")


def test_intro_takes_the_operators_hour_and_the_page_greets_on_open():
    from fastapi.testclient import TestClient
    from pathlib import Path

    import camera_agent as ca
    from context import CameraSpec

    cfg = ca.AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t",
                       cameras=[CameraSpec(camera_id="c", frame_url="http://x", role="f")])
    rt = ca.CameraAgentRuntime(cfg)

    class _NoPiper:
        async def synthesize(self, text):
            raise RuntimeError("down")
    rt.piper = _NoPiper()
    tc = TestClient(ca.build_app(rt))
    assert tc.get("/intro?hour=8").json()["text"].startswith("Good morning")
    assert tc.get("/intro?hour=19").json()["text"].startswith("Good evening")
    assert tc.get("/intro?hour=19").json()["audio_b64"] is None      # text always, audio when Piper is up

    html = (Path(__file__).resolve().parents[1] / "demo" / "index.html").read_text()
    # greets when the page opens (after auth resolves), not only on Talk
    assert 'if(!introPlayed){ introPlayed=true; playIntro(); }' in html
    # the browser's own hour rides along
    assert '"/intro?hour="+new Date().getHours()' in html
    # autoplay-safe: held audio is spoken on the first gesture or on Talk
    assert "playHeldIntro" in html and 'addEventListener("pointerdown"' in html
