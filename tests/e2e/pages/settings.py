# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings -- retention, as a representative form that round-trips."""

from __future__ import annotations

from harness import selectors as S

from .base import BasePage


class RecordingSettingsPage(BasePage):
    """The Retention Policy form on /settings/recording."""

    path = "/settings/recording"

    def set_retention_days(self, days: int) -> None:
        self.find(S.RETENTION_DAYS).fill(str(days))

    def retention_days(self) -> str:
        return self.find(S.RETENTION_DAYS).input_value()

    def set_protect_flagged(self, on: bool) -> None:
        checkbox = self.find(S.PROTECT_FLAGGED)
        checkbox.check() if on else checkbox.uncheck()

    def save(self):
        """Submit, returning the PUT response.

        Waiting on the network rather than the snackbar is deliberate: the
        snackbar auto-dismisses after five seconds, so an assertion against it
        races the timer on a slow machine and fails for reasons that have
        nothing to do with saving.
        """
        with self.page.expect_response(
            lambda r: "/recordings/retention" in r.url and r.request.method == "PUT"
        ) as caught:
            self.find(S.SAVE_RETENTION).click()
        return caught.value
