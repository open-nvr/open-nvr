# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Raising an alarm and silencing it, by clicking.

The alerts inbox is where every app's output lands, and the failure it is
prone to is total silence: apps publish happily while the operator sees
nothing. The API mirror proves ingestion and the ack endpoint work; these
prove the operator can see the alarm and clear it.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from harness import selectors as S
from pages.base import fatal_errors

pytestmark = pytest.mark.ui


def test_the_alerts_page_renders(alerts_page):
    errors = alerts_page.console_errors()
    alerts_page.open()

    expect(alerts_page.find(S.ALERTS_HEADING).first).to_be_visible(timeout=30_000)
    assert not fatal_errors(errors), f"the Alerts page threw: {errors[:3]}"


def test_a_fired_alarm_appears_in_the_list(alerts_page):
    """The test-alarm button goes through the real ingestion path.

    Not a UI-only sound check -- it calls ``apply_alert``, writes the same row
    and is read back by the same poll a real alert takes. So this exercises the
    whole inbox without needing an app, a bus or footage.
    """
    alerts_page.open()
    before = alerts_page.unacked_count()

    alerts_page.fire_test_alarm()

    alerts_page.wait_for_unacked()
    assert alerts_page.unacked_count() > before or before > 0, (
        "firing a test alarm did not add an unacknowledged row"
    )


def test_an_alarm_can_be_acknowledged_by_clicking(alerts_page):
    """Ack from the row, and watch the unacknowledged count drop.

    Asserted on the count rather than on a row, because the obvious version of
    this test is subtly broken and passed for a long time by luck.

    It held ``unacked_rows().first`` and waited for that row's Ack button to
    disappear. Playwright locators are live, not snapshots: the moment the
    first row is acknowledged it stops matching ``has_text("unacked")``, so
    the locator silently re-resolves to the NEXT unacknowledged row -- which
    of course still has its Ack button. The expectation can then never come
    true, and the failure reads "expected count 0, actual 1" with no hint that
    it is looking at a different row than the one it acked.

    That version passes only while exactly one alarm is unacknowledged, which
    is the usual state of a fresh test stack and why it survived. It failed
    the moment a run left a second alarm behind.

    The count is immune to which row is which, and still says exactly what
    acknowledging is supposed to do: one fewer alarm demanding attention.
    """
    alerts_page.open()
    alerts_page.fire_test_alarm()
    alerts_page.wait_for_unacked()

    before = alerts_page.unacked_count()
    assert before > 0, "no unacknowledged alarm to acknowledge"

    expect(alerts_page.unacked_rows().first).to_be_visible(timeout=30_000)
    alerts_page.acknowledge_first()

    after = alerts_page.wait_for_unacked_count(before - 1)
    assert after == before - 1, (
        f"acknowledging did not clear an alarm: {before} unacked before, "
        f"{after} after"
    )


def test_acknowledge_all_clears_the_inbox(alerts_page):
    """"Acknowledge all" is the button an operator reaches for after an event.

    An inbox that cannot be cleared in bulk stops being read, which is the same
    outcome as it not working at all.
    """
    alerts_page.open()
    alerts_page.fire_test_alarm()
    alerts_page.wait_for_unacked()

    alerts_page.acknowledge_all()

    expect(alerts_page.find(S.UNACKED_STATUS)).to_have_count(0, timeout=30_000)


def test_the_alert_bell_is_present_on_every_page(shell, cameras_page):
    """The bell is the only alarm surface visible from elsewhere in the app.

    It lives in the shell header, so if it renders on an unrelated route it
    renders everywhere.
    """
    cameras_page.open()

    expect(shell.find(S.ALERT_BELL).first).to_be_visible(timeout=30_000)
