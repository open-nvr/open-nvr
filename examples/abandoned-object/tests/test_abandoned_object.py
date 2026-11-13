# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the item lifecycle, the attendance rule, the alert policy,
the operator actions and config parsing."""
from __future__ import annotations

import datetime as _dt
import time as _time
from typing import Any

import pytest

import abandoned_object as ao
from abandoned_object import (
    ABANDONED,
    ESCALATED,
    MOVING,
    UNATTENDED,
    WITH_OWNER,
    AbandonedObjectDetector,
    AppConfig,
    CameraWatch,
    load_config,
)
from opennvr_app_sdk.geometry import Point, Zone

#: Event time and the wall clock are one timeline in these tests (the
#: unattended clock is real seconds), so the base is "now".
BASE = _time.time()


def _ts(seconds: float) -> str:
    dt = _dt.datetime.fromtimestamp(BASE + seconds, _dt.timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _bbox(cx: float, cy: float, h: float = 0.05, w: float = 0.05) -> dict[str, float]:
    return {"x": cx - w / 2, "y": cy - h / 2, "w": w, "h": h}


def _det(label: str, cx: float, cy: float, track: str, h: float = 0.05) -> dict[str, Any]:
    return {"label": label, "confidence": 0.9, "bbox": _bbox(cx, cy, h=h), "track_id": track}


def _bag(cx: float = 0.5, cy: float = 0.5, track: str = "b1", label: str = "backpack",
         h: float = 0.05) -> dict[str, Any]:
    return _det(label, cx, cy, track, h=h)


def _person(cx: float = 0.52, cy: float = 0.52, track: str = "p1") -> dict[str, Any]:
    return _det("person", cx, cy, track)


def _event(*dets: dict[str, Any], camera_id: str = "cam1", at: float = 0.0) -> dict[str, Any]:
    return {"correlation_id": "corr-1", "adapter": "yolov8", "adapter_version": "1",
            "camera_id": camera_id, "completed_at": _ts(at),
            "result": {"detections": list(dets)}}


def _camera(camera_id: str = "cam1") -> CameraWatch:
    zone = Zone.from_config("hall", [[0, 0], [1920, 0], [1920, 1080], [0, 1080]])
    return CameraWatch(camera_id=camera_id, zone=zone, frame_width=1920, frame_height=1080)


def _config(*cameras: CameraWatch, **knobs) -> AppConfig:
    cameras = cameras or (_camera(),)
    knobs.setdefault("settle_seconds", 0.0)
    knobs.setdefault("unattended_seconds", 30.0)
    knobs.setdefault("owner_grace_seconds", 5.0)
    knobs.setdefault("alert_cooldown_seconds", 0.0)
    knobs.setdefault("escalate_after_seconds", 0.0)
    knobs.setdefault("track_ttl_seconds", 120.0)
    return AppConfig(
        nats_url="nats://x:4222", nats_token=None, subject_pattern="opennvr.inference.>",
        object_labels=["backpack"], cameras={c.camera_id: c for c in cameras},
        webhook_url=None, attach_snapshot=False, **knobs,
    )


class _NullDispatcher:
    def __init__(self) -> None:
        self.fired: list[Any] = []

    def fire(self, alert):  # noqa: ANN001
        self.fired.append(alert)
        return {}


def _detector(cfg: AppConfig | None = None) -> AbandonedObjectDetector:
    return AbandonedObjectDetector(cfg or _config(), _NullDispatcher())


@pytest.fixture()
def clock(monkeypatch):
    """Drive the app's wall clock. ``clock(t)`` sets it and returns t, so
    a test can move both timelines with one call."""
    holder = {"t": BASE}
    monkeypatch.setattr(ao.time, "time", lambda: holder["t"])

    def _set(t: float) -> float:
        holder["t"] = t
        return t

    return _set


def _item(d: AbandonedObjectDetector, camera: str = "cam1", track: str = "b1"):
    rec = d._items.get((camera, track))
    return rec.data if rec is not None else None


def _state(d: AbandonedObjectDetector, camera: str = "cam1", track: str = "b1") -> str | None:
    data = _item(d, camera, track)
    return data["state"] if data else None


# ── Settling ─────────────────────────────────────────────────────────


def test_an_item_carried_past_the_camera_never_settles(clock):
    d = _detector(_config(settle_seconds=5.0))
    d.handle_event(_event(_bag(0.2, 0.2), at=0))
    clock(BASE + 2)
    assert d.handle_event(_event(_bag(0.5, 0.5), at=2)) == []
    clock(BASE + 4)
    assert d.handle_event(_event(_bag(0.8, 0.8), at=4)) == []
    assert _state(d) == MOVING


def test_settle_seconds_must_pass_before_the_item_counts(clock):
    d = _detector(_config(settle_seconds=10.0))
    d.handle_event(_event(_bag(), at=0))
    clock(BASE + 5)
    d.handle_event(_event(_bag(), at=5))
    assert _state(d) == MOVING
    assert d.state_snapshot()["today"]["items"] == 0
    clock(BASE + 11)
    d.handle_event(_event(_bag(), at=11))
    assert _state(d) == UNATTENDED
    assert d.state_snapshot()["today"]["items"] == 1


# ── Attendance ───────────────────────────────────────────────────────


def test_an_item_beside_its_owner_never_alerts(clock):
    d = _detector()
    for t in (0, 10, 20, 30, 40):
        clock(BASE + t)
        assert d.handle_event(_event(_bag(), _person(), at=t)) == []
    assert _state(d) == WITH_OWNER


def test_a_person_across_the_frame_is_not_attendance(clock):
    d = _detector(_config(owner_radius=0.05))
    clock(BASE)
    d.handle_event(_event(_bag(0.2, 0.2), _person(0.9, 0.9), at=0))
    assert _state(d) == UNATTENDED
    clock(BASE + 31)
    assert len(d.handle_event(_event(_bag(0.2, 0.2), _person(0.9, 0.9), at=31))) == 1


def test_the_owner_walking_away_starts_the_clock_and_alerts(clock):
    d = _detector()
    clock(BASE)
    d.handle_event(_event(_bag(), _person(), at=0))
    assert _state(d) == WITH_OWNER
    # Gone, but inside the grace window — still attended.
    clock(BASE + 3)
    assert d.handle_event(_event(_bag(), at=3)) == []
    assert _state(d) == WITH_OWNER
    # Past the grace window — alone from the moment they were last near.
    clock(BASE + 8)
    assert d.handle_event(_event(_bag(), at=8)) == []
    assert _state(d) == UNATTENDED
    clock(BASE + 31)
    fired = d.handle_event(_event(_bag(), at=31))
    assert len(fired) == 1
    assert fired[0].alert_type == "abandoned-object"
    assert _state(d) == ABANDONED


def test_the_alert_names_who_left_it(clock):
    d = _detector()
    clock(BASE)
    d.handle_event(_event(_bag(), _person(track="p7"), at=0))
    clock(BASE + 8)
    d.handle_event(_event(_bag(), at=8))
    clock(BASE + 31)
    alert = d.handle_event(_event(_bag(), at=31))[0]
    assert alert.evidence["owner_track"] == "p7"
    assert alert.evidence["owner_left_seconds_ago"] >= 30
    assert "track p7" in alert.description


def test_an_item_that_appears_alone_says_so(clock):
    d = _detector()
    clock(BASE)
    d.handle_event(_event(_bag(), at=0))
    clock(BASE + 31)
    alert = d.handle_event(_event(_bag(), at=31))[0]
    assert alert.evidence["owner_track"] is None
    assert "nobody was near it" in alert.description


def test_someone_coming_back_before_the_threshold_closes_the_countdown(clock):
    d = _detector()
    clock(BASE)
    d.handle_event(_event(_bag(), at=0))
    assert _state(d) == UNATTENDED
    clock(BASE + 20)
    d.handle_event(_event(_bag(), _person(), at=20))
    assert _state(d) == WITH_OWNER
    clock(BASE + 31)
    assert d.handle_event(_event(_bag(), _person(), at=31)) == []


# ── Reclaimed ────────────────────────────────────────────────────────


def test_moving_an_abandoned_item_reclaims_and_closes_it(clock):
    d = _detector()
    clock(BASE)
    d.handle_event(_event(_bag(), at=0))
    clock(BASE + 31)
    assert len(d.handle_event(_event(_bag(), at=31))) == 1
    clock(BASE + 40)
    assert d.handle_event(_event(_bag(0.9, 0.9), _person(0.9, 0.9), at=40)) == []
    assert _item(d) is None
    assert d.state_snapshot()["today"]["reclaimed"] == 1


def test_an_abandoned_item_that_vanishes_is_treated_as_taken(clock):
    d = _detector(_config(track_ttl_seconds=15.0))
    for t in (0, 10, 20, 30):
        clock(BASE + t)
        d.handle_event(_event(_bag(), at=t))
    assert _state(d) == ABANDONED
    # Nothing more is published for it: somebody walked off with it, or
    # a crowd is standing in the way. Either way it is no longer there
    # to dispatch a guard to.
    d.tick(clock(BASE + 50))
    assert _item(d) is None
    snap = d.state_snapshot()
    assert snap["today"]["reclaimed"] == 1
    assert snap["unattended_now"] == 0


def test_an_item_that_leaves_before_the_threshold_just_closes(clock):
    d = _detector(_config(track_ttl_seconds=5.0))
    clock(BASE)
    d.handle_event(_event(_bag(), at=0))
    assert _state(d) == UNATTENDED
    d.tick(clock(BASE + 12))
    assert _item(d) is None
    assert d.state_snapshot()["today"]["alerts"] == 0


# ── The Tier-0 silence ───────────────────────────────────────────────


def test_a_state_poll_that_crosses_the_threshold_still_sends_the_alert(clock):
    """/state advances the machine, so a page poll can be the call that
    crosses a threshold. The item is left abandoned either way, so an
    alert produced there has to be dispatched or it is lost for good."""
    d = _detector()
    clock(BASE)
    d.handle_event(_event(_bag(), at=0))
    clock(BASE + 31)
    snap = d.state_snapshot()
    assert snap["abandoned_now"] == 1
    assert [a.alert_type for a in d._dispatcher.fired] == ["abandoned-object"]
    # And no later tick re-sends it.
    assert d.tick(clock(BASE + 40)) == []
    assert len(d._dispatcher.fired) == 1


def test_an_escalation_holds_until_the_alert_hours_come_back(clock):
    now = _dt.datetime.fromtimestamp(BASE)
    start = (now + _dt.timedelta(hours=2)).time().replace(second=0, microsecond=0)
    end = (now + _dt.timedelta(hours=4)).time().replace(second=0, microsecond=0)
    d = _detector(_config(escalate_after_seconds=60.0))
    clock(BASE)
    d.handle_event(_event(_bag(), at=0))
    clock(BASE + 31)
    assert len(d.handle_event(_event(_bag(), at=31))) == 1
    # Hours close before the escalation is due: it must not be spent.
    d.cfg.active_hours = ao.ActiveHours(start, end)
    assert d.tick(clock(BASE + 95)) == []
    assert _item(d)["escalated"] is False
    d.cfg.active_hours = None
    esc = d.tick(clock(BASE + 100))
    assert len(esc) == 1 and esc[0].alert_type == "abandoned-object-escalated"
    assert _state(d) == ESCALATED


def test_the_sweep_alerts_when_no_further_events_arrive(clock):
    """Tier-0 publishes only frames with detections. An item alone in an
    empty hall produces nothing, so the wall-clock sweep has to be what
    crosses the threshold."""
    d = _detector()
    clock(BASE)
    d.handle_event(_event(_bag(), at=0))
    assert d.tick(clock(BASE + 20)) == []
    fired = d.tick(clock(BASE + 31))
    assert len(fired) == 1 and fired[0].alert_type == "abandoned-object"
    assert d.tick(clock(BASE + 45)) == []          # once, not per sweep


def test_out_of_order_events_are_ignored(clock):
    d = _detector()
    clock(BASE + 10)
    d.handle_event(_event(_bag(), at=10))
    before = dict(_item(d))
    clock(BASE + 11)
    d.handle_event(_event(_bag(), at=4))
    assert _item(d)["since"] == before["since"]


# ── Alert policy ─────────────────────────────────────────────────────


def test_one_alert_per_item(clock):
    d = _detector()
    clock(BASE)
    d.handle_event(_event(_bag(), at=0))
    clock(BASE + 31)
    assert len(d.handle_event(_event(_bag(), at=31))) == 1
    clock(BASE + 40)
    assert d.handle_event(_event(_bag(), at=40)) == []


def test_the_cooldown_merges_a_pile_of_bags_into_one_alert(clock):
    d = _detector(_config(alert_cooldown_seconds=60.0))
    clock(BASE)
    d.handle_event(_event(_bag(0.4, 0.4, "b1"), _bag(0.6, 0.6, "b2"), at=0))
    clock(BASE + 31)
    fired = d.handle_event(_event(_bag(0.4, 0.4, "b1"), _bag(0.6, 0.6, "b2"), at=31))
    assert len(fired) == 1
    # Both are still shown as abandoned — the page must not hide the second.
    snap = d.state_snapshot()
    assert snap["abandoned_now"] == 2 and snap["today"]["alerts"] == 1


def test_escalation_fires_once_and_ignores_the_cooldown(clock):
    d = _detector(_config(escalate_after_seconds=60.0, alert_cooldown_seconds=600.0))
    clock(BASE)
    d.handle_event(_event(_bag(), at=0))
    clock(BASE + 31)
    assert len(d.handle_event(_event(_bag(), at=31))) == 1
    assert d.tick(clock(BASE + 60)) == []
    esc = d.tick(clock(BASE + 95))
    assert len(esc) == 1
    assert esc[0].alert_type == "abandoned-object-escalated"
    assert esc[0].severity == "critical"
    assert d.tick(clock(BASE + 140)) == []
    assert _state(d) == ESCALATED


def test_acknowledging_stops_the_escalation_but_keeps_the_item(clock):
    d = _detector(_config(escalate_after_seconds=60.0))
    clock(BASE)
    d.handle_event(_event(_bag(), at=0))
    clock(BASE + 31)
    d.handle_event(_event(_bag(), at=31))
    d.on_action("acknowledge", {})
    assert d.tick(clock(BASE + 120)) == []
    assert _state(d) == ABANDONED
    assert d.state_snapshot()["items"][0]["acked"] is True


def test_a_merged_item_never_escalates_on_its_own(clock):
    """The second bag of a pile was folded into one alert; it must not
    reappear later as a critical with no first alert behind it."""
    d = _detector(_config(alert_cooldown_seconds=600.0, escalate_after_seconds=60.0))
    clock(BASE)
    d.handle_event(_event(_bag(0.4, 0.4, "b1"), _bag(0.6, 0.6, "b2"), at=0))
    clock(BASE + 31)
    assert len(d.handle_event(_event(_bag(0.4, 0.4, "b1"), _bag(0.6, 0.6, "b2"), at=31))) == 1
    esc = d.tick(clock(BASE + 95))
    assert len(esc) == 1
    assert esc[0].evidence["track_id"] == "b1"


def test_outside_active_hours_the_item_is_followed_but_quiet(clock):
    now = _dt.datetime.fromtimestamp(BASE)
    start = (now + _dt.timedelta(hours=2)).time().replace(second=0, microsecond=0)
    end = (now + _dt.timedelta(hours=4)).time().replace(second=0, microsecond=0)
    d = _detector(_config(active_hours=ao.ActiveHours(start, end)))
    clock(BASE)
    d.handle_event(_event(_bag(), at=0))
    clock(BASE + 31)
    assert d.handle_event(_event(_bag(), at=31)) == []
    snap = d.state_snapshot()
    assert snap["abandoned_now"] == 1                  # visible on the page
    assert snap["today"]["alerts"] == 0                # but nothing fired
    assert snap["alerts_active_now"] is False


# ── Filters ──────────────────────────────────────────────────────────


def test_size_filters_drop_litter_and_vehicles(clock):
    d = _detector(_config(min_bbox_height=0.04, max_bbox_height=0.30))
    clock(BASE)
    d.handle_event(_event(_bag(0.3, 0.3, "small", h=0.01),
                          _bag(0.6, 0.6, "huge", h=0.6),
                          _bag(0.5, 0.2, "ok", h=0.1), at=0))
    assert sorted(k[1] for k in dict(d._items.items())) == ["ok"]


def test_only_items_inside_the_zone_are_followed(clock):
    cam = CameraWatch(camera_id="cam1",
                      zone=Zone.from_config("bay", [[960, 0], [1920, 0], [1920, 540], [960, 540]]),
                      frame_width=1920, frame_height=1080)
    d = _detector(_config(cam))
    clock(BASE)
    d.handle_event(_event(_bag(0.1, 0.9, "outside"), _bag(0.8, 0.2, "inside"), at=0))
    assert sorted(k[1] for k in dict(d._items.items())) == ["inside"]


def test_other_classes_and_untracked_detections_are_ignored(clock):
    d = _detector()
    clock(BASE)
    d.handle_event(_event({"label": "backpack", "bbox": _bbox(0.5, 0.5)},         # no track
                          _det("chair", 0.3, 0.3, "c1"), at=0))
    assert dict(d._items.items()) == {}
    assert d._warned_missing_track is True


def test_events_from_an_unpicked_camera_are_ignored(clock):
    d = _detector()
    clock(BASE)
    assert d.handle_event(_event(_bag(), camera_id="cam9", at=0)) == []
    assert dict(d._items.items()) == {}


# ── Fixtures ─────────────────────────────────────────────────────────


def test_a_configured_fixture_is_never_followed(clock):
    cam = _camera()
    cam.fixtures = [Point(960, 540)]
    d = _detector(_config(cam, fixture_radius=0.05))
    clock(BASE)
    d.handle_event(_event(_bag(0.5, 0.5), at=0))
    assert dict(d._items.items()) == {}


def test_marking_a_fixture_silences_that_spot(clock):
    d = _detector(_config(fixture_radius=0.05))
    clock(BASE)
    d.handle_event(_event(_bag(), at=0))
    clock(BASE + 31)
    assert len(d.handle_event(_event(_bag(), at=31))) == 1
    out = d.on_action("mark_fixture", {"camera": "cam1", "track": "b1"})
    assert out["fixtures"] == 1 and _item(d) is None
    # The bin is back next frame and stays out of the list for good.
    clock(BASE + 40)
    assert d.handle_event(_event(_bag(), at=40)) == []
    clock(BASE + 200)
    assert d.handle_event(_event(_bag(), at=200)) == []
    assert dict(d._items.items()) == {}
    d.on_action("clear_fixtures", {})
    clock(BASE + 210)
    d.handle_event(_event(_bag(), at=210))
    assert _state(d) == UNATTENDED


def test_a_config_edit_does_not_forget_spots_marked_on_the_page(clock):
    """``fixtures`` is per camera, and the catalog never sends the key at
    all (it is not a manifest param) — so a live config edit leaves an
    operator's marked spots standing. Clearing them is an action."""
    d = _detector(_config(fixture_radius=0.05))
    clock(BASE)
    d.handle_event(_event(_bag(), at=0))
    d.on_action("mark_fixture", {"camera": "cam1", "track": "b1"})
    assert len(d.cfg.cameras["cam1"].fixtures) == 1
    d.on_config_update({"fixtures": {}})
    assert len(d.cfg.cameras["cam1"].fixtures) == 1
    d.on_config_update({"unattended_seconds": 90})
    assert len(d.cfg.cameras["cam1"].fixtures) == 1
    # An explicit empty list for a camera IS "none here".
    d.on_config_update({"fixtures": {"cam1": []}})
    assert d.cfg.cameras["cam1"].fixtures == []


def test_fixtures_can_be_configured_per_camera_in_either_key_style():
    cams = {"cam3": _camera("cam3")}
    ao._fixtures_from({"3": [[0.5, 0.5]]}, cams)
    assert [(p.x, p.y) for p in cams["cam3"].fixtures] == [(960.0, 540.0)]
    ao._fixtures_from({"cam3": [{"x": 0.25, "y": 0.5}]}, cams)
    assert [(p.x, p.y) for p in cams["cam3"].fixtures] == [(480.0, 540.0)]


# ── Actions ──────────────────────────────────────────────────────────


def test_acknowledge_one_item_only(clock):
    d = _detector()
    clock(BASE)
    d.handle_event(_event(_bag(0.4, 0.4, "b1"), _bag(0.6, 0.6, "b2"), at=0))
    clock(BASE + 31)
    d.handle_event(_event(_bag(0.4, 0.4, "b1"), _bag(0.6, 0.6, "b2"), at=31))
    d.on_action("acknowledge", {"camera": "cam1", "track": "b1"})
    assert _item(d, track="b1")["acked"] is True
    assert _item(d, track="b2")["acked"] is False


def test_resolve_closes_the_item(clock):
    d = _detector()
    clock(BASE)
    d.handle_event(_event(_bag(), at=0))
    out = d.on_action("resolve", {"camera": "cam1", "track": "b1"})
    assert out["ok"] is True and _item(d) is None
    assert d.state_snapshot()["today"]["reclaimed"] == 1


def test_actions_reject_what_they_cannot_find(clock):
    d = _detector()
    clock(BASE)
    d.handle_event(_event(_bag(), at=0))
    with pytest.raises(KeyError):
        d.on_action("resolve", {"camera": "cam9", "track": "b1"})
    with pytest.raises(KeyError):
        d.on_action("resolve", {"camera": "cam1", "track": "nope"})
    with pytest.raises(KeyError):
        d.on_action("clear_fixtures", {"camera": "cam9"})
    with pytest.raises(KeyError):
        d.on_action("no-such-action", {})


# ── Surfaces ─────────────────────────────────────────────────────────


def test_state_snapshot_shape(clock):
    cam2 = _camera("cam2")
    cam2.drawn = False
    d = _detector(_config(_camera(), cam2))
    clock(BASE)
    d.handle_event(_event(_bag(), _person(), at=0))
    d.handle_event(_event(_bag(0.2, 0.2, "b2"), at=0))
    clock(BASE + 20)
    snap = d.state_snapshot()
    assert snap["camera_count"] == 2
    assert snap["needs_zone"] == ["cam2"]
    assert snap["unattended_seconds"] == 30.0
    assert snap["attended_now"] == 1
    assert snap["unattended_now"] == 1
    assert snap["items"][0]["track"] == "b2"           # sorted, longest alone first
    assert 0 < snap["items"][0]["progress"] <= 1
    row = next(r for r in snap["per_camera"] if r["camera"] == "cam1")
    assert row["items"] == 2 and row["zone"] == "hall"
    assert snap["recent"] and snap["since"] > 0


def test_ui_html_renders_the_items_and_the_policy(clock):
    d = _detector()
    clock(BASE)
    d.handle_event(_event(_bag(), at=0))
    html = d.ui_html()
    assert "Abandoned Object" in html
    assert "unattended" in html
    assert "backpack" in html
    assert "alerts around the clock" in html


def test_ui_html_survives_an_empty_deployment():
    d = _detector(_config())
    d.cfg.cameras = {}
    html = d.ui_html()
    assert "No cameras selected" in html
    assert "Nothing on the floor" in html


# ── Live config + cameras ────────────────────────────────────────────


def test_config_update_applies_knobs_labels_and_zones_live(clock):
    d = _detector()
    d.on_config_update({
        "object_labels": ["suitcase"],
        "unattended_seconds": 120,
        "owner_radius": 0.3,
        "active_hours": {"start": "22:00", "end": "06:00"},
        "zones": {"cam1": [[0.5, 0.0], [1.0, 0.0], [1.0, 0.5], [0.5, 0.5]]},
    })
    assert d.cfg.object_labels == ["suitcase"]
    assert d.cfg.unattended_seconds == 120.0
    assert d.cfg.owner_radius == 0.3
    assert d.cfg.active_hours.start == _dt.time(22, 0)
    cam = d.cfg.cameras["cam1"]
    assert cam.drawn is True
    assert not cam.zone.contains(Point(100, 900))
    # A bad edit is refused wholesale rather than half-applied.
    d.on_config_update({"unattended_seconds": "soon"})
    assert d.cfg.unattended_seconds == 120.0


def test_config_update_with_no_zone_falls_back_to_the_whole_frame():
    d = _detector()
    d.on_config_update({"zones": {"cam1": [[0.5, 0.0], [1.0, 0.0], [1.0, 0.5]]}})
    assert d.cfg.cameras["cam1"].drawn is True
    d.on_config_update({"zones": {}})
    cam = d.cfg.cameras["cam1"]
    assert cam.drawn is False and cam.zone.contains(Point(10, 10))


def test_picked_cameras_appear_and_disappear(clock):
    d = _detector(_config(auto_cameras=True))
    d.cfg.cameras = {}
    added, removed = d.refresh_cameras([3, 4])
    assert added == ["cam3", "cam4"] and removed == []
    clock(BASE)
    d.handle_event(_event(_bag(), camera_id="cam3", at=0))
    assert _state(d, "cam3") == UNATTENDED
    added, removed = d.refresh_cameras([4])
    assert added == [] and removed == ["cam3"]
    assert dict(d._items.items()) == {}
    assert d.state_snapshot()["needs_zone"] == ["cam4"]


# ── Config parsing ───────────────────────────────────────────────────


def _write(tmp_path, body: str) -> str:
    path = tmp_path / "config.yml"
    path.write_text(body)
    return str(path)


def test_load_config_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, """
nats_url: "nats://x:4222"
opennvr_url: "http://core:8000"
"""))
    assert cfg.object_labels == ["backpack", "handbag", "suitcase"]
    assert cfg.unattended_seconds == 60.0
    assert cfg.settle_seconds == 5.0
    assert cfg.move_tolerance == 0.02
    assert cfg.owner_radius == 0.15
    assert cfg.auto_cameras is True
    assert cfg.cameras == {}


def test_load_config_requires_a_bus_and_somewhere_to_get_cameras(tmp_path):
    with pytest.raises(ValueError, match="nats_url"):
        load_config(_write(tmp_path, "object_labels: [backpack]\n"))
    with pytest.raises(ValueError, match="camera entry"):
        load_config(_write(tmp_path, 'nats_url: "nats://x:4222"\n'))


def test_load_config_rejects_an_empty_label_list(tmp_path):
    with pytest.raises(ValueError, match="object_labels"):
        load_config(_write(tmp_path, """
nats_url: "nats://x:4222"
opennvr_url: "http://core:8000"
object_labels: []
"""))


def test_load_config_reads_cameras_zones_and_fixtures(tmp_path):
    cfg = load_config(_write(tmp_path, """
nats_url: "nats://x:4222"
unattended_seconds: 45
settle_seconds: 2
active_hours: {start: "20:00", end: "05:00"}
fixtures:
  cam-hall: [[0.5, 0.5]]
cameras:
  - camera_id: "cam-hall"
    frame_width: 1280
    frame_height: 720
    zone_name: "concourse"
    zone: [[100, 100], [1000, 100], [1000, 600], [100, 600]]
"""))
    cam = cfg.cameras["cam-hall"]
    assert cam.zone.name == "concourse" and cam.drawn is True
    assert cfg.unattended_seconds == 45.0 and cfg.settle_seconds == 2.0
    assert cfg.active_hours.end == _dt.time(5, 0)
    assert [(p.x, p.y) for p in cam.fixtures] == [(640.0, 360.0)]
    assert cfg.auto_cameras is False


def test_load_config_accepts_the_old_pixel_knobs(tmp_path):
    """The 1.0 config spoke in pixels on a 1920-wide frame. Same scene,
    same behaviour, without hand-editing every deployment."""
    cfg = load_config(_write(tmp_path, """
nats_url: "nats://x:4222"
opennvr_url: "http://core:8000"
dwell_seconds: 90
move_tolerance_px: 38.4
person_radius_px: 288
"""))
    assert cfg.unattended_seconds == 90.0
    assert cfg.move_tolerance == pytest.approx(0.02)
    assert cfg.owner_radius == pytest.approx(0.15)


def test_load_config_rejects_malformed_values(tmp_path):
    with pytest.raises(ValueError, match="active_hours"):
        load_config(_write(tmp_path, """
nats_url: "nats://x:4222"
opennvr_url: "http://core:8000"
active_hours: {start: "half past eight", end: "05:00"}
"""))
    with pytest.raises(ValueError, match="alert_severity"):
        load_config(_write(tmp_path, """
nats_url: "nats://x:4222"
opennvr_url: "http://core:8000"
alert_severity: "urgent"
"""))
    with pytest.raises(ValueError, match="numeric knob"):
        load_config(_write(tmp_path, """
nats_url: "nats://x:4222"
opennvr_url: "http://core:8000"
unattended_seconds: "a minute"
"""))


def test_manifest_matches_the_app(tmp_path):
    m = ao.MANIFEST
    assert m.version == "1.1.0"
    assert m.provides == ["left_items"]
    assert "multi_object_tracking" in m.requires_tasks
    assert [a.name for a in m.emits] == ["abandoned-object", "abandoned-object-escalated"]
    assert {a.name for a in m.actions} == {"acknowledge", "resolve", "mark_fixture",
                                          "clear_fixtures"}
    assert ESCALATED == "escalated"
