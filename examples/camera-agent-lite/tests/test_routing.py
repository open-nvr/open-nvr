# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic router: vision vs command vs system vs text."""
import pytest

from routing import IntentRouter, parse_camera_reference, vision_score

KNOWN = ["camera_1", "camera_2", "camera_3"]


def make_router(known=None, default=None, min_confidence=0.6):
    return IntentRouter(
        known_ids_fn=lambda: KNOWN if known is None else known,
        default_camera_fn=lambda: default,
        min_confidence=min_confidence,
    )


# ---- camera reference parsing --------------------------------------------- #

@pytest.mark.parametrize("text,expected", [
    ("what is on camera two", "camera_2"),
    ("show me camera 3", "camera_3"),
    ("cam number 1 please", "camera_1"),
    ("camera #2", "camera_2"),
])
def test_parse_camera_reference(text, expected):
    assert parse_camera_reference(text, KNOWN).camera_id == expected


def test_parse_bare_camera():
    ref = parse_camera_reference("what does the camera see", KNOWN)
    assert ref.camera_id is None and ref.bare


def test_parse_unknown_number():
    ref = parse_camera_reference("camera nine", KNOWN)
    assert ref.camera_id is None and ref.number == 9


# ---- vision routing -------------------------------------------------------- #

async def test_visual_question_routes_to_vision():
    r = make_router()
    d = await r.route("what is happening on camera two right now?")
    assert d.route == "vision"
    assert d.camera_id == "camera_2"


async def test_motion_question_needs_multiple_frames():
    r = make_router()
    d = await r.route("is anyone walking toward the entrance on camera one?")
    assert d.route == "vision"
    assert d.requires_multiple_frames


async def test_bare_camera_resolves_to_default():
    r = make_router(default="camera_1")
    d = await r.route("is the room empty on the camera?")
    assert d.route == "vision"
    assert d.camera_id == "camera_1"


async def test_status_question_is_not_vision():
    r = make_router()
    d = await r.route("is camera one online?")
    assert d.route == "tool"
    assert d.tool_call.name == "get_camera_status"
    assert d.tool_call.args == {"camera_id": "camera_1"}


def test_vision_score_ignores_bare_camera_mention():
    assert vision_score("camera two") == 0.0
    assert vision_score("what do you see on camera two") > 0.5


# ---- command / system routing ---------------------------------------------- #

async def test_list_cameras_command():
    d = await make_router().route("list cameras")
    assert d.route == "tool"
    assert d.tool_call.name == "list_cameras"


async def test_time_routes_to_current_time_tool():
    d = await make_router().route("what time is it?")
    assert d.route == "tool"
    assert d.tool_call.name == "current_time"


async def test_system_status():
    d = await make_router().route("system status")
    assert d.route == "tool"
    assert d.tool_call.name == "system_status"


async def test_unknown_camera_number_asks_for_clarification():
    d = await make_router().route("what is happening on camera nine?")
    assert d.route == "clarification"
    assert "camera_9" in d.clarification


async def test_general_chat_routes_to_text():
    d = await make_router().route("explain how SIP registration works")
    assert d.route == "text"


async def test_empty_input_rejected():
    d = await make_router().route("   ")
    assert d.route == "reject"
