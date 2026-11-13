# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An alert is raised, reaches the inbox, and can be silenced.

The alerts inbox is where every app's output lands: line crossings, loitering,
intrusion, plate hits. If the inbox is broken, an operator sees nothing at all
while the apps keep happily publishing -- the most complete kind of silent
failure this product has, and the one the apps bus outage produced for months.

``POST /alerts-inbox/test`` exists precisely so that "is the alarm system
working?" has a one-click answer. Crucially it fires through the **real**
ingestion path -- the same ``apply_alert``, the same table, the same poll --
rather than being a UI-only sound test that would pass while the consumer is
dead. That makes it the right lever for an end-to-end test: no app, no bus, no
footage needed, but everything downstream of ingestion is genuinely exercised.

Acknowledgement is tested alongside because an inbox you cannot clear is not
usable, and because ack is where per-caller scoping applies.
"""

from __future__ import annotations

import pytest

from harness import routes
from harness.budgets import BUDGETS
from harness.waiting import eventually

pytestmark = pytest.mark.smoke


def _alerts(client, **params) -> dict:
    return client.json(routes.ALERTS_INBOX, params=params or None)


def test_a_fired_alert_reaches_the_inbox(client):
    """The ingestion path, end to end.

    An alert appearing here means it went through ``apply_alert``, landed in
    the table, and came back out of the listing the bell polls.
    """
    before = _alerts(client, unacked="true").get("unacked_count", 0)

    client.post(routes.ALERTS_INBOX_TEST, json_body={"severity": "high"})

    after = eventually(
        lambda: _alerts(client, unacked="true"),
        until=lambda payload: payload.get("unacked_count", 0) > before,
        budget=BUDGETS.ALERT_DELIVERED,
        describe="a fired test alert to appear in the inbox",
    )

    assert after["alerts"], "unacked_count rose but the listing is empty"
    assert after["unacked_count"] > before


def test_an_alert_carries_what_the_ui_needs_to_show_it(client):
    """A row nobody can render is not an alert.

    The Alerts & Incidents page and the notification bell both key off these
    fields; a row that arrives without them is delivered and useless.
    """
    client.post(routes.ALERTS_INBOX_TEST, json_body={"severity": "high"})

    alert = eventually(
        lambda: next(iter(_alerts(client, limit=10).get("alerts") or []), None),
        budget=BUDGETS.ALERT_DELIVERED,
        describe="an alert to be listed",
    )

    assert alert.get("id"), f"the alert has no id, so it cannot be acked: {alert}"
    assert alert.get("severity"), f"the alert has no severity: {alert}"


def test_an_unknown_severity_is_rejected(client):
    """The severity vocabulary is closed.

    Severity drives the ring configuration -- how loudly, and whether, an
    operator is interrupted. Accepting an unknown value would create alerts
    that no ring rule matches and that therefore never make a sound.
    """
    response = client.post(
        routes.ALERTS_INBOX_TEST,
        json_body={"severity": "not-a-real-severity"},
        expect=None,
    )

    assert response.status_code == 422, (
        f"an unknown severity was accepted (HTTP {response.status_code}); it "
        "would produce alerts that match no ring rule and stay silent"
    )


def test_acknowledging_clears_an_alert(client):
    """An inbox you cannot clear fills up and stops being read.

    Acks the specific alert rather than everything, so the test does not
    depend on -- or disturb -- whatever else is in the inbox.
    """
    client.post(routes.ALERTS_INBOX_TEST, json_body={"severity": "high"})

    alert = eventually(
        lambda: next(
            (
                row
                for row in _alerts(client, unacked="true", limit=20).get("alerts") or []
                if row.get("id")
            ),
            None,
        ),
        budget=BUDGETS.ALERT_DELIVERED,
        describe="an unacknowledged alert to ack",
    )
    alert_id = alert["id"]

    client.post(routes.ALERTS_INBOX_ACK, json_body={"ids": [alert_id]})

    remaining = eventually(
        lambda: _alerts(client, unacked="true", limit=50),
        until=lambda payload: alert_id
        not in {row.get("id") for row in payload.get("alerts") or []},
        budget=BUDGETS.QUICK,
        describe=f"alert {alert_id} to disappear from the unacknowledged list",
    )

    assert alert_id not in {row.get("id") for row in remaining.get("alerts") or []}


def test_acknowledging_twice_is_harmless(client):
    """Ack is idempotent, and the first silencer keeps the audit trail.

    The bell can double-fire and two operators can hit acknowledge at once;
    neither should produce an error or rewrite who silenced it first.
    """
    client.post(routes.ALERTS_INBOX_TEST, json_body={"severity": "high"})

    alert = eventually(
        lambda: next(
            (
                row
                for row in _alerts(client, unacked="true", limit=20).get("alerts") or []
                if row.get("id")
            ),
            None,
        ),
        budget=BUDGETS.ALERT_DELIVERED,
        describe="an unacknowledged alert to ack twice",
    )

    first = client.post(
        routes.ALERTS_INBOX_ACK, json_body={"ids": [alert["id"]]}, expect=None
    )
    second = client.post(
        routes.ALERTS_INBOX_ACK, json_body={"ids": [alert["id"]]}, expect=None
    )

    assert first.status_code < 400, f"the first ack failed: {first.text[:200]}"
    assert second.status_code < 400, (
        f"acking an already-acknowledged alert errored (HTTP "
        f"{second.status_code}): {second.text[:200]}"
    )
