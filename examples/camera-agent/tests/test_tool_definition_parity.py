# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The agent's read tools say exactly what the SDK says.

The model-facing text of the ten read tools — names, descriptions,
argument schemas — lives in ``opennvr_app_sdk.agent.tool_definitions``,
shared with core's workflow engine (RFC #620). The agent keeps its own
handlers. If the agent ever advertised its own variant of a definition,
a description fixed for one host would silently stay broken in the
other; this is the test that refuses that.
"""
from __future__ import annotations

import tools
from camera_agent import AppConfig, CameraAgentRuntime
from context import CameraSpec
from opennvr_app_sdk.agent import tool_definitions as sdk


def _runtime(**cfg):
    return CameraAgentRuntime(AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t",
        cameras=[CameraSpec("gate", "http://x/1.jpg", "gate"),
                 CameraSpec("yard", "http://x/2.jpg", "yard")], **cfg))


def test_the_agent_builds_from_the_sdk_definitions():
    assert tools.build_tool_definitions is sdk.build_tool_definitions
    assert tools.READ_TOOL_NAMES == sdk.READ_TOOL_NAMES


def test_every_read_tool_the_agent_advertises_is_the_sdk_text():
    rt = _runtime()
    shared = {d["function"]["name"]: d for d in sdk.build_tool_definitions(["gate", "yard"])}
    advertised = [d for d in rt.tool_definitions if d["function"]["name"] in shared]
    assert advertised, "the agent advertises none of the read tools"
    for d in advertised:
        assert d == shared[d["function"]["name"]], d["function"]["name"]


def test_every_read_tool_has_an_agent_handler():
    """Each host implements every shared tool, even ones a site's
    skills keep from being advertised."""
    rt = _runtime()
    missing = [n for n in sdk.READ_TOOL_NAMES if n not in rt.tool_handlers]
    assert missing == []


def test_enabled_tools_only_narrows_never_rewrites():
    rt = _runtime(enabled_tools=["describe_camera", "recent_events", "create_alarm"])
    shared = {d["function"]["name"]: d for d in sdk.build_tool_definitions(["gate", "yard"])}
    read = [d for d in rt.tool_definitions if d["function"]["name"] in shared]
    assert [d["function"]["name"] for d in read] == ["describe_camera", "recent_events"]
    assert all(d == shared[d["function"]["name"]] for d in read)
