# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""opennvr_app_sdk.agent.tool_definitions: the shared model-facing text."""
from __future__ import annotations

import json

import pytest
from opennvr_app_sdk.agent import READ_TOOL_NAMES, build_tool_definitions

#: Names that move things. None may ever be a tool an AI step can call
#: (RFC #620: "AI informs; people and plain rules move things").
ACTUATORS = {"open_gate", "unlock", "set_site_mode", "siren", "open_now",
             "create_alarm", "stop_alarm", "create_monitor", "stop_monitor"}


def _names(defs):
    return [d["function"]["name"] for d in defs]


def test_the_ten_read_tools_in_order():
    assert _names(build_tool_definitions(["gate", "yard"])) == list(READ_TOOL_NAMES)
    assert len(READ_TOOL_NAMES) == 10 == len(set(READ_TOOL_NAMES))


def test_nothing_that_moves_things_is_a_read_tool():
    assert not set(READ_TOOL_NAMES) & ACTUATORS


def test_every_definition_is_complete_function_calling_shape():
    for d in build_tool_definitions(["gate"]):
        assert d["type"] == "function"
        f = d["function"]
        assert f["description"].strip(), f["name"]
        params = f["parameters"]
        assert params["type"] == "object" and isinstance(params["properties"], dict)
        assert set(params.get("required", [])) <= set(params["properties"]), f["name"]


def test_the_site_cameras_are_baked_into_the_enums():
    """So a model cannot invent a camera the site does not have."""
    text = json.dumps(build_tool_definitions(["gate", "yard"]))
    assert '"gate"' in text and '"yard"' in text
    assert "__no_cameras_configured__" not in text
    empty = json.dumps(build_tool_definitions([]))
    assert "__no_cameras_configured__" in empty


@pytest.mark.parametrize("enabled", [["describe_camera", "recent_events"], []])
def test_enabled_narrows_and_keeps_the_order(enabled):
    assert _names(build_tool_definitions(["gate"], enabled=enabled)) == [
        n for n in READ_TOOL_NAMES if n in enabled]


def test_the_same_site_gets_the_same_text():
    """Hosts compare against this; it must be a pure function of its input."""
    assert build_tool_definitions(["gate"]) == build_tool_definitions(["gate"])
