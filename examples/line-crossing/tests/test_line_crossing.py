# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Focused tests for the line-crossing predicate and tripwire geometry."""
from __future__ import annotations

import line_crossing as lc
from line import Point, Tripwire


# ── Geometry ───────────────────────────────────────────────────────


def test_tripwire_detects_directional_crossing():
    # Vertical wire down the middle of a 1000-wide frame, A=top B=bottom.
    wire = Tripwire.from_config("mid", a=[500, 0], b=[500, 1000], count_direction="both")
    # Moving left→right crosses it.
    assert wire.crossing(Point(400, 500), Point(600, 500)) is not None
    # Moving right→left crosses it the other way.
    assert wire.crossing(Point(600, 500), Point(400, 500)) is not None
    # Moving along one side does not.
    assert wire.crossing(Point(400, 100), Point(400, 900)) is None


def test_tripwire_respects_count_direction():
    wire = Tripwire.from_config("mid", a=[500, 0], b=[500, 1000], count_direction="a_to_b")
    one_way = wire.crossing(Point(400, 500), Point(600, 500))
    other_way = wire.crossing(Point(600, 500), Point(400, 500))
    # Exactly one of the two directions should be counted.
    assert (one_way is None) != (other_way is None)


def test_grazing_the_line_is_not_a_crossing():
    wire = Tripwire.from_config("mid", a=[500, 0], b=[500, 1000])
    # Ends exactly on the line → not a committed crossing.
    assert wire.crossing(Point(400, 500), Point(500, 500)) is None


# ── handle_event state machine ─────────────────────────────────────


def _camera() -> lc.CameraWire:
    wire = Tripwire.from_config("mid", a=[960, 0], b=[960, 1080], count_direction="both")
    return lc.CameraWire(camera_id="cam-1", wire=wire, frame_width=1920, frame_height=1080)


def _config(camera) -> lc.AppConfig:
    return lc.AppConfig(
        nats_url="nats://x:4222", nats_token=None,
        subject_pattern="opennvr.inference.>", watch_labels=["person"],
        track_ttl_seconds=30.0, cameras={camera.camera_id: camera},
        webhook_url=None,
    )


class _NullDispatcher:
    def fire(self, alert):  # noqa: ANN001
        return {}


def _event(track_id, cx_norm, *, ts="2026-01-01T00:00:00Z"):
    # A person whose bbox center sits at cx_norm of frame width.
    return {
        "camera_id": "cam-1",
        "correlation_id": "corr-1",
        "completed_at": ts,
        "result": {
            "detections": [
                {"label": "person", "track_id": track_id,
                 "bbox": {"x": cx_norm - 0.02, "y": 0.48, "w": 0.04, "h": 0.04}},
            ]
        },
    }


def _detector(camera):
    return lc.LineCrossingDetector(_config(camera), _NullDispatcher())


def test_first_sighting_does_not_fire():
    d = _detector(_camera())
    assert d.handle_event(_event("t1", 0.30)) == []   # left of wire, first frame


def test_track_crossing_fires_once():
    d = _detector(_camera())
    d.handle_event(_event("t1", 0.30, ts="2026-01-01T00:00:00Z"))      # left
    fired = d.handle_event(_event("t1", 0.70, ts="2026-01-01T00:00:01Z"))  # right → cross
    assert len(fired) == 1
    assert fired[0].evidence["track_id"] == "t1"
    assert fired[0].evidence["direction"] in ("a_to_b", "b_to_a")


def test_no_recross_without_movement_back():
    d = _detector(_camera())
    d.handle_event(_event("t1", 0.30, ts="2026-01-01T00:00:00Z"))
    d.handle_event(_event("t1", 0.70, ts="2026-01-01T00:00:01Z"))      # cross → fire
    again = d.handle_event(_event("t1", 0.75, ts="2026-01-01T00:00:02Z"))  # stays right
    assert again == []


def test_untracked_detections_ignored():
    d = _detector(_camera())
    ev = _event("t1", 0.70)
    del ev["result"]["detections"][0]["track_id"]
    assert d.handle_event(ev) == []


def test_unknown_camera_ignored():
    d = _detector(_camera())
    ev = _event("t1", 0.70)
    ev["camera_id"] = "cam-other"
    assert d.handle_event(ev) == []


# ── App Catalog tripwire editor → running rule (normalized line) ───────


def test_registry_line_override_scaled_to_pixels(tmp_path):
    """The catalog tripwire editor stores a top-level normalized `line`
    dict keyed by camera_id; load_config applies it scaled to pixels,
    overriding the nested line."""
    import yaml as _yaml

    from line_crossing import load_config

    cfg = tmp_path / "c.yml"
    cfg.write_text(_yaml.safe_dump({
        "nats_url": "nats://x",
        "cameras": [{
            "camera_id": "cam-1", "frame_width": 1000, "frame_height": 800,
            "line": {"a": [0, 0], "b": [1, 1]},  # nested — must be OVERRIDDEN
        }],
        "line": {"cam-1": {"a": [0.2, 0.5], "b": [0.8, 0.5], "count_direction": "a_to_b"}},
    }))
    parsed = load_config(str(cfg))
    wire = parsed.cameras["cam-1"].wire
    assert (wire.a.x, wire.a.y) == (200.0, 400.0)
    assert (wire.b.x, wire.b.y) == (800.0, 400.0)
    assert wire.count_direction == "a_to_b"


# ── Counting, policy, filters, live config ───────────────────────────


def _cross(d, track="t1", t0="2026-01-01T10:00:00Z", t1="2026-01-01T10:00:01Z",
           left=0.30, right=0.70):
    d.handle_event(_event(track, left, ts=t0))
    return d.handle_event(_event(track, right, ts=t1))


def test_counts_per_direction_today_and_hourly():
    d = _detector(_camera())
    _cross(d, "t1")                                    # left → right
    _cross(d, "t2", left=0.70, right=0.30)             # right → left
    _cross(d, "t3")
    s = d.state_snapshot()
    a, b = s["today"]["a_to_b"], s["today"]["b_to_a"]
    assert {a, b} == {2, 1}
    assert s["today"]["net"] in (1, -1)
    assert s["total_crossings"] == 3
    row = s["per_camera"][0]
    assert row["camera"] == "cam-1" and row["total"] == 3 and row["last"]
    buckets = s["hourly"]["cam-1"]
    assert len(buckets) == 24
    assert sum(x["a_to_b"] + x["b_to_a"] for x in buckets) == 3
    assert s["recent"][-1]["direction"] in ("a_to_b", "b_to_a")
    assert s["recent"][-1]["word"] in ("in", "out")


def test_alert_mode_off_counts_but_never_alerts():
    cam = _camera(); cfg = _config(cam); cfg.alert_mode = "off"
    d = lc.LineCrossingDetector(cfg, _NullDispatcher())
    assert _cross(d) == []
    assert d.state_snapshot()["total_crossings"] == 1
    assert d.state_snapshot()["alerts_today"] == 0


def test_cooldown_makes_a_group_one_alert_but_counts_them_all():
    cam = _camera(); cfg = _config(cam); cfg.alert_cooldown_seconds = 60
    d = lc.LineCrossingDetector(cfg, _NullDispatcher())
    first = _cross(d, "t1")
    second = _cross(d, "t2")
    third = _cross(d, "t3")
    assert len(first) == 1 and second == [] and third == []
    s = d.state_snapshot()
    assert s["total_crossings"] == 3 and s["alerts_today"] == 1
    assert [r["alerted"] for r in s["recent"]] == [True, False, False]


def test_active_hours_gate_alerts_not_counting():
    import datetime as _dt
    cam = _camera(); cfg = _config(cam)
    cfg.active_hours = lc.ActiveHours(_dt.time(22, 0), _dt.time(6, 0))
    d = lc.LineCrossingDetector(cfg, _NullDispatcher())
    d._now_local = lambda: _dt.datetime(2026, 1, 1, 14, 0)     # afternoon
    assert _cross(d, "t1") == []
    d._now_local = lambda: _dt.datetime(2026, 1, 1, 23, 30)    # night
    assert len(_cross(d, "t2")) == 1
    s = d.state_snapshot()
    assert s["total_crossings"] == 2 and s["alerts_today"] == 1
    assert s["alerts_active_now"] is True


def test_passthrough_threshold_alerts_at_multiples():
    cam = _camera(); cfg = _config(cam)
    cfg.alert_mode = "threshold"; cfg.passthrough_threshold = 3
    d = lc.LineCrossingDetector(cfg, _NullDispatcher())
    fired = [len(_cross(d, f"t{i}")) for i in range(1, 7)]
    assert fired == [0, 0, 1, 0, 0, 1]
    alert = _cross(d, "t7", left=0.70, right=0.30)
    assert alert == []                                  # 7 — not a multiple
    d2 = lc.LineCrossingDetector(cfg, _NullDispatcher())
    _cross(d2, "a"); _cross(d2, "b"); (third,) = _cross(d2, "c")
    assert third.alert_type == "passthrough" and third.severity == "low"
    assert "3 crossings today" in third.title


def test_short_lived_tracks_and_small_boxes_are_filtered():
    cam = _camera(); cfg = _config(cam); cfg.min_track_age_seconds = 5
    d = lc.LineCrossingDetector(cfg, _NullDispatcher())
    assert _cross(d, "t1", t0="2026-01-01T10:00:00Z", t1="2026-01-01T10:00:01Z") == []   # 1 s old
    assert len(_cross(d, "t2", t0="2026-01-01T10:00:00Z", t1="2026-01-01T10:00:09Z")) == 1
    cfg2 = _config(cam); cfg2.min_bbox_height = 0.2                 # events have h=0.04
    d2 = lc.LineCrossingDetector(cfg2, _NullDispatcher())
    assert _cross(d2) == []
    assert d2.state_snapshot()["total_crossings"] == 0


def test_alert_carries_direction_label_and_configured_severity():
    cam = _camera(); cfg = _config(cam)
    cfg.alert_severity = "medium"; cfg.label_a_to_b = "entering"; cfg.label_b_to_a = "leaving"
    d = lc.LineCrossingDetector(cfg, _NullDispatcher())
    (alert,) = _cross(d)
    assert alert.severity == "medium"
    assert alert.evidence["direction_label"] in ("entering", "leaving")
    assert alert.evidence["direction_label"] in alert.title
    assert alert.alert_type == "line-crossing"
    assert alert.images == {}                            # no platform client in tests


def test_daily_reset_hour_rolls_today_but_not_totals():
    import datetime as _dt
    cam = _camera(); cfg = _config(cam); cfg.daily_reset_hour = 6
    d = lc.LineCrossingDetector(cfg, _NullDispatcher())
    d._now_local = lambda: _dt.datetime(2026, 1, 1, 23, 0)
    d._today_key = d._day_key(d._now_local())
    _cross(d, "t1")
    d._now_local = lambda: _dt.datetime(2026, 1, 2, 5, 0)     # before 06:00 — same "day"
    _cross(d, "t2")
    assert d.state_snapshot()["today"]["a_to_b"] + d.state_snapshot()["today"]["b_to_a"] == 2
    d._now_local = lambda: _dt.datetime(2026, 1, 2, 6, 30)     # rolled
    s = d.state_snapshot()
    assert s["today"]["a_to_b"] + s["today"]["b_to_a"] == 0
    assert s["total_crossings"] == 2


def test_footfall_delta_is_published_once_and_cleared():
    cam = _camera(); cfg = _config(cam)
    d = lc.LineCrossingDetector(cfg, _NullDispatcher())
    sent: list = []

    class _Pub:
        def publish(self, schema, *, camera_id, payload, correlation_id=None):
            sent.append((schema, camera_id, payload)); return True
    d._publisher = _Pub()
    _cross(d, "t1"); _cross(d, "t2", left=0.70, right=0.30)
    assert d.flush_footfall() == 1
    schema, cam_id, payload = sent[0]
    assert schema == lc.FOOTFALL_SCHEMA and cam_id == "cam-1"
    assert payload["entries"] + payload["exits"] == 2 and payload["dwell_count"] == 0
    assert d.flush_footfall() == 0                      # nothing new
    cfg.publish_footfall = False
    _cross(d, "t3")
    assert d.flush_footfall() == 0 and sent[-1] == (schema, cam_id, payload)


def test_camera_without_a_line_is_reported_not_silent():
    cam = lc.CameraWire(camera_id="cam-1", wire=None, frame_width=1000, frame_height=1000)
    d = lc.LineCrossingDetector(_config(cam), _NullDispatcher())
    assert _cross(d) == []
    s = d.state_snapshot()
    assert s["needs_line"] == ["cam-1"]
    assert s["per_camera"][0]["line"].startswith("—")
    assert "No line drawn" in d.ui_html()


def test_live_config_applies_line_labels_and_policy():
    cam = lc.CameraWire(camera_id="cam1", wire=None, frame_width=1000, frame_height=1000)
    d = lc.LineCrossingDetector(_config(cam), _NullDispatcher())
    d.on_config_update({
        "line": {"1": {"a": [0.5, 0.0], "b": [0.5, 1.0], "count_direction": "both"}},  # numeric key
        "watch_labels": ["car", "Truck"],
        "alert_mode": "threshold", "passthrough_threshold": 2,
        "label_a_to_b": "north", "active_hours": {"start": "08:00", "end": "18:00"},
        "alert_severity": "critical", "min_bbox_height": 0.05,
    })
    assert d.cfg.cameras["cam1"].wire is not None
    assert d.cfg.watch_labels == ["car", "truck"]
    assert d.cfg.alert_mode == "threshold" and d.cfg.passthrough_threshold == 2
    assert d.cfg.label_a_to_b == "north" and d.cfg.label_b_to_a == "out"
    assert d.cfg.active_hours.start.hour == 8
    # Idempotent re-delivery: nothing flips.
    wire_before = d.cfg.cameras["cam1"].wire
    d.on_config_update({"line": {"1": {"a": [0.5, 0.0], "b": [0.5, 1.0], "count_direction": "both"}}})
    assert d.cfg.cameras["cam1"].wire is wire_before
    # A bad value is ignored, not applied.
    d.on_config_update({"alert_mode": "loud"})
    assert d.cfg.alert_mode == "threshold"
    # Removing the line takes the camera back to "needs a line".
    d.on_config_update({"line": {}})
    assert d.cfg.cameras["cam1"].wire is None


def test_picked_cameras_are_counted_and_read_their_drawn_lines():
    cfg = _config(_camera()); cfg.cameras = {}; cfg.auto_cameras = True
    d = lc.LineCrossingDetector(cfg, _NullDispatcher())
    d._last_config = {"line": {"2": {"a": [0, 0.5], "b": [1, 0.5]}}}
    d.on_cameras_update(frozenset({2}))
    assert list(d.cfg.cameras) == ["cam2"]
    assert d.cfg.cameras["cam2"].wire is not None       # the drawn line was applied on arrival
    # Unpicking the last camera empties the set: nothing picked, count nowhere.
    d.on_cameras_update(frozenset())
    assert d.cfg.cameras == {}


def test_pinned_yaml_cameras_ignore_picks():
    d = _detector(_camera())
    d.on_cameras_update(frozenset({9}))
    assert list(d.cfg.cameras) == [_camera().camera_id]


def test_state_paths_declared_in_the_manifest_resolve():
    d = _detector(_camera())
    _cross(d)
    state = d.state_snapshot()
    for view in lc.MANIFEST.state_schema:
        node = state
        for part in view.path.split("."):
            assert part in node, f"{view.name}: {view.path} missing"
            node = node[part]


def test_ui_html_is_static_and_escapes():
    cam = _camera(); cfg = _config(cam); cfg.label_a_to_b = "<b>x</b>"
    d = lc.LineCrossingDetector(cfg, _NullDispatcher())
    _cross(d)
    page = d.ui_html()
    assert "<script" not in page.lower()
    assert "&lt;b&gt;x&lt;/b&gt;" in page and "<b>x</b>" not in page


def test_load_config_with_no_cameras_waits_for_picks(tmp_path):
    import yaml as _yaml
    cfg = tmp_path / "c.yml"
    cfg.write_text(_yaml.safe_dump({
        "nats_url": "nats://x", "opennvr_url": "http://core",
        "line": {"3": {"a": [0.1, 0.5], "b": [0.9, 0.5], "count_direction": "a_to_b"}},
        "alert_mode": "off", "active_hours": {"start": "22:00", "end": "06:00"},
    }))
    parsed = lc.load_config(str(cfg))
    assert parsed.cameras == {} and parsed.auto_cameras
    d = lc.LineCrossingDetector(parsed, _NullDispatcher())
    d._last_config = {"line": {"3": {"a": [0.1, 0.5], "b": [0.9, 0.5], "count_direction": "a_to_b"}}}
    d.on_cameras_update(frozenset({3}))
    assert d.cfg.cameras["cam3"].wire.count_direction == "a_to_b"
    assert parsed.alert_mode == "off" and parsed.active_hours.start.hour == 22
