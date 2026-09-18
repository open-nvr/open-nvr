# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings -- retention (a representative form that round-trips) and API tokens."""

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


class ApiTokensPage(BasePage):
    """Settings > API Tokens: create a token, read its one-time secret, revoke."""

    path = "/settings/api-tokens"
    ready = S.API_TOKEN_NEW

    def create(self, name: str, scopes: list[str] | None = None) -> str:
        """Fill and submit the form; return the secret it shows once.

        ``scopes=None`` keeps the Home Assistant preset the form starts with.
        """
        self.find(S.API_TOKEN_NEW).first.click()
        self.find(S.API_TOKEN_NAME).first.fill(name)
        if scopes is not None:
            for box in self.page.get_by_role("checkbox").all():
                label = box.locator("xpath=..").inner_text()
                wanted = any(label.startswith(s) for s in scopes)
                if box.is_enabled() and box.is_checked() != wanted:
                    box.click()
        self.find(S.API_TOKEN_CREATE).first.click()
        secret = self.find(S.API_TOKEN_SECRET).first
        secret.wait_for(state="visible")
        return secret.inner_text().strip()

    def row(self, name: str):
        return self.find(S.API_TOKEN_ROW).filter(has_text=name)

    def revoke(self, name: str):
        """Click Revoke on ``name`` (accepting the confirm); return the DELETE response."""
        self.accept_native_dialogs()
        with self.page.expect_response(
            lambda r: "/api-tokens/" in r.url and r.request.method == "DELETE"
        ) as caught:
            self.find(S.API_TOKEN_REVOKE, scope=self.row(name)).first.click()
        return caught.value
