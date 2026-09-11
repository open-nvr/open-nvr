# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""The ``App`` facade — the front door of the SDK.

The load-bearing claim of the facade is that it is a code generator with
one output: whatever an app declares, what runs is an ordinary
:class:`Detector`. These tests pin both halves — the compiled detector
behaves like any other Detector, and the ergonomics the facade promises
(labels, zones, dwell, cooldown, auto-filled alerts) hold.
"""
from __future__ import annotations

import datetime as dt

import pytest

from opennvr_app_sdk import Alert, App, DetectionEvent, Detector
from opennvr_app_sdk.testing import (
    RecorderChannel, app_config, detection, inference_event,
)

ZONE = [[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [0.0, 1.0]]  # left half of frame

#: The wire shape the catalog's geometry editor writes and core
#: validates: each zone is its own per-camera param, whose value maps
#: camera id → polygon (server/routers/apps.py `_value_matches_type`).
DRIVEWAY = {"cam-1": ZONE, "cam-2": ZONE}


def at(seconds: float) -> str:
    """A ``completed_at`` ISO string ``seconds`` after a fixed epoch."""
    base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    return (base + dt.timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def build(app: App, **cfg):
    recorder = RecorderChannel()
    return app.build(app_config(**cfg), recorder.dispatcher()), recorder


# ── Compilation ─────────────────────────────────────────────────────


def test_compiles_to_a_detector_subclass():
    app = App("demo")

    @app.on_detection("person")
    def rule(event):
        event.alert("seen")

    cls = app.detector_class()
    assert issubclass(cls, Detector)
    assert cls.__name__ == "DemoApp"
    assert cls.manifest.id == "demo"


def test_manifest_defaults_and_passthrough():
    app = App(
        "gate-watch", name="Gate Watch", version="2.1.0", category="perimeter",
        summary="Watches the gate.", author="ACME", pricing="paid",
        price_note="$29/camera/year",
    )

    @app.on_detection("car", severity="high")
    def car_at_gate(event):
        event.alert("car")

    m = app.manifest()
    assert (m.id, m.name, m.version, m.category) == (
        "gate-watch", "Gate Watch", "2.1.0", "perimeter")
    assert m.subscribes == "opennvr.inference.>"
    assert m.requires_tasks == ["object_detection"]
    assert m.author == "ACME" and m.pricing == "paid"
    # An alert type is derived per rule, at the rule's severity.
    assert [(a.name, a.severity) for a in m.emits] == [("car-at-gate", "high")]


def test_name_is_derived_from_the_id_when_omitted():
    assert App("smart-doorbell").manifest().name == "Smart Doorbell"


def test_declaring_a_zone_adds_a_geometry_param_named_after_it():
    """One param per zone, named after the zone — which is how the
    operator learns which polygons this app expects, and the only shape
    core's config validator accepts for a per-camera geometry param."""
    app = App("demo")

    @app.on_detection("person", zone="driveway")
    def rule(event):
        event.alert("x")

    @app.on_detection("car", zone="kerb")
    def other(event):
        event.alert("y")

    params = {p.name: p for p in app.manifest().params}
    assert set(params) == {"driveway", "kerb"}
    assert params["driveway"].per_camera is True
    assert params["driveway"].to_dict()["type"] == "geometry.polygon"
    assert "driveway" in params["driveway"].description


def test_a_zone_can_carry_a_description_for_the_operator():
    app = App("demo").zone("driveway", "The gravel in front of the garage.")

    @app.on_detection("person", zone="driveway")
    def rule(event):
        event.alert("x")

    (param,) = app.manifest().params
    assert param.description == "The gravel in front of the garage."


def test_a_zone_cannot_collide_with_a_param():
    app = App("demo").param("driveway", float)
    with pytest.raises(ValueError, match="already declared"):
        app.zone("driveway")


def test_zone_names_must_be_config_keys():
    with pytest.raises(ValueError, match="snake_case"):
        App("demo").zone("Front Gate")


def test_no_geometry_param_without_a_zone():
    app = App("demo")

    @app.on_detection("person")
    def rule(event):
        event.alert("x")

    assert [p.name for p in app.manifest().params] == []


def test_params_become_manifest_fields_and_config_fields():
    app = App("demo").param(
        "dwell_s", float, default=30.0, description="How long.",
    ).param("plates", list, default=["AAA"])

    @app.on_detection("person")
    def rule(event):
        event.alert("x")

    assert [p.name for p in app.manifest().params] == ["dwell_s", "plates"]
    cfg = app.config_class()(nats_url="nats://x:4222")
    assert cfg.dwell_s == 30.0
    assert cfg.plates == ["AAA"]
    # Mutable defaults are per-instance, not shared.
    cfg.plates.append("BBB")
    assert app.config_class()(nats_url="nats://x:4222").plates == ["AAA"]


def test_run_without_handlers_is_a_configuration_error():
    with pytest.raises(RuntimeError, match="no handlers registered"):
        App("demo").run([])


# ── Filtering ───────────────────────────────────────────────────────


def test_fires_on_the_watched_label_only():
    app = App("demo")
    seen = []

    @app.on_detection("person")
    def rule(event):
        seen.append(event.label)
        event.alert(f"{event.label} on {event.camera}")

    det, recorder = build(app)
    fired = det.handle_event(inference_event(
        detection("person"), detection("car"), camera_id="cam-1"))
    assert seen == ["person"]
    assert [a.title for a in fired] == ["person on cam-1"]
    assert recorder.alerts == fired


def test_no_labels_means_every_label():
    app = App("demo")
    seen = []

    @app.on_detection()
    def rule(event):
        seen.append(event.label)

    det, _ = build(app)
    det.handle_event(inference_event(detection("person"), detection("car")))
    assert seen == ["person", "car"]


def test_confidence_floor():
    app = App("demo")
    seen = []

    @app.on_detection("person", min_confidence=0.8)
    def rule(event):
        seen.append(event.confidence)

    det, _ = build(app)
    det.handle_event(inference_event(
        detection("person", confidence=0.5), detection("person", confidence=0.9)))
    assert seen == [0.9]


def test_camera_filter_accepts_one_or_many():
    app = App("demo")
    seen = []

    @app.on_detection("person", camera=["cam-1", "cam-2"])
    def rule(event):
        seen.append(event.camera)

    det, _ = build(app)
    for cam in ("cam-1", "cam-3", "cam-2"):
        det.handle_event(inference_event(detection("person"), camera_id=cam))
    assert seen == ["cam-1", "cam-2"]


# ── Zones ───────────────────────────────────────────────────────────


def test_zone_filter_uses_the_box_centre():
    app = App("demo")
    seen = []

    @app.on_detection("person", zone="driveway")
    def rule(event):
        seen.append(event.zone)

    det, _ = build(app, driveway=DRIVEWAY)
    det.handle_event(inference_event(
        detection("person", x=0.1, y=0.1, w=0.1, h=0.1),   # centre 0.15 → inside
        detection("person", x=0.8, y=0.8, w=0.1, h=0.1),   # centre 0.85 → outside
    ))
    assert seen == ["driveway"]


def test_a_zone_applies_only_to_the_cameras_it_is_drawn_on():
    app = App("demo")
    seen = []

    @app.on_detection("person", zone="driveway")
    def rule(event):
        seen.append(event.camera)

    det, _ = build(app, driveway={"cam-1": ZONE})
    for cam in ("cam-1", "cam-9"):
        det.handle_event(inference_event(
            detection("person", x=0.1, y=0.1), camera_id=cam))
    assert seen == ["cam-1"]


def test_a_bare_polygon_means_every_camera():
    """Hand-written config predating the geometry editor."""
    app = App("demo")
    seen = []

    @app.on_detection("person", zone="driveway")
    def rule(event):
        seen.append(event.camera)

    det, _ = build(app, driveway=ZONE)
    for cam in ("cam-1", "cam-9"):
        det.handle_event(inference_event(
            detection("person", x=0.1, y=0.1), camera_id=cam))
    assert seen == ["cam-1", "cam-9"]


def test_an_undrawn_zone_warns_instead_of_going_silent(caplog):
    app = App("demo")

    @app.on_detection("person", zone="driveway")
    def loitering(event):
        event.alert("x")

    det, _ = build(app)                      # nobody drew the polygon
    with caplog.at_level("WARNING"):
        for _ in range(3):
            det.handle_event(inference_event(detection("person")))
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1, "warn once per rule per camera, not per event"
    assert "zone 'driveway'" in warnings[0]
    assert "cam-1" in warnings[0] and "App Catalog" in warnings[0]


def test_in_zone_with_no_name_means_any_zone():
    app = App("demo").zone("driveway")
    answers = []

    @app.on_detection("person")
    def rule(event):
        answers.append((event.in_zone(), event.in_zone("nope"), event.zones))

    det, _ = build(app, driveway=DRIVEWAY)
    det.handle_event(inference_event(detection("person", x=0.1, y=0.1)))
    assert answers == [(True, False, ["driveway"])]


def test_malformed_zones_are_ignored_not_fatal():
    app = App("demo")
    calls = []

    @app.on_detection("person")
    def rule(event):
        calls.append(event.zone)

    app.zone("bad")
    app.zone("ok")
    det, _ = build(app, bad={"cam-1": [[0.0, 0.0], [1.0, 1.0]]},
                   ok={"cam-1": ZONE})
    det.handle_event(inference_event(detection("person", x=0.1, y=0.1)))
    assert calls == ["ok"]


# ── Dwell and cooldown ──────────────────────────────────────────────


def test_dwell_waits_then_fires_once_per_episode():
    app = App("demo")

    @app.on_detection("person", dwell=30)
    def loitering(event):
        event.alert(f"loitering {event.dwell_s:.0f}s")

    det, _ = build(app)
    titles = []
    for second in (0, 10, 29, 31, 40):
        for alert in det.handle_event(inference_event(
                detection("person", track_id="t1"), completed_at=at(second))):
            titles.append(alert.title)
    assert titles == ["loitering 31s"]


def test_cooldown_throttles_repeat_alerts():
    app = App("demo")

    @app.on_detection("car", cooldown=60)
    def parked(event):
        event.alert("car")

    det, _ = build(app)
    fired = []
    for second in (0, 30, 59, 61, 100, 130):
        fired += det.handle_event(inference_event(
            detection("car", track_id="c1"), completed_at=at(second)))
    assert len(fired) == 3  # t=0, t=61, t=130


def test_dwell_and_cooldown_are_keyed_per_object():
    app = App("demo")

    @app.on_detection("person", cooldown=60)
    def rule(event):
        event.alert(f"person {event.track_id}")

    det, _ = build(app)
    fired = det.handle_event(inference_event(
        detection("person", track_id="a"), detection("person", track_id="b")))
    assert [a.title for a in fired] == ["person a", "person b"]


def test_first_seen_flags_the_start_of_an_episode():
    app = App("demo")
    flags = []

    @app.on_detection("person")
    def rule(event):
        flags.append(event.first_seen)

    det, _ = build(app)
    for second in (0, 5):
        det.handle_event(inference_event(
            detection("person", track_id="t1"), completed_at=at(second)))
    assert flags == [True, False]


# ── The event object ────────────────────────────────────────────────


def test_alert_fills_in_the_envelope():
    app = App("demo")

    @app.on_detection("person", zone="driveway", severity="critical")
    def intruder(event):
        event.alert("Intruder", "Someone is in the driveway.", tags=["night"])

    det, _ = build(app, driveway={"cam-front": ZONE})
    event = inference_event(
        detection("person", confidence=0.77, track_id="t9", x=0.1, y=0.1),
        camera_id="cam-front", correlation_id="corr-1")
    (alert,) = det.handle_event(event)
    assert isinstance(alert, Alert)
    assert alert.camera_id == "cam-front"
    assert alert.severity == "critical"          # inherited from the rule
    assert alert.correlation_id == "corr-1"
    assert alert.description == "Someone is in the driveway."
    assert alert.evidence["label"] == "person"
    assert alert.evidence["confidence"] == 0.77
    assert alert.evidence["track_id"] == "t9"
    assert alert.evidence["zone"] == "driveway"
    assert alert.evidence["adapter"] == "yolov8"
    assert alert.tags == ["demo", "person", "driveway", "night"]
    # The app's identity is stamped on the alert, as for any Detector.
    assert alert.source.name == "demo"


def test_explicit_severity_beats_the_rule_default():
    app = App("demo")

    @app.on_detection("person", severity="low")
    def rule(event):
        event.alert("x", severity="high")

    det, _ = build(app)
    (alert,) = det.handle_event(inference_event(detection("person")))
    assert alert.severity == "high"


def test_description_defaults_to_the_title():
    app = App("demo")

    @app.on_detection("person")
    def rule(event):
        event.alert("Just the title")

    det, _ = build(app)
    (alert,) = det.handle_event(inference_event(detection("person")))
    assert alert.description == "Just the title"


def test_returned_alerts_are_dispatched_too():
    app = App("demo")

    @app.on_detection("person")
    def returns_one(event):
        return Alert(title="returned", description="d", camera_id=event.camera)

    det, recorder = build(app)
    fired = det.handle_event(inference_event(detection("person")))
    assert [a.title for a in fired] == ["returned"]
    assert recorder.titles == ["returned"]


def test_event_sees_the_rest_of_the_frame():
    app = App("demo")
    counts = []

    @app.on_detection("person")
    def rule(event):
        counts.append((event.count(), event.count("car"), event.count("dog")))

    det, _ = build(app)
    det.handle_event(inference_event(
        detection("person"), detection("car"), detection("car")))
    assert counts == [(3, 2, 0)]


def test_bbox_and_centre_are_normalized():
    app = App("demo")
    seen = []

    @app.on_detection("person")
    def rule(event):
        seen.append((event.bbox, event.center.x, event.center.y))

    det, _ = build(app)
    det.handle_event(inference_event(
        detection("person", x=0.2, y=0.4, w=0.2, h=0.2)))
    (bbox, cx, cy) = seen[0]
    assert bbox == {"x": 0.2, "y": 0.4, "w": 0.2, "h": 0.2}
    assert (round(cx, 3), round(cy, 3)) == (0.3, 0.5)


def test_remember_and_recall_persist_across_events():
    app = App("demo")
    seen = []

    @app.on_detection("person")
    def rule(event):
        seen.append(event.recall("hits", 0))
        event.remember(hits=event.recall("hits", 0) + 1)

    det, _ = build(app)
    for second in (0, 1, 2):
        det.handle_event(inference_event(
            detection("person", track_id="t1"), completed_at=at(second)))
    assert seen == [0, 1, 2]


def test_missing_or_malformed_fields_never_raise():
    app = App("demo")
    seen = []

    @app.on_detection(min_confidence=0.0)
    def rule(event):
        seen.append((event.label, event.confidence, event.bbox, event.track_id))

    det, _ = build(app)
    det.handle_event({"camera_id": "cam-1", "result": {"detections": [
        {}, {"label": "person", "confidence": "not-a-number", "bbox": "nope"},
    ]}})
    assert seen[0] == ("", 0.0, {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}, None)
    assert seen[1][1] == 0.0


# ── Escape hatches and isolation ────────────────────────────────────


def test_on_event_sees_the_whole_event():
    app = App("demo")
    seen = []

    @app.on_event()
    def raw(camera_id, detections, event):
        seen.append((camera_id, len(detections)))
        return [Alert(title="raw", description="d", camera_id=camera_id)]

    det, _ = build(app)
    fired = det.handle_event(inference_event(detection("person"), detection("car")))
    assert seen == [("cam-1", 2)]
    assert [a.title for a in fired] == ["raw"]


def test_on_setup_runs_once_with_the_config():
    app = App("demo").param("threshold", float, default=0.5)
    calls = []

    @app.on_setup()
    def prepare(cfg):
        calls.append(cfg.threshold)

    @app.on_detection("person")
    def rule(event):
        pass

    build(app, threshold=0.9)
    assert calls == [0.9]


def test_a_raising_rule_does_not_kill_the_loop():
    app = App("demo")

    @app.on_detection("person")
    def explodes(event):
        raise RuntimeError("boom")

    @app.on_detection("person")
    def survives(event):
        event.alert("still here")

    det, _ = build(app)
    fired = det.handle_event(inference_event(detection("person")))
    assert [a.title for a in fired] == ["still here"]


def test_several_rules_fire_independently():
    app = App("demo")

    @app.on_detection("person")
    def people(event):
        event.alert("person")

    @app.on_detection("car")
    def cars(event):
        event.alert("car")

    det, _ = build(app)
    fired = det.handle_event(inference_event(detection("person"), detection("car")))
    assert sorted(a.title for a in fired) == ["car", "person"]


def test_config_is_reachable_from_the_app_and_the_event():
    app = App("demo").param("threshold", float, default=0.5)
    seen = []

    @app.on_detection("person")
    def rule(event):
        seen.append((event.config.threshold, app.config.threshold))

    det, _ = build(app, threshold=0.25)
    det.handle_event(inference_event(detection("person")))
    assert seen == [(0.25, 0.25)]


def test_repr_is_useful():
    app = App("demo")
    reprs = []

    @app.on_detection("person")
    def rule(event):
        reprs.append(repr(event))

    det, _ = build(app)
    det.handle_event(inference_event(detection("person", confidence=0.9)))
    assert reprs[0].startswith("<DetectionEvent person on cam-1 conf=0.90")


def test_detection_event_is_exported():
    assert DetectionEvent.__module__.endswith("facade")


# ── Regressions ─────────────────────────────────────────────────────
#
# Each of these is a defect a review found by reproduction. They are
# grouped because the failure mode they share is the worst kind: the app
# looks healthy and quietly does the wrong thing.


def test_dwell_measures_time_inside_the_filters_not_time_on_camera():
    """`zone="driveway", dwell=30` must mean thirty seconds IN the
    driveway. Starting the clock on first sighting instead turns it into
    "thirty seconds on camera, then one frame in the driveway"."""
    app = App("demo")

    @app.on_detection("person", zone="driveway", dwell=30)
    def loitering(event):
        event.alert(f"loitering {event.dwell_s:.0f}s")

    det, _ = build(app, driveway=DRIVEWAY)
    titles = []
    # 40s outside the zone (centre 0.85), then inside from t=40.
    for second in (0, 20, 39):
        titles += [a.title for a in det.handle_event(inference_event(
            detection("person", track_id="t1", x=0.8, y=0.8),
            completed_at=at(second)))]
    assert titles == [], "dwell accrued while the object was outside the zone"
    for second in (40, 50, 71):
        titles += [a.title for a in det.handle_event(inference_event(
            detection("person", track_id="t1", x=0.1, y=0.1),
            completed_at=at(second)))]
    assert titles == ["loitering 31s"]


def test_a_confidence_floor_also_gates_the_dwell_clock():
    app = App("demo")

    @app.on_detection("person", min_confidence=0.8, dwell=20)
    def rule(event):
        event.alert("fired")

    det, _ = build(app)
    fired = []
    for second in (0, 10, 25):
        fired += det.handle_event(inference_event(
            detection("person", confidence=0.2, track_id="t1"),
            completed_at=at(second)))
    assert fired == [], "sub-threshold noise satisfied the dwell"
    for second in (26, 40, 47):
        fired += det.handle_event(inference_event(
            detection("person", confidence=0.9, track_id="t1"),
            completed_at=at(second)))
    assert len(fired) == 1


def test_two_rules_with_the_same_function_name_do_not_share_a_latch():
    """Handlers built by a factory, or two lambdas, collide on
    ``__name__``. Identity must be positional."""
    app = App("demo")

    def make(threshold, tag):
        def rule(event):                      # noqa: D401 — same name on purpose
            event.alert(tag)
        return rule

    app.on_detection("person", dwell=10, emits="first")(make(10, "first"))
    app.on_detection("person", dwell=20, emits="second")(make(20, "second"))

    det, _ = build(app)
    titles = []
    for second in (0, 11, 21, 30):
        titles += [a.title for a in det.handle_event(inference_event(
            detection("person", track_id="t1"), completed_at=at(second)))]
    assert sorted(titles) == ["first", "second"]
    assert {a.name for a in app.manifest().emits} == {"first", "second"}


def test_returning_the_alert_you_fired_does_not_dispatch_it_twice():
    app = App("demo")

    @app.on_detection("person")
    def rule(event):
        return event.alert("once")            # both fires AND returns

    det, recorder = build(app)
    fired = det.handle_event(inference_event(detection("person")))
    assert [a.title for a in fired] == ["once"]
    assert len(recorder.alerts) == 1


def test_a_new_object_alerts_after_the_previous_one_leaves():
    """Without a track id the key is (camera, label); the latch must be
    re-armed by absence, or the second person of the day never alerts."""
    app = App("demo")

    @app.on_detection("person", dwell=10, forget=15)
    def rule(event):
        event.alert(f"person after {event.dwell_s:.0f}s")

    det, _ = build(app)
    titles = []
    for second in (0, 5, 10, 12):            # person A: alerts at t=10
        titles += [a.title for a in det.handle_event(inference_event(
            detection("person"), completed_at=at(second)))]
    for second in (40, 45, 52):              # person B, after a 28s gap
        titles += [a.title for a in det.handle_event(inference_event(
            detection("person"), completed_at=at(second)))]
    # Both episodes alert, and the second one's dwell is measured from
    # when person B arrived — not from when person A did.
    assert titles == ["person after 10s", "person after 12s"], \
        f"the second episode never alerted: {titles}"


def test_an_undated_event_does_not_evict_every_camera():
    """`parse_event_ts` falls back to the wall clock for a missing
    timestamp. Mixing that with event time let one malformed event jump
    the clock forward and garbage-collect every other camera."""
    app = App("demo")

    @app.on_detection("person", dwell=30, forget=60)
    def rule(event):
        event.alert(f"loitering on {event.camera}")

    det, _ = build(app)
    for camera in ("cam-1", "cam-2"):
        for second in (0, 20):
            det.handle_event(inference_event(
                detection("person"), camera_id=camera, completed_at=at(second)))
    # A publisher sends an event with no completed_at at all.
    det.handle_event({"camera_id": "cam-3",
                      "result": {"detections": [detection("person")]}})
    titles = []
    for camera in ("cam-1", "cam-2"):
        titles += [a.title for a in det.handle_event(inference_event(
            detection("person"), camera_id=camera, completed_at=at(31)))]
    assert sorted(titles) == ["loitering on cam-1", "loitering on cam-2"]


def test_alert_type_names_stay_valid_whatever_the_function_is_called():
    """`opennvr-app validate` requires [a-z0-9_-]+. A capitalised
    handler, or a lambda, must not produce a manifest that fails it."""
    import re

    app = App("demo")
    app.on_detection("person")(lambda event: None)

    def Loitering(event):                     # noqa: N802 — the point
        pass

    app.on_detection("car")(Loitering)
    for alert_type in app.manifest().emits:
        assert re.match(r"^[a-z0-9_-]+$", alert_type.name), alert_type.name


def test_a_rule_that_raises_still_respects_its_cooldown():
    app = App("demo")
    calls = []

    @app.on_detection("person", cooldown=60)
    def explodes(event):
        calls.append(event.ts)
        raise RuntimeError("boom")

    det, _ = build(app)
    for second in (0, 10, 30, 61, 90):
        det.handle_event(inference_event(
            detection("person", track_id="t1"), completed_at=at(second)))
    assert len(calls) == 2, "a failing rule re-raised on every single event"


def test_mutable_param_defaults_are_not_shared_between_configs():
    app = App("demo").param("regions", list, default=[[0, 0], [1, 1]])

    @app.on_detection("person")
    def rule(event):
        pass

    cls = app.config_class()
    first, second = cls(), cls()
    first.regions[0].append(999)
    assert second.regions == [[0, 0], [1, 1]], "a nested default was shared"


def test_an_app_id_that_the_platform_cannot_use_is_refused_at_the_source():
    for bad in ("Gate Watch", "GateWatch", "gate--watch", "gate_watch"):
        with pytest.raises(ValueError, match="kebab-case"):
            App(bad)
    App("gate-watch")                          # the good one still works


def test_a_derived_manifest_field_points_at_the_decorator_that_owns_it():
    with pytest.raises(TypeError, match=r"app\.param"):
        App("demo", params=[])
    with pytest.raises(TypeError, match=r"@app\.action"):
        App("demo", actions=[])
    with pytest.raises(TypeError, match=r"@app\.on_license"):
        App("demo", entitlement="license_key")


def test_live_config_is_applied_and_the_zone_cache_dropped():
    app = App("demo")
    seen = []

    @app.on_detection("person", zone="driveway")
    def rule(event):
        seen.append(event.camera)

    det, _ = build(app)                        # no polygon yet
    det.handle_event(inference_event(detection("person", x=0.1, y=0.1)))
    assert seen == []
    det.on_config_update({"driveway": {"cam-1": ZONE}})
    det.handle_event(inference_event(detection("person", x=0.1, y=0.1)))
    assert seen == ["cam-1"], "a redrawn zone needed a restart"
