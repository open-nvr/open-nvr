# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the per-track dwell state machine, the alert policy, the
dwell history, the operator actions and config parsing."""
from __future__ import annotations

import datetime as _dt
from typing import Any

import pytest

import loitering_detection as ld
from loitering_detection import (
    AppConfig,
    CameraWatch,
    LoiteringDetector,
    load_config,
)
from opennvr_app_sdk.geometry import Zone

BASE = 1_700_000_000.0


def _ts(seconds: float) -> str:
    dt = _dt.datetime.fromtimestamp(BASE + seconds, _dt.timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _event(*dets: dict[str, Any], camera_id: str = "cam1", at: float = 0.0) -> dict[str, Any]:
    """An inference event. Each det is (track, in_zone, label, h)."""
    out = []
    for d in dets:
        in_zone = d.get("in_zone", True)
        bbox = ({"x": 0.45, "y": 0.45, "w": 0.1, "h": d.get("h", 0.1)} if in_zone
                else {"x": 0.01, "y": 0.01, "w": 0.05, "h": d.get("h", 0.05)})
        out.append({"label": d.get("label", "person"), "confidence": 0.9, "bbox": bbox,
                    "track_id": d.get("track", "t1")})
    return {"correlation_id": "corr-1", "adapter": "yolov8", "adapter_version": "1",
            "camera_id": camera_id, "completed_at": _ts(at),
            "result": {"detections": out}}


def _camera(camera_id: str = "cam1") -> CameraWatch:
    zone = Zone.from_config("centre", [[480, 270], [1440, 270], [1440, 810], [480, 810]])
    return CameraWatch(camera_id=camera_id, zone=zone, frame_width=1920, frame_height=1080)


def _config(*cameras: CameraWatch, **knobs) -> AppConfig:
    cameras = cameras or (_camera(),)
    return AppConfig(
        nats_url="nats://x:4222", nats_token=None, subject_pattern="opennvr.inference.>",
        watch_labels=["person"], threshold_seconds=knobs.pop("threshold_seconds", 10.0),
        grace_period_seconds=knobs.pop("grace_period_seconds", 5.0),
        cameras={c.camera_id: c for c in cameras}, webhook_url=None,
        attach_snapshot=False, **knobs,
    )


class _NullDispatcher:
    def fire(self, alert):  # noqa: ANN001
        return {}


def _detector(cfg: AppConfig | None = None) -> LoiteringDetector:
    return LoiteringDetector(cfg or _config(), _NullDispatcher())


# ── Dwell per track ──────────────────────────────────────────────────


def test_no_alert_before_threshold():
    d = _detector()
    assert d.handle_event(_event({"track": "a"}, at=0)) == []
    assert d.handle_event(_event({"track": "a"}, at=9)) == []


def test_alert_once_at_threshold_per_track():
    d = _detector()
    d.handle_event(_event({"track": "a"}, at=0))
    fired = d.handle_event(_event({"track": "a"}, at=10))
    assert len(fired) == 1
    assert fired[0].alert_type == "loitering"
    assert fired[0].evidence["track_id"] == "a"
    assert fired[0].evidence["dwell_seconds"] == 10.0
    assert d.handle_event(_event({"track": "a"}, at=20)) == []   # latched


def test_two_tracks_are_two_stays():
    d = _detector()
    d.handle_event(_event({"track": "a"}, at=0))
    d.handle_event(_event({"track": "a"}, {"track": "b"}, at=6))
    assert len(d.handle_event(_event({"track": "a"}, {"track": "b"}, at=10))) == 1   # only a
    assert len(d.handle_event(_event({"track": "a"}, {"track": "b"}, at=16))) == 1   # now b


def test_out_of_zone_is_not_a_stay():
    d = _detector()
    d.handle_event(_event({"track": "a", "in_zone": False}, at=0))
    assert d.handle_event(_event({"track": "a", "in_zone": False}, at=30)) == []
    assert d.state_snapshot()["dwelling_now"] == 0


def test_gap_within_grace_keeps_the_stay():
    d = _detector()
    d.handle_event(_event({"track": "a"}, at=0))
    d.handle_event(_event(camera_id="cam1", at=3))            # nobody in frame for 3 s
    fired = d.handle_event(_event({"track": "a"}, at=10))
    assert len(fired) == 1 and fired[0].evidence["dwell_seconds"] == 10.0


def test_gap_beyond_grace_ends_the_stay_and_starts_a_new_one():
    d = _detector()
    d.handle_event(_event({"track": "a"}, at=0))
    d.handle_event(_event({"track": "a"}, at=4))
    d.handle_event(_event(camera_id="cam1", at=20))           # gone > 5 s → stay over
    snap = d.state_snapshot()
    assert snap["dwelling_now"] == 0
    assert snap["today"]["stays"] == 1 and snap["today"]["longest_s"] == 4.0
    d.handle_event(_event({"track": "a"}, at=21))            # back: fresh stay
    assert d.handle_event(_event({"track": "a"}, at=29)) == []
    assert len(d.handle_event(_event({"track": "a"}, at=31))) == 1


def test_silent_camera_stays_are_swept_by_wall_clock():
    """Tier-0 sends nothing for an empty frame, so the last dweller must
    be ended by the sweep, not by a later event."""
    d = _detector()
    d.handle_event(_event({"track": "a"}, at=0))
    d.handle_event(_event({"track": "a"}, at=8))
    assert d.finish_stale(BASE + 10) == 0          # inside grace
    assert d.finish_stale(BASE + 14) == 1          # 6 s silent > grace 5 s
    snap = d.state_snapshot()
    assert snap["dwelling_now"] == 0 and snap["today"]["stays"] == 1
    assert snap["today"]["longest_s"] == 8.0


def test_untracked_detections_fall_back_to_per_label():
    d = _detector()
    ev = _event({"track": None}, at=0)
    ev["result"]["detections"][0]["track_id"] = None
    d.handle_event(ev)
    ev2 = _event({"track": None}, at=10)
    ev2["result"]["detections"][0]["track_id"] = None
    fired = d.handle_event(ev2)
    assert len(fired) == 1 and fired[0].evidence["track_id"] == "label:person"


def test_min_bbox_height_filters_small_objects():
    d = _detector(_config(min_bbox_height=0.2))
    d.handle_event(_event({"track": "a", "h": 0.1}, at=0))
    assert d.handle_event(_event({"track": "a", "h": 0.1}, at=30)) == []


def test_unknown_camera_ignored():
    d = _detector()
    assert d.handle_event(_event({"track": "a"}, camera_id="cam9", at=0)) == []
    assert d.handle_event(_event({"track": "a"}, camera_id="cam9", at=30)) == []


# ── Alert policy ─────────────────────────────────────────────────────


def test_escalation_fires_once_after_delay():
    d = _detector(_config(escalate_after_seconds=20.0, alert_severity="medium"))
    d.handle_event(_event({"track": "a"}, at=0))
    first = d.handle_event(_event({"track": "a"}, at=10))
    assert first[0].severity == "medium"
    assert d.handle_event(_event({"track": "a"}, at=25)) == []
    esc = d.handle_event(_event({"track": "a"}, at=30))
    assert len(esc) == 1 and esc[0].alert_type == "loitering-escalated" and esc[0].severity == "high"
    assert d.handle_event(_event({"track": "a"}, at=60)) == []


def test_cooldown_holds_back_second_first_stage_alert(monkeypatch):
    d = _detector(_config(alert_cooldown_seconds=60.0))
    now = [BASE]
    monkeypatch.setattr(ld.time, "time", lambda: now[0])
    d.handle_event(_event({"track": "a"}, at=0))
    assert len(d.handle_event(_event({"track": "a"}, at=10))) == 1
    d.handle_event(_event({"track": "b"}, at=11))
    assert d.handle_event(_event({"track": "b"}, at=21)) == []      # inside cooldown
    now[0] = BASE + 100
    assert len(d.handle_event(_event({"track": "b"}, at=22))) == 1  # cooldown over


def test_outside_active_hours_is_quiet_but_still_counts(monkeypatch):
    cfg = _config(active_hours=ld.ActiveHours(_dt.time(9, 0), _dt.time(17, 0)))
    d = _detector(cfg)
    monkeypatch.setattr(d, "_now_local", lambda: _dt.datetime(2026, 1, 1, 2, 0))
    d.handle_event(_event({"track": "a"}, at=0))
    assert d.handle_event(_event({"track": "a"}, at=30)) == []
    d.handle_event(_event(camera_id="cam1", at=60))
    assert d.state_snapshot()["today"]["stays"] == 1
    assert d.state_snapshot()["alerts_active_now"] is False


def test_after_hours_threshold_alerts_sooner(monkeypatch):
    cfg = _config(active_hours=ld.ActiveHours(_dt.time(9, 0), _dt.time(17, 0)),
                  after_hours_threshold_seconds=3.0)
    d = _detector(cfg)
    monkeypatch.setattr(d, "_now_local", lambda: _dt.datetime(2026, 1, 1, 2, 0))
    d.handle_event(_event({"track": "a"}, at=0))
    fired = d.handle_event(_event({"track": "a"}, at=3))
    assert len(fired) == 1 and fired[0].evidence["threshold_seconds"] == 3.0
    monkeypatch.setattr(d, "_now_local", lambda: _dt.datetime(2026, 1, 1, 12, 0))
    d.handle_event(_event({"track": "b"}, at=10))
    assert d.handle_event(_event({"track": "b"}, at=14)) == []      # daytime: 10 s applies


def test_gathering_alert_once_per_group():
    d = _detector(_config(group_size=2, group_seconds=5.0, threshold_seconds=100.0))
    d.handle_event(_event({"track": "a"}, {"track": "b"}, at=0))
    assert d.handle_event(_event({"track": "a"}, {"track": "b"}, at=4)) == []
    fired = d.handle_event(_event({"track": "a"}, {"track": "b"}, at=6))
    assert len(fired) == 1 and fired[0].alert_type == "gathering" and fired[0].evidence["count"] == 2
    assert d.handle_event(_event({"track": "a"}, {"track": "b"}, at=8)) == []
    d.handle_event(_event({"track": "a"}, at=9))                      # b left: group over
    d.handle_event(_event({"track": "a"}, {"track": "c"}, at=10))
    assert len(d.handle_event(_event({"track": "a"}, {"track": "c"}, at=16))) == 1


def test_dismissed_stay_raises_nothing():
    d = _detector(_config(escalate_after_seconds=5.0))
    d.handle_event(_event({"track": "a"}, at=0))
    out = d.on_action("dismiss", {"camera": "cam1", "track": "a"})
    assert out["ok"] is True
    assert d.handle_event(_event({"track": "a"}, at=30)) == []
    assert d.state_snapshot()["dwelling"][0]["stage"] == "dismissed"
    with pytest.raises(KeyError):
        d.on_action("dismiss", {"camera": "cam1", "track": "zzz"})


# ── Surfaces ─────────────────────────────────────────────────────────


def test_state_snapshot_shape_and_progress(monkeypatch):
    d = _detector()
    monkeypatch.setattr(ld.time, "time", lambda: BASE + 5)
    d.handle_event(_event({"track": "a"}, at=0))
    d.handle_event(_event({"track": "a"}, at=5))
    snap = d.state_snapshot()
    assert snap["dwelling_now"] == 1
    row = snap["dwelling"][0]
    assert row["camera"] == "cam1" and row["stage"] == "watching" and 0.4 <= row["progress"] <= 0.6
    assert snap["per_camera"][0]["zone"] == "centre" and snap["per_camera"][0]["drawn"] is True
    assert len(snap["hourly"]["cam1"]) == 24
    assert "Loitering Detection" in d.ui_html()


def test_finished_stays_feed_history_and_histogram(monkeypatch):
    d = _detector()
    d.handle_event(_event({"track": "a"}, at=0))
    d.handle_event(_event({"track": "a"}, at=45))
    d.handle_event(_event(camera_id="cam1", at=60))
    snap = d.state_snapshot()
    cam = snap["per_camera"][0]
    assert cam["stays_today"] == 1 and cam["histogram"][1]["n"] == 1     # 30s–1m bucket
    published = {}

    class _Pub:
        def publish(self, schema, *, camera_id, payload):
            published[camera_id] = (schema, payload)
            return True
    d._publisher = _Pub()
    assert d.flush_dwell() == 1
    schema, payload = published["cam1"]
    assert schema == "occupancy.footfall.v1"
    assert payload["entries"] == 0 and payload["dwell_count"] == 1 and payload["dwell_max_seconds"] == 45.0
    assert d.flush_dwell() == 0


def test_reset_today_action():
    d = _detector()
    d.handle_event(_event({"track": "a"}, at=0))
    d.handle_event(_event({"track": "a"}, at=10))
    assert d.state_snapshot()["today"]["alerts"] == 1
    d.on_action("reset_today", {})
    assert d.state_snapshot()["today"]["alerts"] == 0


# ── Live config and discovery ────────────────────────────────────────


def test_config_update_applies_zone_labels_and_knobs():
    d = _detector()
    d.on_config_update({
        "watch_labels": ["car"], "threshold_seconds": 3, "escalate_after_seconds": 7,
        "zones": {"1": [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2], [0.1, 0.2]]},
        "active_hours": {"start": "22:00", "end": "06:00"},
    })
    assert d.cfg.watch_labels == ["car"] and d.cfg.threshold_seconds == 3.0
    assert d.cfg.escalate_after_seconds == 7.0 and d.cfg.active_hours is not None
    cam = d.cfg.cameras["cam1"]
    assert cam.drawn and cam.zone.polygon[0].x == 192.0     # 0.1 × 1920
    d.on_config_update({"zones": {}})
    assert d.cfg.cameras["cam1"].drawn is False
    assert d.state_snapshot()["needs_zone"] == ["cam1"]


def test_refresh_cameras_follows_catalog_picks():
    cfg = _config()
    cfg.cameras = {}
    cfg.auto_cameras = True
    d = _detector(cfg)
    added, removed = d.refresh_cameras([1, 3])
    assert added == ["cam1", "cam3"] and removed == []
    d.handle_event(_event({"track": "a"}, camera_id="cam3", at=0))
    assert d.state_snapshot()["dwelling_now"] == 1
    added, removed = d.refresh_cameras([1])
    assert removed == ["cam3"] and d.state_snapshot()["dwelling_now"] == 0


def test_load_config_standalone_and_catalog_zone(tmp_path):
    import yaml
    p = tmp_path / "c.yml"
    p.write_text(yaml.safe_dump({
        "nats_url": "nats://x", "threshold_seconds": 45, "escalate_after_seconds": 30,
        "alert_severity": "high", "group_size": 3,
        "cameras": [{"camera_id": "cam1", "frame_width": 1000, "frame_height": 800,
                     "zone": [[0, 0], [10, 0], [10, 10]], "zone_name": "door"}],
        "zones": {"cam1": [[0.2, 0.5], [0.8, 0.5], [0.8, 0.9]]},
    }))
    cfg = load_config(str(p))
    assert cfg.threshold_seconds == 45.0 and cfg.escalate_after_seconds == 30.0
    assert cfg.alert_severity == "high" and cfg.group_size == 3
    zone = cfg.cameras["cam1"].zone
    assert zone.name == "door" and (zone.polygon[0].x, zone.polygon[0].y) == (200.0, 400.0)


def test_load_config_rejects_bad_values(tmp_path):
    import yaml
    p = tmp_path / "c.yml"
    p.write_text(yaml.safe_dump({"nats_url": "nats://x", "threshold_seconds": 0,
                                 "cameras": [{"camera_id": "cam1"}]}))
    with pytest.raises(ValueError):
        load_config(str(p))
    p.write_text(yaml.safe_dump({"nats_url": "nats://x", "alert_severity": "loud",
                                 "cameras": [{"camera_id": "cam1"}]}))
    with pytest.raises(ValueError):
        load_config(str(p))
    p.write_text(yaml.safe_dump({"nats_url": "nats://x"}))
    with pytest.raises(ValueError):          # no cameras and no opennvr_url
        load_config(str(p))


def test_load_config_catalog_mode_needs_no_cameras(tmp_path):
    import yaml
    p = tmp_path / "c.yml"
    p.write_text(yaml.safe_dump({"nats_url": "nats://x", "opennvr_url": "http://core",
                                 "consume_tier0": True}))
    cfg = load_config(str(p))
    assert cfg.auto_cameras is True and cfg.consume_tier0 is True and cfg.cameras == {}
