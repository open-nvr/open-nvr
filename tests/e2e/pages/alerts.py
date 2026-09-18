# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Alerts & Incidents -- firing an alarm and silencing it."""

from __future__ import annotations

from harness import selectors as S
from harness.budgets import BUDGETS

from .base import BasePage


class AlertsPage(BasePage):
    """The Alarms tab, which is where acknowledgement happens."""

    path = "/alerts-incidents"
    ready = S.ALERTS_HEADING

    def fire_test_alarm(self) -> None:
        """Raise a synthetic alarm through the real ingestion path.

        The product provides this precisely so "is the alarm system working?"
        has a one-click answer, and it goes through the same ``apply_alert``,
        the same table and the same poll as a real alert -- not a UI-only sound
        test that would pass while the consumer is dead. That makes it the
        right lever here: no app, no bus and no footage needed, yet everything
        downstream of ingestion is genuinely exercised.
        """
        self.find(S.FIRE_TEST_ALARM).first.click()

    def unacked_rows(self):
        return self.page.get_by_role("row").filter(has_text="unacked")

    def wait_for_unacked(self, budget: float | None = None) -> int:
        return self.wait_for_count(
            S.UNACKED_STATUS,
            until=lambda n: n > 0,
            budget=budget or BUDGETS.ALERT_DELIVERED,
        )

    def acknowledge_first(self) -> None:
        """Click ``Ack`` on the first unacknowledged row.

        The button is rendered only while the alarm is unacknowledged, so its
        disappearance is itself the confirmation -- there is no snackbar to
        race here.
        """
        row = self.unacked_rows().first
        self.find(S.ACK_ROW, scope=row).first.click()

    def acknowledge_all(self) -> None:
        self.find(S.ACK_ALL).first.click()

    def unacked_count(self) -> int:
        return self.find(S.UNACKED_STATUS).count()

    def wait_for_unacked_count(self, expected: int, budget: float | None = None) -> int:
        """Poll until exactly ``expected`` alarms are unacknowledged.

        Counting is the only assertion that holds here. A locator for "the
        first unacked row" is live, so acknowledging that row makes the
        locator re-resolve to the NEXT unacked one -- which still has its Ack
        button, so a "the button went away" assertion against it can never
        come true once a second alarm exists. See the test for the full story.
        """
        return self.wait_for_count(
            S.UNACKED_STATUS,
            until=lambda n: n == expected,
            budget=budget or BUDGETS.ALERT_DELIVERED,
        )
