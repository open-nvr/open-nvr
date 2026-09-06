# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""opennvr_app_sdk.testing — the helpers app test suites build on."""
from __future__ import annotations

import pytest

from opennvr_app_sdk import (
    Alert, AlertType, AppManifest, Detector, DomainEventSubscriber, OpenNVR, PlateRecognized,
    tier0_to_detections,
)
from opennvr_app_sdk.testing import (
    FakeCore, RecorderChannel, app_config, detection, domain_event, feed, inference_event,
    tier0_event, tier0_track,
)

pytest_plugins = ["opennvr_app_sdk.testing.pytest_plugin"]


class _PersonWatch(Detector):
    manifest = AppManifest(id="person-watch", name="Person Watch", version="1.0.0",
                           category="analytics", emits=[AlertType("person_seen")])

    def on_detections(self, camera_id, detections, event):
        return [Alert(title=f"{d['label']} seen", description="", camera_id=camera_id)
                for d in detections if d["label"] in self.cfg.watch_labels]


def test_detector_through_the_helpers():
    rec = RecorderChannel()
    app = _PersonWatch(app_config(watch_labels=["person"]), rec.dispatcher())
    fired = feed(app,
                 inference_event(detection("person"), detection("bike"), camera_id="yard"),
                 inference_event(detection("car")),
                 b"{not json")
    assert [a.title for a in fired] == ["person seen"] and fired[0].camera_id == "yard"
    assert rec.alerts == fired and rec.titles == ["person seen"]
    rec.clear()
    assert rec.alerts == []


def test_builders_have_the_contract_shapes():
    ev = inference_event(detection("person", x=0.5, y=0.6, track_id=7, confidence=0.7))
    assert ev["result"]["detections"][0] == {"label": "person", "confidence": 0.7,
                                             "bbox": {"x": 0.5, "y": 0.6, "w": 0.1, "h": 0.1},
                                             "track_id": 7}
    assert ev["camera_id"] == "cam-1" and ev["adapter"] == "yolov8" and ev["completed_at"].endswith("Z")
    t0 = tier0_event(tier0_track("car", bbox=(0, 0, 640, 360)), camera_id="gate")
    dets = tier0_to_detections(t0)
    assert dets[0]["label"] == "car" and dets[0]["bbox"] == {"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5}
    env = domain_event("", PlateRecognized("R197GB", confidence=0.9), camera_id="gate", producer="lpr")
    assert env["schema"] == "plate.recognized.v1" and env["payload"]["plate_text"] == "R197GB"
    assert env["camera_id"] == "gate" and env["producer"] == "lpr" and env["id"].startswith("evt_")
    env2 = domain_event("custom.thing.v1", {"a": 1})
    assert env2["schema"] == "custom.thing.v1" and env2["payload"] == {"a": 1}


class _Gate(DomainEventSubscriber):
    manifest = AppManifest(id="gate", name="Gate", version="1.0.0", category="vehicle")
    subscriptions = ["plate.recognized.v1"]

    def on_event(self, event):
        plate = event.typed()
        if plate is not None:
            self.fire(Alert(title=f"plate {plate.plate_text}", description="", camera_id=event.camera_id))


def test_domain_subscriber_through_the_helpers(recorder, app_config_factory):
    app = _Gate(app_config_factory(), dispatcher=recorder.dispatcher())
    feed(app, domain_event("", PlateRecognized("AB12CD"), camera_id="gate"), subject="s")
    assert recorder.titles == ["plate AB12CD"]


def test_fake_core_serves_the_platform_client(fake_core):
    fake_core.cameras = [FakeCore._camera({"name": "Gate", "assignments": [{"skill": "lpr"}]}, 1),
                         FakeCore._camera({"name": "Yard"}, 2)]
    fake_core.snapshots["cam1"] = b"\xff\xd8jpeg"
    nvr = OpenNVR(fake_core.url, token="oak_test-app_" + "0" * 32)
    cams = nvr.cameras()
    assert [c.name for c in cams] == ["Gate", "Yard"]
    assert nvr.snapshot(cams[0]) == b"\xff\xd8jpeg" and nvr.snapshot(cams[1]) is None
    nvr.state.set("seen", {"n": 3})
    assert fake_core.state == {"seen": {"n": 3}}
    assert nvr.state.get("seen") == {"n": 3} and nvr.state.get("nope", 0) == 0
    assert nvr.state.items() == {"seen": {"n": 3}}
    assert nvr.state.delete("seen") is True and nvr.state.delete("seen") is False
    assert fake_core.requests[0]["path"] == "/api/v1/internal/camera-agent/cameras"
    assert fake_core.requests[0]["key"].startswith("oak_")


def test_fake_core_context_manager_and_register():
    import json
    import urllib.request

    with FakeCore() as core:
        req = urllib.request.Request(core.url + "/api/v1/apps/register", method="POST",
                                     data=json.dumps({"url": "http://a:9200", "manifest": {"id": "a"},
                                                      "wants_key": True}).encode(),
                                     headers={"Content-Type": "application/json"})
        body = json.loads(urllib.request.urlopen(req, timeout=5).read())
        assert body["api_key"] == core.app_key and body["registry"]["api_version"] == "1.4"
        assert core.registrations[0]["manifest"] == {"id": "a"}
