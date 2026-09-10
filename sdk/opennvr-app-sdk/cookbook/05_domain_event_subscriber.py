# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""`DomainEventSubscriber` — react to contracted domain events.

Demonstrates: `DomainEventSubscriber`, `subscriptions`, `on_event`,
`DomainEvent`, `DomainEvent.typed`, `self.fire`, `domain_event_app`,
`parse_domain_event`, `PlateRecognized`, `typed_payload`,
`requires_scopes`.

Domain events (`opennvr.events.<domain>.<event>.v<N>.<camera_id>`) are
the versioned, contracted way apps talk to each other — defined in
docs/EVENT_CONTRACTS.md, not in a producer's source. Consuming one that
carries PII (a plate read) is a declared capability: `requires_scopes`
is granted at install and audited, and it shows in the App Catalog.
"""
from dataclasses import dataclass, field
from typing import Any

from opennvr_app_sdk import (
    Alert, AlertType, AppManifest, BaseAppConfig, DomainEvent, DomainEventSubscriber,
    Param, domain_event_app, load_app_config,
)

MANIFEST = AppManifest(
    id="gate-controller",
    name="Gate Controller",
    version="1.0.0",
    category="vehicle",
    summary="Opens the gate for plates on the allow-list; alerts on the rest.",
    # The scope is the ask. Without it the bus will not deliver the event.
    requires_scopes=["events:plate.recognized"],
    params=[Param("allow_list", list, default=[],
                  description="Plates permitted through the gate.")],
    emits=[AlertType("unknown-plate", severity="high")],
)


@dataclass
class AppConfig(BaseAppConfig):
    allow_list: list[str] = field(default_factory=list)


class Gate(DomainEventSubscriber):
    manifest = MANIFEST
    #: Schemas, not subjects — the SDK derives the subject, including
    #: the version segment, so a v2 migration is a one-line change.
    subscriptions = ["plate.recognized.v1"]

    def setup(self) -> None:
        self.allowed = {p.upper().replace(" ", "") for p in self.cfg.allow_list}
        self.opened = 0

    def on_event(self, event: DomainEvent) -> None:
        """Called once per decoded domain event. `event.typed` gives the
        payload as a typed object (`PlateRecognized`) when the schema is
        one the SDK knows; `event.payload` is always the raw dict."""
        plate = event.typed                      # -> PlateRecognized | None
        if plate is None:
            return
        normalized = plate.plate_text.upper().replace(" ", "")
        if normalized in self.allowed:
            self.open_gate(event.camera_id, normalized)
            return
        # `self.fire` dispatches as this app and counts toward /health,
        # exactly as a Detector's returned alerts do.
        self.fire(Alert(
            title=f"Unknown plate {normalized}",
            description=f"{normalized} presented at {event.camera_id}.",
            camera_id=event.camera_id,
            severity="high",
            correlation_id=event.correlation_id,
            evidence={"plate": normalized, "confidence": plate.confidence},
            tags=["gate", "unknown-plate"],
        ))

    def open_gate(self, camera_id: str, plate: str) -> None:
        self.opened += 1
        # …drive the relay / call the controller here.

    def state_snapshot(self) -> dict[str, Any]:
        return {"allow_list_size": len(self.allowed), "opened": self.opened}


def main(argv: list[str] | None = None) -> int:
    return domain_event_app(
        Gate, load_config=lambda p: load_app_config(p, AppConfig)).run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
