# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Skills panel honesty at TOOL level.

Field bug: the "Look back at recent events" card showed ON while
search_history — the tool the user's past-tense question needed — was
hidden by enabled_tools (the panel's enabled check only looked at the
skill's PRIMARY tool). And the card's caption said "uses inference event
bus (NATS)" although search_history reads the durable events store, not
the bus. Every skill entry now lists each bundled tool with whether the
LLM can actually call it, flags partially-advertised skills, captions
the events skill by what is ACTUALLY wired, and the events skill is
usable with EITHER backend (NATS or the events store) — not gated on
NATS alone.
"""
from __future__ import annotations

from pathlib import Path

from camera_agent import SKILL_TOOLS, AppConfig, CameraAgentRuntime
from context import CameraSpec


def _runtime(enabled=None, nats=None, api_url=None):
    cfg = AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t",
                    text_mode=True, enabled_tools=enabled,
                    nats_inference_url=nats, opennvr_api_url=api_url,
                    cameras=[CameraSpec(camera_id="cam1",
                                        frame_url="http://x/1.jpg", role="r")])
    return CameraAgentRuntime(cfg)


def _entry(rt, sid):
    return next(e for e in rt.skills_payload() if e["id"] == sid)


# ── per-tool advertised states ──────────────────────────────────────────

def test_every_skill_entry_lists_all_its_tools():
    rt = _runtime(None, nats="nats://n")
    for sid in SKILL_TOOLS:
        entry = _entry(rt, sid)
        assert [t["name"] for t in entry["tools"]] == SKILL_TOOLS[sid]
        if sid in ("footage", "apps"):
            # Honest even here: these backends aren't wired in this runtime
            # (no footage index / app registry), so their tools are NOT
            # advertised and the chips must say so.
            assert not any(t["advertised"] for t in entry["tools"])
        else:
            # enabled_tools=None advertises everything else → no grey chips.
            assert all(t["advertised"] for t in entry["tools"])
            assert entry["partial"] is False


def test_hidden_tool_reported_and_partial_flagged():
    # The exact field configuration: recent_events advertised,
    # search_history & friends hidden by enabled_tools.
    rt = _runtime(["detect_objects", "describe_camera", "recent_events"],
                  nats="nats://n")
    entry = _entry(rt, "events")
    states = {t["name"]: t["advertised"] for t in entry["tools"]}
    assert states["recent_events"] is True
    assert states["search_history"] is False
    assert states["describe_event"] is False
    assert entry["enabled"] is True       # primary advertised + backend wired
    assert entry["partial"] is True       # …but the card must not imply all work


def test_fully_advertised_events_skill_not_partial():
    rt = _runtime(["recent_events", "search_history", "describe_event",
                   "describe_window"], nats="nats://n")
    entry = _entry(rt, "events")
    assert entry["partial"] is False
    assert all(t["advertised"] for t in entry["tools"])


# ── events skill: two backends, caption + gate reflect actual wiring ────

def test_events_caption_nats_only():
    entry = _entry(_runtime(None, nats="nats://n"), "events")
    assert entry["uses"] == "inference event bus (NATS)"


def test_events_caption_both_backends():
    entry = _entry(_runtime(None, nats="nats://n", api_url="http://srv"),
                   "events")
    assert "inference event bus (NATS)" in entry["uses"]
    assert "events store" in entry["uses"]


def test_events_caption_store_only():
    entry = _entry(_runtime(None, api_url="http://srv"), "events")
    assert entry["uses"] == "events store (visit history)"


def test_events_skill_usable_with_store_only():
    # No NATS, but the durable events store is wired: search_history works,
    # so the skill must be available/enabled — the old NATS-only gate
    # wrongly greyed the whole skill out here.
    rt = _runtime(None, api_url="http://srv")
    assert rt.skill_requirement_met("events") is True
    entry = _entry(rt, "events")
    assert entry["available"] is True and entry["enabled"] is True


def test_events_skill_unavailable_with_neither_backend():
    rt = _runtime(None)
    assert rt.skill_requirement_met("events") is False
    entry = _entry(rt, "events")
    assert entry["available"] is False
    assert "nats_inference_url" in entry["hint"]
    assert "opennvr_api_url" in entry["hint"]


# ── demo UI renders the per-tool chips ──────────────────────────────────

def test_demo_renders_tool_chips():
    html = (Path(__file__).resolve().parents[1] / "demo" / "index.html"
            ).read_text()
    assert "sk-tools" in html          # chips container
    assert "sk-tool" in html
    assert "s.tools" in html           # renderer consumes the payload field
    assert "t.advertised" in html      # greys out hidden tools
    assert "Hidden from the model" in html
