# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""overlay.boxes.v1 — the typed payload and the publisher sugar."""
from opennvr_app_sdk.event_types import EVENT_TYPES, OverlayBoxes, typed_payload
from opennvr_app_sdk.domain_events import DomainEventPublisher


def test_schema_is_registered():
    assert EVENT_TYPES["overlay.boxes.v1"] is OverlayBoxes


def test_round_trip_keeps_boxes_and_extra():
    p = OverlayBoxes(boxes=[{"label": "plate", "box": [0.1, 0.2, 0.3, 0.1]}],
                     frame={"w": 1920, "h": 1080}, seq=4)
    wire = p.to_payload()
    back = typed_payload("overlay.boxes.v1", {**wire, "vendor_note": "x"})
    assert isinstance(back, OverlayBoxes)
    assert back.boxes == p.boxes and back.frame == p.frame and back.seq == 4
    assert back.extra == {"vendor_note": "x"}


def test_boxes_is_required():
    assert typed_payload("overlay.boxes.v1", {"frame": {"w": 1, "h": 1}}) is None


def test_publish_overlay_publishes_the_typed_event(monkeypatch):
    sent = []
    pub = DomainEventPublisher.__new__(DomainEventPublisher)
    pub._producer = "app:anpr"
    class _Ch:
        def publish_json(self, subject, envelope):
            sent.append((subject, envelope)); return True
    pub._channel = _Ch()
    ok = pub.publish_overlay("cam3", [{"label": "plate", "box": [0, 0, 0.5, 0.5]}],
                             frame={"w": 100, "h": 100}, seq=1)
    assert ok is True
    subject, env = sent[0]
    assert subject == "opennvr.events.overlay.boxes.v1.cam3"
    assert env["payload"]["boxes"][0]["label"] == "plate"
    assert env["payload"]["frame"] == {"w": 100, "h": 100}
