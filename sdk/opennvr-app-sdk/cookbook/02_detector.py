# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""`Detector` — the archetype the facade compiles to.

Demonstrates: `Detector`, `Detector.setup`, `Detector.on_detections`,
`Detector.keyed_state`, `Detector.parse_event_ts`, `Alert`,
`AppManifest`, `Param`, `AlertType`, `app()`, `load_app_config`.

Subclass `Detector` when the rule needs more than the decorators give:
several interacting state machines, a custom event walk, or an
`on_detections` that reads the whole batch at once.

A Detector SUBSCRIBES to `opennvr.inference.*` and consumes results
another app is already driving — adapter GPU is paid once and N
subscribers fan out from one stream.
"""
from dataclasses import dataclass, field
from typing import Any

from opennvr_app_sdk import (
    Alert, AlertType, AppManifest, BaseAppConfig, Detector, Param, app,
    load_app_config,
)

MANIFEST = AppManifest(
    id="loitering-detection",
    name="Loitering Detection",
    version="1.0.0",
    category="perimeter",
    summary="Alerts when a person stays in view longer than a threshold.",
    requires_tasks=["object_detection"],
    subscribes="opennvr.inference.>",
    params=[
        Param("dwell_s", float, default=30.0,
              description="Seconds present before it counts as loitering."),
        Param("watch_labels", list, default=["person"],
              description="Labels that count.", suggestions=["person", "dog"]),
    ],
    emits=[AlertType("loitering", severity="medium",
                     description="Someone stayed too long.")],
)


@dataclass
class AppConfig(BaseAppConfig):
    """BaseAppConfig carries every key the SDK itself reads — the NATS
    endpoint, the alert fan-out, the contract server, the registry. Add
    your own fields; validate them in __post_init__."""

    dwell_s: float = 30.0
    watch_labels: list[str] = field(default_factory=lambda: ["person"])

    def __post_init__(self) -> None:
        self.watch_labels = [str(s).lower() for s in self.watch_labels]
        if self.dwell_s <= 0:
            raise ValueError("'dwell_s' must be positive")


def load_config(path: str) -> AppConfig:
    cfg = load_app_config(path, AppConfig)
    if cfg.subject_pattern is None:
        cfg.subject_pattern = "opennvr.inference.>"
    return cfg


class Loitering(Detector):
    manifest = MANIFEST

    def setup(self) -> None:
        """Runs once at construction, after `self.cfg` is set. Allocate
        state here — never in __init__, which the SDK owns."""
        # TTL is in seconds of EVENT time: a camera the rule stops
        # seeing for 10s is forgotten, which ends the episode.
        self.present = self.keyed_state(ttl=10.0)

    def on_detections(
        self, camera_id: str, detections: list[dict[str, Any]], event: dict[str, Any],
    ) -> list[Alert]:
        """THE RULE. Called once per decoded inference event that has a
        camera_id and a result.detections list."""
        people = [d for d in detections
                  if str(d.get("label", "")).lower() in self.cfg.watch_labels]
        if not people:
            return []

        # Prefer the event's own timestamp over the wall clock — a
        # replayed or delayed event must not skew the dwell timer.
        now = self.parse_event_ts(event.get("completed_at"))
        record = self.present.touch(camera_id, at=now)

        if record.age < self.cfg.dwell_s or record.alerted:
            return []
        record.alerted = True          # latch: once per episode
        return [Alert(
            title=f"Loitering on {camera_id}",
            description=f"{len(people)} person(s) present for {record.age:.0f}s.",
            camera_id=camera_id,
            severity="medium",
            correlation_id=str(event.get("correlation_id") or "") or None,
            evidence={"count": len(people), "dwell_s": round(record.age, 1)},
            tags=["loitering"],
        )]


def main(argv: list[str] | None = None) -> int:
    return app(Loitering, load_config=load_config).run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
