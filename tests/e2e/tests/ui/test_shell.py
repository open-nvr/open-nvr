# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The application actually renders for a signed-in operator.

Everything here starts from ``authed_page`` -- a browser with the session
already seeded -- so these are about the app, not about logging in.

What they are really guarding is the gap between "the API works" and "the
product works". Every API journey in this suite can be green while the SPA
fails to boot: a bundle that throws on load, a route that renders nothing, a
page that shows an empty state because it is calling an endpoint that moved.
None of that is visible from the server side.

Assertions target structure and network activity, never pixels. A screenshot
comparison would fail on every legitimate style change and tell you nothing
about whether the page works.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from harness.budgets import BUDGETS
from harness.waiting import eventually

pytestmark = pytest.mark.ui


def _console_errors(page) -> list[str]:
    """Collect page errors. Attach before navigating."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    return errors


def test_the_dashboard_renders_for_a_signed_in_user(authed_page):
    """The app boots past authentication and paints something.

    Deliberately not asserting on a specific widget: the dashboard's contents
    depend on the fleet. What must be true is that we are not bounced to
    /login and the shell is on screen.
    """
    errors = _console_errors(authed_page)
    authed_page.goto("/")

    expect(authed_page.locator("body")).to_be_visible(timeout=20_000)
    expect(authed_page).not_to_have_url(re.compile(r"/login"), timeout=20_000)

    fatal = [e for e in errors if "ResizeObserver" not in e]
    assert not fatal, f"the SPA threw while loading the dashboard: {fatal[:3]}"


def test_the_navigation_shell_is_present(authed_page):
    """An operator needs to be able to get somewhere.

    The shell gates each entry on the caller's permissions (AppShell's
    NAV_PERMISSIONS), which are fetched after first paint -- so the nav grows
    as that request resolves and a snapshot taken too early sees only the
    ungated entry. Polling rather than asserting once is the difference
    between testing the nav and testing the network.

    An admin holds every permission, so "more than one destination" is a
    floor, not a guess at the exact count, which would break every time a
    page is added.
    """
    authed_page.goto("/")
    expect(authed_page.locator("body")).to_be_visible(timeout=20_000)

    links = authed_page.locator("nav a, aside a")
    count = eventually(
        links.count,
        until=lambda n: n > 1,
        budget=BUDGETS.QUICK,
        describe="the navigation shell to render its permission-gated entries",
    )

    assert count > 1, (
        f"the shell rendered {count} destination(s) for an admin, who should "
        "see every one"
    )


@pytest.mark.parametrize("path", ["/cameras", "/live", "/playback", "/vehicles"])
def test_a_core_page_renders_without_throwing(authed_page, path):
    """Each main route mounts and stays mounted.

    A route that throws during render leaves a blank page and a console error,
    which is exactly the failure an API-only suite cannot see. Empty content
    is fine and expected -- an unconfigured stack has little to show -- so
    this asserts the page did not blow up, not that it found data.
    """
    errors = _console_errors(authed_page)
    authed_page.goto(path)

    expect(authed_page.locator("body")).to_be_visible(timeout=20_000)
    expect(authed_page).not_to_have_url(re.compile(r"/login"), timeout=20_000)

    fatal = [e for e in errors if "ResizeObserver" not in e]
    assert not fatal, f"{path} threw while rendering: {fatal[:3]}"


def test_the_camera_page_shows_a_camera_the_api_created(
    authed_page, client, sandbox, config
):
    """The two halves of the product agree.

    A camera created through the API has to appear in the UI. This is the one
    assertion in the browser tier that no API test can make: it proves the
    page queries the same data the API writes, with the same scoping, and
    renders it.
    """
    camera = client.create_camera(
        label="uilisted",
        rtsp_url=f"rtsp://{config.fakecam_ip}:8554/{sandbox.name('uilisted')}",
        ip_address=config.fakecam_ip,
    )

    authed_page.goto("/cameras")
    expect(authed_page.get_by_text(camera["name"])).to_be_visible(timeout=30_000)
