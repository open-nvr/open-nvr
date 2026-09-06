# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""Typed domain-event payloads (event_types) and their seats on
DomainEvent.typed() and DomainEventPublisher.publish_typed()."""
from __future__ import annotations

import json

import pytest

from opennvr_app_sdk import (
    EVENT_TYPES, AccessDecided, DetectionObserved, OccupancyChanged, OccupancyFootfall,
    OccupancyHeatmap, PlateRecognized, VisitRecorded, typed_payload,
)
from opennvr_app_sdk.domain_events import DomainEventPublisher
from opennvr_app_sdk.domain_subscriber import parse_domain_event


def test_every_v1_contract_is_typed_and_round_trips():
    samples = {
        DetectionObserved: {"frame": {"w": 1280, "h": 720}, "calibrating": False,
                            "tracks": [{"id": "t1", "label": "person", "conf": 0.9, "bbox": [1, 2, 3, 4]}]},
        VisitRecorded: {"event_id": 9, "label": "car", "started_at": "2026-09-06T10:00:00Z",
                        "ended_at": "2026-09-06T10:01:00Z", "evidence_path": None},
        PlateRecognized: {"plate_text": "R197GB", "confidence": 0.93, "vehicle_label": "car",
                          "event_id": 9, "plate_box": [10, 10, 80, 30], "plate_box_confidence": 0.88,
                          "plate_box_image": [320, 120]},
        AccessDecided: {"plate_text": "R197GB", "decision": "allow", "reason": "registered",
                        "owner": "Flat 3", "unit": "3", "confidence": 0.93},
        OccupancyChanged: {"count": 4, "level": "over", "max_occupancy": 3, "min_occupancy": None},
        OccupancyHeatmap: {"cols": 16, "rows": 9, "cells": [[17, 3], [18, 1]], "frames": 40,
                           "period_seconds": 60, "labels": ["person"]},
        OccupancyFootfall: {"entries": 3, "exits": 1, "dwell_count": 2, "dwell_seconds": 41.5,
                            "dwell_max_seconds": 30.0, "period_seconds": 60, "labels": ["person"]},
    }
    assert set(EVENT_TYPES) == {c.SCHEMA for c in samples}
    for cls, payload in samples.items():
        obj = cls.from_payload(payload)
        assert isinstance(obj, cls) and obj.extra == {}
        assert obj.to_payload() == payload                       # exact round trip
        assert typed_payload(cls.SCHEMA, payload) == obj


def test_additive_fields_survive_and_required_fields_are_enforced(caplog):
    p = PlateRecognized.from_payload({"plate_text": "X", "lane": 2})
    assert p.extra == {"lane": 2} and p.to_payload() == {
        "plate_text": "X", "confidence": None, "vehicle_label": None, "event_id": None, "lane": 2}
    with pytest.raises(ValueError, match="missing required"):
        PlateRecognized.from_payload({"confidence": 0.9})
    with pytest.raises(ValueError, match="payload must be an object"):
        AccessDecided.from_payload(["allow"])                     # type: ignore[arg-type]
    assert typed_payload("plate.recognized.v1", {}) is None       # logged, not raised
    assert "off-contract" in caplog.text
    assert typed_payload("something.new.v1", {"x": 1}) is None    # not typed by this SDK
    # extra never overrides a contract field on the way out
    q = PlateRecognized("Y", extra={"plate_text": "Z"})
    assert q.to_payload()["plate_text"] == "Y"


def test_access_decisions_fail_closed_on_unknown_values():
    assert AccessDecided("X", "allow", "registered").allow
    assert not AccessDecided("X", "deny", "unknown").allow
    weird = AccessDecided.from_payload({"plate_text": "X", "decision": "escalate", "reason": "new-policy"})
    assert not weird.allow and weird.decision == "escalate"       # parsed, not actuated
    assert OccupancyChanged.from_payload({"count": 1, "level": "critical"}).level == "critical"


def test_domain_event_typed_and_publish_typed():
    raw = json.dumps({"id": "e1", "schema": "access.decided.v1", "camera_id": "gate",
                      "ts": "2026-09-06T10:00:00Z", "producer": "lpr",
                      "payload": {"plate_text": "X", "decision": "allow", "reason": "registered"}})
    ev = parse_domain_event(raw, subject="s")
    assert ev.typed() == AccessDecided("X", "allow", "registered")
    off = parse_domain_event(json.dumps({"schema": "access.decided.v1", "camera_id": "g", "payload": {}}))
    assert off.typed() is None
    unknown = parse_domain_event(json.dumps({"schema": "x.y.v1", "camera_id": "g", "payload": {"a": 1}}))
    assert unknown.typed() is None and unknown.payload == {"a": 1}

    sent = []

    class _Chan:
        def publish_json(self, subject, envelope):
            sent.append((subject, envelope))
            return True

        def close(self):
            pass

    pub = DomainEventPublisher("nats://x", producer="app:test")
    pub._channel = _Chan()
    assert pub.publish_typed(OccupancyChanged(count=2, level="normal"), camera_id="lobby")
    subject, env = sent[0]
    assert subject == "opennvr.events.occupancy.changed.v1.lobby"
    assert env["schema"] == "occupancy.changed.v1" and env["producer"] == "app:test"
    assert env["payload"] == {"count": 2, "level": "normal", "max_occupancy": None, "min_occupancy": None}
    with pytest.raises(TypeError):
        pub.publish_typed({"count": 2}, camera_id="lobby")      # a dict is not typed
