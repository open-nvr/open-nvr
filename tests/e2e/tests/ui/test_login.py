# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Signing in, through the real form.

The only UI test that drives authentication by hand. Every other one starts
from a logged-in browser, because re-authenticating in each test is the
biggest single source of slowness and flake in a browser suite.

It earns the exception because the login journey is genuinely two pages and
three moving parts: the form posts credentials, the client recognises the
MFA-required response and routes to ``/mfa-verify`` carrying the credentials
in router state, and only the TOTP submission mints a session. The API tests
cannot cover that -- they call ``login-json`` with the code already attached,
which is a different code path from what a person actually does.

TOTP handling here is deliberately careful. A wrong or stale code counts as a
failed login, and five of them lock the account for three minutes, which would
wedge every later test in the run.
"""

from __future__ import annotations

import re
import time

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.ui

#: Standard TOTP step. A code is only valid inside its own 30s window.
TOTP_STEP = 30


def _fresh_totp(admin) -> str:
    """A code with enough of its window left to survive the round trip.

    Submitting a code in its last second means the server may well validate it
    in the next window and reject it -- burning one of the five attempts that
    lock the account. Waiting for the next window costs a few seconds and
    removes the whole class of flake.
    """
    remaining = TOTP_STEP - (int(time.time()) % TOTP_STEP)
    if remaining < 5:
        time.sleep(remaining + 1)
    return admin.totp()


def test_the_login_page_is_reachable(ui_page):
    """The edge serves the SPA and it routes to /login when unauthenticated.

    Proves nginx, the SPA bundle and the client-side router are all working
    before any test blames authentication for a page that never loaded.
    """
    ui_page.goto("/login")

    expect(ui_page.locator("form")).to_be_visible(timeout=15_000)
    expect(ui_page.get_by_placeholder("admin")).to_be_visible()


def test_signing_in_reaches_the_dashboard(ui_page, admin):
    """The whole journey: credentials, then TOTP, then the app.

    Asserts on being *out* of the auth pages rather than on any particular
    dashboard widget -- the dashboard's contents change with the fleet, but
    "no longer at /login or /mfa-verify" is exactly what signing in means.
    """
    ui_page.goto("/login")
    ui_page.get_by_placeholder("admin").fill(admin.username)
    ui_page.get_by_placeholder("●●●●●●●●").fill(admin.password)
    ui_page.get_by_role("button", name="Sign in").click()

    # MFA is enabled on the bootstrapped admin, so the client routes here.
    expect(ui_page).to_have_url(re.compile(r"/mfa-verify"), timeout=15_000)

    ui_page.get_by_placeholder("000000").fill(_fresh_totp(admin))
    ui_page.get_by_role("button").filter(has_text="Verify").click()

    expect(ui_page).not_to_have_url(re.compile(r"/login"), timeout=20_000)
    expect(ui_page).not_to_have_url(re.compile(r"/mfa-verify"), timeout=20_000)


def test_a_signed_in_session_survives_a_reload(ui_page, admin):
    """The session is persisted, not just held in memory.

    The SPA reads its token from localStorage on first render. If that write
    is missing, everything works until the operator refreshes and is thrown
    back to the login page -- a bug that never shows up in a single-page test
    run and is immediately obvious in daily use.
    """
    ui_page.goto("/login")
    ui_page.get_by_placeholder("admin").fill(admin.username)
    ui_page.get_by_placeholder("●●●●●●●●").fill(admin.password)
    ui_page.get_by_role("button", name="Sign in").click()
    expect(ui_page).to_have_url(re.compile(r"/mfa-verify"), timeout=15_000)
    ui_page.get_by_placeholder("000000").fill(_fresh_totp(admin))
    ui_page.get_by_role("button").filter(has_text="Verify").click()
    expect(ui_page).not_to_have_url(re.compile(r"/mfa-verify"), timeout=20_000)

    ui_page.reload()

    expect(ui_page).not_to_have_url(re.compile(r"/login"), timeout=20_000)
    assert ui_page.evaluate("() => localStorage.getItem('opennvr.token')"), (
        "no token was persisted to localStorage, so the session cannot survive "
        "a refresh"
    )
