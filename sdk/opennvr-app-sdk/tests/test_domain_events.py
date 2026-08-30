# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""DomainEventPublisher — the producing side of EVENT_CONTRACTS.md."""
from __future__ import annotations

import pytest

from opennvr_app_sdk.domain_events import (
    DomainEventPublisher,
    domain_envelope,
    domain_subject,
)


def test_subject_shape():
    assert (domain_subject("access.decided.v1", "cam3")
            == "opennvr.events.access.decided.v1.cam3")


@pytest.mark.parametrize("schema", [
    "accessdecided.v1", "access.decided", "Access.Decided.v1",
    "access.decided.v", "a.b.c.v1", "",
])
def test_bad_schema_fails_loudly(schema):
    with pytest.raises(ValueError):
        domain_subject(schema, "cam1")


@pytest.mark.parametrize("cam", ["", "cam 1", "cam.1", "cam>*"])
def test_bad_camera_token_fails_loudly(cam):
    with pytest.raises(ValueError):
        domain_subject("access.decided.v1", cam)


def test_envelope_has_every_contract_field():
    env = domain_envelope(
        "access.decided.v1",
        camera_id="cam3",
        payload={"decision": "allow"},
        producer="app:license-plate-recognition",
        correlation_id="corr-9",
    )
    assert set(env) == {"id", "schema", "correlation_id", "camera_id",
                        "ts", "producer", "payload"}
    assert env["id"].startswith("evt_")
    assert env["schema"] == "access.decided.v1"
    assert env["camera_id"] == "cam3"
    assert env["producer"] == "app:license-plate-recognition"
    assert env["payload"] == {"decision": "allow"}
    import json
    json.dumps(env)  # wire-serializable


def test_publisher_builds_subject_and_envelope(monkeypatch):
    pub = DomainEventPublisher("nats://x:4222", token="t",
                               producer="app:lpr")
    seen = {}

    def fake_publish_json(subject, obj):
        seen["subject"] = subject
        seen["obj"] = obj
        return True

    monkeypatch.setattr(pub._channel, "publish_json", fake_publish_json)
    ok = pub.publish("access.decided.v1", camera_id="cam7",
                     payload={"decision": "deny", "reason": "unknown"},
                     correlation_id="c1")
    assert ok is True
    assert seen["subject"] == "opennvr.events.access.decided.v1.cam7"
    assert seen["obj"]["schema"] == "access.decided.v1"
    assert seen["obj"]["correlation_id"] == "c1"
    assert seen["obj"]["payload"]["reason"] == "unknown"


def test_alert_channel_send_still_works_via_publish_json(monkeypatch):
    """The extraction must not change alert publishing semantics."""
    from opennvr_app_sdk.alerts import Alert, AlertSource, NatsAlertChannel

    ch = NatsAlertChannel("nats://x:4222")
    captured = {}
    monkeypatch.setattr(
        ch, "publish_json",
        lambda subject, obj: captured.update(subject=subject, obj=obj) or True)
    alert = Alert(severity="low", title="t", description="d",
                  camera_id="cam1", source=AlertSource())
    assert ch.send(alert) is True
    assert captured["subject"].endswith(".cam1")
    assert captured["obj"]["title"] == "t"
