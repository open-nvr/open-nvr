# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""list_standing, system_status and show_camera: the tools about the
agent and the system rather than the scene."""
from __future__ import annotations

import asyncio

import pytest

from camera_agent import ALWAYS_ON_TOOLS, SKILL_TOOLS, AppConfig, CameraAgentRuntime
from context import CameraSpec


def _runtime(**cfg):
    return CameraAgentRuntime(AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t",
        cameras=[CameraSpec("cam1", "http://x/1.jpg", "the front door", opennvr_camera_id=1),
                 CameraSpec("cam2", "http://x/2.jpg", "the gate", opennvr_camera_id=2)], **cfg))


def _names(rt):
    return {d["function"]["name"] for d in rt.tool_definitions}


def test_list_standing_and_show_camera_are_always_on_and_system_status_needs_core():
    rt = _runtime()
    assert set(ALWAYS_ON_TOOLS) <= _names(rt)
    assert "system_status" not in _names(rt), "no opennvr_api_url, nothing to read"
    assert rt.skill_requirement_met("status") is False
    rt2 = _runtime(opennvr_api_url="http://core")
    assert "system_status" in _names(rt2)
    assert rt2.skill_requirement_met("status") is True
    assert SKILL_TOOLS["status"] == ["system_status"]


def test_the_viewer_tier_may_use_all_three():
    rt = _runtime(opennvr_api_url="http://core")
    viewer = {d["function"]["name"] for d in rt.viewer_tool_definitions}
    assert {"list_standing", "system_status", "show_camera"} <= viewer
    assert "create_alarm" not in viewer


def test_list_standing_reads_back_what_is_set_up():
    rt = _runtime()
    empty = asyncio.run(rt._handle_list_standing({}))
    assert "Nothing is set up" in empty
    asyncio.run(rt._handle_create_alarm({"name": "gate person", "target": "person",
                                         "camera_id": "cam2", "after": "22:00"}))
    asyncio.run(rt._handle_create_monitor({"kind": "count", "target": "person",
                                           "camera_id": "cam1", "max_count": 3}))
    asyncio.run(rt._handle_create_report({"name": "morning", "query": "summarise overnight",
                                          "at": "07:00"}))
    out = asyncio.run(rt._handle_list_standing({}))
    assert "1 alarm" in out and "the gate" in out and "person" in out
    assert "1 watch" in out and "the front door" in out
    assert "1 scheduled report" in out and "summarise overnight" in out
    only = asyncio.run(rt._handle_list_standing({"kind": "alarm"}))
    assert "alarm" in only and "report" not in only
    none = asyncio.run(rt._handle_list_standing({"kind": "report"}))
    assert "morning" in none
    for i in [a["id"] for a in rt.reports.list()]:
        rt.reports.remove(i)
    assert asyncio.run(rt._handle_list_standing({"kind": "report"})) == "No reports are scheduled."


class _Status:
    def __init__(self, data):
        self.data = data

    async def status(self):
        return self.data


_FINE = {
    "cameras": [
        {"camera_id": "cam1", "open_nvr_camera_id": "1", "name": "Front", "online": True,
         "recording_enabled": True, "recording_state": "recording"},
        {"camera_id": "cam2", "open_nvr_camera_id": "2", "name": "Gate", "online": True,
         "recording_enabled": True, "recording_state": "recording"},
    ],
    "storage": {"free_gb": 120.0, "total_gb": 500.0, "min_free_gb": 10.0},
}


def test_system_status_when_everything_is_fine():
    rt = _runtime(opennvr_api_url="http://core")
    rt.status_client = _Status(_FINE)
    out = asyncio.run(rt._handle_system_status({}))
    assert out.startswith("All 2 cameras are online.")
    assert "Recording is fine on all of them." in out
    assert "120 GB free of 500 GB (floor 10 GB)" in out


def test_system_status_names_the_offline_and_stalled_cameras_by_role():
    rt = _runtime(opennvr_api_url="http://core")
    rt.status_client = _Status({
        "cameras": [
            {"camera_id": "cam1", "open_nvr_camera_id": "1", "name": "Front", "online": True,
             "recording_enabled": True, "recording_state": "recording"},
            {"camera_id": "cam23", "open_nvr_camera_id": "2", "name": "Gate", "online": False,
             "recording_enabled": True, "recording_state": "stalled"},
        ],
        "storage": {"free_gb": 2.3, "total_gb": 60.0, "min_free_gb": 2.0},
    })
    out = asyncio.run(rt._handle_system_status({}))
    assert "1 of 2 cameras online. Offline: the gate." in out, out
    assert "Recording problems: the gate recording has stalled." in out
    assert "close to the 2 GB floor" in out
    one = asyncio.run(rt._handle_system_status({"camera_id": "cam1"}))
    assert one.startswith("The front door is online.")
    assert "the gate" not in one


def test_system_status_below_the_floor_says_recordings_are_being_deleted():
    rt = _runtime(opennvr_api_url="http://core")
    data = dict(_FINE); data["storage"] = {"free_gb": 1.5, "total_gb": 60.0, "min_free_gb": 2.0}
    rt.status_client = _Status(data)
    out = asyncio.run(rt._handle_system_status({}))
    assert "below the 2 GB floor" in out and "oldest recordings are being deleted" in out


def test_system_status_when_core_is_unreachable_or_unconfigured():
    rt = _runtime(opennvr_api_url="http://core")
    rt.status_client = _Status(None)
    assert "couldn't reach" in asyncio.run(rt._handle_system_status({}))
    rt0 = _runtime()
    assert "isn't configured" in asyncio.run(rt0._handle_system_status({}))


def test_show_camera_pushes_the_view_to_the_page():
    rt = _runtime()
    q: asyncio.Queue = asyncio.Queue()
    rt._update_subscribers.add(q)
    out = asyncio.run(rt._handle_show_camera({"camera_id": "cam2"}))
    assert out.startswith("Switched the screen to the gate.")
    assert q.get_nowait() == {"show_camera": "cam2"}
    ask = asyncio.run(rt._handle_show_camera({"camera_id": "all"}))
    assert ask.startswith("Which camera should I show?")
    assert q.empty()
