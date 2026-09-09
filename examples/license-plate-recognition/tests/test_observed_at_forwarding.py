# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The alert carries WHEN the plate was seen, not just when we decided.

An Alert's ``fired_at`` is stamped as this app builds it — after the read
crossed the bus and this handler ran. The operator inbox showed that as
the alarm's only time, so one plate read appeared at one moment on the
Alarms page and a different one in the vehicle list, drifting apart by
however long OCR and delivery took (#451).

The platform now puts the look's capture time in the event payload; this
app forwards it in the alert's evidence, where core's inbox picks it up.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from license_plate_recognition import AppConfig, PlateAlerter

SEEN = "2026-08-29T09:59:52+00:00"


def _alerter() -> PlateAlerter:
    return PlateAlerter(AppConfig(nats_url="nats://test:4222"), MagicMock())


def _envelope(**payload_extra):
    return {
        "id": "evt_0123456789ab",
        "schema": "plate.recognized.v1",
        "correlation_id": "corr-1",
        "camera_id": "cam-1",
        # Publish time — deliberately NOT the capture time, so a test
        # that read the wrong field would show up as a wrong value.
        "ts": "2026-08-29T10:00:00+00:00",
        "producer": "kai-c",
        "payload": {
            "plate_text": "ABC1234",
            "confidence": 0.9,
            "vehicle_label": "car",
            "event_id": 42,
            **payload_extra,
        },
    }


def _evidence_of(envelope) -> dict:
    fired = _alerter().handle_event(envelope)
    assert len(fired) == 1
    return fired[0].evidence


def test_the_alert_forwards_the_capture_time():
    assert _evidence_of(_envelope(observed_at=SEEN))["observed_at"] == SEEN


def test_the_capture_time_is_not_the_alerts_own_fired_at():
    """The two must stay distinguishable: that gap IS the pipeline lag
    the single-timestamp display was hiding."""
    alert = _alerter().handle_event(_envelope(observed_at=SEEN))[0]
    assert alert.evidence["observed_at"] == SEEN
    assert alert.fired_at != SEEN


def test_a_platform_that_sends_no_capture_time_forwards_none():
    """Optional by contract: an older platform publishes no observed_at,
    the field lands as None, and core's inbox falls back to fired_at."""
    assert _evidence_of(_envelope())["observed_at"] is None


def test_a_junk_capture_time_is_not_forwarded_as_one():
    """This app does not parse the value — it relays a wire string — but
    it must not pass a non-string through as though it were a timestamp."""
    for junk in (12345, {"a": 1}, [], "", None):
        assert _evidence_of(_envelope(observed_at=junk))["observed_at"] is None
