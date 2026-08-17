# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""describe_event: run the VLM on a PAST event's stored evidence crop.

Closes the "what were they doing?" gap for history — search_history said WHO
and WHEN with a photo kept; describe_event turns that kept photo into a
description without a live look. Tier 2 of the historical-understanding ladder.
"""
from __future__ import annotations

import asyncio

from context import CameraContext, CameraSpec
from tools import CameraTools, build_tool_definitions


class _FakeEvents:
    def __init__(self, crops):
        self._crops = crops           # {event_id: jpeg bytes | None}
        self.asked = []
    async def evidence(self, event_id):
        self.asked.append(event_id)
        return self._crops.get(event_id)


class _VQAClient:
    def __init__(self):
        self.seen = {}
        self.frame = None
    async def infer(self, *, frame_jpeg, extra=None, correlation_id=None):
        self.seen = extra or {}
        self.frame = frame_jpeg
        q = (extra or {}).get("question")
        return {"result": {"answer": "carrying a box" if q else "",
                            "caption": "a person at a desk"}}


def _tools(events, caption):
    ctx = CameraContext(cameras=[CameraSpec(camera_id="cam1", frame_url="x", role="r")])
    return CameraTools(
        context=ctx, detection_client=None, caption_client=caption,
        recognition_client=None, footage_index=None, events_client=events,
    )


def test_describe_event_captions_the_stored_crop():
    ev = _FakeEvents({42: b"CROP-42"})
    cap = _VQAClient()
    out = asyncio.run(_tools(ev, cap).describe_event({"event_id": 42}))
    assert "a person at a desk" in out
    assert cap.frame == b"CROP-42"          # the VLM ran on the stored crop
    assert ev.asked == [42]


def test_describe_event_forwards_question_as_vqa():
    ev = _FakeEvents({7: b"CROP-7"})
    cap = _VQAClient()
    out = asyncio.run(_tools(ev, cap).describe_event(
        {"event_id": 7, "question": "what are they carrying?"}))
    assert "carrying a box" in out
    assert cap.seen.get("question") == "what are they carrying?"


def test_describe_event_no_photo_is_reported():
    ev = _FakeEvents({9: None})
    out = asyncio.run(_tools(ev, _VQAClient()).describe_event({"event_id": 9}))
    assert "don't have a stored photo" in out


def test_describe_event_needs_history_enabled():
    ctx = CameraContext(cameras=[CameraSpec(camera_id="cam1", frame_url="x", role="r")])
    tools = CameraTools(context=ctx, detection_client=None, caption_client=_VQAClient(),
                        recognition_client=None, footage_index=None)  # no events_client
    out = asyncio.run(tools.describe_event({"event_id": 1}))
    assert "History isn't enabled" in out


def test_describe_event_bad_id():
    ev = _FakeEvents({})
    out = asyncio.run(_tools(ev, _VQAClient()).describe_event({"event_id": "nope"}))
    assert "need the event's id" in out


def test_describe_event_tool_is_advertised():
    names = [d["function"]["name"] for d in build_tool_definitions(["cam1"])]
    assert "describe_event" in names
