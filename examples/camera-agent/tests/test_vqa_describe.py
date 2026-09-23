# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""describe_camera is VQA-ready: when the user asks a specific question it's
forwarded to the caption/VLM adapter, a VQA adapter's `answer` is used, and a
plain captioner that ignores the question still returns its `caption`
(test-report S-6 — "what is he wearing?" must be answerable)."""
from __future__ import annotations

import asyncio

from context import CameraContext, CameraSpec
from tools import CameraTools, build_tool_definitions


class _FakeSource:
    camera_id = "cam1"
    def fetch(self):
        return b"jpeg"


class _VQAClient:
    """A VLM adapter: echoes the question as an answer."""
    def __init__(self):
        self.seen = {}
    async def infer(self, *, frame_jpeg, extra=None, correlation_id=None):
        self.seen = extra or {}
        q = (extra or {}).get("question")
        return {"result": {"answer": f"the person is wearing blue" if q else ""}}


class _CaptionOnlyClient:
    """A plain captioner (BLIP): ignores the question, returns a caption."""
    async def infer(self, *, frame_jpeg, extra=None, correlation_id=None):
        return {"result": {"caption": "a man sitting at a desk"}}


def _tools(caption_client):
    ctx = CameraContext(cameras=[CameraSpec(camera_id="cam1", frame_url="x", role="r")])
    ctx.register_frame_source("cam1", _FakeSource())
    return CameraTools(
        context=ctx, detection_client=None, caption_client=caption_client,
        recognition_client=None,
    )


def test_question_forwarded_and_vqa_answer_used():
    c = _VQAClient()
    t = _tools(c)
    out = asyncio.run(t.describe_camera({"camera_id": "cam1", "question": "what is he wearing?"}))
    assert "wearing blue" in out
    # the question reached the adapter (both keys, for adapter-name compatibility)
    assert c.seen.get("question") == "what is he wearing?"
    assert c.seen.get("prompt") == "what is he wearing?"


def test_plain_captioner_still_works_without_question():
    t = _tools(_CaptionOnlyClient())
    out = asyncio.run(t.describe_camera({"camera_id": "cam1"}))
    assert "a man sitting at a desk" in out


class _RecordingCaptioner:
    """Records the extra payload and always returns a caption."""
    def __init__(self):
        self.seen = {}
    async def infer(self, *, frame_jpeg, extra=None, correlation_id=None):
        self.seen = extra or {}
        return {"result": {"caption": "a man at a desk"}}


def test_question_does_not_pin_scene_caption_task():
    # Regression: pinning task="scene_caption" suppressed VQA on moondream
    # (is_vqa requires task != "scene_caption"), so every question got the same
    # generic caption back. With a question we must NOT send scene_caption.
    c = _RecordingCaptioner()
    asyncio.run(_tools(c).describe_camera(
        {"camera_id": "cam1", "question": "what is he doing?"}))
    assert c.seen.get("task") != "scene_caption"
    assert c.seen.get("question") == "what is he doing?"


def test_open_ended_request_asks_for_scene_caption():
    c = _RecordingCaptioner()
    asyncio.run(_tools(c).describe_camera({"camera_id": "cam1"}))   # no question
    assert c.seen.get("task") == "scene_caption"


def test_describe_tool_advertises_question_param():
    defs = build_tool_definitions(["cam1"])
    describe = next(d for d in defs if d["function"]["name"] == "describe_camera")
    assert "question" in describe["function"]["parameters"]["properties"]
