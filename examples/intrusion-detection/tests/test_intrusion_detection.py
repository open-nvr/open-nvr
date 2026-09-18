# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the arming state machine, the breach rule, the alarm
policy, the operator actions and config parsing."""
from __future__ import annotations

import datetime as _dt
import time as _time
from typing import Any

import pytest

import intrusion_detection as idet
from intrusion_detection import (
    ALARM,
    ARMED,
    ARMING,
    BREACH,
    BYPASSED,
    DISARMED,
    AppConfig,
    CameraWatch,
    IntrusionDetector,
    load_config,
)
from opennvr_app_sdk.geometry import Zone

#: Event time and wall clock are the same timeline in this app (the
#: arming delays are real seconds), so the fixture base is "now".
BASE = _time.time()


def _ts(seconds: float) -> str:
    dt = _dt.datetime.fromtimestamp(BASE + seconds, _dt.timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _event(*dets: dict[str, Any], camera_id: str = "cam1", at: float = 0.0) -> dict[str, Any]:
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
    zone = Zone.from_config("yard", [[480, 270], [1440, 270], [1440, 810], [480, 810]])
    return CameraWatch(camera_id=camera_id, zone=zone, frame_width=1920, frame_height=1080)


def _config(*cameras: CameraWatch, **knobs) -> AppConfig:
    cameras = cameras or (_camera(),)
    knobs.setdefault("arm_mode", "always")
    knobs.setdefault("min_presence_seconds", 0.0)
    knobs.setdefault("alarm_cooldown_seconds", 0.0)
    return AppConfig(
        nats_url="nats://x:4222", nats_token=None, subject_pattern="opennvr.inference.>",
        watch_labels=["person"], cameras={c.camera_id: c for c in cameras},
        webhook_url=None, attach_snapshot=False, **knobs,
    )


class _NullDispatcher:
    def fire(self, alert):  # noqa: ANN001
        return {}


@pytest.fixture
def clock(monkeypatch):
    """The arming delays are wall-clock by design (an entry delay is 30
    real seconds), so a test that exercises them drives the clock."""
    now = [BASE]

    def _set(t: float) -> float:
        now[0] = t
        return t

    monkeypatch.setattr(idet.time, "time", lambda: now[0])
    _set.now = lambda: now[0]      # type: ignore[attr-defined]
    return _set


def _detector(cfg: AppConfig | None = None) -> IntrusionDetector:
    return IntrusionDetector(cfg or _config(), _NullDispatcher())


def _state(d: IntrusionDetector, camera_id: str = "cam1") -> str:
    return d._cam_state(camera_id).state


# ── Arming ───────────────────────────────────────────────────────────


def test_always_mode_arms_on_the_first_tick():
    d = _detector()
    assert _state(d) == DISARMED
    d.tick(BASE)
    assert _state(d) == ARMED


def test_off_mode_never_arms():
    d = _detector(_config(arm_mode="off"))
    d.tick(BASE)
    assert _state(d) == DISARMED
    assert d.handle_event(_event({"track": "a"}, at=0)) == []


def test_schedule_mode_follows_armed_hours(monkeypatch):
    cfg = _config(arm_mode="schedule",
                  armed_hours=idet.ArmedHours(_dt.time(19, 0), _dt.time(7, 0)))
    d = _detector(cfg)
    monkeypatch.setattr(d, "_now_local", lambda: _dt.datetime(2026, 1, 1, 12, 0))
    d.tick(BASE)
    assert _state(d) == DISARMED
    monkeypatch.setattr(d, "_now_local", lambda: _dt.datetime(2026, 1, 1, 23, 0))
    d.tick(BASE + 1)
    assert _state(d) == ARMED


def test_exit_delay_passes_through_arming():
    d = _detector(_config(exit_delay_seconds=30.0))
    d.tick(BASE)
    assert _state(d) == ARMING
    d.tick(BASE + 10)
    assert _state(d) == ARMING
    d.tick(BASE + 31)
    assert _state(d) == ARMED


def test_manual_mode_only_arms_by_action():
    d = _detector(_config(arm_mode="manual"))
    d.tick(BASE)
    assert _state(d) == DISARMED
    d.on_action("arm", {})
    assert _state(d) == ARMED
    d.on_action("disarm", {})
    assert _state(d) == DISARMED


def test_override_expires_and_can_be_cleared(monkeypatch, clock):
    cfg = _config(arm_mode="schedule", override_minutes=10.0,
                  armed_hours=idet.ArmedHours(_dt.time(19, 0), _dt.time(7, 0)))
    d = _detector(cfg)
    monkeypatch.setattr(d, "_now_local", lambda: _dt.datetime(2026, 1, 1, 23, 0))
    d.tick(BASE)
    assert _state(d) == ARMED
    d.on_action("disarm", {})
    assert _state(d) == DISARMED
    d.tick(clock(BASE + 300))                # 5 min: override still holds
    assert _state(d) == DISARMED
    d.tick(clock(BASE + 700))                # past 10 min: schedule takes over
    assert _state(d) == ARMED
    d.on_action("disarm", {})
    d.on_action("clear_override", {})
    assert _state(d) == ARMED


# ── The breach rule ──────────────────────────────────────────────────


def test_intrusion_alarms_once_per_intruder():
    d = _detector()
    d.tick(BASE)
    fired = d.handle_event(_event({"track": "a"}, at=0))
    assert len(fired) == 1
    assert fired[0].alert_type == "intrusion" and fired[0].severity == "high"
    assert fired[0].evidence["track_id"] == "a" and fired[0].evidence["zone_name"] == "yard"
    assert _state(d) == ALARM
    # Standing in the zone does not re-alarm on every frame — the 1.0 bug.
    for i in range(1, 6):
        assert d.handle_event(_event({"track": "a"}, at=i)) == []


def test_out_of_zone_is_not_a_breach():
    d = _detector()
    d.tick(BASE)
    assert d.handle_event(_event({"track": "a", "in_zone": False}, at=0)) == []
    assert _state(d) == ARMED


def test_disarmed_camera_raises_nothing():
    d = _detector(_config(arm_mode="manual"))
    d.tick(BASE)
    assert d.handle_event(_event({"track": "a"}, at=0)) == []
    assert d.state_snapshot()["today"]["breaches"] == 1     # still counted


def test_min_presence_filters_a_clipping_box():
    d = _detector(_config(min_presence_seconds=3.0))
    d.tick(BASE)
    assert d.handle_event(_event({"track": "a"}, at=0)) == []    # first frame
    assert d.handle_event(_event({"track": "a"}, at=2)) == []    # 2 s < 3 s
    fired = d.handle_event(_event({"track": "a"}, at=4))
    assert len(fired) == 1 and fired[0].evidence["inside_seconds"] == 4.0


def test_min_bbox_height_filters_far_objects():
    d = _detector(_config(min_bbox_height=0.2))
    d.tick(BASE)
    assert d.handle_event(_event({"track": "a", "h": 0.05}, at=0)) == []
    assert _state(d) == ARMED


def test_entry_delay_gives_a_countdown_then_alarms(clock):
    d = _detector(_config(entry_delay_seconds=20.0))
    d.tick(BASE)
    fired = d.handle_event(_event({"track": "a"}, at=0))
    assert fired == [] and _state(d) == BREACH
    row = d.state_snapshot()["per_camera"][0]
    assert row["state"] == BREACH and row["countdown_s"] is not None
    assert d.tick(clock(BASE + 10)) == []
    late = d.tick(clock(BASE + 25))
    assert len(late) == 1 and late[0].alert_type == "intrusion"
    assert _state(d) == ALARM


def test_disarming_during_entry_delay_stops_the_alarm():
    d = _detector(_config(entry_delay_seconds=20.0))
    d.tick(BASE)
    d.handle_event(_event({"track": "a"}, at=0))
    assert _state(d) == BREACH
    d.on_action("disarm", {})
    assert _state(d) == DISARMED
    assert d.tick(BASE + 30) == []


def test_untracked_detections_fall_back_to_per_label():
    d = _detector()
    d.tick(BASE)
    ev = _event({"track": None}, at=0)
    ev["result"]["detections"][0]["track_id"] = None
    fired = d.handle_event(ev)
    assert len(fired) == 1 and fired[0].evidence["track_id"] == "label:person"


def test_unknown_camera_ignored():
    d = _detector()
    d.tick(BASE)
    assert d.handle_event(_event({"track": "a"}, camera_id="cam9", at=0)) == []


# ── Alarm policy ─────────────────────────────────────────────────────


def test_escalation_fires_once_while_they_stay(clock):
    d = _detector(_config(escalate_after_seconds=30.0, alert_severity="high",
                          track_ttl_seconds=120.0))
    d.tick(BASE)
    first = d.handle_event(_event({"track": "a"}, at=0))
    assert first[0].severity == "high"
    assert d.tick(clock(BASE + 10)) == []
    esc = d.tick(clock(BASE + 40))
    assert len(esc) == 1
    assert esc[0].alert_type == "intrusion-escalated" and esc[0].severity == "critical"
    assert d.tick(clock(BASE + 90)) == []


def test_cooldown_merges_a_group_into_one_alarm(clock):
    d = _detector(_config(alarm_cooldown_seconds=60.0, alarm_reset_seconds=1.0,
                          track_ttl_seconds=2.0))
    d.tick(BASE)
    assert len(d.handle_event(_event({"track": "a"}, at=0))) == 1
    # Zone clears, camera re-arms, a second person arrives inside the cooldown.
    d.handle_event(_event(camera_id="cam1", at=clock(BASE + 5) - BASE))
    d.tick(clock(BASE + 10))
    assert _state(d) == ARMED
    assert d.handle_event(_event({"track": "b"}, at=10)) == []      # merged
    assert _state(d) == ALARM


def test_alarm_resets_when_the_zone_clears(clock):
    d = _detector(_config(alarm_reset_seconds=30.0, track_ttl_seconds=2.0))
    d.tick(BASE)
    d.handle_event(_event({"track": "a"}, at=0))
    assert _state(d) == ALARM
    d.handle_event(_event(camera_id="cam1", at=clock(BASE + 5) - BASE))   # they leave
    d.tick(clock(BASE + 10))
    assert _state(d) == ALARM                               # not yet
    d.tick(clock(BASE + 40))
    assert _state(d) == ARMED


def test_alarm_reset_waits_after_a_silent_zone(clock):
    """Tier-0 stops publishing when the zone empties, so the reset clock
    starts where the sweep ends the last track — not at the camera's last
    quiet moment, which may be hours old."""
    d = _detector(_config(alarm_reset_seconds=30.0, track_ttl_seconds=2.0))
    d.tick(BASE)                                  # armed, zone quiet
    d.handle_event(_event({"track": "a"}, at=100))  # they arrive much later
    assert _state(d) == ALARM
    # They walk off and nothing more is published. The track ages out at
    # 100 + ttl; the reset is 30s from there, not from the earlier quiet.
    d.tick(clock(BASE + 103))
    assert _state(d) == ALARM
    d.tick(clock(BASE + 125))
    assert _state(d) == ALARM
    d.tick(clock(BASE + 133))
    assert _state(d) == ARMED


# ── Actions ──────────────────────────────────────────────────────────


def test_bypass_excludes_a_camera_then_expires(clock):
    d = _detector()
    d.tick(BASE)
    d.on_action("bypass", {"camera": "cam1", "minutes": 10})
    assert _state(d) == BYPASSED
    assert d.handle_event(_event({"track": "a"}, at=0)) == []
    row = d.state_snapshot()["per_camera"][0]
    assert row["state"] == BYPASSED and row["countdown_s"] > 0
    d.tick(clock(BASE + 700))
    assert _state(d) == ARMED
    with pytest.raises(KeyError):
        d.on_action("bypass", {"camera": "nope", "minutes": 5})


def test_bypass_zero_minutes_clears_it():
    d = _detector()
    d.tick(BASE)
    d.on_action("bypass", {"camera": "cam1", "minutes": 10})
    d.on_action("bypass", {"camera": "cam1", "minutes": 0})
    assert _state(d) in (ARMED, DISARMED)


def test_acknowledge_rearms_without_waiting():
    d = _detector(_config(alarm_reset_seconds=600.0))
    d.tick(BASE)
    d.handle_event(_event({"track": "a"}, at=0))
    assert _state(d) == ALARM
    d.on_action("acknowledge", {})
    assert _state(d) == ARMED


def test_acknowledge_does_not_refire_on_the_next_frame(clock):
    """Acknowledging while the intruder is still inside re-arms, but the
    cooldown still holds — an operator clearing the panel must not be
    handed the same alarm again a frame later."""
    d = _detector(_config(alarm_cooldown_seconds=60.0, alarm_reset_seconds=600.0))
    d.tick(BASE)
    assert len(d.handle_event(_event({"track": "a"}, at=0))) == 1
    d.on_action("acknowledge", {})
    assert _state(d) == ARMED
    assert d.handle_event(_event({"track": "a"}, at=clock(BASE + 2) - BASE)) == []
    assert _state(d) == ALARM
    assert d.state_snapshot()["today"]["alarms"] == 1


def test_arm_disarm_all_cameras():
    d = _detector(_config(_camera("cam1"), _camera("cam2"), arm_mode="manual"))
    out = d.on_action("arm", {})
    assert set(out["cameras"]) == {"cam1", "cam2"}
    assert _state(d, "cam1") == ARMED and _state(d, "cam2") == ARMED
    d.on_action("disarm", {"camera": "cam2"})
    assert _state(d, "cam1") == ARMED and _state(d, "cam2") == DISARMED
    with pytest.raises(KeyError):
        d.on_action("arm", {"camera": "cam9"})


# ── Surfaces ─────────────────────────────────────────────────────────


def test_state_snapshot_shape():
    d = _detector(_config(_camera("cam1"), _camera("cam2")))
    d.tick(BASE)
    d.handle_event(_event({"track": "a"}, at=0))
    snap = d.state_snapshot()
    assert snap["camera_count"] == 2 and snap["armed_count"] == 2
    assert snap["in_alarm"] == 1 and snap["today"]["alarms"] == 1
    assert snap["intruders"][0]["camera"] == "cam1"
    assert snap["per_camera"][0]["zone"] == "yard"
    assert "Intrusion Detection" in d.ui_html()


def test_needs_zone_is_reported():
    cfg = _config()
    cfg.cameras["cam1"] = idet._whole_frame("cam1", 1000, 1000)
    d = _detector(cfg)
    assert d.state_snapshot()["needs_zone"] == ["cam1"]


# ── Config and discovery ─────────────────────────────────────────────


def test_config_update_applies_live():
    d = _detector()
    d.on_config_update({
        "watch_labels": ["car"], "arm_mode": "manual", "entry_delay_seconds": 15,
        "min_presence_seconds": 4, "escalate_after_seconds": 90,
        "zones": {"1": [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2], [0.1, 0.2]]},
    })
    assert d.cfg.watch_labels == ["car"] and d.cfg.arm_mode == "manual"
    assert d.cfg.entry_delay_seconds == 15.0 and d.cfg.min_presence_seconds == 4.0
    cam = d.cfg.cameras["cam1"]
    assert cam.drawn and cam.zone.polygon[0].x == 192.0     # 0.1 × 1920


def test_bad_arm_mode_is_rejected_live_and_at_load(tmp_path):
    d = _detector()
    d.on_config_update({"arm_mode": "sometimes"})
    assert d.cfg.arm_mode == "always"        # unchanged, edit ignored
    import yaml
    p = tmp_path / "c.yml"
    p.write_text(yaml.safe_dump({"nats_url": "nats://x", "arm_mode": "sometimes",
                                 "cameras": [{"camera_id": "cam1"}]}))
    with pytest.raises(ValueError):
        load_config(str(p))


def test_refresh_cameras_follows_catalog_picks():
    cfg = _config()
    cfg.cameras = {}
    cfg.auto_cameras = True
    d = _detector(cfg)
    added, removed = d.refresh_cameras([1, 4])
    assert added == ["cam1", "cam4"] and removed == []
    d.tick(BASE)
    assert _state(d, "cam4") == ARMED
    added, removed = d.refresh_cameras([1])
    assert removed == ["cam4"] and len(d.state_snapshot()["per_camera"]) == 1


def test_load_config_reads_zones_and_legacy_restricted_hours(tmp_path):
    import yaml
    p = tmp_path / "c.yml"
    p.write_text(yaml.safe_dump({
        "nats_url": "nats://x", "consume_tier0": True,
        "restricted_hours": {"start": "22:00", "end": "06:00"},   # pre-1.1 spelling
        "entry_delay_seconds": 20, "exit_delay_seconds": 45,
        "cameras": [{"camera_id": "cam1", "frame_width": 1000, "frame_height": 800,
                     "zone_name": "fence"}],
        "zones": {"cam1": [[0.2, 0.5], [0.8, 0.5], [0.8, 0.9]]},
    }))
    cfg = load_config(str(p))
    assert cfg.consume_tier0 is True
    assert cfg.armed_hours is not None and cfg.armed_hours.start == _dt.time(22, 0)
    assert cfg.entry_delay_seconds == 20.0 and cfg.exit_delay_seconds == 45.0
    zone = cfg.cameras["cam1"].zone
    assert zone.name == "fence" and (zone.polygon[0].x, zone.polygon[0].y) == (200.0, 400.0)


def test_load_config_catalog_mode_needs_no_cameras(tmp_path):
    import yaml
    p = tmp_path / "c.yml"
    p.write_text(yaml.safe_dump({"nats_url": "nats://x", "opennvr_url": "http://core"}))
    cfg = load_config(str(p))
    assert cfg.auto_cameras is True and cfg.cameras == {}


def test_load_config_rejects_missing_cameras(tmp_path):
    import yaml
    p = tmp_path / "c.yml"
    p.write_text(yaml.safe_dump({"nats_url": "nats://x"}))
    with pytest.raises(ValueError):
        load_config(str(p))
