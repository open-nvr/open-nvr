# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""Testing an app — no broker, no core, no Docker.

Demonstrates: `RecorderChannel`, `app_config`, `detection`,
`inference_event`, `tier0_event`, `tier0_track`, `domain_event`, `feed`,
`FakeCore`, the pytest plugin, and `App.build`.

The forty lines every app's test suite used to carry are in the SDK, so
the shapes stay right when the contracts move. These are the tests worth
writing for a rule: it fires on the thing, it stays quiet on the
near-miss, and the envelope carries what a downstream consumer needs.
"""
from opennvr_app_sdk import App
from opennvr_app_sdk.testing import (
    FakeCore, RecorderChannel, app_config, detection, domain_event, feed,
    inference_event, tier0_event, tier0_track,
)

app = App("demo", name="Demo", category="analytics", summary="s")
app.param("min_confidence", float, default=0.5)


@app.on_detection("person", severity="high")
def rule(event):
    if event.confidence >= event.config.min_confidence:
        event.alert(f"Person on {event.camera}")


def build(**overrides):
    """The three lines that stand in for a whole deployment.

    `app_config` is a plain namespace: give it every key your rule
    reads, exactly as config.yml would."""
    recorder = RecorderChannel()
    settings = {"min_confidence": 0.5, **overrides}
    return app.build(app_config(**settings), recorder.dispatcher()), recorder


def test_fires_on_a_person():
    detector, recorder = build()
    fired = feed(detector, inference_event(detection("person", confidence=0.9),
                                           camera_id="cam-1"))
    assert [a.title for a in fired] == ["Person on cam-1"]
    # `feed` returns what fired; the recorder proves it was dispatched.
    assert recorder.alerts == fired
    assert recorder.titles == ["Person on cam-1"]


def test_quiet_on_the_near_miss():
    detector, _ = build(min_confidence=0.95)
    assert feed(detector, inference_event(detection("person", confidence=0.6))) == []


def test_the_envelope_carries_what_consumers_need():
    detector, _ = build()
    (alert,) = feed(detector, inference_event(
        detection("person", confidence=0.9, track_id="t1"),
        camera_id="cam-1", correlation_id="corr-1"))
    assert alert.severity == "high"
    assert alert.correlation_id == "corr-1"
    assert alert.evidence["track_id"] == "t1"


def test_tier0_events_too():
    """Tier-0 has its own builders — tracks carry pixel boxes and a
    frame size, not normalized ones."""
    detector, _ = build()
    event = tier0_event(tier0_track("person", track_id="t1", score=0.9),
                        camera_id="cam-1")
    detector.consume_tier0 = True
    assert isinstance(feed(detector, event), list)


def test_domain_events_too():
    """For a DomainEventSubscriber, `domain_event` builds the
    EVENT_CONTRACTS.md envelope around a payload."""
    event = domain_event("plate.recognized.v1", {"plate_text": "MH12AB1234"},
                         camera_id="cam-gate")
    assert event["schema"] == "plate.recognized.v1"


def test_against_a_whole_fake_platform():
    """`FakeCore` is a tiny in-process platform — cameras, snapshot,
    state, alerts, register — for apps that use `OpenNVR()`. No mocks,
    a real HTTP server, so the client's own parsing is under test too."""
    from opennvr_app_sdk import OpenNVR

    with FakeCore(cameras=[{"name": "Front door"}]) as core:
        nvr = OpenNVR(core.url, token="test-key")
        cameras = nvr.cameras()
        assert [c.name for c in cameras] == ["Front door"]
        nvr.state.set("seen", 3)
        assert nvr.state.get("seen") == 3


# Prefer fixtures? Add this to conftest.py and get `recorder`,
# `app_config_factory` and `fake_core` for free:
#
#     pytest_plugins = ["opennvr_app_sdk.testing.pytest_plugin"]
