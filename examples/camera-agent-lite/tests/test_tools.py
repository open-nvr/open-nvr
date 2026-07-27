# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tool validation, execution, and the handlers against a static file camera."""
import pathlib

import pytest

from adapter_clients import VlmResult
from context import CameraContext
from tools import (
    SPECS,
    ToolContext,
    ToolExecutor,
    ToolRegistry,
    ValidationError,
    register_tools,
    validate_args,
)

# A tiny JPEG-ish payload; nothing decodes it, so magic bytes are enough.
FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"


class FakeVlm:
    def __init__(self, text="I can see one person in the frame.", error=None):
        self.text = text
        self.error = error
        self.calls = []

    async def analyse_image(self, jpeg, question):
        self.calls.append({"n": 1, "question": question})
        return VlmResult(text=self.text, inference_ms=1.0, error=self.error)

    async def analyse_frames(self, jpegs, question):
        self.calls.append({"n": len(jpegs), "question": question})
        return VlmResult(text=self.text, inference_ms=1.0, error=self.error)


@pytest.fixture
def frame_file(tmp_path: pathlib.Path) -> pathlib.Path:
    p = tmp_path / "frame.jpg"
    p.write_bytes(FAKE_JPEG)
    return p


def make_executor(frame_file, vlm=None):
    ctx = CameraContext(cameras=[
        {"camera_id": "camera_1", "name": "Front door",
         "frame_url": frame_file.as_uri()},
        {"camera_id": "camera_2", "name": "Yard",
         "frame_url": frame_file.as_uri()},
    ])
    vlm = vlm or FakeVlm()
    registry = ToolRegistry()
    register_tools(registry, ToolContext(context=ctx, vlm=vlm,
                                         multi_frame_interval_ms=10))
    return ToolExecutor(registry), vlm


# ---- validation ------------------------------------------------------------ #

def test_missing_required_argument():
    with pytest.raises(ValidationError, match="camera_id"):
        validate_args(SPECS["look_at_camera"], {"question": "who is there?"})


def test_unknown_argument_rejected():
    with pytest.raises(ValidationError, match="unknown argument"):
        validate_args(SPECS["list_cameras"], {"nope": 1})


def test_wrong_type_rejected():
    with pytest.raises(ValidationError, match="must be string"):
        validate_args(SPECS["get_camera_status"], {"camera_id": 2})


def test_defaults_applied():
    cleaned = validate_args(SPECS["look_at_camera"],
                            {"camera_id": "camera_1", "question": "hi"})
    assert cleaned["temporal"] is False


# ---- executor -------------------------------------------------------------- #

async def test_unknown_tool_rejected(frame_file):
    executor, _ = make_executor(frame_file)
    result = await executor.execute("run_shell", {"cmd": "rm -rf /"})
    assert not result.ok
    assert "unknown tool" in result.error


async def test_invalid_args_never_reach_handler(frame_file):
    executor, vlm = make_executor(frame_file)
    result = await executor.execute("look_at_camera", {"camera_id": "camera_1"})
    assert not result.ok and "invalid arguments" in result.error
    assert vlm.calls == []


# ---- handlers -------------------------------------------------------------- #

async def test_look_at_camera_happy_path(frame_file):
    executor, vlm = make_executor(frame_file)
    result = await executor.execute(
        "look_at_camera", {"camera_id": "camera_1", "question": "who is there?"})
    assert result.ok
    assert result.data["answer"] == "I can see one person in the frame."
    assert vlm.calls[0]["n"] == 1


async def test_look_at_camera_temporal_uses_multiple_frames(frame_file):
    executor, vlm = make_executor(frame_file)
    result = await executor.execute(
        "look_at_camera",
        {"camera_id": "camera_1", "question": "did someone walk by?", "temporal": True})
    assert result.ok
    assert vlm.calls[0]["n"] == 3


async def test_look_at_camera_unknown_camera(frame_file):
    executor, _ = make_executor(frame_file)
    result = await executor.execute(
        "look_at_camera", {"camera_id": "camera_9", "question": "who?"})
    assert not result.ok
    assert "unavailable" in result.error


async def test_look_at_camera_vlm_error_is_friendly(frame_file):
    executor, _ = make_executor(frame_file, vlm=FakeVlm(error="adapter down"))
    result = await executor.execute(
        "look_at_camera", {"camera_id": "camera_1", "question": "who?"})
    assert not result.ok
    assert "vision model" in result.error


async def test_list_cameras(frame_file):
    executor, _ = make_executor(frame_file)
    result = await executor.execute("list_cameras", {})
    assert result.ok
    ids = [c["camera_id"] for c in result.data["cameras"]]
    assert ids == ["camera_1", "camera_2"]


async def test_get_camera_status(frame_file):
    executor, _ = make_executor(frame_file)
    result = await executor.execute("get_camera_status", {"camera_id": "camera_2"})
    assert result.ok
    assert result.data["state"] == "online"


async def test_current_time(frame_file):
    executor, _ = make_executor(frame_file)
    result = await executor.execute("current_time", {})
    assert result.ok and result.data["spoken"]
