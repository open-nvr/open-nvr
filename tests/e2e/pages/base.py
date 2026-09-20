# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared behaviour for every page object."""

from __future__ import annotations

from typing import Any

from harness.budgets import BUDGETS
from harness.selectors import Selector
from harness.waiting import eventually

#: Playwright's own waits are in milliseconds; the suite thinks in seconds.
DEFAULT_TIMEOUT_MS = int(BUDGETS.QUICK * 1000)


class BasePage:
    """A view, and the things every view can do.

    Subclasses set ``path`` and add journey methods. Anything that needs a
    selector goes through :meth:`find`, so no page object ever contains a raw
    string locator either -- selectors live in one file and one file only.
    """

    #: Route this page lives at, relative to the base URL.
    path: str = "/"
    #: Selector that proves the page actually rendered, checked by ``open()``.
    ready: Selector | None = None

    def __init__(self, page: Any) -> None:
        self.page = page

    # -- navigation ------------------------------------------------------
    def open(self) -> "BasePage":
        """Navigate here and wait for the page to be recognisably itself.

        Asserting on ``ready`` rather than on ``load`` matters: this is a SPA,
        so the document finishes loading long before the route has fetched its
        data and rendered. A test that starts clicking at ``load`` races the
        first paint every time.
        """
        self.page.goto(self.path)
        if self.ready is not None:
            self.find(self.ready).first.wait_for(
                state="visible", timeout=DEFAULT_TIMEOUT_MS
            )
        return self

    # -- locating --------------------------------------------------------
    def find(self, selector: Selector, scope: Any = None, **fmt: Any) -> Any:
        """Resolve a selector against this page, or within ``scope``."""
        return selector.locate(scope if scope is not None else self.page, **fmt)

    def wait_for_count(
        self, selector: Selector, until, *, budget: float | None = None, **fmt: Any
    ) -> int:
        """Poll a selector's match count until ``until(count)`` holds.

        Needed because much of this UI renders progressively -- permission-gated
        nav, tables that populate after a fetch -- so a single count taken too
        early is a snapshot of the network, not of the page.
        """
        locator = self.find(selector, **fmt)
        return eventually(
            locator.count,
            until=until,
            budget=budget or BUDGETS.QUICK,
            describe=f"{selector.name} to settle",
        )

    # -- native dialogs --------------------------------------------------
    def accept_native_dialogs(self) -> None:
        """Auto-accept ``window.confirm`` / ``alert`` for the rest of this page.

        Playwright **dismisses** dialogs by default, so a delete guarded by
        ``window.confirm`` silently does nothing and the test then fails on a
        row that is still there -- with no hint that a dialog was involved.
        Camera delete, bulk delete and Live View's shutdown all need this.
        """
        self.page.on("dialog", lambda dialog: dialog.accept())

    # -- diagnostics -----------------------------------------------------
    def console_errors(self) -> list[str]:
        """Start collecting uncaught page errors. Call before navigating.

        A React route that throws during render leaves a blank page and a
        console error and nothing else -- invisible to any API-level check.
        """
        errors: list[str] = []
        self.page.on("pageerror", lambda exc: errors.append(str(exc)))
        return errors


def fatal_errors(errors: list[str]) -> list[str]:
    """Filter out console noise that is not a real failure.

    ``ResizeObserver loop completed with undelivered notifications`` is emitted
    by well-behaved layout code in Chromium and means nothing here.
    """
    return [e for e in errors if "ResizeObserver" not in e]


__all__ = ["BasePage", "fatal_errors", "DEFAULT_TIMEOUT_MS"]
