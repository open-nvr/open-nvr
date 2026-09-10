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


def test_declaring_a_zone_adds_the_geometry_param():
    app = App("demo")

    @app.on_detection("person", zone="driveway")
    def rule(event):
        event.alert("x")

    params = {p.name: p for p in app.manifest().params}
    assert params["zones"].per_camera is True
    assert params["zones"].to_dict()["type"] == "geometry.polygon"


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

    det, _ = build(app, zones={"driveway": ZONE})
    det.handle_event(inference_event(
        detection("person", x=0.1, y=0.1, w=0.1, h=0.1),   # centre 0.15 → inside
        detection("person", x=0.8, y=0.8, w=0.1, h=0.1),   # centre 0.85 → outside
    ))
    assert seen == ["driveway"]


def test_zones_may_be_declared_per_camera():
    app = App("demo")
    seen = []

    @app.on_detection("person", zone="driveway")
    def rule(event):
        seen.append(event.camera)

    det, _ = build(app, zones={"cam-1": {"driveway": ZONE}})
    det.handle_event(inference_event(
        detection("person", x=0.1, y=0.1), camera_id="cam-1"))
    det.handle_event(inference_event(
        detection("person", x=0.1, y=0.1), camera_id="cam-9"))
    assert seen == ["cam-1"]


def test_in_zone_with_no_name_means_any_zone():
    app = App("demo")
    answers = []

    @app.on_detection("person")
    def rule(event):
        answers.append((event.in_zone(), event.in_zone("nope"), event.zones))

    det, _ = build(app, zones={"driveway": ZONE})
    det.handle_event(inference_event(detection("person", x=0.1, y=0.1)))
    assert answers == [(True, False, ["driveway"])]


def test_malformed_zones_are_ignored_not_fatal():
    app = App("demo")
    calls = []

    @app.on_detection("person")
    def rule(event):
        calls.append(event.zone)

    det, _ = build(app, zones={"bad": [[0.0, 0.0], [1.0, 1.0]], "ok": ZONE})
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

    det, _ = build(app, zones={"driveway": ZONE})
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
