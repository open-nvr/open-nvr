# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Publishing Tier-0 results onto OpenNVR's existing inference event bus.

Reuses the subject convention adapters already use
(``opennvr.inference.<adapter>.<camera_id>.completed``) with adapter ``tier0``,
so existing consumers (``ai_detection_results``, inference listeners) pick these
up with no change. The sink only builds the subject + JSON and hands it to an
injected ``publish(subject, data)`` — the async NATS connection lives in the
entrypoint, keeping this pure and testable.
"""
from __future__ import annotations

import json
from collections.abc import Callable

from .pipeline import FrameResult

# publish(subject: str, data: bytes) -> None  (best-effort; never raises upward)
PublishFn = Callable[[str, bytes], None]

ADAPTER = "tier0"
SCHEMA = "opennvr.tier0.v1"


def subject_for(camera_id: str) -> str:
    return f"opennvr.inference.{ADAPTER}.{camera_id}.completed"


def build_payload(camera_id: str, result: FrameResult, frame) -> dict:
    return {
        "schema": SCHEMA,
        "adapter": ADAPTER,
        "camera_id": camera_id,
        "seq": getattr(frame, "seq", None),
        "ts": getattr(frame, "ts", None),
        "calibrating": result.calibrating,
        "tracks": [
            {
                "id": t.id,
                "label": t.label,
                "score": round(float(t.score), 4),
                "box": list(t.box),
                "stationary": t.stationary,
            }
            for t in result.tracks
        ],
    }


class EventSink:
    """ResultSink that publishes to the inference bus via an injected publish fn."""

    def __init__(self, publish: PublishFn, *, publish_empty: bool = False) -> None:
        self._publish = publish
        # By default only publish frames that produced tracks — a 5 fps stream of
        # empty results would be pure noise on the bus.
        self.publish_empty = publish_empty

    def publish(self, camera_id: str, result: FrameResult, frame) -> None:
        if not result.tracks and not self.publish_empty:
            return
        payload = build_payload(camera_id, result, frame)
        self._publish(subject_for(camera_id), json.dumps(payload).encode("utf-8"))
