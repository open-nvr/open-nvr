# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""Tier-0 consumption helpers — snapshot parsing + the best-frame client."""
from __future__ import annotations

import pytest

from opennvr_app_sdk import (
    BestFrameClient,
    Tier0Snapshot,
    describe_counts,
    is_tier0_subject,
    make_best_frame_fetch,
    snapshot_from_event,
)

_EVENT = {
    "schema": "opennvr.tier0.v1",
    "camera_id": "front",
    "ts": 123.0,
    "seq": 9,
    "tracks": [
        {"id": 1, "label": "person", "best": True},
        {"id": 2, "label": "car", "best": False},
        {"id": 3, "label": "car", "best": True},
    ],
}


def test_snapshot_counts_presence_and_best():
    s = snapshot_from_event(_EVENT)
    assert s.camera_id == "front" and s.ts == 123.0 and s.total == 3
    assert s.counts == {"person": 1, "car": 2}
    assert s.count("car") == 2 and s.present("person") and not s.present("dog")
    assert s.has_best(1) is True and s.has_best(2) is False
    assert [t["id"] for t in s.tracks_with_best()] == [1, 3]


def test_snapshot_is_defensive_on_junk():
    s = snapshot_from_event({})
    assert s.tracks == [] and s.counts == {} and s.total == 0
    assert s.describe() == ""
    assert isinstance(snapshot_from_event(None), Tier0Snapshot)  # type: ignore[arg-type]
    # non-dict track entries are dropped, not crashed on
    s2 = snapshot_from_event({"tracks": ["junk", 3, {"label": "person"}]})
    assert s2.total == 1 and s2.counts == {"person": 1}


def test_describe_counts_skips_empty_label():
    assert describe_counts({"": 2, "car": 1}) == "a car"     # empty label ignored


def test_describe_counts_phrasing():
    assert describe_counts({"person": 1}) == "a person"
    assert describe_counts({"person": 2, "car": 1}) == "a car, 2 people"  # sorted by label
    assert describe_counts({"apple": 1}) == "an apple"      # vowel article
    assert describe_counts({}) == ""


def test_is_tier0_subject():
    assert is_tier0_subject("opennvr.inference.tier0.front.completed")
    assert not is_tier0_subject("opennvr.inference.yolov8.front.completed")


@pytest.mark.asyncio
async def test_best_frame_client_maps_camera_and_status():
    seen = {}

    async def http_get(url):
        seen["url"] = url
        return (200, b"IMG") if "camera=7" in url else (404, b"")

    client = BestFrameClient("http://tier0:9109/", resolve_camera=lambda c: "7",
                             http_get=http_get)
    assert await client.fetch("front", track_id=5) == b"IMG"
    assert seen["url"] == "http://tier0:9109/best_frame?camera=7&track=5"
    # no track id -> camera-latest URL
    await client.fetch("front")
    assert seen["url"] == "http://tier0:9109/best_frame?camera=7"


@pytest.mark.asyncio
async def test_best_frame_client_returns_none_on_miss_and_blank_camera():
    async def http_get(url):
        return (404, b"")

    client = BestFrameClient("http://t:9109", http_get=http_get)
    assert await client.fetch("front") is None
    # a resolver that yields empty -> no fetch, None
    blank = BestFrameClient("http://t:9109", resolve_camera=lambda c: "", http_get=http_get)
    assert await blank.fetch("front") is None


@pytest.mark.asyncio
async def test_make_best_frame_fetch_binds_a_simple_callable():
    async def http_get(url):
        return (200, b"J")

    fetch = make_best_frame_fetch("http://t:9109", http_get=http_get)
    assert await fetch("cam") == b"J"


# ── tier0_to_detections bridge ─────────────────────────────────────

def _tier0_event(**over):
    ev = {
        "schema": "opennvr.tier0.v1",
        "adapter": "tier0",
        "camera_id": "front",
        "frame": {"w": 1920, "h": 1080},
        "tracks": [
            {"id": 7, "label": "person", "score": 0.91,
             "box": [192, 108, 384, 540], "stationary": False, "best": True},
        ],
    }
    ev.update(over)
    return ev


def test_bridge_maps_track_to_contract_detection():
    from opennvr_app_sdk import tier0_to_detections

    dets = tier0_to_detections(_tier0_event())
    assert len(dets) == 1
    d = dets[0]
    assert d["label"] == "person"
    assert d["track_id"] == 7
    assert d["stationary"] is False and d["best"] is True
    bbox = d["bbox"]
    assert abs(bbox["x"] - 0.1) < 1e-6
    assert abs(bbox["y"] - 0.1) < 1e-6
    assert abs(bbox["w"] - 0.1) < 1e-6
    assert abs(bbox["h"] - 0.4) < 1e-6


def test_bridge_without_frame_dims_omits_bbox_keeps_label():
    from opennvr_app_sdk import tier0_to_detections

    dets = tier0_to_detections(_tier0_event(frame={}))
    assert len(dets) == 1
    assert "bbox" not in dets[0]
    assert dets[0]["label"] == "person"


def test_bridge_skips_malformed_tracks():
    from opennvr_app_sdk import tier0_to_detections

    ev = _tier0_event(tracks=[{"no": "label"}, "junk",
                              {"label": "car", "box": [0, 0, 960, 540]}])
    dets = tier0_to_detections(ev)
    assert [d["label"] for d in dets] == ["car"]
