# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""Publishing domain events — how one app becomes another app's input.

Demonstrates: `DomainEventPublisher`, `.publish`, `.publish_typed`,
`domain_subject`, `domain_envelope`, `PlateRecognized`,
`DetectionObserved`, `VisitRecorded`, `AccessDecided`,
`OccupancyChanged`, `typed_payload`, `EVENT_TYPES`.

An alert is for a human; a domain event is for another app. Publishing
one is how an app that recognises plates lets a gate controller, a
visitor log and a dashboard all react without any of them knowing the
plate reader exists.

Publish a CONTRACTED schema (docs/EVENT_CONTRACTS.md) — the version is
part of the subject, so a v2 can run beside v1 during a migration and
every subscriber picks explicitly. The typed payload classes below are
that contract as Python, so a missing required field fails at publish
time rather than in someone else's app.
"""
from opennvr_app_sdk import (
    DomainEventPublisher, PlateRecognized, domain_envelope, domain_subject,
    typed_payload,
)


def publish_a_plate(publisher: DomainEventPublisher, camera_id: str) -> None:
    """The typed route — preferred. The payload class carries the
    contract's required fields, so this cannot publish a malformed
    event; the subject and envelope are derived for you."""
    publisher.publish_typed(
        PlateRecognized(
            plate_text="MH12AB1234",
            confidence=0.94,
            vehicle_label="car",
            observed_at="2026-09-10T08:15:00Z",
        ),
        camera_id=camera_id,
        correlation_id="corr-abc123",       # thread it, never mint a new one
    )


def publish_raw(publisher: DomainEventPublisher, camera_id: str) -> None:
    """The untyped route, for a schema this SDK version does not know
    yet. You are responsible for matching EVENT_CONTRACTS.md."""
    publisher.publish(
        "plate.recognized.v1",
        {"plate_text": "MH12AB1234", "confidence": 0.94},
        camera_id=camera_id,
    )


def what_the_wire_looks_like(camera_id: str) -> tuple[str, dict]:
    """The two helpers underneath both routes — useful in tests, and for
    understanding what a subscriber will actually match."""
    subject = domain_subject("plate.recognized.v1", camera_id)
    # -> "opennvr.events.plate.recognized.v1.cam-gate"
    envelope = domain_envelope(
        "plate.recognized.v1",
        camera_id=camera_id,
        payload={"plate_text": "MH12AB1234"},
        producer="app:anpr",
    )
    # -> {"id": "evt_…", "schema": …, "correlation_id": …, "camera_id": …,
    #     "ts": …, "producer": …, "payload": {…}}
    return subject, envelope


def read_it_back(schema: str, payload: dict):
    """The consuming side of the same contract: `typed_payload` returns
    the typed class for a known schema, or None. `EVENT_TYPES` maps
    every schema this SDK knows."""
    return typed_payload(schema, payload)


def main() -> None:
    publisher = DomainEventPublisher("nats://nats:4222", token="…")
    try:
        publish_a_plate(publisher, "cam-gate")
    finally:
        publisher.close()          # drains in-flight publishes
