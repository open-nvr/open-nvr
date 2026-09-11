# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""The cookbook is executable documentation.

Every file in `cookbook/` is imported here, which is the point: an
example that references a name the SDK no longer exports, or calls a
method with the wrong signature, fails in CI rather than misleading a
reader months later. The runnable ones are then actually run.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

COOKBOOK = Path(__file__).resolve().parent.parent / "cookbook"
FILES = sorted(COOKBOOK.glob("*.py"))


def load(path: Path):
    name = f"cookbook_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)          # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module", params=FILES, ids=lambda p: p.stem)
def example(request):
    return load(request.param)


def test_the_cookbook_is_not_empty():
    assert len(FILES) >= 19, "cookbook files went missing"


def test_every_example_imports(example):
    """Importing is the assertion: every name it references exists, and
    nothing reaches the network at import time."""
    assert example.__doc__, f"{example.__name__} has no module docstring"


def test_every_example_names_what_it_demonstrates(example):
    """The docstring's `Demonstrates:` line is the index entry — without
    it an example is just code."""
    assert "Demonstrates:" in (example.__doc__ or "")


# ── The examples that can actually run, run ─────────────────────────


def test_facade_example_builds_a_working_app():
    module = load(COOKBOOK / "01_app_facade.py")
    from opennvr_app_sdk.testing import (
        RecorderChannel, app_config, detection, feed, inference_event,
    )

    manifest = module.app.manifest()
    assert manifest.id == "driveway-watch"
    assert {a.name for a in manifest.emits} == {"loitering", "vehicle-left"}
    # The zone is a param of its own, named after itself — the shape the
    # catalog's geometry editor writes.
    assert [p.name for p in manifest.params] == ["driveway", "night_only", "dwell_s"]
    assert [v.kind for v in manifest.state_schema] == ["metric", "gauge", "log"]
    assert [a.name for a in manifest.actions] == ["mute"]

    cfg = app_config(
        night_only=False, dwell_s=30.0,
        driveway={"cam-1": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]})
    detector = module.app.build(cfg, RecorderChannel().dispatcher())
    # Five people in one frame trips the whole-frame rule immediately.
    fired = feed(detector, inference_event(*[detection("person") for _ in range(5)]))
    assert any("5 people" in a.title for a in fired)
    # The declared dashboard resolves against the store the rules keep.
    snapshot = detector.state_snapshot()
    for view in manifest.state_schema:
        assert view.path in snapshot
    # The declared action dispatches, defaults filled in.
    assert detector.on_action("mute", {}) == {"muted_for_minutes": 60}


def test_detector_example_fires_after_the_dwell():
    import datetime as dt

    module = load(COOKBOOK / "02_detector.py")
    from opennvr_app_sdk.testing import (
        RecorderChannel, detection, feed, inference_event,
    )

    cfg = module.AppConfig(nats_url="nats://test:4222", dwell_s=30.0)
    detector = module.Loitering(cfg, RecorderChannel().dispatcher())

    base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    titles = []
    for second in (0, 15, 31, 45):
        stamp = (base + dt.timedelta(seconds=second)).isoformat().replace("+00:00", "Z")
        titles += [a.title for a in feed(detector, inference_event(
            detection("person"), camera_id="cam-1", completed_at=stamp))]
    # Once the threshold is crossed, and once only — the latch works.
    assert titles == ["Loitering on cam-1"]


def test_detector_example_validates_its_config():
    module = load(COOKBOOK / "02_detector.py")
    with pytest.raises(ValueError, match="dwell_s"):
        module.AppConfig(nats_url="nats://test:4222", dwell_s=0)


def test_geometry_and_state_example():
    module = load(COOKBOOK / "08_state_and_geometry.py")
    from opennvr_app_sdk import Point

    assert module.in_driveway({"bbox": {"x": 0.4, "y": 0.6, "w": 0.1, "h": 0.1}})
    assert not module.in_driveway({"bbox": {"x": 0.4, "y": 0.0, "w": 0.1, "h": 0.1}})
    assert module.crossed(Point(0.5, 0.2), Point(0.5, 0.8)) in ("a_to_b", "b_to_a")
    assert module.crossed(Point(0.5, 0.2), Point(0.5, 0.3)) is None

    dwell = module.Dwell(threshold_s=30.0)
    assert dwell.saw("cam-1", "t1", at=0.0) is None
    assert dwell.saw("cam-1", "t1", at=31.0) == pytest.approx(31.0)
    assert dwell.saw("cam-1", "t1", at=40.0) is None       # latched
    assert dwell.tracked == 1


def test_manifest_example_declares_every_surface():
    module = load(COOKBOOK / "09_manifest_and_surfaces.py")
    from opennvr_app_sdk.openapi import contract_openapi
    from opennvr_app_sdk.testing import RecorderChannel, app_config

    manifest = module.MANIFEST
    assert {v.kind for v in manifest.state_schema} == {
        "metric", "gauge", "table", "log", "gallery"}
    assert [a.name for a in manifest.actions] == ["search", "reindex"]

    detector = module.FootageSearch(app_config(), RecorderChannel().dispatcher())
    # Every declared state path resolves into the snapshot.
    snapshot = detector.state_snapshot()
    for view in manifest.state_schema:
        assert view.path.split(".", 1)[0] in snapshot
    # Actions dispatch, and their error contract holds.
    assert detector.on_action("search", {"query": "red car"}) == {"hits": []}
    with pytest.raises(ValueError):
        detector.on_action("search", {"query": "  "})
    with pytest.raises(KeyError):
        detector.on_action("nope", {})
    assert "Footage Search" in detector.ui_html()
    # And the generated spec has a path per declared action.
    paths = contract_openapi(manifest)["paths"]
    assert "/actions/search" in paths and "/actions/reindex" in paths
    assert "/ui" in paths


def test_selling_example_verifies_and_refuses_keys():
    import hashlib
    import hmac

    module = load(COOKBOOK / "10_selling_an_app.py")
    from opennvr_app_sdk.testing import RecorderChannel, app_config

    detector = module.AnprPro(app_config(max_cameras=2),
                              RecorderChannel().dispatcher())
    payload = "pro:2027-01-01:10"
    signature = hmac.new(module._SECRET, payload.encode(),
                         hashlib.sha256).hexdigest()[:16]

    good = detector.verify_license(f"{payload}.{signature}")
    assert good.valid and good.plan == "pro" and good.limits == {"cameras": 10}
    assert detector.verify_license(f"{payload}.deadbeefdeadbeef").valid is False
    assert detector.verify_license("nonsense").valid is False

    # The verdict is applied live, and idempotently.
    detector.on_entitlement_update({"plan": "pro", "limits": {"cameras": 10}})
    detector.on_entitlement_update({"plan": "pro", "limits": {"cameras": 10}})
    assert (detector.plan, detector.camera_limit) == ("pro", 10)


def test_the_testing_example_passes_its_own_tests():
    """16_testing.py is a test file about testing; run it."""
    module = load(COOKBOOK / "16_testing.py")
    for name in dir(module):
        if name.startswith("test_"):
            getattr(module, name)()


def test_contract_server_example_serves_the_whole_surface():
    import httpx

    module = load(COOKBOOK / "17_contract_server.py")
    server = module.serve()
    port = server.port
    try:
        base = f"http://127.0.0.1:{port}"
        assert httpx.get(f"{base}/health", trust_env=False).json()["ready"] is True
        assert httpx.get(f"{base}/manifest", trust_env=False).json()["id"] == "standalone"
        assert httpx.get(f"{base}/openapi.json", trust_env=False).json()["openapi"] \
            == "3.1.0"
        assert httpx.get(f"{base}/asyncapi.json", trust_env=False).json()["asyncapi"] \
            == "3.0.0"
        # The action surface is key-gated, so an unauthenticated POST is 401.
        assert httpx.post(f"{base}/actions/ping", json={}, trust_env=False)\
            .status_code == 401
    finally:
        server.stop()


def test_alerts_example_builds_a_valid_envelope():
    module = load(COOKBOOK / "18_alerts_and_channels.py")

    alert = module.a_well_formed_alert("cam-front-door", "corr-1")
    wire = alert.to_wire()
    assert set(wire) >= {"alert_id", "fired_at", "title", "description", "severity",
                         "source", "camera_id", "correlation_id", "evidence", "tags"}
    assert module.where_it_lands(alert).startswith("opennvr.alerts.app.")

    relayed = module.emitting_as_someone_else()
    assert relayed.source.name == "siem-bridge"

    dispatcher = module.custom_fan_out("http://webhook.invalid", "http://pager.invalid")
    assert [c.name for c in dispatcher.channels] == ["stdout", "webhook", "pager"]


def test_publisher_example_builds_the_contracted_envelope():
    module = load(COOKBOOK / "12_domain_event_publisher.py")

    subject, envelope = module.what_the_wire_looks_like("cam-gate")
    assert subject == "opennvr.events.plate.recognized.v1.cam-gate"
    assert set(envelope) >= {"id", "schema", "camera_id", "ts", "producer", "payload"}
    assert envelope["schema"] == "plate.recognized.v1"

    typed = module.read_it_back("plate.recognized.v1", {"plate_text": "MH12AB1234"})
    assert typed is not None and typed.plate_text == "MH12AB1234"
    assert module.read_it_back("not.a.schema.v1", {}) is None


def test_tier0_example_reads_a_snapshot():
    module = load(COOKBOOK / "11_tier0.py")
    from opennvr_app_sdk.testing import tier0_event, tier0_track

    event = tier0_event(tier0_track("person", track_id="t1"),
                        tier0_track("car", track_id="t2"),
                        tier0_track("car", track_id="t3"),
                        camera_id="cam-dock")
    described = module.read_the_snapshot("opennvr.inference.tier0.cam-dock", event)
    assert described and "person" in described and "car" in described
    # A non-tier0 subject is not this function's business.
    assert module.read_the_snapshot("opennvr.inference.yolov8", event) is None


def test_credentials_example_builds_the_expected_header():
    module = load(COOKBOOK / "15_cameras_and_credentials.py")
    assert module.who_am_i("a-key")["X-Internal-Api-Key"] == "a-key"
    assert module.default_zone_for("cam-1")[0] == [0, 0]
