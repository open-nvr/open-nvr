# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Test-report fixes: forced-grounding routes attribute/activity questions to
the caption model (not the detector), and enabled_tools actually restricts ALL
advertised tools (not just the base set)."""
from __future__ import annotations

import pytest

import camera_agent as ca
from camera_agent import AppConfig, CameraAgentRuntime, build_app
from context import CameraSpec
from fastapi.testclient import TestClient


# ── forced-grounding tool routing (S-4 / L-3 / V-3) ─────────────────────


@pytest.mark.parametrize("q", [
    "what is the man doing",
    "what is he wearing",
    "describe the person in detail",
    "what's happening on the camera",
    "what does the scene look like",
    "what colour is his shirt",
])
def test_describe_questions_route_to_caption(q):
    assert ca._pick_forced_tool(q) == "describe_camera"


@pytest.mark.parametrize("q", [
    "how many people are there",
    "is anyone at the door",
    "count the cars",
    "any package on the porch",
])
def test_presence_count_questions_route_to_detector(q):
    assert ca._pick_forced_tool(q) == "detect_objects"


def test_open_what_do_you_see_defaults_to_caption():
    assert ca._pick_forced_tool("what do you see") == "describe_camera"


# ── enabled_tools restricts ALL advertised tools (#4) ───────────────────


def _runtime(enabled):
    cfg = AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t",
                    text_mode=True, enabled_tools=enabled,
                    cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="r")])
    return CameraAgentRuntime(cfg)


def test_enabled_tools_filters_control_tools_too():
    rt = _runtime(["detect_objects", "describe_camera", "create_alarm"])
    names = {t["function"]["name"] for t in rt.tool_definitions}
    assert names == {"detect_objects", "describe_camera", "create_alarm"}
    # control tools NOT in the allow-list must be gone (the old bug kept them)
    assert "create_monitor" not in names
    assert "create_background_task" not in names
    assert "enroll_face" not in names


def test_enabled_none_advertises_everything():
    rt = _runtime(None)
    names = {t["function"]["name"] for t in rt.tool_definitions}
    assert {"detect_objects", "describe_camera", "create_monitor",
            "create_alarm", "create_background_task"} <= names


def test_health_reports_advertised_tools_not_all_handlers():
    rt = _runtime(["detect_objects", "describe_camera"])
    health = TestClient(build_app(rt)).get("/health").json()
    assert sorted(health["tools"]) == ["describe_camera", "detect_objects"]


def test_recent_events_takes_all_as_every_camera():
    """Every other tool spells every camera 'all'; recent_events wanted
    '__any__' and refused 'all' — which Gemma chose three times of three
    for "what did the cameras see overnight?" (the NPU eval, 2026-11-08)."""
    import asyncio
    rt = _runtime(None)
    out = asyncio.run(rt.tools.recent_events({"camera_id": "all", "window_seconds": 600}))
    assert not out.startswith("ERROR"), out
    out_any = asyncio.run(rt.tools.recent_events({"camera_id": "__any__", "window_seconds": 600}))
    assert out == out_any

