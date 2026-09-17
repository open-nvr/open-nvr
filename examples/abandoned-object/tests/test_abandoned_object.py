# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Focused tests for the abandoned-object predicate."""
from __future__ import annotations

import abandoned_object as ao
from zone import Zone


def _camera() -> ao.CameraZone:
    zone = Zone.from_config("hall", [[0, 0], [1920, 0], [1920, 1080], [0, 1080]])
    return ao.CameraZone(camera_id="cam-1", zone=zone, frame_width=1920, frame_height=1080)


def _config(camera, **over) -> ao.AppConfig:
    base = dict(
        nats_url="nats://x:4222", nats_token=None,
        subject_pattern="opennvr.inference.>",
        object_labels=["backpack"], person_label="person",
        dwell_seconds=30.0, move_tolerance_px=40.0,
        person_radius_px=250.0, owner_grace_seconds=10.0,
        track_ttl_seconds=120.0, cameras={camera.camera_id: camera},
        webhook_url=None,
    )
    base.update(over)
    return ao.AppConfig(**base)


class _NullDispatcher:
    def fire(self, alert):  # noqa: ANN001
        return {}


def _bbox(cx, cy, w=0.05, h=0.05):
    # center (cx,cy) in normalized coords → x,y origin
    return {"x": cx - w / 2, "y": cy - h / 2, "w": w, "h": h}


def _event(dets, ts):
    return {
        "camera_id": "cam-1", "correlation_id": "c1",
        "completed_at": ts,
        "result": {"detections": dets},
    }


def _ts(sec):
    # seconds → ISO timestamp on a fixed day
    mm, ss = divmod(sec, 60)
    hh, mm = divmod(mm, 60)
    return f"2026-01-01T{hh:02d}:{mm:02d}:{ss:02d}Z"


def _det(label, cx, cy, track_id):
    return {"label": label, "track_id": track_id, "bbox": _bbox(cx, cy)}


def _detector(camera, **cfg):
    return ao.AbandonedObjectDetector(_config(camera, **cfg), _NullDispatcher())


def test_stationary_unattended_object_fires_after_dwell():
    d = _detector(_camera(), dwell_seconds=30.0)
    bag = lambda t: _event([_det("backpack", 0.5, 0.5, "b1")], _ts(t))
    assert d.handle_event(bag(0)) == []         # first sighting
    assert d.handle_event(bag(20)) == []        # still under dwell
    fired = d.handle_event(bag(31))             # past 30s, no person near
    assert len(fired) == 1
    assert fired[0].evidence["label"] == "backpack"


def test_person_nearby_suppresses_alert():
    d = _detector(_camera(), dwell_seconds=30.0, person_radius_px=300.0)
    # Bag + a person standing right next to it the whole time.
    def ev(t):
        return _event(
            [_det("backpack", 0.5, 0.5, "b1"), _det("person", 0.51, 0.51, "p1")],
            _ts(t),
        )
    d.handle_event(ev(0))
    assert d.handle_event(ev(35)) == []         # owner present → suppressed


def test_owner_leaves_then_object_is_abandoned():
    d = _detector(_camera(), dwell_seconds=30.0, owner_grace_seconds=10.0,
                  person_radius_px=300.0)
    # Person with the bag early, then person gone.
    d.handle_event(_event(
        [_det("backpack", 0.5, 0.5, "b1"), _det("person", 0.51, 0.51, "p1")], _ts(0)))
    # 31s later, bag still there, no person for >10s grace → fire.
    fired = d.handle_event(_event([_det("backpack", 0.5, 0.5, "b1")], _ts(31)))
    assert len(fired) == 1


def test_moving_object_resets_and_does_not_fire():
    d = _detector(_camera(), dwell_seconds=30.0, move_tolerance_px=40.0)
    d.handle_event(_event([_det("backpack", 0.2, 0.2, "b1")], _ts(0)))
    # Object carried to a very different spot at t=31 → anchor resets.
    fired = d.handle_event(_event([_det("backpack", 0.8, 0.8, "b1")], _ts(31)))
    assert fired == []


def test_fires_once_not_repeatedly():
    d = _detector(_camera(), dwell_seconds=30.0)
    d.handle_event(_event([_det("backpack", 0.5, 0.5, "b1")], _ts(0)))
    assert len(d.handle_event(_event([_det("backpack", 0.5, 0.5, "b1")], _ts(31)))) == 1
    assert d.handle_event(_event([_det("backpack", 0.5, 0.5, "b1")], _ts(40))) == []


def test_untracked_objects_ignored():
    d = _detector(_camera())
    det = {"label": "backpack", "bbox": _bbox(0.5, 0.5)}  # no track_id
    assert d.handle_event(_event([det], _ts(0))) == []


# ── Connected: cameras picked in the catalog ───────────────────────


def test_a_picked_camera_needs_no_yaml_entry_and_an_unpicked_one_is_ignored():
    d = _detector(_camera())
    d.cfg.cameras = {}
    d._config_poll_thread = object()
    d.picked_cameras = frozenset({3})
    assert d._camera_for("cam3") is not None          # whole frame until drawn
    assert d.handle_event(_event([_det("backpack", 0.5, 0.5, "b1")], _ts(0))) == []
    d.on_config_update({"zones": {"3": [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2]]}})
    zone = d._camera_for("cam3").zone
    assert not zone.contains(ao.Point(960, 540))       # the drawn zone replaced the frame
