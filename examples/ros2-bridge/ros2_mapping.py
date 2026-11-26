# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What an OpenNVR event becomes on ROS 2: its topic, its QoS, its message.

Pure Python, no ROS: the tests run it anywhere, and the publisher
(``ros2_publisher.py``) only hands the result to rclpy.

**Topics.** ``/<namespace>/<site>/<camera>/<schema>`` for a domain event
(``/opennvr/dock_east/cam23/plate_recognized_v1``) and
``/<namespace>/<site>/alerts`` for alerts. A ROS 2 name token is letters,
digits and single underscores, never starting with a digit, so
``23`` becomes ``cam23`` and anything else is folded to ``_``. The
schema's version stays in the name: a ``v2`` payload gets its own topic
and never breaks a ``v1`` subscriber.

**QoS.** Detections and overlays are high-rate and stale a moment later:
best effort, a short queue. Everything else is something a subscriber
must not miss: reliable, a longer queue.

**Message.** Phase 1 is JSON on ``std_msgs/String`` (no custom message
package to build on the robot). The body is the event's envelope plus
``stamp``, the time it happened, and ``stamp_source``, which time that is:
``observed_at`` (capture time, plate and face reads), else the envelope's
``ts`` (when it was published), or an alert's ``fired_at``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

#: High-rate schemas: newest wins, losing one is fine.
BEST_EFFORT_SCHEMAS = frozenset({"detection.observed.v1", "overlay.boxes.v1"})


@dataclass(frozen=True)
class Qos:
    """What the publisher asks ROS 2 for: ``reliable`` or ``best_effort``,
    keep the last ``depth`` messages."""

    reliability: str
    depth: int


EVENT_QOS = Qos("reliable", 50)
FAST_QOS = Qos("best_effort", 5)
ALERT_QOS = Qos("reliable", 100)


def name_token(value: Any, *, prefix: str) -> str:
    """One ROS 2 name token from ``value``: letters, digits and single
    underscores; ``prefix`` in front when it would start with a digit or
    be empty (``"23"`` -> ``"cam23"``)."""
    token = re.sub(r"[^A-Za-z0-9_]", "_", str(value))
    token = re.sub(r"_+", "_", token).strip("_")
    if not token or token[0].isdigit():
        token = f"{prefix}{token}"
    return token


def camera_token(camera_id: Any) -> str:
    """``"cam23"`` stays; ``"23"`` and ``23`` become ``cam23``."""
    return name_token(camera_id, prefix="cam")


def event_topic(namespace: str, site: str, schema: str, camera_id: Any) -> str:
    return "/" + "/".join((name_token(namespace, prefix="n"), name_token(site, prefix="site"),
                           camera_token(camera_id), name_token(schema, prefix="e")))


def alert_topic(namespace: str, site: str) -> str:
    return "/" + "/".join((name_token(namespace, prefix="n"), name_token(site, prefix="site"),
                           "alerts"))


def event_qos(schema: str) -> Qos:
    return FAST_QOS if schema in BEST_EFFORT_SCHEMAS else EVENT_QOS


def event_message(envelope: dict[str, Any], *, frame_id: str | None = None) -> str:
    """The JSON body for a domain event: the envelope, plus ``stamp`` and
    ``stamp_source`` (which time it is), plus ``frame_id`` (the camera's
    frame on the robot's side, when one is configured)."""
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    observed = payload.get("observed_at")
    stamp, source = ((observed, "observed_at") if observed
                     else (envelope.get("ts"), "ts"))
    body = {"schema": envelope.get("schema"), "id": envelope.get("id"),
            "camera_id": envelope.get("camera_id"), "stamp": stamp, "stamp_source": source,
            "ts": envelope.get("ts"), "frame_id": frame_id or camera_token(envelope.get("camera_id")),
            "producer": envelope.get("producer"),
            "correlation_id": envelope.get("correlation_id"), "payload": payload}
    return json.dumps(body, separators=(",", ":"), default=str)


def alert_message(alert: dict[str, Any]) -> str:
    """The JSON body for an alert: the alert as OpenNVR fired it, with
    ``stamp`` = ``fired_at``."""
    body = {"schema": "alert", "stamp": alert.get("fired_at"), "stamp_source": "fired_at",
            "alert": alert}
    return json.dumps(body, separators=(",", ":"), default=str)
