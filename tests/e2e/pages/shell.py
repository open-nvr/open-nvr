# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The application shell -- navigation and the alert bell, present everywhere."""

from __future__ import annotations

from harness import selectors as S

from .base import BasePage


class Shell(BasePage):
    """Chrome that wraps every authenticated route."""

    path = "/"

    def nav_destinations(self) -> int:
        """How many navigation entries are currently rendered.

        Entries are gated on the caller's permissions, which are fetched after
        first paint, so this grows as that request resolves. Callers should
        poll rather than assert on one reading -- ``wait_for_nav`` does.
        """
        return self.find(S.NAV_LINKS).count()

    def wait_for_nav(self, at_least: int = 2) -> int:
        """Wait until the permission-gated nav has rendered."""
        return self.wait_for_count(S.NAV_LINKS, until=lambda n: n >= at_least)

    def nav_labels(self) -> list[str]:
        """Every destination on offer, for comparing what two roles can see.

        Group headers count, and leaving them out makes this measure nothing.
        Only the pinned NVR group renders its links directly; the other four
        are accordions that are collapsed by default, so their links are not
        in the DOM at all. A link-only reading therefore returns the same four
        entries for an administrator and for a viewer holding three
        permissions -- identical sets, and no evidence either way.

        The gating shows in the headers: a group with no permitted items is
        dropped, so an administrator gets four of them and a viewer none.
        """
        labels = [
            (text or "").strip()
            for text in self.find(S.NAV_LINKS).all_inner_texts()
            if (text or "").strip()
        ]
        labels += self.nav_group_labels()
        return labels

    def nav_group_labels(self) -> list[str]:
        """The collapsible group headers alone."""
        return [
            (text or "").strip()
            for text in self.find(S.NAV_GROUP_HEADERS).all_inner_texts()
            if (text or "").strip()
        ]

    def open_alert_bell(self) -> None:
        self.find(S.ALERT_BELL).first.click()
