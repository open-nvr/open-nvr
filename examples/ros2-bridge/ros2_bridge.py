# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""ROS 2 bridge, phase 1: OpenNVR's events out to ROS 2, read-only.

The RFC (discussion #985): robots as cameras, OpenNVR as the eyes and the
judgement. OpenNVR perceives, records and decides; the robot's own stack
(ROS 2, Nav2) owns motion and safety. Phase 1 is the one-way part: the
contracted domain events (detections, plates, visits, occupancy) and the
alerts, published on ROS 2 topics a robot or a fleet manager subscribes
to. Nothing comes back, so nothing on the robot can be moved by it.

* **In:** the apps bus, as this app: the schemas in ``events`` (each a
  declared, granted ``events:`` scope) and, with ``alerts`` on,
  ``opennvr.alerts.>``. NATS never leaves the box: the bridge runs on it.
* **Out:** ``std_msgs/String`` JSON on ``/<namespace>/<site>/<camera>/<schema>``
  and ``/<namespace>/<site>/alerts`` (``ros2_mapping.py`` has the names,
  QoS and body). Towards the robot it is plain ROS 2 networking: host
  networking, ``ROS_DOMAIN_ID``, and a discovery server or Zenoh router
  when the robot is on another subnet or Wi-Fi (README).

Later phases (RFC): the robot in (its position as ``robot.pose.v1``, its
cameras recorded), then intents as app actions behind the workflow
safety rule.

Run:
    python ros2_bridge.py --config config.yml
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from opennvr_app_sdk import AppManifest, Param
from opennvr_app_sdk.config import load_yaml
from opennvr_app_sdk.domain_subscriber import (
    DomainEvent,
    DomainEventSubscriber,
    domain_event_app,
)

from ros2_mapping import (
    ALERT_QOS,
    alert_message,
    alert_topic,
    camera_token,
    event_message,
    event_qos,
    event_topic,
)
from ros2_publisher import LogPublisher, Publisher

logger = logging.getLogger("ros2-bridge")

#: What phase 1 bridges by default: what a robot can act on. Faces are
#: left out (biometric; add face.recognized.v1 deliberately if wanted).
DEFAULT_EVENTS = ["detection.observed.v1", "plate.recognized.v1",
                  "visit.recorded.v1", "occupancy.changed.v1"]
ALERT_SUBJECT = "opennvr.alerts.>"

MANIFEST = AppManifest(
    id="ros2-bridge",
    name="ROS 2 Bridge",
    version="0.1.0",
    category="integration",
    summary=(
        "Publishes OpenNVR's detections, plates, visits, occupancy and alerts "
        "on ROS 2 topics for robots and fleet managers. Read-only: nothing "
        "comes back from ROS 2."
    ),
    requires_tasks=[],
    # Each default event, as a granted and audited scope; an event added
    # in config needs its scope too, or the bus refuses the subscription.
    requires_scopes=[f"events:{s.rsplit('.', 1)[0]}" for s in DEFAULT_EVENTS],
    subscribes=ALERT_SUBJECT,
    params=[
        Param("site", str, default="site",
              description="This site's name in the topics: /opennvr/<site>/..."),
        Param("namespace", str, default="opennvr", description="The topics' first level."),
        Param("events", list, default=DEFAULT_EVENTS,
              description="The event schemas to publish (docs/EVENT_CONTRACTS.md)."),
        Param("alerts", bool, default=True, description="Publish alerts on /<ns>/<site>/alerts."),
        Param("cameras", list, default=[],
              description="Only these cameras ('cam3' or '3'); empty is every camera."),
        Param("frame_ids", dict, default={},
              description="A camera's frame on the robot's side ({cam3: gate_cam}); "
                          "default is the camera's topic name."),
        Param("dry_run", bool, default=False,
              description="Log what would be published instead of publishing (no ROS 2 needed)."),
    ],
    emits=[],
    # It bridges what other apps already limited to their cameras.
    camera_picker=False,
)


# ── Config ─────────────────────────────────────────────────────────


@dataclass
class AppConfig:
    nats_url: str
    nats_token: str | None = None
    site: str = "site"
    namespace: str = "opennvr"
    events: list[str] = field(default_factory=lambda: list(DEFAULT_EVENTS))
    alerts: bool = True
    cameras: list[str] = field(default_factory=list)
    frame_ids: dict[str, str] = field(default_factory=dict)
    dry_run: bool = False


def _str_list(raw: dict[str, Any], key: str, default: list[str]) -> list[str]:
    value = raw.get(key, default)
    if not isinstance(value, list) or not all(isinstance(v, str | int) for v in value):
        raise ValueError(f"config: {key!r} must be a list")
    return [str(v) for v in value]


def load_config(path: str) -> AppConfig:
    """Parse the YAML config; ``ValueError`` on anything malformed."""
    raw = load_yaml(path)
    nats_url = str(raw.get("nats_url") or "").strip()
    if not nats_url:
        raise ValueError("config: 'nats_url' is required")
    events = _str_list(raw, "events", DEFAULT_EVENTS)
    for schema in events:
        if not schema.count(".") >= 2 or not schema.rsplit(".", 1)[-1].startswith("v"):
            raise ValueError(f"config: {schema!r} is not an event schema such as "
                             "plate.recognized.v1")
    frame_ids = raw.get("frame_ids") or {}
    if not isinstance(frame_ids, dict):
        raise ValueError("config: 'frame_ids' must be a mapping of camera to frame")  # noqa: TRY004 — the runner reports ValueError as a config error
    return AppConfig(
        nats_url=nats_url,
        nats_token=str(raw["nats_token"]) if raw.get("nats_token") else None,
        site=str(raw.get("site") or "site"),
        namespace=str(raw.get("namespace") or "opennvr"),
        events=events,
        alerts=bool(raw.get("alerts", True)),
        cameras=_str_list(raw, "cameras", []),
        frame_ids={camera_token(k): str(v) for k, v in frame_ids.items()},
        dry_run=bool(raw.get("dry_run", False)),
    )


# ── The bridge ─────────────────────────────────────────────────────


class Ros2Bridge(DomainEventSubscriber):
    """Each event and alert from the bus, published once on its topic.
    A message that cannot be published is logged and counted; the next
    one is still tried."""

    manifest = MANIFEST

    def __init__(self, config: AppConfig, publisher: Publisher | None = None) -> None:
        self._publisher = publisher
        super().__init__(config)

    def setup(self) -> None:
        self.subscriptions = list(self.cfg.events)
        self._cameras = {camera_token(c) for c in self.cfg.cameras}
        self.counts = {"published": 0, "skipped_camera": 0, "failed": 0}
        self.topics: dict[str, int] = {}

    @property
    def publisher(self) -> Publisher:
        if self._publisher is None:
            if self.cfg.dry_run:
                self._publisher = LogPublisher()
            else:
                from ros2_publisher import Ros2Publisher
                self._publisher = Ros2Publisher()
        return self._publisher

    # ── Subjects: the chosen events, and the alerts ───────────────

    def _nats_subjects(self) -> list[str]:
        subjects = super()._nats_subjects() if self.cfg.events else []
        if self.cfg.alerts:
            subjects.append(ALERT_SUBJECT)
        if not subjects:
            raise ValueError("nothing to bridge: no events and alerts off")
        return subjects

    def _handle_raw(self, data: bytes, *, subject: str = "") -> bool:
        if not subject.startswith("opennvr.alerts."):
            return super()._handle_raw(data, subject=subject)
        try:
            alert = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            logger.warning("skipping non-JSON alert on %r", subject)
            return False
        if not isinstance(alert, dict):
            return False
        self._contract_note_event()
        return self.on_alert(alert)

    # ── Sinks ─────────────────────────────────────────────────────

    def _wanted(self, camera_id: Any) -> bool:
        if self._cameras and camera_token(camera_id) not in self._cameras:
            self.counts["skipped_camera"] += 1
            return False
        return True

    def _send(self, topic: str, qos, text: str) -> bool:
        try:
            self.publisher.publish(topic, qos, text)
        except Exception:
            logger.exception("publishing on %s failed", topic)
            self.counts["failed"] += 1
            return False
        self.counts["published"] += 1
        self.topics[topic] = self.topics.get(topic, 0) + 1
        return True

    def on_event(self, event: DomainEvent) -> None:
        if not self._wanted(event.camera_id):
            return
        cam = camera_token(event.camera_id)
        self._send(event_topic(self.cfg.namespace, self.cfg.site, event.schema, event.camera_id),
                   event_qos(event.schema),
                   event_message(event.raw, frame_id=self.cfg.frame_ids.get(cam)))

    def on_alert(self, alert: dict[str, Any]) -> bool:
        if alert.get("camera_id") is not None and not self._wanted(alert["camera_id"]):
            return True
        return self._send(alert_topic(self.cfg.namespace, self.cfg.site), ALERT_QOS,
                          alert_message(alert))

    # ── Lifecycle and state ───────────────────────────────────────

    async def run(self, *, once: bool = False) -> None:
        try:
            await super().run(once=once)
        finally:
            if self._publisher is not None:
                self._publisher.close()

    def state_snapshot(self) -> dict[str, Any]:
        """``GET /state``: what has been published, and where."""
        return {**self.counts, "topics": dict(sorted(self.topics.items())),
                "dry_run": self.cfg.dry_run}


def main(argv: list[str] | None = None) -> int:
    return domain_event_app(Ros2Bridge, load_config=load_config).run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
