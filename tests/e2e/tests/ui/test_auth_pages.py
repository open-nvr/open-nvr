# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pages an operator meets before they have an account.

``test_login.py`` covers signing in. This covers the surrounding auth surface:
that an unauthenticated visitor is sent to the login page rather than into the
app, and that the setup and MFA routes render rather than white-screening.

First-time setup itself cannot be driven here without destroying the session
every other test depends on -- the token is consumed on first use -- so the
full claim-the-account journey stays in the API tier, which can assert it on a
genuinely fresh database via ``--fresh``.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from harness import selectors as S
from pages.base import BasePage, fatal_errors

pytestmark = pytest.mark.ui


def test_an_unauthenticated_visitor_gets_the_login_form(ui_page):
    """No session means the app is not reachable, on any route.

    Note what is asserted and what is not. ``ProtectedShell`` renders ``<Login>``
    **in place** rather than navigating, so the URL still reads ``/cameras``
    while the login form is on screen. An earlier version of this test waited
    for a redirect that the app never performs and failed against correct
    behaviour.

    What matters to security is not the URL -- it is that the protected view
    did not render. So this asserts the login form is present and the Cameras
    page is not.
    """
    page = BasePage(ui_page)
    ui_page.goto("/cameras")

    expect(page.find(S.LOGIN_SUBMIT).first).to_be_visible(timeout=30_000)
    expect(page.find(S.CAMERAS_HEADING)).to_have_count(0)


def test_the_first_time_setup_page_renders(ui_page):
    """The very first screen a new install shows.

    It renders whether or not setup is still pending -- a claimed account
    redirects -- so this asserts only that the route does not throw, which is
    the failure that would strand a brand-new install with a white page.
    """
    page = BasePage(ui_page)
    errors = page.console_errors()
    ui_page.goto("/first-time-setup")

    expect(ui_page.locator("body")).to_be_visible(timeout=30_000)
    assert not fatal_errors(errors), f"the first-time-setup page threw: {errors[:3]}"


def test_the_mfa_pages_render(ui_page):
    """MFA is mandatory, so these routes are on the critical path.

    If either throws, an enrolled operator cannot get in at all.
    """
    page = BasePage(ui_page)
    for path in ("/mfa-verify", "/mfa-setup"):
        errors = page.console_errors()
        ui_page.goto(path)
        expect(ui_page.locator("body")).to_be_visible(timeout=30_000)
        assert not fatal_errors(errors), f"{path} threw: {errors[:3]}"
