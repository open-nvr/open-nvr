"""Tests for the doorstep state machine, the counting-skill choice, the
who-took-it evidence, reminders, the actions and config parsing.

The counting skill is faked at the seam the app uses for it: a fake
platform client whose ``ai.infer`` answers what a test says the doorstep
holds. Nothing here touches the network.
"""
from __future__ import annotations

import datetime as _dt
import time as _time
from typing import Any

import pytest

import package_delivery as pd
from opennvr_app_sdk.geometry import Zone
from package_delivery import (
    CLEAR,
    COURIER,
    DELIVERED,
    KNOWN,
    METHOD_NONE,
    METHOD_OBJECT,
    METHOD_PACKAGE,
    METHOD_PROXY,
    METHOD_VQA,
    NOBODY,
    PICKED_UP,
    REMINDER_DUE,
    SNOOZED,
    STRANGER,
    TAKEN,
    UNKNOWN,
    WAITING,
    AppConfig,
    CameraWatch,
    Count,
    Counter,
    DailyHours,
    PackageDeliveryDetector,
    load_config,
    parse_count,
)

BASE = _time.time()


def _ts(seconds: float) -> str:
    dt = _dt.datetime.fromtimestamp(BASE + seconds, _dt.timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _bbox(cx: float, cy: float, h: float = 0.05, w: float = 0.05) -> dict[str, float]:
    return {"x": cx - w / 2, "y": cy - h / 2, "w": w, "h": h}


def _det(label: str, cx: float, cy: float, track: str | None, **extra) -> dict[str, Any]:
    d = {"label": label, "confidence": 0.9, "bbox": _bbox(cx, cy), "track_id": track}
    d.update(extra)
    return d


def _person(cx: float = 0.5, cy: float = 0.6, track: str = "p1") -> dict[str, Any]:
    return _det("person", cx, cy, track)


def _truck(track: str = "v1") -> dict[str, Any]:
    return _det("truck", 0.1, 0.9, track)


def _bag(track: str = "b1", cx: float = 0.5, cy: float = 0.6) -> dict[str, Any]:
    return _det("suitcase", cx, cy, track, stationary=True)


def _event(*dets: dict[str, Any], camera_id: str = "cam1", at: float = 0.0) -> dict[str, Any]:
    return {"correlation_id": "corr-1", "adapter": "yolov8", "adapter_version": "1",
            "camera_id": camera_id, "completed_at": _ts(at),
            "result": {"detections": list(dets)}}


def _camera(camera_id: str = "cam1", drawn: bool = True) -> CameraWatch:
    # The porch is the lower middle of the frame; the road is the bottom-left.
    zone = Zone.from_config("porch", [[600, 500], [1300, 500], [1300, 900], [600, 900]])
    return CameraWatch(camera_id=camera_id, zone=zone, frame_width=1920, frame_height=1080,
                       drawn=drawn)


def _config(*cameras: CameraWatch, **knobs) -> AppConfig:
    cameras = cameras or (_camera(),)
    knobs.setdefault("settle_seconds", 0.0)
    knobs.setdefault("alert_cooldown_seconds", 0.0)
    knobs.setdefault("track_ttl_seconds", 5.0)
    knobs.setdefault("reminder_minutes", 60.0)
    knobs.setdefault("delivery_hours", None)      # tests pin hours explicitly
    knobs.setdefault("consume_tier0", True)
    knobs.setdefault("attach_snapshot", False)
    return AppConfig(
        nats_url="nats://x:4222", nats_token=None, subject_pattern="opennvr.inference.>",
        cameras={c.camera_id: c for c in cameras}, webhook_url=None, **knobs,
    )


class _NullDispatcher:
    def __init__(self) -> None:
        self.fired: list[Any] = []

    def fire(self, alert):  # noqa: ANN001
        self.fired.append(alert)
        return {}


class _FakeAI:
    """A KAI-C stand-in: answers whatever the test put on the doorstep."""

    def __init__(self, caps: dict[str, Any] | None) -> None:
        self.caps = caps
        self.answer: Any = 0
        self.calls: list[dict[str, Any]] = []
        self.fail = False

    def capabilities(self):
        return self.caps

    def infer(self, adapter, jpeg, *, task, camera_id=None, params=None, correlation_id=None):
        self.calls.append({"adapter": adapter, "task": task, "camera_id": camera_id,
                           "params": params})
        if self.fail:
            raise RuntimeError("adapter down")
        if task in ("vqa", "visual_qa", "visual_question_answering"):
            return {"result": {"answer": self.answer}}
        return {"result": {"detections": self.answer}}


class _FakePlatform:
    def __init__(self, caps: dict[str, Any] | None) -> None:
        self.ai = _FakeAI(caps)
        self.snapshots = 0
        self.mode: dict[str, Any] = {"mode": "armed_away", "changed_at": None}

    def snapshot(self, camera):
        self.snapshots += 1
        return b"\xff\xd8jpeg"

    def save_evidence(self, jpeg):
        return f"evidence/{self.snapshots}.jpg"

    def site_mode(self):
        return self.mode


VQA_CAPS = {"adapters": {"moondream": {"capabilities": {"tasks_advertised": ["vqa", "scene_caption"]}}}}
PKG_CAPS = {"adapters": [{"name": "parcel-net", "tasks_advertised": ["package_detection"],
                          "labels": ["package"]}]}
BOX_CAPS = {"adapters": {"oiv7": {"tasks_advertised": ["object_detection"],
                                  "labels": ["person", "box", "car"]}}}
COCO_CAPS = {"adapters": {"default": {"tasks_advertised": ["object_detection"],
                                      "labels": ["person", "car", "suitcase"]}}}


def _app(cfg: AppConfig | None = None, caps: Any = VQA_CAPS, hours=None):
    d = PackageDeliveryDetector(cfg or _config(), _NullDispatcher())
    d._nvr, d._nvr_tried = _FakePlatform(caps), True
    d.refresh_counter(BASE, capabilities=caps if caps is not None else {})
    # Nothing waiting at the start of a test unless it says so.
    for door in d._doors.values():
        door.pending_check = None
    if hours is not None:
        d.cfg.delivery_hours = hours
    return d


@pytest.fixture()
def clock(monkeypatch):
    holder = {"t": BASE}
    monkeypatch.setattr(pd.time, "time", lambda: holder["t"])

    def _set(t: float) -> float:
        holder["t"] = t
        return t

    return _set


def _door(d, cam="cam1"):
    return d._doors[cam]


def _deliver(d, clock, at: float = 0.0, track: str = "p1", with_truck: bool = True,
             answer: str = "There is 1 package."):
    """A courier walks up (with a truck outside), leaves, and the count
    goes up. Returns the alerts the check fired."""
    clock(BASE + at)
    dets = [_person(track=track)] + ([_truck()] if with_truck else [])
    d.handle_event(_event(*dets, at=at))
    clock(BASE + at + 3)
    d.handle_event(_event(*([_truck()] if with_truck else []), at=at + 3))  # person gone
    d._nvr.ai.answer = answer
    return d.tick(BASE + at + 3.5)


# ── Choosing how to count ────────────────────────────────────────────


def test_the_best_registered_skill_counts_parcels():
    c = Counter()
    c.choose(PKG_CAPS, BASE)
    assert (c.method, c.adapter) == (METHOD_PACKAGE, "parcel-net")
    c.choose(BOX_CAPS, BASE)
    assert (c.method, c.adapter, c.labels) == (METHOD_OBJECT, "oiv7", ("box",))
    c.choose(VQA_CAPS, BASE)
    assert (c.method, c.adapter) == (METHOD_VQA, "moondream")
    # A COCO detector has no box class: the Tier-0 bags stand in.
    c.choose(COCO_CAPS, BASE)
    assert c.method == METHOD_PROXY
    c.choose(None, BASE)
    assert c.method == METHOD_PROXY


def test_an_unhealthy_adapter_is_not_chosen():
    caps = {"adapters": {"parcel-net": {"tasks_advertised": ["package_detection"],
                                        "healthy": False},
                         "moondream": {"tasks_advertised": ["vqa"]}}}
    c = Counter()
    c.choose(caps, BASE)
    assert c.method == METHOD_VQA


def test_the_proxy_needs_the_tier0_stream():
    d = _app(_config(consume_tier0=False), caps=COCO_CAPS)
    assert d._counter.method == METHOD_NONE
    assert d.state_snapshot()["counted_by"]["quality"] == "none"


def test_the_page_is_told_how_parcels_are_counted():
    d = _app()
    note = d.state_snapshot()["counted_by"]
    assert note["method"] == METHOD_VQA and note["quality"] == "fair"
    assert "moondream" in note["note"] and "every 15 min" in note["note"]


@pytest.mark.parametrize("answer, n", [
    ("2", 2), ("There are two boxes on the step.", 2), ("No packages.", 0),
    ("A single parcel.", 1), ("0", 0), ("", None), ("I cannot tell.", None), (3, 3),
])
def test_a_vqa_answer_is_read_as_a_number_or_not_at_all(answer, n):
    assert parse_count(answer) == n


# ── Delivered ────────────────────────────────────────────────────────


def test_a_courier_leaving_triggers_one_settled_check(clock):
    d = _app(_config(settle_seconds=4.0))
    clock(BASE)
    d.handle_event(_event(_person(), _truck(), at=0))
    assert _door(d).pending_check is None            # still at the door
    clock(BASE + 2)
    d.handle_event(_event(_truck(), at=2))            # person gone from the frame
    due, reason = _door(d).pending_check
    assert reason == "person-left" and due == pytest.approx(BASE + 6)
    # Not before it is due, and only once.
    assert d.tick(BASE + 4) == []
    d._nvr.ai.answer = "1"
    d.tick(BASE + 6)
    assert _door(d).pending_check is None and d._nvr.snapshots == 1


def test_a_count_going_up_is_a_delivery_by_the_courier(clock):
    d = _app()
    fired = _deliver(d, clock)
    door = _door(d)
    assert door.count == 1 and door.present_since == pytest.approx(BASE + 3.5)
    assert len(fired) == 1 and fired[0].alert_type == pd.EVENT_DELIVERED
    ev = d.state_snapshot()["events"][0]
    assert ev["kind"] == DELIVERED and ev["who"]["kind"] == COURIER
    assert ev["count_before"] == 0 and ev["count_after"] == 1 and ev["vehicle"] == "truck"
    assert any("truck stopped" in r for r in ev["who"]["reasons"])
    assert d.state_snapshot()["today"]["delivered"] == 1


def test_a_person_who_never_reached_the_porch_is_not_a_courier(clock):
    d = _app()
    clock(BASE)
    d.handle_event(_event(_person(cx=0.1, cy=0.2, track="walker"), at=0))   # on the pavement
    clock(BASE + 3)
    d.handle_event(_event(at=3))
    assert _door(d).pending_check is None


def test_a_delivery_carries_before_and_after_photos(clock):
    d = _app(_config(attach_snapshot=True))
    d.tick(BASE)                                     # a startup check: the "before"
    _door(d).pending_check = (BASE, "startup")
    d._nvr.ai.answer = "0"
    d.tick(BASE)
    fired = _deliver(d, clock, at=10)
    assert fired and set(fired[0].images) == {"before", "after"}


# ── Collected, or taken ──────────────────────────────────────────────


def test_a_known_face_collecting_is_an_owner_pickup(clock):
    d = _app()
    _deliver(d, clock)
    clock(BASE + 600)
    d.note_known_visitor("cam1", "Ravi", BASE + 590)
    d.handle_event(_event(_person(track="p9"), at=600))
    clock(BASE + 603)
    d.handle_event(_event(at=603))
    d._nvr.ai.answer = "0"
    fired = d.tick(BASE + 604)
    ev = d.state_snapshot()["events"][0]
    assert ev["kind"] == PICKED_UP and ev["who"] == {
        "kind": KNOWN, "name": "Ravi", "reasons": ["known face: Ravi"]}
    assert ev["dwell_seconds"] == pytest.approx(600.5, abs=1)
    assert fired[0].alert_type == pd.EVENT_PICKED_UP and fired[0].severity == "low"
    assert _door(d).count == 0 and d.state_snapshot()["today"]["picked_up"] == 1


def test_the_courier_taking_it_straight_back_is_a_misdelivery_not_a_theft(clock):
    d = _app(_config(courier_grace_seconds=120.0))
    _deliver(d, clock, track="courier")
    clock(BASE + 30)
    d.handle_event(_event(_person(track="courier"), at=30))   # came back for it
    clock(BASE + 33)
    d.handle_event(_event(at=33))
    d._nvr.ai.answer = "0"
    d.tick(BASE + 34)
    ev = d.state_snapshot()["events"][0]
    assert ev["kind"] == PICKED_UP and ev["who"]["kind"] == COURIER
    assert ev["who"]["reasons"] == ["the same person who brought it took it back"]


def test_a_stranger_after_hours_is_taken_high(clock):
    d = _app(hours=DailyHours(_dt.time(8, 0), _dt.time(20, 0)))
    d._now_local = lambda: _dt.datetime(2026, 9, 20, 12, 0)
    _deliver(d, clock)
    d._now_local = lambda: _dt.datetime(2026, 9, 20, 23, 30)
    clock(BASE + 3600)
    d.handle_event(_event(_person(track="p5"), at=3600))
    clock(BASE + 3603)
    d.handle_event(_event(at=3603))
    d._nvr.ai.answer = "none"
    fired = d.tick(BASE + 3604)
    ev = d.state_snapshot()["events"][0]
    assert ev["kind"] == TAKEN and ev["severity"] == "high"
    assert ev["who"]["kind"] == STRANGER
    assert "outside delivery hours" in ev["who"]["reasons"]
    assert "no known face matched" in ev["who"]["reasons"]
    assert fired[0].alert_type == pd.EVENT_TAKEN and fired[0].severity == "high"
    assert d.state_snapshot()["today"]["taken"] == 1


def test_the_quick_grab_after_the_van_is_taken_even_in_hours(clock):
    d = _app(hours=DailyHours(_dt.time(8, 0), _dt.time(20, 0)))
    d._now_local = lambda: _dt.datetime(2026, 9, 20, 12, 0)
    _deliver(d, clock, track="courier")
    clock(BASE + 200)
    d.handle_event(_event(_person(track="someone-else"), at=200))
    clock(BASE + 203)
    d.handle_event(_event(at=203))
    d._nvr.ai.answer = "0"
    d.tick(BASE + 204)
    ev = d.state_snapshot()["events"][0]
    assert ev["kind"] == TAKEN and ev["who"]["kind"] == STRANGER
    assert any(r.startswith("taken 3 min after delivery") for r in ev["who"]["reasons"])


def test_a_pickup_with_no_evidence_either_way_is_unknown_not_theft(clock):
    d = _app(hours=DailyHours(_dt.time(8, 0), _dt.time(20, 0)))
    d._now_local = lambda: _dt.datetime(2026, 9, 20, 12, 0)
    _deliver(d, clock)
    clock(BASE + 3600)
    d.handle_event(_event(_person(track="p7"), at=3600))
    clock(BASE + 3603)
    d.handle_event(_event(at=3603))
    d._nvr.ai.answer = "0"
    fired = d.tick(BASE + 3604)
    ev = d.state_snapshot()["events"][0]
    assert ev["kind"] == PICKED_UP and ev["who"]["kind"] == UNKNOWN
    assert ev["severity"] == "low" and fired[0].severity == "low"


def test_a_site_armed_away_turns_an_unknown_pickup_into_taken(clock):
    d = _app(hours=DailyHours(_dt.time(8, 0), _dt.time(20, 0)))
    d._now_local = lambda: _dt.datetime(2026, 9, 20, 12, 0)
    _deliver(d, clock)
    d._nvr.mode = {"mode": "armed_away", "changed_at": "2026-09-20T09:00:00Z"}
    clock(BASE + 3600)
    d.handle_event(_event(_person(track="p7"), at=3600))
    clock(BASE + 3603)
    d.handle_event(_event(at=3603))
    d._nvr.ai.answer = "0"
    d.tick(BASE + 3604)
    ev = d.state_snapshot()["events"][0]
    assert ev["kind"] == TAKEN and "the site is armed away" in ev["who"]["reasons"]


def test_a_mode_nobody_ever_set_is_not_evidence(clock):
    """The platform's default mode is armed_away. That says nothing about
    anybody being out, so it must not tip a pick-up into theft."""
    d = _app(hours=DailyHours(_dt.time(8, 0), _dt.time(20, 0)))
    d._now_local = lambda: _dt.datetime(2026, 9, 20, 12, 0)
    _deliver(d, clock)
    d._nvr.mode = {"mode": "armed_away", "changed_at": None}
    clock(BASE + 3600)
    d.handle_event(_event(_person(track="p7"), at=3600))
    clock(BASE + 3603)
    d.handle_event(_event(at=3603))
    d._nvr.ai.answer = "0"
    d.tick(BASE + 3604)
    ev = d.state_snapshot()["events"][0]
    assert ev["kind"] == PICKED_UP and not any("armed" in r for r in ev["who"]["reasons"])


def test_a_parcel_that_vanishes_with_nobody_seen_is_reported_gently(clock):
    d = _app()
    _deliver(d, clock)
    # A scheduled re-count finds it gone; nobody walked past the camera.
    clock(BASE + 3.5 + 15 * 60)
    d._nvr.ai.answer = "0"
    fired = d.tick(BASE + 3.5 + 15 * 60)
    ev = d.state_snapshot()["events"][0]
    assert ev["kind"] == PICKED_UP and ev["who"]["kind"] == NOBODY
    assert fired and fired[0].severity == "low"
    assert _door(d).last_check_reason == "scheduled"


def test_a_count_that_could_not_be_made_changes_nothing(clock):
    d = _app()
    _deliver(d, clock)
    _door(d).pending_check = (BASE + 100, "action")
    d._nvr.ai.fail = True
    assert d.tick(BASE + 100) == []
    assert _door(d).count == 1 and d.state_snapshot()["events"][0]["kind"] == DELIVERED
    d._nvr.ai.fail = False
    _door(d).pending_check = (BASE + 101, "action")
    d._nvr.ai.answer = "I cannot tell."
    assert d.tick(BASE + 101) == [] and _door(d).count == 1


# ── Reminders ────────────────────────────────────────────────────────


def test_reminders_come_on_the_cadence_until_collected(clock):
    d = _app(_config(reminder_minutes=30.0, recheck_minutes=0.0))
    _deliver(d, clock)
    assert d.tick(BASE + 3.5 + 29 * 60) == []
    fired = d.tick(BASE + 3.5 + 30 * 60)
    assert len(fired) == 1 and fired[0].alert_type == pd.EVENT_REMINDER
    assert "still waiting" in fired[0].title
    assert d.state_snapshot()["per_camera"][0]["state"] == WAITING
    assert d.tick(BASE + 3.5 + 45 * 60) == []
    assert d.tick(BASE + 3.5 + 60 * 60)[0].alert_type == pd.EVENT_REMINDER
    assert d.state_snapshot()["today"]["reminders"] == 2


def test_acknowledge_and_snooze_stop_reminders(clock):
    d = _app(_config(reminder_minutes=30.0, recheck_minutes=0.0))
    _deliver(d, clock)
    d.on_action("snooze", {"camera": "cam1", "minutes": 90})
    assert d.state_snapshot()["per_camera"][0]["state"] == SNOOZED
    assert d.tick(BASE + 3.5 + 60 * 60) == []
    assert d.tick(BASE + 3.5 + 95 * 60)[0].alert_type == pd.EVENT_REMINDER
    d.on_action("acknowledge", {"camera": "cam1"})
    assert d.tick(BASE + 3.5 + 200 * 60) == []
    assert d.state_snapshot()["per_camera"][0]["state"] == "acknowledged"
    # The next delivery starts them again.
    _deliver(d, clock, at=300 * 60, track="p2", answer="2")
    assert _door(d).acked is False and _door(d).count == 2


def test_the_page_shows_a_reminder_that_is_due(clock):
    d = _app(_config(reminder_minutes=30.0, recheck_minutes=0.0))
    _deliver(d, clock)
    _door(d).next_reminder = BASE + 100        # due, not yet sent
    clock(BASE + 200)
    # /state runs the sweep: the reminder goes out and is dispatched.
    snap = d.state_snapshot()
    assert snap["per_camera"][0]["state"] == WAITING
    assert d._dispatcher.fired and d._dispatcher.fired[-1].alert_type == pd.EVENT_REMINDER


# ── Re-counts ────────────────────────────────────────────────────────


def test_recounts_are_scheduled_while_parcels_wait_and_idle_otherwise(clock):
    d = _app(_config(recheck_minutes=15.0, idle_recheck_minutes=60.0))
    _door(d).pending_check = (BASE, "startup")
    d._nvr.ai.answer = "0"
    d.tick(BASE)
    assert d.tick(BASE + 59 * 60) == [] and _door(d).pending_check is None
    d.tick(BASE + 60 * 60)
    assert _door(d).last_check == pytest.approx(BASE + 60 * 60)
    assert _door(d).last_check_reason == "scheduled"
    _deliver(d, clock, at=61 * 60)
    d._nvr.ai.answer = "1"
    d.tick(BASE + 61 * 60 + 3.5 + 15 * 60)
    assert _door(d).last_check == pytest.approx(BASE + 61 * 60 + 3.5 + 15 * 60)


def test_check_now_counts_immediately(clock):
    d = _app()
    d.on_action("check_now", {"camera": "cam1"})
    d._nvr.ai.answer = "2"
    d.tick(BASE + 1)
    assert _door(d).count == 2 and _door(d).last_check_reason == "action"
    assert d.state_snapshot()["events"][0]["who"]["kind"] == UNKNOWN


# ── The COCO stand-in ────────────────────────────────────────────────


def test_without_a_package_skill_the_tier0_bags_stand_in(clock):
    d = _app(caps=COCO_CAPS)
    assert d._counter.method == METHOD_PROXY
    clock(BASE)
    d.handle_event(_event(_person(track="p1"), at=0))
    clock(BASE + 3)
    d.handle_event(_event(_bag("b1"), at=3))       # person gone, a bag on the step
    fired = d.tick(BASE + 4)
    assert _door(d).count == 1 and fired[0].alert_type == pd.EVENT_DELIVERED
    assert d.state_snapshot()["events"][0]["method"] == METHOD_PROXY
    assert d._nvr.snapshots == 0                   # no frame, no model call
    # The bag leaves with someone.
    clock(BASE + 100)
    d.handle_event(_event(_person(track="p2"), at=100))
    clock(BASE + 110)
    d.handle_event(_event(at=110))
    d.tick(BASE + 111)
    assert _door(d).count == 0 and d.state_snapshot()["events"][0]["kind"] == PICKED_UP


# ── Counting with a detector ─────────────────────────────────────────


def test_a_box_detector_counts_only_inside_the_porch(clock):
    d = _app(caps=BOX_CAPS)
    d._nvr.ai.answer = [
        {"label": "box", "confidence": 0.8, "bbox": _bbox(0.5, 0.6)},     # on the porch
        {"label": "box", "confidence": 0.7, "bbox": _bbox(0.1, 0.9)},     # on the road
        {"label": "person", "confidence": 0.9, "bbox": _bbox(0.5, 0.6)},
    ]
    d.on_action("check_now", {"camera": "cam1"})
    d.tick(BASE + 1)
    assert _door(d).count == 1
    ev = d.state_snapshot()["events"][0]
    assert ev["method"] == METHOD_OBJECT and ev["confidence"] == 0.8
    call = d._nvr.ai.calls[-1]
    assert call["adapter"] == "oiv7" and call["task"] == "object_detection"
    assert call["camera_id"] == "cam1"


def test_the_shipped_moondream_spelling_is_picked_and_asked_by_that_name(clock):
    # adapters_index.yml: moondream-vlm advertises [visual_qa, scene_caption];
    # tasks.yml folds visual_qa into vqa. The picker must see it as VQA and
    # ask with the name the adapter advertises.
    d = _app(caps={"adapters": {"moondream-vlm": {
        "tasks_advertised": ["visual_qa", "scene_caption"]}}})
    c = d._counter
    assert c.method == METHOD_VQA and c.adapter == "moondream-vlm" and c.task == "visual_qa"
    d._nvr.ai.answer = "two"
    d.on_action("check_now", {"camera": "cam1"})
    d.tick(BASE + 1)
    call = d._nvr.ai.calls[-1]
    assert call["task"] == "visual_qa" and _door(d).count == 2


def test_a_vqa_model_is_asked_the_question_with_the_camera(clock):
    d = _app()
    d._nvr.ai.answer = "3"
    d.on_action("check_now", {"camera": "cam1"})
    d.tick(BASE + 1)
    call = d._nvr.ai.calls[-1]
    assert call["task"] == "vqa" and "how many" in call["params"]["question"].lower()
    assert call["camera_id"] == "cam1" and _door(d).count == 3


# ── Actions ──────────────────────────────────────────────────────────


def test_collected_closes_the_door(clock):
    d = _app()
    _deliver(d, clock)
    clock(BASE + 500)
    out = d.on_action("picked_up", {"camera": "cam1"})
    assert out == {"ok": True, "camera": "cam1", "collected": 1}
    assert _door(d).count == 0
    ev = d.state_snapshot()["events"][0]
    assert ev["kind"] == PICKED_UP and ev["who"]["kind"] == "operator"
    assert ev["dwell_seconds"] == pytest.approx(496.5, abs=1)
    assert d.state_snapshot()["per_camera"][0]["state"] == CLEAR


def test_not_a_package_clears_and_is_recorded(clock):
    d = _app()
    _deliver(d, clock)
    out = d.on_action("not_a_package", {"camera": "cam1"})
    assert out["cleared"] == 1 and _door(d).count == 0
    assert d.state_snapshot()["events"][0]["kind"] == "false_alarm"
    assert d.state_snapshot()["today"]["false_alarms"] == 1


def test_actions_refuse_a_camera_that_is_not_ours():
    d = _app()
    with pytest.raises(KeyError):
        d.on_action("picked_up", {"camera": "cam9"})
    with pytest.raises(KeyError):
        d.on_action("nonsense", {"camera": "cam1"})


# ── Known visitors from the bus ──────────────────────────────────────


def test_only_a_known_visitor_at_one_of_our_doors_counts():
    d = _app()
    d.on_alert_envelope({"alert_type": "known_visitor", "camera_id": "cam1",
                         "evidence": {"name": "Priya"}})
    d.on_alert_envelope({"alert_type": "known_visitor", "camera_id": "cam9",
                         "evidence": {"name": "Nobody"}})
    d.on_alert_envelope({"alert_type": "unknown_visitor", "camera_id": "cam1"})
    d.on_alert_envelope("garbage")   # type: ignore[arg-type]
    assert [k[:2] for k in d._known] == [("cam1", "Priya")]


# ── State and surfaces ───────────────────────────────────────────────


def test_the_state_names_doors_with_no_zone_and_the_hours():
    d = _app(_config(_camera("cam1"), _camera("cam2", drawn=False)),
             hours=DailyHours(_dt.time(7, 30), _dt.time(21, 0)))
    snap = d.state_snapshot()
    assert snap["needs_zone"] == ["cam2"]
    assert snap["hours"] == {"start": "07:30", "end": "21:00"}
    assert [r["camera"] for r in snap["per_camera"]] == ["cam1", "cam2"]
    assert snap["per_camera"][0]["waiting"] is False
    assert snap["waiting_now"] == 0


def test_the_state_carries_what_home_assistant_reads(clock):
    d = _app()
    _deliver(d, clock)
    row = d.state_snapshot()["per_camera"][0]
    assert row["waiting"] is True and row["count"] == 1
    assert row["last_delivery_iso"].endswith("+00:00")
    assert d.state_snapshot()["waiting_now"] == 1
    # Every entity path resolves to something in /state.
    snap = d.state_snapshot()
    for e in pd.MANIFEST.entities:
        if not e.state_path:
            continue
        path = e.state_path.replace("per_camera[camera={camera}].", "")
        if e.per_camera:
            assert path in row, e.key
        else:
            cur: Any = snap
            for part in path.split("."):
                assert part in cur, e.key
                cur = cur[part]


def test_the_manifest_matches_the_catalog():
    m = pd.MANIFEST
    assert m.provides == ["deliveries"] and m.tier0_labels == pd.PROXY_LABELS
    assert {a.name for a in m.actions} == {"picked_up", "not_a_package", "snooze",
                                           "check_now", "acknowledge"}
    assert {e.name for e in m.emits} == {pd.EVENT_DELIVERED, pd.EVENT_PICKED_UP,
                                         pd.EVENT_TAKEN, pd.EVENT_REMINDER}
    for e in m.entities:
        if e.action:
            assert e.action in {a.name for a in m.actions}


def test_ui_html_renders(clock):
    d = _app()
    _deliver(d, clock)
    html = d.ui_html()
    assert "Package Delivery" in html and "cam1" in html and "delivered" in html


# ── Config ───────────────────────────────────────────────────────────


def test_load_config_reads_zones_by_either_name_and_the_hours(tmp_path):
    p = tmp_path / "c.yml"
    p.write_text(
        "nats_url: nats://x:4222\n"
        "consume_tier0: true\n"
        "delivery_hours: {start: '07:00', end: '22:00'}\n"
        "reminder_minutes: 45\n"
        "cameras:\n"
        "  - camera_id: cam1\n"
        "    roi: [[0.3, 0.5], [0.7, 0.5], [0.7, 0.9], [0.3, 0.9]]\n"
        "  - camera_id: cam2\n"
        "zone:\n"
        "  '2': [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]\n"
    )
    cfg = load_config(str(p))
    assert cfg.cameras["cam1"].drawn and cfg.cameras["cam2"].drawn
    assert cfg.delivery_hours == DailyHours(_dt.time(7, 0), _dt.time(22, 0))
    assert cfg.reminder_minutes == 45.0 and cfg.consume_tier0


def test_load_config_needs_cameras_or_the_catalog(tmp_path):
    p = tmp_path / "c.yml"
    p.write_text("nats_url: nats://x:4222\n")
    with pytest.raises(ValueError, match="camera entry"):
        load_config(str(p))
    p.write_text("nats_url: nats://x:4222\nopennvr_url: http://core:8000\n")
    cfg = load_config(str(p))
    assert cfg.auto_cameras and cfg.cameras == {}


def test_live_config_edits_apply_and_bad_ones_are_ignored():
    d = _app()
    d.on_config_update({"reminder_minutes": 10, "delivery_hours": {"start": "09:00", "end": "18:00"},
                        "zone": {"1": [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]}})
    assert d.cfg.reminder_minutes == 10.0
    assert d.cfg.delivery_hours == DailyHours(_dt.time(9, 0), _dt.time(18, 0))
    assert d.cfg.cameras["cam1"].zone.name == "porch"
    d.on_config_update({"reminder_minutes": "lots"})
    assert d.cfg.reminder_minutes == 10.0
    d.on_config_update({"zone": {"1": []}})
    assert d.cfg.cameras["cam1"].drawn is False


def test_cameras_come_and_go_with_the_catalog():
    d = _app(_config(auto_cameras=True))
    d.cfg.cameras.clear()
    d._doors.clear()
    added, removed = d.refresh_cameras([3, 4])
    assert added == ["cam3", "cam4"] and set(d._doors) == {"cam3", "cam4"}
    assert d._doors["cam3"].pending_check[1] == "startup"
    added, removed = d.refresh_cameras([4])
    assert removed == ["cam3"] and "cam3" not in d._doors


def test_events_from_other_cameras_are_ignored():
    d = _app()
    assert d.handle_event(_event(_person(), camera_id="cam7")) == []
    assert d._doors["cam1"].people == {}


def test_a_day_rolls_the_counters(clock):
    d = _app()
    _deliver(d, clock)
    assert d.state_snapshot()["today"]["delivered"] == 1
    d._today_key = "1999-01-01"
    assert d.state_snapshot()["today"]["delivered"] == 0
