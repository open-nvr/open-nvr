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


def test_coasting_tracks_are_not_drawn():
    """Reported: phantom boxes pile up on a moving scene while the real
    vehicle goes unboxed. The tracker coasts an unmatched track for up
    to five minutes at its last position; the overlay must draw only what
    was detected THIS frame."""
    out = tc.to_overlay_payload(_raw([
        {"id": 1, "label": "car", "score": 0.9, "box": [0, 0, 100, 100], "matched": True},
        {"id": 2, "label": "car", "score": 0.9, "box": [0, 0, 100, 100], "matched": False},
        {"id": 3, "label": "car", "score": 0.9, "box": [0, 0, 100, 100], "matched": False},
    ]))
    assert [t["id"] for t in out["tracks"]] == [1]


def test_a_frame_of_only_coasting_tracks_publishes_nothing():
    """A calibrating or detect-skipped frame returns every track unmatched.
    That must be 'nothing to draw', not 'draw last known positions'."""
    out = tc.to_overlay_payload(_raw([
        {"id": 1, "label": "car", "score": 0.9, "box": [0, 0, 100, 100], "matched": False},
    ]))
    assert out is None


def test_missing_matched_field_is_treated_as_matched():
    """Additive-only contract: a producer that predates `matched` keeps
    drawing. Going dark on an older Tier-0 would be a regression."""
    out = tc.to_overlay_payload(_raw([
        {"id": 1, "label": "car", "score": 0.9, "box": [0, 0, 100, 100]},
    ]))
    assert out and [t["id"] for t in out["tracks"]] == [1]


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


# ─── app overlay path + site switch ─────────────────────────────────────


def _app_msg(app="license-plate-recognition", cam="cam3", boxes=None, producer=None):
    env = {"schema": "overlay.boxes.v1", "camera_id": cam,
           "producer": producer if producer is not None else f"app:{app}",
           "ts": 1.0,
           "payload": {"boxes": boxes if boxes is not None else
                       [{"label": "plate", "box": [0.2, 0.3, 0.1, 0.05], "score": 0.9}]}}
    return SimpleNamespace(subject=f"opennvr.events.overlay.boxes.v1.{cam}",
                           data=json.dumps(env).encode())


def _run_app(msg, monkeypatch, *, allowed):
    bus = _Bus()
    from services import event_bus_service
    monkeypatch.setattr(event_bus_service, "get_event_bus", lambda: bus)
    monkeypatch.setattr(tc, "_app_may_draw", lambda app_id, now=None: allowed)
    asyncio.run(tc._handle_app_message(msg))
    return bus.published


def test_app_boxes_are_forwarded_when_the_operator_allowed_it(monkeypatch):
    out = _run_app(_app_msg(), monkeypatch, allowed=True)
    assert len(out) == 1
    ev = out[0]
    assert ev["event_type"] == "tracks" and ev["task"] == "overlay"
    assert ev["camera_id"] == 3
    assert ev["payload"]["source"] == "app:license-plate-recognition"
    assert ev["payload"]["tracks"][0]["box"] == [0.2, 0.3, 0.1, 0.05]
    assert ev["payload"]["tracks"][0]["label"] == "plate"


def test_app_boxes_are_dropped_when_not_allowed(monkeypatch):
    """The whole point of the per-app switch: publishing is free, DRAWING
    is a privilege the operator grants."""
    assert _run_app(_app_msg(), monkeypatch, allowed=False) == []


def test_app_pixel_boxes_use_the_shipped_frame_size(monkeypatch):
    env = json.loads(_app_msg().data)
    env["payload"] = {"frame": {"w": 1000, "h": 500},
                      "boxes": [{"label": "zone", "box": [100, 50, 300, 150]}]}
    msg = SimpleNamespace(subject="opennvr.events.overlay.boxes.v1.cam3",
                          data=json.dumps(env).encode())
    out = _run_app(msg, monkeypatch, allowed=True)
    # [x=100, y=50, w=300, h=150] of a 1000×500 frame — xywh, per contract.
    assert out[0]["payload"]["tracks"][0]["box"] == [0.1, 0.1, 0.3, 0.3]


def test_xywh_to_xyxy():
    assert tc._xywh_to_xyxy([0.2, 0.3, 0.1, 0.05]) == [0.2, 0.3, pytest.approx(0.3), pytest.approx(0.35)]
    assert tc._xywh_to_xyxy([1, 2, 3]) is None
    assert tc._xywh_to_xyxy(None) is None


def test_app_box_without_score_is_drawn():
    """An app's zone has no confidence; the score filter must not eat it."""
    raw = {"frame": {}, "tracks": [{"label": "zone", "box": [0, 0, 0.5, 0.5]}]}
    out = tc.to_overlay_payload(raw, min_score=0.0)
    assert out and out["tracks"][0]["score"] == 0.0


def test_producer_prefix_is_stripped_for_the_lookup(monkeypatch):
    seen = []
    def _may(app_id, now=None):
        seen.append(app_id); return True
    bus = _Bus()
    from services import event_bus_service
    monkeypatch.setattr(event_bus_service, "get_event_bus", lambda: bus)
    monkeypatch.setattr(tc, "_app_may_draw", _may)
    asyncio.run(tc._handle_app_message(_app_msg(producer="app:smart-doorbell")))
    assert seen == ["smart-doorbell"]


def test_app_allow_cache_reads_db_once_per_ttl(monkeypatch):
    """Overlay frames arrive many times a second; the DB must not."""
    calls = []
    class _Q:
        def __init__(self, v): self.v = v
        def filter(self, *a, **k): return self
        def first(self): calls.append(1); return self.v
    class _DB:
        def query(self, *a): return _Q((True, True))
        def close(self): pass
    import types
    fake_core = types.SimpleNamespace(SessionLocal=lambda: _DB())
    monkeypatch.setitem(sys.modules, "core.database", fake_core)
    monkeypatch.setitem(sys.modules, "models",
                        types.SimpleNamespace(InstalledApp=types.SimpleNamespace(
                            overlay_enabled="o", enabled="e", id="i")))
    tc._invalidate_app_allow_cache()
    assert tc._app_may_draw("x", now=100.0) is True
    assert tc._app_may_draw("x", now=105.0) is True    # cached
    assert tc._app_may_draw("x", now=100.0 + tc._APP_ALLOW_TTL_S + 1) is True  # refreshed
    assert len(calls) == 2


def test_site_switch_off_disables_the_bridge(monkeypatch):
    """DETECTION_OVERLAY_ENABLED=false must return before touching NATS.
    core.config.settings is a full pydantic Settings() that needs every
    site secret to construct, so the consumer's lazy `from core.config
    import settings` is served a stand-in here."""
    import types
    fake = types.SimpleNamespace(
        settings=types.SimpleNamespace(detection_overlay_enabled=False,
                                       nats_url="nats://would-connect",
                                       internal_api_key="k"))
    monkeypatch.setitem(sys.modules, "core.config", fake)
    # A nats import would prove the gate was skipped; make it explode.
    monkeypatch.setitem(sys.modules, "nats", None)
    asyncio.run(tc.run_consumer_loop())   # returns; no ImportError raised


def test_site_switch_on_reaches_the_nats_import(monkeypatch):
    """The inverse: with the switch on and a URL set, the loop proceeds to
    `import nats` — which we make fail so the loop returns cleanly. Pins
    that the gate is the switch, not something else short-circuiting."""
    import types
    fake = types.SimpleNamespace(
        settings=types.SimpleNamespace(detection_overlay_enabled=True,
                                       nats_url="nats://would-connect",
                                       internal_api_key="k"))
    monkeypatch.setitem(sys.modules, "core.config", fake)
    monkeypatch.setitem(sys.modules, "nats", None)   # import → ImportError → return
    asyncio.run(tc.run_consumer_loop())
