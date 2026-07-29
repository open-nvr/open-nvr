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


async def test_please_wait_gets_nudged_into_acting(brain):
    brain._fake_llm.up = True
    await brain.setup()
    brain._fake_llm.script = [
        # 1st reply: promises action, calls nothing — the bug from the field.
        {"role": "assistant", "content":
            "Let's check the list of cameras. Please wait while I get that information."},
        # After the nudge it actually calls the tool…
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "1", "function": {
             "name": "list_cameras", "arguments": "{}"}}]},
        # …and answers.
        {"role": "assistant", "content": "There are two cameras: Door and Yard."},
    ]
    answer = await brain.ask("are you sure about the camera count?")
    assert answer == "There are two cameras: Door and Yard."
    # The nudge message reached the model.
    from services import NUDGE_MESSAGE
    flat = [m for m in brain._fake_llm.chats[-1] if m.get("role") == "user"]
    assert any(m["content"] == NUDGE_MESSAGE for m in flat)


async def test_conversation_history_carries_over(brain):
    brain._fake_llm.up = True
    await brain.setup()
    brain._fake_llm.script = [
        {"role": "assistant", "content": "You have two cameras."},
        {"role": "assistant", "content": "They are called Door and Yard."},
    ]
    await brain.ask("how many cameras do I have?")
    await brain.ask("what are their names?")
    # The second call's prompt must contain the first exchange.
    msgs = brain._fake_llm.chats[-1]
    contents = [m.get("content", "") for m in msgs]
    assert "how many cameras do I have?" in contents
    assert "You have two cameras." in contents


def test_sounds_unfinished_patterns():
    from services import sounds_unfinished
    assert sounds_unfinished("Please wait while I get that information.")
    assert sounds_unfinished("Let me check the cameras.")
    assert sounds_unfinished("I'll look at the camera now.")
    assert not sounds_unfinished("There are 2 cameras configured.")
    assert not sounds_unfinished("Camera one is online and recording.")


@pytest.fixture
def brain2(tmp_path, monkeypatch):
    """Two named cameras, no default — the ambiguous-camera setup."""
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(FAKE_JPEG)
    cfg = Config(cameras=[
        {"camera_id": "camera_1", "name": "sparsh", "frame_url": frame.as_uri()},
        {"camera_id": "camera_2", "name": "cpplus", "frame_url": frame.as_uri()},
    ])
    fake_vlm = FakeVlm()
    fake_llm = FakeLlm(up=True)
    monkeypatch.setattr(services, "VlmClient", lambda *a, **k: fake_vlm)
    monkeypatch.setattr(services, "LlmClient", lambda *a, **k: fake_llm)
    b = services.AgentBrain(cfg)
    b._fake_vlm = fake_vlm
    b._fake_llm = fake_llm
    return b


async def test_ambiguous_vision_question_asks_which_camera(brain2):
    await brain2.setup()
    # "these cameras" resolves to no specific camera -> direct clarification
    # (never reaches the LLM, which would produce tool-speak).
    answer = await brain2.ask("What do you see on these cameras?")
    assert "which camera" in answer.lower()
    assert "sparsh" in answer and "cpplus" in answer
    # Only the setup() warm-up chat happened — the question itself never
    # reached the LLM.
    assert len(brain2._fake_llm.chats) == 1


async def test_tool_narration_gets_retried(brain2):
    await brain2.setup()
    brain2._fake_llm.script = [
        {"role": "assistant", "content":
            "Call look_at_camera with camera_id 'cp2s' to see what is visible."},
        {"role": "assistant", "content": "The room is empty."},
    ]
    answer = await brain2.ask("tell me something about my setup")
    assert answer == "The room is empty."
    nudges = [m for m in brain2._fake_llm.chats[-1]
              if m.get("role") == "user" and "Never tell the user about tools" in m.get("content", "")]
    assert nudges


async def test_persistent_tool_narration_degrades_gracefully(brain2):
    await brain2.setup()
    brain2._fake_llm.script = [
        {"role": "assistant", "content":
            "Call look_at_camera with camera_id 'cp2s' to see what is visible."},
        {"role": "assistant", "content":
            "Use the look_at_camera function with the right camera_id."},
    ]
    answer = await brain2.ask("tell me something about my setup")
    assert "look_at_camera" not in answer and "camera_id" not in answer
    assert "sparsh" in answer and "cpplus" in answer
