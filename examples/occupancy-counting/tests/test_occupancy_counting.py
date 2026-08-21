# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Focused tests for the occupancy-counting predicate.

We drive ``OccupancyCounter.handle_event`` directly with synthetic
inference events — no NATS, no adapters — and assert the edge-triggered
state machine fires on the right transitions and not on every frame.
"""
from __future__ import annotations

import occupancy_counting as oc
from zone import Zone


def _camera(max_occ=2, min_occ=None) -> oc.CameraZone:
    # A full-frame zone so any centered bbox counts.
    zone = Zone.from_config("room", [[0, 0], [1920, 0], [1920, 1080], [0, 1080]])
    return oc.CameraZone(
        camera_id="cam-1", zone=zone,
        frame_width=1920, frame_height=1080,
        max_occupancy=max_occ, min_occupancy=min_occ,
    )


def _config(camera: oc.CameraZone, *, debounce=1, clear=False) -> oc.AppConfig:
    return oc.AppConfig(
        nats_url="nats://x:4222", nats_token=None,
        subject_pattern="opennvr.inference.>",
        watch_labels=["person"], debounce_frames=debounce,
        clear_alerts=clear, cameras={camera.camera_id: camera},
        webhook_url=None,
    )


class _NullDispatcher:
    """Swallows fired alerts — handle_event also returns them, which is
    what we assert on."""

    def fire(self, alert):  # noqa: ANN001
        return {}


def _event(n_people: int, *, camera_id="cam-1") -> dict:
    # n people, each a centered bbox (0.4,0.4 origin, 0.1 size → center
    # ~0.45 of frame, comfortably inside a full-frame zone).
    dets = [
        {"label": "person", "bbox": {"x": 0.4, "y": 0.4, "w": 0.1, "h": 0.1}}
        for _ in range(n_people)
    ]
    return {
        "camera_id": camera_id,
        "correlation_id": "corr-1",
        "result": {"detections": dets},
    }


def _counter(camera, **cfg_kw):
    return oc.OccupancyCounter(_config(camera, **cfg_kw), _NullDispatcher())


def test_counts_people_in_zone():
    c = _counter(_camera())
    cam = c._config.cameras["cam-1"]
    assert c.count_in_zone(cam, _event(3)["result"]["detections"]) == 3


def test_fires_over_when_count_exceeds_max():
    c = _counter(_camera(max_occ=2))
    assert c.handle_event(_event(2)) == []      # at limit → normal
    fired = c.handle_event(_event(3))           # over the limit
    assert len(fired) == 1
    assert fired[0].evidence["level"] == "over"
    assert fired[0].evidence["count"] == 3


def test_over_fires_once_not_every_frame():
    c = _counter(_camera(max_occ=2))
    assert len(c.handle_event(_event(5))) == 1   # transition normal→over
    assert c.handle_event(_event(6)) == []       # still over → silent
    assert c.handle_event(_event(7)) == []


def test_cleared_alert_only_when_enabled():
    # Without clear_alerts, returning to normal is silent.
    c = _counter(_camera(max_occ=2), clear=False)
    c.handle_event(_event(5))                    # → over
    assert c.handle_event(_event(1)) == []       # back to normal, silent

    # With clear_alerts, returning to normal fires a low-severity alert.
    c2 = _counter(_camera(max_occ=2), clear=True)
    c2.handle_event(_event(5))                   # → over
    cleared = c2.handle_event(_event(1))         # back to normal
    assert len(cleared) == 1
    assert cleared[0].severity == "low"


def test_under_occupancy():
    c = _counter(_camera(max_occ=5, min_occ=1))
    assert c.handle_event(_event(2)) == []       # in band
    fired = c.handle_event(_event(0))            # below minimum
    assert len(fired) == 1
    assert fired[0].evidence["level"] == "under"


def test_debounce_requires_persistence():
    c = _counter(_camera(max_occ=2), debounce=2)
    assert c.handle_event(_event(9)) == []       # 1st over frame → pending
    fired = c.handle_event(_event(9))            # 2nd over frame → commit
    assert len(fired) == 1


def test_unknown_camera_ignored():
    c = _counter(_camera())
    assert c.handle_event(_event(9, camera_id="cam-unknown")) == []


# ── Tier-0 consumption (the always-on detector) ────────────────────
#
# A default OpenNVR install runs detect-pipeline and NO per-frame adapter
# loop, so Tier-0 events are the ONLY detections on the bus. These tests
# drive the app with the real Tier-0 payload shape (detect_pipeline's
# bus.build_payload) rather than adapter-shaped events.


def _tier0_event(n_people: int, *, camera_id="cam-1", w=1920, h=1080) -> dict:
    """A Tier-0 event as detect-pipeline publishes it: top-level ``tracks``
    with PIXEL boxes and a ``frame`` size — no ``result`` block at all."""
    return {
        "schema": "opennvr.tier0.v1",
        "adapter": "tier0",
        "camera_id": camera_id,
        "seq": 41,
        "ts": 1234.5,                 # monotonic — never a date
        "wall_ts": 1_755_700_000.0,   # epoch seconds
        "frame": {"w": w, "h": h},
        "calibrating": False,
        "tracks": [
            {"id": i + 1, "label": "person", "score": 0.9,
             # centred box: 0.4-0.5 of the frame in both axes
             "box": [int(w * 0.4), int(h * 0.4), int(w * 0.5), int(h * 0.5)],
             "stationary": False, "best": True}
            for i in range(n_people)
        ],
    }


def test_tier0_events_are_counted_and_fire():
    """The regression this whole slice exists for: on a stock install the
    app saw Tier-0 events and silently dropped every one."""
    counter = _counter(_camera(max_occ=2))
    assert counter.consume_tier0 is True, "app default must consume Tier-0"
    assert counter.handle_event(_tier0_event(2)) == []      # at the ceiling
    alerts = counter.handle_event(_tier0_event(3))          # over it
    assert len(alerts) == 1
    assert alerts[0].evidence["level"] == "over"
    assert alerts[0].evidence["count"] == 3


def test_tier0_pixel_boxes_map_into_the_zone():
    """Tracks carry PIXEL boxes; the SDK bridge normalises them and the app
    scales back into zone space. A track outside the zone must not count."""
    zone = Zone.from_config("left-half", [[0, 0], [960, 0], [960, 1080], [0, 1080]])
    camera = oc.CameraZone(camera_id="cam-1", zone=zone, frame_width=1920,
                           frame_height=1080, max_occupancy=0, min_occupancy=None)
    counter = _counter(camera)
    # One person centred at 45% width → inside the left half → over max(0).
    fired = counter.handle_event(_tier0_event(1))
    assert len(fired) == 1 and fired[0].evidence["count"] == 1
    # Same person on the right half → not counted, so no alert and the
    # state machine returns to normal.
    right = _tier0_event(1)
    right["tracks"][0]["box"] = [1500, 400, 1700, 600]
    assert counter.handle_event(right) == []
    assert counter._states["cam-1"].last_count == 0


def test_tier0_can_be_turned_off():
    """Operators who also drive a heavy adapter can opt out and keep the
    single (adapter) stream — no double counting."""
    camera = _camera(max_occ=0)
    cfg = _config(camera)
    cfg.consume_tier0 = False
    counter = oc.OccupancyCounter(cfg, _NullDispatcher())
    assert counter.handle_event(_tier0_event(3)) == []


def test_unknown_camera_id_warns_once(caplog):
    """``cam-1`` vs ``cam1`` used to be a silent no-op forever."""
    counter = _counter(_camera())
    with caplog.at_level("WARNING"):
        counter.handle_event(_tier0_event(1, camera_id="cam1"))
        counter.handle_event(_tier0_event(1, camera_id="cam1"))
    warnings = [r for r in caplog.records if "not in my config" in r.message]
    assert len(warnings) == 1, "warn once per unknown camera, not per frame"


# ── Camera auto-derivation ─────────────────────────────────────────
#
# Hand-copying camera ids is how this app ends up counting nothing:
# OpenNVR names cameras ``cam1``, hand-written configs say ``cam-1``.
# With no cameras listed, the app asks OpenNVR instead of refusing.


def _write_config(tmp_path, body: str) -> str:
    p = tmp_path / "config.yml"
    p.write_text(
        'nats_url: "nats://x:4222"\n'
        "max_occupancy: 5\n" + body
    )
    return str(p)


def test_cameras_are_derived_from_opennvr_when_unlisted(tmp_path, monkeypatch):
    import opennvr_app_sdk.cameras as sdk_cameras

    monkeypatch.setattr(
        oc, "discover_cameras",
        lambda url, api_key=None: [{"camera_id": "cam1"}, {"camera_id": "cam2"}],
    )
    cfg = oc.load_config(_write_config(tmp_path, 'opennvr_url: "http://core:8000"\ncameras: []\n'))
    assert sorted(cfg.cameras) == ["cam1", "cam2"]
    cam = cfg.cameras["cam1"]
    # Whole-frame zone in the SDK's unit space — correct at any real
    # resolution because detector boxes are normalised before comparison.
    assert cam.frame_width == sdk_cameras.UNIT_FRAME
    assert cam.max_occupancy == 5                      # app-level default applies
    counter = oc.OccupancyCounter(cfg, _NullDispatcher())
    fired = counter.handle_event(_tier0_event(6, camera_id="cam1"))
    assert len(fired) == 1 and fired[0].evidence["count"] == 6


def test_explicit_cameras_still_win(tmp_path, monkeypatch):
    called = {"n": 0}

    def _never(*a, **k):
        called["n"] += 1
        return [{"camera_id": "cam9"}]

    monkeypatch.setattr(oc, "discover_cameras", _never)
    cfg = oc.load_config(_write_config(tmp_path, (
        "cameras:\n"
        '  - camera_id: "cam1"\n'
        "    frame_width: 1920\n"
        "    frame_height: 1080\n"
        "    zone: [[0, 0], [1920, 0], [1920, 1080], [0, 1080]]\n"
    )))
    assert list(cfg.cameras) == ["cam1"]
    assert called["n"] == 0, "must not call OpenNVR when cameras are pinned"


def test_no_cameras_and_no_discovery_is_a_clear_error(tmp_path, monkeypatch):
    import pytest

    monkeypatch.setattr(oc, "discover_cameras", lambda url, api_key=None: [])
    with pytest.raises(ValueError, match="discovered"):
        oc.load_config(_write_config(tmp_path, "cameras: []\n"))
