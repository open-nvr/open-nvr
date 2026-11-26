# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ROS 2 bridge without ROS: names, QoS, bodies, filtering, alerts."""
from __future__ import annotations

import json
import textwrap

import pytest

import ros2_bridge
from ros2_bridge import DEFAULT_EVENTS, MANIFEST, AppConfig, Ros2Bridge, load_config
from ros2_mapping import (
    ALERT_QOS,
    EVENT_QOS,
    FAST_QOS,
    alert_topic,
    camera_token,
    event_qos,
    event_topic,
    name_token,
)


class FakePublisher:
    def __init__(self, fail_on: str | None = None) -> None:
        self.sent: list[tuple[str, object, dict]] = []
        self.fail_on = fail_on
        self.closed = False

    def publish(self, topic, qos, text):
        if self.fail_on and self.fail_on in topic:
            raise RuntimeError("publisher gone")
        self.sent.append((topic, qos, json.loads(text)))

    def close(self):
        self.closed = True


def envelope(schema="plate.recognized.v1", camera="cam23", payload=None, **kw):
    return {"id": "evt_1", "schema": schema, "camera_id": camera,
            "ts": "2026-11-25T10:00:01Z", "producer": "kai-c", "correlation_id": "c1",
            "payload": payload if payload is not None else {"plate_text": "KA01AB1234"}, **kw}


def bridge(**cfg):
    pub = FakePublisher(cfg.pop("fail_on", None))
    return Ros2Bridge(AppConfig(nats_url="nats://x", site="dock-east", **cfg), publisher=pub), pub


def feed(b, subject, body):
    return b._handle_raw(json.dumps(body).encode(), subject=subject)


# ── names ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value, expected", [
    ("cam23", "cam23"), ("23", "cam23"), (23, "cam23"), ("front gate", "front_gate"),
    ("a--b__c", "a_b_c"), ("", "cam"), ("_x_", "x")])
def test_a_camera_is_a_valid_ros_name_token(value, expected):
    assert camera_token(value) == expected


def test_topics_are_valid_ros_names():
    assert event_topic("opennvr", "dock-east", "plate.recognized.v1", "23") == \
        "/opennvr/dock_east/cam23/plate_recognized_v1"
    assert alert_topic("opennvr", "1st floor") == "/opennvr/site1st_floor/alerts"
    for token in event_topic("opennvr", "dock-east", "plate.recognized.v1", "23").split("/")[1:]:
        assert token[0].isalpha() and "__" not in token and name_token(token, prefix="x") == token


def test_detections_are_best_effort_and_the_rest_reliable():
    assert event_qos("detection.observed.v1") == FAST_QOS == FAST_QOS.__class__("best_effort", 5)
    assert event_qos("plate.recognized.v1") == EVENT_QOS
    assert EVENT_QOS.reliability == ALERT_QOS.reliability == "reliable"


# ── events ────────────────────────────────────────────────────────────


def test_an_event_goes_to_its_camera_topic_with_its_envelope():
    b, pub = bridge()
    assert feed(b, "opennvr.events.plate.recognized.v1.cam23", envelope()) is True
    ((topic, qos, body),) = pub.sent
    assert topic == "/opennvr/dock_east/cam23/plate_recognized_v1" and qos == EVENT_QOS
    assert body["payload"] == {"plate_text": "KA01AB1234"}
    assert (body["id"], body["schema"], body["camera_id"], body["frame_id"]) == (
        "evt_1", "plate.recognized.v1", "cam23", "cam23")
    assert b.state_snapshot()["topics"] == {"/opennvr/dock_east/cam23/plate_recognized_v1": 1}


def test_the_stamp_says_which_time_it_is():
    b, pub = bridge()
    feed(b, "s", envelope(payload={"plate_text": "X", "observed_at": "2026-11-25T09:59:58Z"}))
    feed(b, "s", envelope(schema="detection.observed.v1", payload={"tracks": []}))
    (_, _, plate), (_, qos, det) = pub.sent
    assert (plate["stamp"], plate["stamp_source"]) == ("2026-11-25T09:59:58Z", "observed_at")
    assert (det["stamp"], det["stamp_source"]) == ("2026-11-25T10:00:01Z", "ts")
    assert qos == FAST_QOS


def test_only_the_chosen_cameras_and_a_configured_frame():
    b, pub = bridge(cameras=["23"], frame_ids={"cam23": "gate_camera"})
    feed(b, "s", envelope(camera="cam5"))
    feed(b, "s", envelope(camera="cam23"))
    assert [t for t, _, _ in pub.sent] == ["/opennvr/dock_east/cam23/plate_recognized_v1"]
    assert pub.sent[0][2]["frame_id"] == "gate_camera"
    assert b.counts["skipped_camera"] == 1


def test_off_contract_messages_are_skipped_and_the_bridge_carries_on():
    b, pub = bridge(fail_on="cam9")
    assert b._handle_raw(b"not json", subject="opennvr.events.x.v1.cam1") is False
    assert feed(b, "s", {"schema": "plate.recognized.v1"}) is False       # no camera, no payload
    feed(b, "s", envelope(camera="cam9"))                                   # publishing fails
    feed(b, "s", envelope(camera="cam1"))
    assert [t.split("/")[3] for t, _, _ in pub.sent] == ["cam1"]
    assert b.counts == {"published": 1, "skipped_camera": 0, "failed": 1}


# ── alerts ────────────────────────────────────────────────────────────


def test_alerts_go_to_the_site_alert_topic():
    b, pub = bridge()
    alert = {"alert_id": "a1", "title": "Intrusion", "severity": "high", "camera_id": "cam3",
             "fired_at": "2026-11-25T10:00:00Z", "source": {"kind": "app", "name": "x"}}
    assert feed(b, "opennvr.alerts.app.x.cam3", alert) is True
    ((topic, qos, body),) = pub.sent
    assert (topic, qos) == ("/opennvr/dock_east/alerts", ALERT_QOS)
    assert (body["schema"], body["stamp"], body["stamp_source"]) == (
        "alert", "2026-11-25T10:00:00Z", "fired_at")
    assert body["alert"] == alert


def test_subjects_follow_the_config():
    b, _ = bridge()
    assert b._nats_subjects() == [f"opennvr.events.{s}.>" for s in DEFAULT_EVENTS] + [
        "opennvr.alerts.>"]
    b, _ = bridge(events=[], alerts=True)
    assert b._nats_subjects() == ["opennvr.alerts.>"]
    b, _ = bridge(events=[], alerts=False)
    with pytest.raises(ValueError, match="nothing to bridge"):
        b._nats_subjects()


def test_the_manifest_asks_for_exactly_the_default_events_and_the_alerts():
    assert MANIFEST.requires_scopes == ["events:detection.observed", "events:plate.recognized",
                                        "events:visit.recorded", "events:occupancy.changed"]
    assert MANIFEST.subscribes == "opennvr.alerts.>"


def test_dry_run_needs_no_ros(caplog):
    b = Ros2Bridge(AppConfig(nats_url="nats://x", dry_run=True))
    caplog.set_level("INFO", logger="ros2-bridge")
    feed(b, "s", envelope())
    assert "dry run: /opennvr/site/cam23/plate_recognized_v1" in caplog.text
    assert b.state_snapshot()["published"] == 1


async def test_the_publisher_is_closed_when_the_bridge_stops(monkeypatch):
    b, pub = bridge()

    async def no_bus(self, *, once):
        return None
    monkeypatch.setattr(ros2_bridge.DomainEventSubscriber, "_run_nats_loop", no_bus)
    await b.run(once=True)
    assert pub.closed is True


# ── config ────────────────────────────────────────────────────────────


def write(tmp_path, text):
    p = tmp_path / "config.yml"
    p.write_text(textwrap.dedent(text))
    return str(p)


def test_config_defaults_and_values(tmp_path):
    cfg = load_config(write(tmp_path, """
        nats_url: nats://nats-apps:4222
        site: dock-east
        cameras: [23, cam5]
        frame_ids: { "23": gate_camera }
    """))
    assert (cfg.site, cfg.namespace, cfg.events, cfg.alerts) == (
        "dock-east", "opennvr", DEFAULT_EVENTS, True)
    assert cfg.cameras == ["23", "cam5"] and cfg.frame_ids == {"cam23": "gate_camera"}


@pytest.mark.parametrize("text, says", [
    ("site: x", "nats_url"),
    ("nats_url: n\nevents: plate", "must be a list"),
    ("nats_url: n\nevents: [plates]", "not an event schema"),
    ("nats_url: n\nframe_ids: [a]", "frame_ids"),
])
def test_config_refuses_what_it_cannot_use(tmp_path, text, says):
    with pytest.raises(ValueError, match=says):
        load_config(write(tmp_path, text))


def test_the_example_config_loads():
    from pathlib import Path
    cfg = load_config(str(Path(__file__).resolve().parents[1] / "config.example.yml"))
    assert cfg.site == "dock_east" and cfg.events == DEFAULT_EVENTS
