# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The demo's detection overlay is fed from core's /events/ws, not NATS.

Core decides what is drawable; the agent relays and re-scopes. These
pin the relay: the parser accepts exactly core's `tracks` shape, the
runtime maps core's integer camera id to the agent's camera by
opennvr_camera_id, and a viewer never receives a camera outside their
scope.
"""
from __future__ import annotations

import asyncio

from camera_agent import AppConfig, CameraAgentRuntime
from context import CameraSpec, core_tracks_frame, set_camera_scope


# ─── parser ─────────────────────────────────────────────────────────────


def _ev(**over):
    base = {"event_type": "tracks", "camera_id": 3, "task": "tier0",
            "payload": {"calibrating": False,
                        "tracks": [{"id": 5, "label": "person", "score": 0.9,
                                    "box": [0.1, 0.1, 0.4, 0.4]}]}}
    base.update(over)
    return base


def test_parses_a_core_tracks_event():
    cam, frame = core_tracks_frame(_ev())
    assert cam == 3
    assert frame["calibrating"] is False
    assert frame["tracks"][0]["box"] == [0.1, 0.1, 0.4, 0.4]
    assert "source" not in frame


def test_app_boxes_carry_their_source():
    e = _ev(task="overlay")
    e["payload"]["source"] = "app:license-plate-recognition"
    _, frame = core_tracks_frame(e)
    assert frame["source"] == "app:license-plate-recognition"


def test_other_event_types_are_ignored():
    assert core_tracks_frame(_ev(event_type="inference_result")) is None
    assert core_tracks_frame(_ev(event_type="camera_status")) is None
    assert core_tracks_frame({"event_type": "subscribed", "filters": {}}) is None


def test_camera_id_must_be_an_integer():
    assert core_tracks_frame(_ev(camera_id="cam3")) is None
    assert core_tracks_frame(_ev(camera_id=None)) is None
    assert core_tracks_frame(_ev(camera_id=True)) is None


def test_empty_or_malformed_tracks_are_dropped():
    e = _ev(); e["payload"]["tracks"] = []
    assert core_tracks_frame(e) is None
    e = _ev(); e["payload"]["tracks"] = ["junk", None]
    assert core_tracks_frame(e) is None
    assert core_tracks_frame("not a dict") is None


def test_the_agent_does_not_re_normalize():
    """Boxes come out exactly as core sent them — the maths lives in one
    place now. A pixel-looking box is passed through untouched too;
    core would never send one, and the agent must not guess."""
    e = _ev(); e["payload"]["tracks"][0]["box"] = [192, 108, 960, 540]
    _, frame = core_tracks_frame(e)
    assert frame["tracks"][0]["box"] == [192, 108, 960, 540]


# ─── relay + scope ──────────────────────────────────────────────────────


def _runtime():
    cfg = AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t",
        cameras=[
            CameraSpec(camera_id="front-door", frame_url="http://x/1.jpg", role="f",
                       opennvr_camera_id=3),
            CameraSpec(camera_id="garage", frame_url="http://x/2.jpg", role="g",
                       opennvr_camera_id=7),
        ],
    )
    return CameraAgentRuntime(cfg)


def test_relay_maps_core_id_to_the_agent_camera():
    rt = _runtime()
    q = rt.subscribe_updates()
    rt._relay_tracks(3, {"calibrating": False, "tracks": [{"label": "person", "box": [0, 0, 1, 1]}]})
    pushed = q.get_nowait()
    assert pushed["tracks"]["camera"] == "front-door"
    assert pushed["tracks"]["tracks"][0]["label"] == "person"


def test_relay_drops_cameras_this_agent_does_not_know():
    rt = _runtime()
    q = rt.subscribe_updates()
    rt._relay_tracks(99, {"calibrating": False, "tracks": [{"label": "x", "box": [0, 0, 1, 1]}]})
    assert q.empty()


def test_updates_socket_filters_tracks_by_the_viewers_scope():
    """The re-scoping the service ticket relies on. The /updates handler
    drops a tracks push whose camera is outside the session's scope; this
    exercises the same predicate the handler uses."""
    from camera_agent import _tracks_push_visible

    set_camera_scope({"front-door"})
    try:
        assert _tracks_push_visible({"tracks": {"camera": "front-door"}}) is True
        assert _tracks_push_visible({"tracks": {"camera": "garage"}}) is False
        assert _tracks_push_visible({"working": "x"}) is True        # not a tracks push
    finally:
        set_camera_scope(None)
    assert _tracks_push_visible({"tracks": {"camera": "garage"}}) is True   # unscoped session
