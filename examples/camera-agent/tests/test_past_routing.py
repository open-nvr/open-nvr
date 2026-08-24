# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Past-tense questions must reach the HISTORY tools, not a live look.

Field bug: "did you see a person today?" was answered with the current
frame ("a potted plant") because (a) the shipped docker configs' system
prompts routed EVERY camera question to detect_objects/describe_camera,
(b) search_history wasn't in enabled_tools at all, and (c) forced
grounding (_pick_forced_tool) only knew the two live tools — "person"
matched the detector wordlist and the past tense was ignored.

Covers: _is_past_question, _window_from_text, _pick_forced_call, the
per-turn clock line + advertised-only tool guidance in
build_system_prompt, and a drift guard — every tool a shipped config's
system_prompt names must be present in that config's enabled_tools.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

import camera_agent as ca
from camera_agent import AppConfig, CameraAgentRuntime
from context import CameraSpec
from tools import build_tool_definitions

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 8, 24, 9, 30, 0, tzinfo=IST)


# ── _is_past_question ───────────────────────────────────────────────────

@pytest.mark.parametrize("q", [
    "did you see a person today?",
    "No, I'm asking that are you able to see a person today?",
    "can you check the recording if you see a person in the last 30 minutes?",
    "did anyone come to the door in the last 30 minutes?",
    "was there a dog here yesterday?",
    "have you seen anybody this morning?",
    "did a red truck come by earlier?",
    "which cars entered today?",
])
def test_past_questions_detected(q):
    assert ca._is_past_question(q)


@pytest.mark.parametrize("q", [
    "is anyone at the door?",
    "what do you see?",
    "how many people are there?",
    "what is the man wearing?",
    "alarm me if someone is at the door after 10pm",
    "watch the driveway and tell me if more than 3 cars show up",
])
def test_present_questions_not_past(q):
    assert not ca._is_past_question(q)


# ── _window_from_text ───────────────────────────────────────────────────

def test_window_last_30_minutes():
    start, secs = ca._window_from_text("in the last 30 minutes", now=NOW)
    assert secs == 1800
    assert start == (NOW - timedelta(minutes=30)).isoformat(timespec="seconds")
    assert "+05:30" in start          # tz offset present, as search_history needs


def test_window_last_2_hours():
    start, secs = ca._window_from_text("over the past 2 hours", now=NOW)
    assert secs == 7200
    assert start == (NOW - timedelta(hours=2)).isoformat(timespec="seconds")


def test_window_today_starts_at_midnight():
    start, secs = ca._window_from_text("did you see a person today", now=NOW)
    assert start == NOW.replace(hour=0, minute=0, second=0,
                                microsecond=0).isoformat(timespec="seconds")
    assert secs == int(9.5 * 3600)


def test_window_yesterday_starts_previous_midnight():
    start, _ = ca._window_from_text("was a dog here yesterday", now=NOW)
    assert start == (NOW.replace(hour=0, minute=0, second=0, microsecond=0)
                     - timedelta(days=1)).isoformat(timespec="seconds")


def test_window_unparseable_is_open_with_hour_fallback():
    start, secs = ca._window_from_text("did anyone come by", now=NOW)
    assert start is None
    assert secs == 3600


# ── _pick_forced_call ───────────────────────────────────────────────────

ALL = {"describe_camera", "detect_objects", "search_history", "recent_events",
       "search_footage"}


def test_past_person_question_forces_search_history():
    name, args = ca._pick_forced_call(
        "did you see a person today?", "cam4", ALL, now=NOW)
    assert name == "search_history"
    assert args["camera_id"] == "cam4"
    assert args["label"] == "person"
    assert args["start_time"].startswith("2026-08-24T00:00:00")


def test_past_recording_question_forces_search_history():
    name, args = ca._pick_forced_call(
        "can you check the recording if you see a person in the last 30 minutes?",
        "cam4", ALL, now=NOW)
    assert name == "search_history"
    assert args["label"] == "person"
    assert args["start_time"] == (NOW - timedelta(minutes=30)).isoformat(
        timespec="seconds")


def test_past_vehicle_label_mapped():
    name, args = ca._pick_forced_call(
        "did a truck come by yesterday?", "cam1", ALL, now=NOW)
    assert name == "search_history"
    assert args["label"] == "truck"


def test_past_without_search_history_uses_recent_events():
    advertised = {"describe_camera", "detect_objects", "recent_events"}
    name, args = ca._pick_forced_call(
        "did anyone come in the last 30 minutes?", "cam1", advertised, now=NOW)
    assert name == "recent_events"
    assert args == {"camera_id": "cam1", "window_seconds": 1800}


def test_past_with_no_history_tools_falls_back_to_live():
    advertised = {"describe_camera", "detect_objects"}
    name, args = ca._pick_forced_call(
        "did you see a person today?", "cam1", advertised, now=NOW)
    assert name == "detect_objects"       # honest fallback, never a crash
    assert args == {"camera_id": "cam1"}


def test_present_question_keeps_live_routing():
    name, args = ca._pick_forced_call("is anyone at the door?", "cam1", ALL)
    assert name == "detect_objects"
    assert args == {"camera_id": "cam1"}
    name, _ = ca._pick_forced_call("what is he wearing?", "cam1", ALL)
    assert name == "describe_camera"


def test_forced_tool_never_outside_advertised():
    # A present-tense count question when detect_objects is hidden must not
    # pick a tool the operator's enabled_tools excluded.
    name, _ = ca._pick_forced_call(
        "how many people are there?", "cam1", {"describe_camera"})
    assert name == "describe_camera"


# ── build_system_prompt: clock + advertised-only guidance ───────────────

def _runtime(enabled):
    cfg = AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t",
                    text_mode=True, enabled_tools=enabled,
                    cameras=[CameraSpec(camera_id="cam1",
                                        frame_url="http://x/1.jpg", role="r")])
    return CameraAgentRuntime(cfg)


def test_prompt_carries_current_clock_with_offset():
    prompt = _runtime(None).build_system_prompt()
    now = datetime.now().astimezone()
    assert now.strftime("%Y-%m-%d") in prompt
    assert "UTC" in prompt
    assert "ISO 8601" in prompt


def test_background_task_guidance_only_when_advertised():
    with_task = _runtime(None).build_system_prompt()
    assert "create_background_task" in with_task
    without = _runtime(["detect_objects", "describe_camera",
                        "search_history"]).build_system_prompt()
    assert "create_background_task" not in without


def test_monitor_alarm_report_guidance_only_when_advertised():
    slim = _runtime(["detect_objects", "describe_camera"]).build_system_prompt()
    for name in ("create_monitor", "create_alarm", "create_report"):
        assert name not in slim
    full = _runtime(None).build_system_prompt()
    for name in ("create_monitor", "create_alarm", "create_report"):
        assert name in full


# ── drift guard: shipped configs' prompts vs their enabled_tools ────────

_CONFIG_DIR = Path(__file__).resolve().parents[1]
_SHIPPED = ["config.docker.yml", "config.docker.chat.yml"]

_CONTROL_TOOLS = {
    "create_monitor", "stop_monitor", "create_alarm", "stop_alarm",
    "create_report", "stop_report", "create_background_task",
    "enroll_face", "list_people", "forget_face",
    "list_apps", "app_status", "recent_app_alerts",
}


def _all_tool_names() -> set[str]:
    base = {t["function"]["name"] for t in build_tool_definitions(["cam1"])}
    return base | _CONTROL_TOOLS


@pytest.mark.parametrize("fname", _SHIPPED)
def test_config_prompt_names_only_enabled_tools(fname):
    raw = yaml.safe_load((_CONFIG_DIR / fname).read_text())
    enabled = set(raw["enabled_tools"])
    prompt = raw["system_prompt"]
    mentioned = {n for n in _all_tool_names()
                 if re.search(rf"\b{n}\b", prompt)}
    assert mentioned, f"{fname}: prompt names no tools at all?"
    missing = mentioned - enabled
    assert not missing, (
        f"{fname}: system_prompt tells the model to call {sorted(missing)} "
        f"but enabled_tools does not advertise them — a small model will "
        f"stall or substitute a live-scene tool (the potted-plant bug)."
    )


@pytest.mark.parametrize("fname", _SHIPPED)
def test_shipped_configs_advertise_history_and_route_past(fname):
    raw = yaml.safe_load((_CONFIG_DIR / fname).read_text())
    enabled = set(raw["enabled_tools"])
    assert {"search_history", "recent_events"} <= enabled
    assert "search_history" in raw["system_prompt"]
    assert re.search(r"past", raw["system_prompt"], re.IGNORECASE)
