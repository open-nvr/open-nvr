# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The camera's own analytics as alerts: the topic map, the active edge,
the per-camera source and allowlist, the envelope, and delivery."""
from __future__ import annotations

import asyncio
import os
import secrets
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

from services import native_analytics as na  # noqa: E402


@pytest.mark.parametrize("event, expected", [
    ({"event_type": "Crossed", "description": "tns1:RuleEngine/LineDetector/Crossed"}, "line_crossing"),
    ({"event_type": "ObjectsInside", "description": "tns1:RuleEngine/FieldDetector/ObjectsInside"}, "intrusion"),
    ({"event_type": "Motion", "description": "tns1:RuleEngine/CellMotionDetector/Motion"}, "motion"),
    ({"event_type": "Tamper", "description": "tns1:RuleEngine/TamperDetector/Tamper"}, "tamper_covered"),
    ({"event_type": "linedetection", "description": "linedetection alarm"}, "line_crossing"),      # Hikvision
    ({"event_type": "fielddetection"}, "intrusion"),
    ({"event_type": "unattendedBaggage"}, "object_left"),
    ({"event_type": "VMD"}, "motion"),
    ({"event_type": "CrossLineDetection"}, "line_crossing"),                                       # Dahua
    ({"event_type": "CrossRegionDetection"}, "intrusion"),
    ({"event_type": "VideoBlind"}, "tamper_covered"),
    ({"event_type": "VideoLoss"}, "video_loss"),
    ({"event_type": "LeftDetection"}, "object_left"),
    ({"event_type": "WanderDetection"}, "loitering"),
    ({"event_type": "AlarmLocal"}, "camera_event"),
])
def test_native_topics_map_to_opennvr_alert_types(event, expected):
    assert na.classify(event) == expected


def test_only_the_active_edge_raises():
    assert na.is_active_edge({"event_state": "active"})
    assert na.is_active_edge({"event_state": None})            # one-shot ISAPI events carry no state
    assert not na.is_active_edge({"event_state": "inactive"})
    assert not na.is_active_edge({"event_state": "Stop"})


def test_source_and_allowlist_decide_what_becomes_an_alert():
    assert na.effective_source(None) == "opennvr" and na.effective_source("BOTH") == "both"
    assert na.effective_source("nonsense") == "opennvr"
    assert not na.wants_alerts("opennvr", None, "intrusion")
    assert na.wants_alerts("camera", None, "intrusion")
    assert na.wants_alerts("both", ["intrusion"], "intrusion")
    assert not na.wants_alerts("both", ["intrusion"], "motion")
    assert na.wants_alerts("both", [], "motion")                 # an empty list is "all"


def test_envelope_is_an_app_shaped_alert_with_a_camera_source():
    event = {"event_type": "Crossed", "event_state": "active", "rule": "Gate line",
             "description": "tns1:RuleEngine/LineDetector/Crossed", "occurred_at": "2026-10-24T10:00:00+00:00"}
    env = na.envelope_for(camera_id=7, camera_name="Gate", driver_name="onvif", event=event,
                          alert_type="line_crossing")
    assert env["title"] == "Line crossed: Gate line at Gate (camera)"
    assert env["camera_id"] == "cam7" and env["severity"] == "medium" and env["alert_type"] == "line_crossing"
    assert env["source"] == {"kind": "camera", "name": "onvif"}
    assert env["evidence"]["rule"] == "Gate line" and env["evidence"]["native"] is True
    assert env["evidence"]["observed_at"] == "2026-10-24T10:00:00+00:00"
    again = na.envelope_for(camera_id=7, camera_name="Gate", driver_name="onvif", event=event,
                            alert_type="line_crossing")
    assert again["alert_id"] == env["alert_id"]                   # the repeated edge is one alert
    other = na.envelope_for(camera_id=7, camera_name="Gate", driver_name="onvif",
                            event={**event, "occurred_at": "2026-10-24T10:00:01+00:00"},
                            alert_type="line_crossing")
    assert other["alert_id"] != env["alert_id"]


def test_apply_source_pauses_detection_only_for_camera_mode():
    cam = SimpleNamespace(analytics_source=None, detection_enabled=True)
    change = na.apply_source(cam, "camera")
    assert cam.analytics_source == "camera" and cam.detection_enabled is False
    assert change["from"] == {"analytics_source": "opennvr", "detection_enabled": True}
    na.apply_source(cam, "both")
    assert cam.analytics_source == "both" and cam.detection_enabled is True
    cam.detection_enabled = False                                 # the operator's own choice
    na.apply_source(cam, "opennvr")
    assert cam.analytics_source is None and cam.detection_enabled is False   # left alone


def test_route_delivers_only_when_the_camera_asks(monkeypatch):
    delivered: list[dict] = []

    class _Q:
        def __init__(self, rows):
            self._rows = rows

        def filter(self, *a, **k):
            return self

        def first(self):
            return self._rows

    class _Session:
        def __init__(self, camera):
            self.camera = camera

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def query(self, model):
            name = getattr(model, "__name__", "")
            return _Q(self.camera if name == "Camera" else SimpleNamespace(driver_name="hikvision"))

    camera = SimpleNamespace(id=3, name="Yard", analytics_source="both", native_event_types=["intrusion"],
                             manufacturer="Hikvision")
    import types
    fake_db = types.ModuleType("core.database")
    fake_db.SessionLocal = lambda: _Session(camera)
    monkeypatch.setitem(sys.modules, "core.database", fake_db)
    fake_models = types.ModuleType("models")
    fake_models.Camera = type("Camera", (), {"id": None})
    fake_models.CameraCapability = type("CameraCapability", (), {"camera_id": None})
    monkeypatch.setitem(sys.modules, "models", fake_models)

    async def fake_deliver(envelope):
        delivered.append(envelope)

    monkeypatch.setattr(na, "deliver", fake_deliver)

    assert asyncio.run(na.route(3, {"event_type": "fielddetection", "event_state": "active"})) == "intrusion"
    assert asyncio.run(na.route(3, {"event_type": "fielddetection", "event_state": "inactive"})) is None
    assert asyncio.run(na.route(3, {"event_type": "linedetection", "event_state": "active"})) is None   # not allowed
    camera.analytics_source = None
    assert asyncio.run(na.route(3, {"event_type": "fielddetection", "event_state": "active"})) is None
    assert len(delivered) == 1
    assert delivered[0]["source"] == {"kind": "camera", "name": "hikvision"}
    assert delivered[0]["title"] == "Intrusion at Yard (camera)"


def test_deliver_writes_the_inbox_then_the_bus(monkeypatch):
    calls: list[str] = []
    import types
    inbox = types.ModuleType("services.alerts_inbox")

    def apply_alert(envelope, db=None, out=None):
        calls.append("inbox")
        out.update({"id": 9, "severity": envelope["severity"]})
        return "stored"

    inbox.apply_alert = apply_alert
    monkeypatch.setitem(sys.modules, "services.alerts_inbox", inbox)
    bus = types.ModuleType("services.event_bus_service")

    async def publish_app_alert(**kw):
        calls.append(f"bell:{kw['camera_id']}:{kw['alert_type']}")

    bus.publish_app_alert = publish_app_alert
    monkeypatch.setitem(sys.modules, "services.event_bus_service", bus)

    async def fake_bus(envelope):
        calls.append("nats")

    monkeypatch.setattr(na, "_publish_bus", fake_bus)
    env = na.envelope_for(camera_id=4, camera_name="Dock", driver_name="dahua",
                          event={"event_type": "CrossLineDetection", "event_state": "active"},
                          alert_type="line_crossing")
    asyncio.run(na.deliver(env))
    assert calls == ["inbox", "bell:4:line_crossing", "nats"]


def test_migration_chains_off_head():
    text = (REPO_ROOT / "server" / "migrations" / "versions" / "aa22bb33cc44_add_camera_analytics_source.py").read_text()
    assert 'revision: str = "aa22bb33cc44"' in text
    assert 'down_revision: str | None = "e4b9c2d6f0a3"' in text
    assert 'sa.Column("analytics_source"' in text and 'sa.Column("native_event_types"' in text
