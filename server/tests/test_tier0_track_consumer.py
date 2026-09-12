# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The Tier-0 → event-bus bridge behind the live detection overlay.

The overlay draws whatever this produces, so the contract is pinned:
boxes come out as normalized [x, y, w, h] regardless of the detector's
resolution, the camera handle is mapped to core's integer id (the
entitlement check is a set of ints), and junk is dropped rather than
raised.
"""
import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

from services import tier0_track_consumer as tc  # noqa: E402


# ─── normalize_box ──────────────────────────────────────────────────────


def test_pixel_box_is_normalized_by_frame_size():
    assert tc.normalize_box([192, 108, 960, 540], 1920, 1080) == [0.1, 0.1, 0.4, 0.4]


def test_already_normalized_box_is_not_divided_again():
    """A producer that normalized upstream must not be squashed into the
    top-left corner by a second division."""
    assert tc.normalize_box([0.1, 0.1, 0.5, 0.5], 1920, 1080) == [0.1, 0.1, 0.4, 0.4]


def test_box_is_clamped_to_the_frame():
    assert tc.normalize_box([-50, -50, 2000, 1200], 1920, 1080) == [0.0, 0.0, 1.0, 1.0]


def test_inverted_corners_are_sorted():
    assert tc.normalize_box([960, 540, 192, 108], 1920, 1080) == [0.1, 0.1, 0.4, 0.4]


@pytest.mark.parametrize("box", [None, "x", [1, 2, 3], [1, 2, 3, "a"], [float("nan")] * 4])
def test_junk_boxes_are_none(box):
    assert tc.normalize_box(box, 1920, 1080) is None


def test_zero_area_box_is_none():
    assert tc.normalize_box([10, 10, 10, 50], 1920, 1080) is None


def test_pixel_box_without_frame_size_is_none():
    """Cannot normalize pixels without knowing the frame; refusing beats
    drawing a box 1920 units wide."""
    assert tc.normalize_box([100, 100, 200, 200], None, None) is None
    assert tc.normalize_box([100, 100, 200, 200], 0, 0) is None


# ─── to_overlay_payload ─────────────────────────────────────────────────


def _raw(tracks, *, w=1920, h=1080, calibrating=False):
    return {"schema": "opennvr.tier0.v1", "camera_id": "cam3", "seq": 7,
            "wall_ts": 1.0, "frame": {"w": w, "h": h},
            "calibrating": calibrating, "tracks": tracks}


def test_payload_shape_is_the_overlay_contract():
    out = tc.to_overlay_payload(_raw([
        {"id": 5, "label": "person", "score": 0.91, "box": [192, 108, 960, 540],
         "stationary": False, "best": True},
    ]))
    assert out == {
        "schema": "opennvr.overlay.tracks.v1", "seq": 7, "wall_ts": 1.0,
        "calibrating": False, "frame": {"w": 1920, "h": 1080},
        "tracks": [{"id": 5, "label": "person", "score": 0.91,
                    "box": [0.1, 0.1, 0.4, 0.4], "stationary": False}],
    }


def test_low_score_tracks_are_filtered():
    out = tc.to_overlay_payload(_raw([
        {"id": 1, "label": "person", "score": 0.1, "box": [0, 0, 100, 100]},
        {"id": 2, "label": "car", "score": 0.9, "box": [0, 0, 100, 100]},
    ]))
    assert [t["id"] for t in out["tracks"]] == [2]


def test_nothing_drawable_returns_none():
    assert tc.to_overlay_payload(_raw([])) is None
    assert tc.to_overlay_payload(_raw([{"id": 1, "label": "x", "score": 0.9, "box": None}])) is None
    assert tc.to_overlay_payload(_raw([{"id": 1, "label": "x", "score": 0.01, "box": [0, 0, 9, 9]}])) is None


def test_non_dict_tracks_are_skipped_not_fatal():
    out = tc.to_overlay_payload(_raw([
        "junk", None, 42,
        {"id": 1, "label": "person", "score": 0.9, "box": [0, 0, 100, 100]},
    ]))
    assert len(out["tracks"]) == 1


def test_missing_label_becomes_object():
    out = tc.to_overlay_payload(_raw([{"id": 1, "score": 0.9, "box": [0, 0, 100, 100]}]))
    assert out["tracks"][0]["label"] == "object"


# ─── _handle_message: mapping + republish ───────────────────────────────


class _Bus:
    def __init__(self):
        self.published = []

    async def publish(self, event):
        self.published.append(event)


def _run(msg, monkeypatch):
    bus = _Bus()
    from services import event_bus_service
    monkeypatch.setattr(event_bus_service, "get_event_bus", lambda: bus)
    asyncio.run(tc._handle_message(msg))
    return bus.published


def test_camera_handle_is_mapped_to_the_integer_id(monkeypatch):
    """The entitlement check on the WebSocket is a set of ints; an event
    keyed "cam3" would be dropped for every scoped user."""
    msg = SimpleNamespace(
        subject="opennvr.inference.tier0.cam3.completed",
        data=json.dumps(_raw([{"id": 1, "label": "person", "score": 0.9,
                               "box": [0, 0, 100, 100]}])).encode())
    out = _run(msg, monkeypatch)
    assert len(out) == 1
    ev = out[0]
    assert ev["event_type"] == "tracks"
    assert ev["camera_id"] == 3 and isinstance(ev["camera_id"], int)
    assert ev["task"] == "tier0"
    assert ev["payload"]["tracks"][0]["box"] == [0.0, 0.0, 0.0521, 0.0926]


def test_camera_id_falls_back_to_the_subject(monkeypatch):
    raw = _raw([{"id": 1, "label": "person", "score": 0.9, "box": [0, 0, 100, 100]}])
    raw["camera_id"] = "front-door"   # a name, not a handle
    msg = SimpleNamespace(subject="opennvr.inference.tier0.cam7.completed",
                          data=json.dumps(raw).encode())
    out = _run(msg, monkeypatch)
    assert out and out[0]["camera_id"] == 7


def test_unmappable_camera_is_dropped_not_raised(monkeypatch):
    raw = _raw([{"id": 1, "label": "person", "score": 0.9, "box": [0, 0, 100, 100]}])
    raw["camera_id"] = "front-door"
    msg = SimpleNamespace(subject="weird", data=json.dumps(raw).encode())
    assert _run(msg, monkeypatch) == []


def test_empty_frames_publish_nothing(monkeypatch):
    """5 fps of empty results must not become 5 fps of empty events."""
    msg = SimpleNamespace(subject="opennvr.inference.tier0.cam3.completed",
                          data=json.dumps(_raw([])).encode())
    assert _run(msg, monkeypatch) == []


@pytest.mark.parametrize("data", [b"not json", b"[1,2,3]", b"null"])
def test_garbage_is_dropped_not_raised(monkeypatch, data):
    msg = SimpleNamespace(subject="opennvr.inference.tier0.cam3.completed", data=data)
    assert _run(msg, monkeypatch) == []


# ─── the bus itself gates tracks per camera ─────────────────────────────


def test_tracks_events_are_subject_to_camera_entitlement():
    """The WS fix from the S9S disclosure applies here too: a viewer
    scoped to camera 1 must not receive camera 3's boxes any more than
    its video."""
    from services.event_bus_service import _Subscriber
    sub = _Subscriber(queue_size=4, camera_id=None, tasks=None,
                      allowed_camera_ids=frozenset({1}))
    assert sub.matches({"event_type": "tracks", "camera_id": 1}) is True
    assert sub.matches({"event_type": "tracks", "camera_id": 3}) is False
