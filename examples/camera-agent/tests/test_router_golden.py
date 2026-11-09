# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The router's golden set: a synthetic dataset of user phrasings with
the decision each must get, run deterministically with no model.

Every row is one thing a person says to the agent and what the router
must do with it: Tier 0 with a given tool (and the arguments that
matter), a Tier-1 hint, Tier 2 (the model decides), or the router's own
"which camera?" question. The rows are grouped the way the live eval
cases are (tools/eval_cases.yml), so a phrasing that goes wrong on the
box gets a row here as well as there.

Grow this file from real misses. A row that fails is either a router
bug or a wrong expectation — decide which before changing either.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import router

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 9, 14, 30, 0, tzinfo=IST)
CAMS = ["cam1", "cam2", "cam3"]
ROLES = {"cam1": "the front door", "cam2": "the gate", "cam3": "the back yard"}
ALL = {"describe_camera", "detect_objects", "search_history", "recent_events", "describe_window",
       "create_monitor", "stop_monitor", "create_alarm", "stop_alarm",
       "create_report", "stop_report", "list_standing", "system_status", "show_camera",
       "list_apps", "app_status", "recent_app_alerts", "recent_plates", "camera_snapshot"}


def _decide(text, *, preferred=None, last_kind=None, advertised=ALL, cameras=CAMS, roles=ROLES):
    return router.decide(text, cameras=cameras, advertised=advertised, preferred=preferred,
                         roles=roles, now=NOW, last_kind=last_kind)


# (utterance, tier, tool-or-hint, args that must match)
GOLDEN = [
    # ── live presence / count → detect_objects ──
    ("is anyone at the front door", 0, "detect_objects", {"camera_id": "cam1"}),
    ("how many people are at the gate", 0, "detect_objects", {"camera_id": "cam2"}),
    ("any cars in the back yard", 0, "detect_objects", {"camera_id": "cam3"}),
    ("is there a dog on camera 3", 0, "detect_objects", {"camera_id": "cam3"}),
    ("is anybody there", 0, "detect_objects", {"camera_id": "all"}),
    ("is the gate clear", 0, "describe_camera", {"camera_id": "cam2"}),
    # ── live description → describe_camera ──
    ("what do you see at the gate", 0, "describe_camera", {"camera_id": "cam2"}),
    ("what is the person at the front door wearing", 0, "describe_camera", {"camera_id": "cam1"}),
    ("describe the back yard", 0, "describe_camera", {"camera_id": "cam3"}),
    ("what's happening at the gate", 0, "describe_camera", {"camera_id": "cam2"}),
    # ── history → search_history / recent_events ──
    ("did anyone come to the gate today", 0, "search_history", {}),
    ("was there a car at the front door this morning", 0, "search_history", {}),
    ("what happened in the last ten minutes", 0, "recent_events", {}),
    ("any visitors last night", 0, "search_history", {}),
    # ── standing requests: the model fills the slots, the router says which verb ──
    ("alarm me if someone is at the gate after 10pm", 1, "create_alarm", {}),
    ("set an alarm if a person is seen on the front door after 6pm", 1, "create_alarm", {}),
    ("notify me when a car arrives at the gate", 1, "create_monitor", {}),
    ("watch the back yard and tell me if more than 3 people show up", 1, "create_monitor", {}),
    ("every morning at 7 summarise overnight activity", 1, "create_report", {}),
    ("every hour tell me what you see", 1, "create_report", {}),
    # bare stop requests: the stop tools pick the item, so the router runs them
    ("stop the alarm", 0, "stop_alarm", {"action": "disarm"}),
    ("silence the alarm", 0, "stop_alarm", {"action": "silence"}),
    ("cancel the report", 0, "stop_report", {}),
    ("stop watching the gate", 0, "stop_monitor", {}),
    ("stop watching for that", 0, "stop_monitor", {}),
    ("cancel alarm 2", 0, "stop_alarm", {"alarm_id": 2}),
    # "remove it" right after an alarm was armed means that alarm
    ("can you remove it", 0, "stop_alarm", {"__last_kind": "alarm"}),
    ("remove the notification", 0, "stop_monitor", {"__last_kind": "monitor"}),
    ("cancel that", 0, "stop_report", {"__last_kind": "report"}),
    ("stop talking", 2, None, {}),
    # an absolute clock window with nothing named → the footage narration
    ("what happened at the gate between 2pm and 3pm", 0, "describe_window", {"camera_id": "cam2"}),
    # ── NEW: what is set up → list_standing ──
    ("what alarms do I have", 0, "list_standing", {"kind": "alarm"}),
    ("do I have any alarms", 0, "list_standing", {"kind": "alarm"}),
    ("which reports are scheduled", 0, "list_standing", {"kind": "report"}),
    ("what are you watching for", 0, "list_standing", {"kind": "all"}),
    ("what have I set up", 0, "list_standing", {"kind": "all"}),
    ("list my watches", 0, "list_standing", {"kind": "monitor"}),
    ("show me my alarms", 0, "list_standing", {"kind": "alarm"}),
    ("are there any notifications set up", 0, "list_standing", {"kind": "monitor"}),
    # ── NEW: system health → system_status ──
    ("is everything okay", 0, "system_status", {}),
    ("is any camera offline", 0, "system_status", {}),
    ("which cameras are online", 0, "system_status", {}),
    ("is the gate recording", 0, "system_status", {"camera_id": "cam2"}),
    ("are all the cameras recording", 0, "system_status", {}),
    ("how much disk space is left", 0, "system_status", {}),
    ("what's the system status", 0, "system_status", {}),
    ("is the front door camera working", 0, "system_status", {"camera_id": "cam1"}),
    ("are you working", 0, "system_status", {}),
    # ── NEW: show a camera → show_camera ──
    ("show me the gate", 0, "show_camera", {"camera_id": "cam2"}),
    ("open the front door camera", 0, "show_camera", {"camera_id": "cam1"}),
    ("switch to the back yard", 0, "show_camera", {"camera_id": "cam3"}),
    ("show camera 2", 0, "show_camera", {"camera_id": "cam2"}),
    ("pull up the gate feed please", 0, "show_camera", {"camera_id": "cam2"}),
    # "show me" plus a question is a look, not a view switch
    ("show me what's happening at the gate", 0, "describe_camera", {"camera_id": "cam2"}),
    ("show me who is at the gate", 2, None, {}),
    ("show me the history", 2, None, {}),
    # ── status-looking words that are about the scene stay on the scene ──
    ("is anyone down there at the gate", 0, "detect_objects", {"camera_id": "cam2"}),
    ("is the front door clear", 0, "describe_camera", {"camera_id": "cam1"}),
    # ── roster / config: no tool, answered from the roster ──
    ("how many cameras do you have", 2, None, {}),
    ("which cameras do you have", 2, None, {}),
    # ── small talk: no tool ──
    ("hello", 0, None, {}),
    ("thanks, that's all", 0, None, {}),
    ("good morning", 0, None, {}),
    ("bye, thank you", 0, None, {}),
    # ── other tools' questions: the model decides ──
    ("any plates this morning", 2, None, {}),
    ("what apps are running", 2, None, {}),
    ("who is at the gate", 2, None, {}),
    ("take a snapshot of the gate", 2, None, {}),
]


@pytest.mark.parametrize("text,tier,tool,args", GOLDEN, ids=[g[0] for g in GOLDEN])
def test_golden_router_decision(text, tier, tool, args):
    last_kind = args.pop("__last_kind", None) if args else None
    d = _decide(text, last_kind=last_kind)
    assert d.tier == tier, f"{text!r}: tier {d.tier} ({d.reason}), expected {tier}"
    if tier == 0:
        assert d.tool == tool, f"{text!r}: tool {d.tool} ({d.reason}), expected {tool}"
        for k, v in (args or {}).items():
            assert d.args.get(k) == v, f"{text!r}: {k}={d.args.get(k)!r}, expected {v!r}"
    elif tier == 1:
        assert d.hint == tool, f"{text!r}: hint {d.hint} ({d.reason}), expected {tool}"


def test_the_router_asks_which_camera_for_a_description_of_many():
    cams = [f"cam{i}" for i in range(1, 7)]
    roles = {c: f"room {i}" for i, c in enumerate(cams, 1)}
    d = _decide("what is the person wearing", cameras=cams, roles=roles)
    assert d.ask and d.ask.startswith("Which camera")


def test_the_new_routes_need_their_tools_advertised():
    """A site that keeps list_standing / system_status / show_camera out
    of enabled_tools gets yesterday's behaviour, not a dead route."""
    without = ALL - {"list_standing", "system_status", "show_camera"}
    assert _decide("what alarms do I have", advertised=without).tool is None
    assert _decide("is everything okay", advertised=without).tool is None
    d = _decide("show me the gate", advertised=without)
    assert d.tool == "describe_camera"
