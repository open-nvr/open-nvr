# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AgentBrain: vision fast-path, LLM tool loop, deterministic degradation."""
import json

import pytest

import services
from adapter_clients import VlmResult
from camera_agent import Config

FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"


class FakeVlm:
    def __init__(self):
        self.calls = []

    async def analyse_image(self, jpeg, question):
        self.calls.append(question)
        return VlmResult(text="One person is standing at the door.", inference_ms=1.0)

    async def analyse_frames(self, jpegs, question):
        self.calls.append(question)
        return VlmResult(text="Someone walked past.", inference_ms=1.0)

    async def health(self):
        return True

    async def aclose(self):
        pass


class FakeLlm:
    """Scriptable LLM: first reply asks for a tool, second gives the answer."""

    def __init__(self, up=True, script=None):
        self.up = up
        self.script = list(script or [])
        self.chats = []

    async def chat(self, messages, tools=None, **params):
        self.chats.append(messages)
        if self.script:
            msg = self.script.pop(0)
        else:
            msg = {"role": "assistant", "content": "ok"}
        return {"choices": [{"message": msg}], "usage": {}}

    async def health(self):
        return self.up

    async def aclose(self):
        pass


@pytest.fixture
def brain(tmp_path, monkeypatch):
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(FAKE_JPEG)
    cfg = Config(cameras=[
        {"camera_id": "camera_1", "name": "Door", "frame_url": frame.as_uri()},
    ])
    fake_vlm = FakeVlm()
    fake_llm = FakeLlm(up=False)
    monkeypatch.setattr(services, "VlmClient", lambda *a, **k: fake_vlm)
    monkeypatch.setattr(services, "LlmClient", lambda *a, **k: fake_llm)
    b = services.AgentBrain(cfg)
    b._fake_vlm = fake_vlm
    b._fake_llm = fake_llm
    return b


async def test_vision_fast_path_skips_llm(brain):
    await brain.setup()
    answer = await brain.ask("what is happening on camera one?")
    assert answer == "One person is standing at the door."
    assert brain._fake_vlm.calls  # VLM was used
    # No conversational LLM call happened (only the health probes ran).
    assert brain._fake_llm.chats == []


async def test_deterministic_list_cameras_without_llm(brain):
    await brain.setup()
    answer = await brain.ask("list cameras")
    assert "camera 1" in answer
    assert "online" in answer


async def test_deterministic_time_without_llm(brain):
    await brain.setup()
    answer = await brain.ask("what time is it?")
    assert answer.startswith("It's ")


async def test_text_question_without_llm_degrades(brain):
    await brain.setup()
    answer = await brain.ask("explain SIP registration")
    assert "language model" in answer


async def test_llm_tool_loop(brain):
    brain._fake_llm.up = True
    await brain.setup()
    # Script set AFTER setup so the warm-up chat doesn't consume it.
    brain._fake_llm.script = [
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "1", "function": {
             "name": "get_camera_status",
             "arguments": json.dumps({"camera_id": "camera_1"})}}]},
        {"role": "assistant", "content": "Camera one is online and recording."},
    ]
    answer = await brain.ask("should I be worried about anything right now?")
    assert answer == "Camera one is online and recording."
    # The tool result was fed back to the model.
    last_messages = brain._fake_llm.chats[-1]
    tool_msgs = [m for m in last_messages if m.get("role") == "tool"]
    assert tool_msgs and json.loads(tool_msgs[0]["content"])["ok"] is True


async def test_llm_unknown_tool_survives(brain):
    brain._fake_llm.up = True
    await brain.setup()
    brain._fake_llm.script = [
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "1", "function": {
             "name": "delete_everything", "arguments": "{}"}}]},
        {"role": "assistant", "content": "Sorry, I can't do that."},
    ]
    answer = await brain.ask("wipe everything please")
    assert answer == "Sorry, I can't do that."
    tool_msgs = [m for m in brain._fake_llm.chats[-1] if m.get("role") == "tool"]
    assert "unknown tool" in tool_msgs[0]["content"]
