# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""Test helpers for OpenNVR apps — no broker, no core, no Docker.

Every example app's test suite used to carry the same forty lines: a
recorder channel, a ``SimpleNamespace`` config, an inference event
literal. This package is those lines, once, versioned with the SDK so
the shapes stay right when the contracts move.

    from opennvr_app_sdk.testing import (
        RecorderChannel, app_config, detection, inference_event, feed,
    )

    def test_fires_on_a_person():
        recorder = RecorderChannel()
        app = MyDetector(app_config(watch_labels=["person"]), recorder.dispatcher())
        fired = feed(app, inference_event(detection("person", x=0.5, y=0.5)))
        assert [a.title for a in fired] == ["Person seen"]
        assert recorder.alerts == fired

There is also a pytest plugin with the same things as fixtures
(``recorder``, ``app_config_factory``, ``fake_core``): add
``pytest_plugins = ["opennvr_app_sdk.testing.pytest_plugin"]`` to your
``conftest.py``. ``FakeCore`` is a tiny in-process platform (cameras,
snapshot, state, alerts, register) for apps that use ``OpenNVR()``.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Iterable

from ..alerts import Alert, AlertDispatcher
from ..domain_events import domain_envelope
from .fake_core import FakeCore

__all__ = [
    "RecorderChannel", "app_config", "detection", "inference_event", "tier0_event",
    "domain_event", "feed", "FakeCore",
]


class RecorderChannel:
    """An alert channel that keeps every alert in ``alerts``."""

    name = "recorder"

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def send(self, alert: Alert) -> bool:
        self.alerts.append(alert)
        return True

    def dispatcher(self) -> AlertDispatcher:
        """An ``AlertDispatcher`` that fires only into this recorder."""
        return AlertDispatcher([self])

    def clear(self) -> None:
        self.alerts.clear()

    @property
    def titles(self) -> list[str]:
        return [a.title for a in self.alerts]


def app_config(**overrides: Any) -> SimpleNamespace:
    """A config object with every key the SDK archetypes read, plus
    yours. Bus and core point at test hosts; nothing is contacted."""
    base: dict[str, Any] = dict(
        nats_url="nats://test:4222", nats_token=None, subject_pattern=None,
        webhook_url=None, nats_alerts_url=None, nats_alerts_token=None,
        nats_alerts_subject_prefix="opennvr.alerts",
        contract_port=None, contract_bind_host="127.0.0.1", contract_host=None,
        opennvr_url="http://core.test:8000", opennvr_token=None,
        poll_interval_seconds=0.01,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def detection(label: str = "person", *, confidence: float = 0.9,
              x: float = 0.4, y: float = 0.4, w: float = 0.1, h: float = 0.1,
              track_id: str | int | None = None, **extra: Any) -> dict[str, Any]:
    """One contract-shaped detection (normalised ``bbox`` in 0–1)."""
    det: dict[str, Any] = {"label": label, "confidence": confidence,
                           "bbox": {"x": x, "y": y, "w": w, "h": h}}
    if track_id is not None:
        det["track_id"] = track_id
    det.update(extra)
    return det


def inference_event(*detections: dict[str, Any], camera_id: str = "cam-1",
                    adapter: str = "yolov8", completed_at: str | None = None,
                    correlation_id: str | None = None, **overrides: Any) -> dict[str, Any]:
    """An adapter ``InferenceCompletedEvent`` (what ``Detector`` consumes
    from ``opennvr.inference.>``) carrying ``detections``."""
    event: dict[str, Any] = {
        "correlation_id": correlation_id or f"corr-{uuid.uuid4().hex[:8]}",
        "adapter": adapter, "adapter_version": "1.0.0",
        "camera_id": camera_id, "model_fingerprint": "sha256:test",
        "completed_at": completed_at or _now(),
        "result": {"detections": list(detections)},
    }
    event.update(overrides)
    return event


def tier0_event(*tracks: dict[str, Any], camera_id: str = "cam-1",
                frame: tuple[int, int] = (1280, 720), calibrating: bool = False,
                **overrides: Any) -> dict[str, Any]:
    """A Tier-0 (``opennvr.tier0.v1``) event. Tracks carry pixel boxes:
    ``{"id", "label", "score", "box": [x1, y1, x2, y2]}``; use
    ``tier0_track`` or pass dicts. ``tier0_to_detections`` bridges it."""
    event: dict[str, Any] = {
        "schema": "opennvr.tier0.v1", "camera_id": camera_id, "ts": _now(),
        "frame": {"w": frame[0], "h": frame[1]}, "calibrating": calibrating,
        "tracks": list(tracks),
    }
    event.update(overrides)
    return event


def tier0_track(label: str = "person", *, track_id: str = "t1", score: float = 0.9,
                bbox: tuple[float, float, float, float] = (500, 300, 600, 500),
                stationary: bool = False, **extra: Any) -> dict[str, Any]:
    t: dict[str, Any] = {"id": track_id, "label": label, "score": score, "box": list(bbox),
                         "stationary": stationary}
    t.update(extra)
    return t


def domain_event(schema: str, payload: Any, *, camera_id: str = "cam-1",
                 producer: str = "test", correlation_id: str | None = None) -> dict[str, Any]:
    """A contracted domain-event envelope. ``payload`` may be a typed
    payload (``PlateRecognized(...)``) or a dict."""
    from ..event_types import is_typed_payload

    if is_typed_payload(payload):
        schema = schema or type(payload).SCHEMA
        payload = payload.to_payload()
    return domain_envelope(schema, camera_id=camera_id, payload=payload,
                           producer=producer, correlation_id=correlation_id)


def feed(app: Any, *events: dict[str, Any] | bytes | str, subject: str = "") -> list[Alert]:
    """Run raw events through an app's decode → handle → dispatch path
    (``_handle_raw``) exactly as the bus would, without a broker. Returns
    the alerts fired across all of them (a ``DomainEventSubscriber``
    reports fired alerts through its dispatcher; here its return value
    is the count of accepted events, so check the recorder)."""
    fired: list[Alert] = []
    for ev in events:
        raw = ev if isinstance(ev, (bytes, str)) else json.dumps(ev)
        raw = raw.encode() if isinstance(raw, str) else raw
        try:
            out = app._handle_raw(raw, subject=subject)
        except TypeError:
            out = app._handle_raw(raw)
        if isinstance(out, list):
            fired.extend(out)
    return fired


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__.append("tier0_track")
