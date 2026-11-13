# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every page renders for an admin without crashing.

The safety net for UI refactors: one parametrised test per route in
``app/src/main.tsx`` (plus the settings, access-control and network sub-tabs),
each asserting the four failures a restyle most often introduces and that no
API test can see:

* **an uncaught exception** -- a component that throws during render;
* **the error boundary** -- the route crashed and the shell caught it (it
  logs ``ErrorBoundary caught`` and shows "Something went wrong");
* **a 5xx from the API** -- the page's own requests failing server-side;
* **horizontal overflow** -- the page is wider than the window, so the whole
  view scrolls sideways.

Deliberately NOT asserted: plain ``console.error`` output. Several pages log
expected 404s (optional adapters, an app that is not installed), and a test
that fails on those would be red from day one and ignored thereafter. Such
messages are attached to the failure text only, for context.

Known failures are listed in ``KNOWN_5XX`` / ``KNOWN_OVERFLOW`` with the reason,
so the test is green on today's ``main`` and turns red on anything *new*.
Remove an entry when its fix lands -- that is what keeps the list honest.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from pages.app_routes import ROUTES
from pages.base import BasePage, fatal_errors

pytestmark = pytest.mark.ui

# Endpoints that return 5xx on a correctly working stack today, with why.
# Matched as substrings of the request URL.
KNOWN_5XX: dict[str, str] = {}

# Routes whose layout is wider than the 1400px test window today.
KNOWN_OVERFLOW: dict[str, str] = {}

# How long a page gets to settle: its first fetches fire on mount, and a 5xx
# that arrives after the assertion would be missed.
SETTLE_MS = 2_500


@pytest.mark.parametrize("path", ROUTES)
def test_the_page_renders_without_crashing(authed_page, path):
    page = BasePage(authed_page)
    page.path = path

    thrown = page.console_errors()
    logged: list[str] = []
    server_errors: list[str] = []

    def on_console(msg):
        if msg.type == "error":
            logged.append(msg.text[:300])

    def on_response(resp):
        if resp.status >= 500 and not any(k in resp.url for k in KNOWN_5XX):
            server_errors.append(f"{resp.status} {resp.request.method} {resp.url}")

    authed_page.on("console", on_console)
    authed_page.on("response", on_response)

    page.open()
    expect(authed_page.locator("main")).to_be_visible(timeout=30_000)
    authed_page.wait_for_timeout(SETTLE_MS)

    context = f"\nconsole errors seen: {logged[:5]}" if logged else ""

    assert not fatal_errors(thrown), f"{path} threw while rendering: {thrown[:3]}{context}"

    crashed = [m for m in logged if "ErrorBoundary caught" in m]
    boundary_visible = authed_page.get_by_text("This view crashed").count() > 0
    assert not crashed and not boundary_visible, (
        f"{path} hit the error boundary: {crashed[:2]}{context}"
    )

    assert not server_errors, f"{path} got server errors: {server_errors[:5]}{context}"

    if path not in KNOWN_OVERFLOW:
        widths = authed_page.evaluate(
            "() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]"
        )
        assert widths[0] <= widths[1] + 1, (
            f"{path} scrolls sideways: content is {widths[0]}px in a {widths[1]}px window"
        )
