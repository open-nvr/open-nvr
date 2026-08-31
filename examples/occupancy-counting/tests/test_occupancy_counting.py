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


def test_no_cameras_and_nowhere_to_ask_is_a_clear_error(tmp_path, monkeypatch):
    """No list AND no opennvr_url is a genuine misconfiguration."""
    import pytest

    monkeypatch.setattr(oc, "discover_cameras", lambda url, api_key=None: [])
    with pytest.raises(ValueError, match="opennvr_url"):
        oc.load_config(_write_config(tmp_path, "cameras: []\n"))


def test_core_reachable_but_no_cameras_yet_boots_empty(tmp_path, monkeypatch):
    """A fresh install where the app was enabled before any camera exists
    must boot and wait — exiting here would crash-loop the container
    forever over something the operator fixes in the UI a minute later."""
    monkeypatch.setattr(oc, "discover_cameras", lambda url, api_key=None: [])
    cfg = oc.load_config(_write_config(
        tmp_path, 'opennvr_url: "http://core:8000"\ncameras: []\n'))
    assert cfg.cameras == {} and cfg.cameras_auto_derived is True


def test_cameras_added_after_boot_are_picked_up(tmp_path, monkeypatch):
    """A set captured once at boot would silently never watch a camera
    added tomorrow."""
    live = [{"camera_id": "cam1"}]
    monkeypatch.setattr(oc, "discover_cameras", lambda url, api_key=None: list(live))
    cfg = oc.load_config(_write_config(
        tmp_path, 'opennvr_url: "http://core:8000"\ncameras: []\n'))
    counter = oc.OccupancyCounter(cfg, _NullDispatcher())
    assert sorted(cfg.cameras) == ["cam1"]

    live.append({"camera_id": "cam2"})                 # operator adds a camera
    added, removed = counter.refresh_cameras()
    assert added == ["cam2"] and removed == []
    # ...and it is counted immediately, with the app-level threshold.
    fired = counter.handle_event(_tier0_event(6, camera_id="cam2"))
    assert len(fired) == 1 and fired[0].evidence["count"] == 6

    live.remove({"camera_id": "cam1"})                 # ...and removes one
    added, removed = counter.refresh_cameras()
    assert added == [] and removed == ["cam1"]
    assert "cam1" not in cfg.cameras


def test_refresh_ignores_an_empty_discovery_blip(tmp_path, monkeypatch):
    """Core answering with nothing mid-poll must not delete every camera."""
    live = [{"camera_id": "cam1"}]
    monkeypatch.setattr(oc, "discover_cameras", lambda url, api_key=None: list(live))
    cfg = oc.load_config(_write_config(
        tmp_path, 'opennvr_url: "http://core:8000"\ncameras: []\n'))
    counter = oc.OccupancyCounter(cfg, _NullDispatcher())
    live.clear()
    assert counter.refresh_cameras() == ([], [])
    assert sorted(cfg.cameras) == ["cam1"], "a blip must not stop the app watching"


def test_pinned_cameras_are_never_refreshed(tmp_path, monkeypatch):
    called = {"n": 0}

    def _count(*a, **k):
        called["n"] += 1
        return [{"camera_id": "cam9"}]

    monkeypatch.setattr(oc, "discover_cameras", _count)
    cfg = oc.load_config(_write_config(tmp_path, (
        "cameras:\n"
        '  - camera_id: "cam1"\n'
        "    frame_width: 1920\n"
        "    frame_height: 1080\n"
        "    zone: [[0, 0], [1920, 0], [1920, 1080], [0, 1080]]\n"
    )))
    counter = oc.OccupancyCounter(cfg, _NullDispatcher())
    assert counter.refresh_cameras() == ([], [])
    assert called["n"] == 0 and list(cfg.cameras) == ["cam1"]


def test_discovered_cameras_require_an_app_level_ceiling(tmp_path, monkeypatch):
    """A camera the refresh loop adds later has no per-camera threshold to
    fall back on — without an app-level default it would silently get 0 and
    alert on the first person past. Missing max_occupancy must fail the
    same way it does when a camera exists at boot."""
    import pytest

    monkeypatch.setattr(oc, "discover_cameras", lambda url, api_key=None: [])
    p = tmp_path / "config.yml"
    p.write_text('nats_url: "nats://x:4222"\nopennvr_url: "http://core:8000"\ncameras: []\n')
    with pytest.raises(ValueError, match="max_occupancy"):
        oc.load_config(str(p))


# ── Review-nit fixes ───────────────────────────────────────────────
#
# Three small holes found on a post-merge review pass: refresh must
# present the same credentials boot did; refresh's dict mutation must be
# runnable on the event loop with a pre-fetched list (no cross-thread
# writes); and a missing ceiling with DISCOVERED cameras must not blame
# a YAML "camera entry 0" that does not exist.


def test_refresh_uses_the_same_api_key_boot_did(tmp_path, monkeypatch):
    """Boot honoured a YAML ``internal_api_key``; a refresh that falls
    back to the env var would discover cameras once and never again."""
    seen_keys = []

    def _spy(url, api_key=None):
        seen_keys.append(api_key)
        return [{"camera_id": "cam1"}]

    monkeypatch.setattr(oc, "discover_cameras", _spy)
    cfg = oc.load_config(_write_config(
        tmp_path,
        'opennvr_url: "http://core:8000"\n'
        'internal_api_key: "yaml-secret"\n'
        "cameras: []\n",
    ))
    counter = oc.OccupancyCounter(cfg, _NullDispatcher())
    counter.refresh_cameras()
    assert seen_keys == ["yaml-secret", "yaml-secret"], (
        "boot and refresh must present the same credentials"
    )


def test_refresh_accepts_a_prefetched_camera_list(tmp_path, monkeypatch):
    """``refresh_cameras(discovered=...)`` must apply the set without a
    network call — the discovery loop fetches in a worker thread and
    mutates on the event loop, so the mutation path alone must never
    touch the network."""
    calls = {"n": 0}

    def _boot_only(url, api_key=None):
        calls["n"] += 1
        return [{"camera_id": "cam1"}]

    monkeypatch.setattr(oc, "discover_cameras", _boot_only)
    cfg = oc.load_config(_write_config(
        tmp_path, 'opennvr_url: "http://core:8000"\ncameras: []\n'))
    counter = oc.OccupancyCounter(cfg, _NullDispatcher())
    assert calls["n"] == 1                              # boot discovery only

    added, removed = counter.refresh_cameras(
        discovered=[{"camera_id": "cam1"}, {"camera_id": "cam2"}]
    )
    assert added == ["cam2"] and removed == []
    assert calls["n"] == 1, "a supplied list must not trigger a fetch"
    # Blip immunity holds on the pre-fetched path too.
    assert counter.refresh_cameras(discovered=[]) == ([], [])
    assert sorted(cfg.cameras) == ["cam1", "cam2"]


def test_missing_ceiling_with_discovered_cameras_names_the_real_cause(
    tmp_path, monkeypatch
):
    """With cameras FOUND by discovery and no app-level max_occupancy,
    the error must describe the discovery case — not "camera entry 0
    malformed", an entry that does not exist in the operator's YAML."""
    import pytest

    monkeypatch.setattr(
        oc, "discover_cameras", lambda url, api_key=None: [{"camera_id": "cam1"}]
    )
    p = tmp_path / "config.yml"
    p.write_text(
        'nats_url: "nats://x:4222"\nopennvr_url: "http://core:8000"\ncameras: []\n'
    )
    with pytest.raises(ValueError, match="discovered from OpenNVR"):
        oc.load_config(str(p))
    with pytest.raises(ValueError) as excinfo:
        oc.load_config(str(p))
    assert "camera entry 0" not in str(excinfo.value)


# ── Per-camera assignment scoping (slice 2) ────────────────────────
#
# "Cameras 2-3 count people" on the settings page must scope this app to
# exactly those cameras — additively: nothing assigned anywhere means
# watch everything, exactly as before assignments existed.


def _discovered(*specs):
    out = []
    for spec in specs:
        cam_id, *skills = spec.split(":")
        cam = {"camera_id": cam_id}
        if skills and skills[0]:
            cam["assignments"] = [{"skill": s} for s in skills[0].split("+")]
        out.append(cam)
    return out


def test_boot_scopes_to_assigned_cameras(tmp_path, monkeypatch):
    """cam1 is for LPR, cams 2-3 for occupancy → watch exactly 2 and 3."""
    monkeypatch.setattr(oc, "discover_cameras", lambda url, api_key=None: _discovered(
        "cam1:license_plate_recognition",
        "cam2:occupancy_counting",
        "cam3:occupancy_counting+object_detection",
        "cam4:",
    ))
    cfg = oc.load_config(_write_config(
        tmp_path, 'opennvr_url: "http://core:8000"\ncameras: []\n'))
    assert sorted(cfg.cameras) == ["cam2", "cam3"]
    counter = oc.OccupancyCounter(cfg, _NullDispatcher())
    # The unassigned camera's events are "not my camera", not counted.
    assert counter.handle_event(_tier0_event(6, camera_id="cam4")) == []
    fired = counter.handle_event(_tier0_event(6, camera_id="cam2"))
    assert len(fired) == 1


def test_no_assignments_anywhere_means_watch_everything(tmp_path, monkeypatch):
    """Back-compat: assignments that exist for OTHER skills only do not
    restrict this app either way — restriction starts with OUR skill."""
    monkeypatch.setattr(oc, "discover_cameras", lambda url, api_key=None: _discovered(
        "cam1:", "cam2:",
    ))
    cfg = oc.load_config(_write_config(
        tmp_path, 'opennvr_url: "http://core:8000"\ncameras: []\n'))
    assert sorted(cfg.cameras) == ["cam1", "cam2"]


def test_refresh_follows_assignment_changes(tmp_path, monkeypatch):
    """Assigning / un-assigning on the settings page takes effect within
    one refresh — including un-assigning the LAST camera, which lifts the
    restriction entirely (back to watch-everything, by the rule)."""
    live = _discovered("cam1:", "cam2:")
    monkeypatch.setattr(oc, "discover_cameras", lambda url, api_key=None: [dict(c) for c in live])
    cfg = oc.load_config(_write_config(
        tmp_path, 'opennvr_url: "http://core:8000"\ncameras: []\n'))
    counter = oc.OccupancyCounter(cfg, _NullDispatcher())
    assert sorted(cfg.cameras) == ["cam1", "cam2"]

    # Operator assigns occupancy to cam2 only → scope narrows.
    live[:] = _discovered("cam1:", "cam2:occupancy_counting")
    added, removed = counter.refresh_cameras()
    assert removed == ["cam1"] and added == []
    assert sorted(cfg.cameras) == ["cam2"]

    # Operator removes the assignment again → restriction lifts.
    live[:] = _discovered("cam1:", "cam2:")
    added, removed = counter.refresh_cameras()
    assert added == ["cam1"] and removed == []
    assert sorted(cfg.cameras) == ["cam1", "cam2"]


def test_explicit_camera_list_ignores_assignments(tmp_path, monkeypatch):
    """A pinned YAML camera list is the operator's word — assignments
    never second-guess it (and no discovery call is made at all)."""
    called = {"n": 0}

    def _count(*a, **k):
        called["n"] += 1
        return _discovered("cam9:occupancy_counting")

    monkeypatch.setattr(oc, "discover_cameras", _count)
    cfg = oc.load_config(_write_config(tmp_path, (
        "cameras:\n"
        '  - camera_id: "cam1"\n'
        "    frame_width: 1920\n"
        "    frame_height: 1080\n"
        "    zone: [[0, 0], [1920, 0], [1920, 1080], [0, 1080]]\n"
    )))
    assert list(cfg.cameras) == ["cam1"] and called["n"] == 0


# ── occupancy.changed.v1 history feed ───────────────────────────────


def test_history_publishes_on_count_change_and_transition(monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(oc.time, "monotonic", lambda: t["now"])
    app = _counter(_camera())
    app.handle_event(_event(1))
    calls = app._occupancy_publisher.calls
    assert len(calls) == 1
    assert calls[0]["schema"] == "occupancy.changed.v1"
    assert calls[0]["camera_id"] == "cam-1"
    assert calls[0]["payload"] == {
        "count": 1, "level": "normal", "max_occupancy": 2,
        "min_occupancy": None}
    # Same count again → nothing (change-driven, not per-frame).
    app.handle_event(_event(1))
    assert len(calls) == 1
    # A committed level transition publishes even inside the interval.
    t["now"] += 1.0
    app.handle_event(_event(3))  # over max_occ=2, debounce=1 → commit
    levels = [c["payload"]["level"] for c in calls]
    assert "over" in levels
    assert calls[-1]["payload"]["count"] == 3


def test_history_interval_suppresses_chatter(monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(oc.time, "monotonic", lambda: t["now"])
    app = _counter(_camera())
    app.handle_event(_event(1))
    t["now"] += 2.0
    app.handle_event(_event(2))   # count changed but < 10s → suppressed…
    calls = app._occupancy_publisher.calls
    normal_only = [c for c in calls if c["payload"]["level"] == "normal"]
    assert len(normal_only) == 1
    t["now"] += 10.0
    app.handle_event(_event(2))   # …interval passed → published
    normal_only = [c for c in app._occupancy_publisher.calls
                   if c["payload"]["level"] == "normal"]
    assert len(normal_only) == 2
    assert app.state_snapshot()["history_events_published"] >= 2
