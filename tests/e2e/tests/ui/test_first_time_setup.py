# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The claim-the-account journey, through the page an operator actually uses.

``test_bootstrap.py`` proves the API contract. This drives the three screens:
the setup token and password, the QR code that must be *confirmed* with a
code from the authenticator before anything proceeds, and the recovery codes
that must be acknowledged as saved before the app opens.

The one-time token is consumed on first use, so this can only run on a stack
whose admin account is still unclaimed (``run.py --fresh``); otherwise it
skips. When it does run it claims the account, and it hands the credentials
to the harness so the ``admin`` fixture logs in with them instead of finding
the account taken. ``conftest.py`` schedules it before every other test for
that reason.
"""

from __future__ import annotations

import os

import httpx
import pyotp
import pytest
from playwright.sync_api import expect

from harness import compose, routes
from harness.bootstrap import generate_password, remember
from harness.budgets import BUDGETS

pytestmark = [pytest.mark.ui, pytest.mark.setup_journey]


def test_setup_confirms_the_authenticator_before_the_app_opens(ui_page, config):
    with httpx.Client(base_url=config.core.rstrip("/"), timeout=BUDGETS.REQUEST) as http:
        check = http.post(routes.api(routes.AUTH_CHECK_SETUP))
        check.raise_for_status()
        status = check.json()
    if not status.get("setup_required"):
        pytest.skip("the admin account is already claimed; run with --fresh")
    token = os.environ.get("E2E_SETUP_TOKEN") or compose.read_setup_token()
    if not token:
        pytest.skip("no setup token available (E2E_SETUP_TOKEN unset and core logs unreadable)")

    username = status.get("username") or "admin"
    password = generate_password()

    # Step 1: the token and a password.
    ui_page.goto("/first-time-setup")
    expect(ui_page.get_by_text("Step 1 of 3")).to_be_visible(timeout=30_000)
    ui_page.get_by_label("Setup token").fill(token)
    ui_page.get_by_label("Password", exact=True).fill(password)
    ui_page.get_by_label("Confirm password").fill(password)
    ui_page.get_by_role("button", name="Create account").click()

    # Step 2: the QR code, and no way past it without a code from the app.
    expect(ui_page.get_by_text("Step 2 of 3")).to_be_visible(timeout=30_000)
    expect(ui_page.get_by_role("button", name="Continue to sign in")).to_have_count(0)
    secret = ui_page.locator("code").first.inner_text().strip()
    assert len(secret) >= 16, f"the manual-entry secret looks wrong: {secret!r}"
    totp = pyotp.TOTP(secret)

    code_field = ui_page.get_by_label("Code from the app")
    verify = ui_page.get_by_role("button", name="Verify and finish")
    wrong = next(c for c in ("000000", "111111") if c != totp.now())
    code_field.fill(wrong)
    verify.click()
    expect(ui_page.get_by_text("did not match")).to_be_visible(timeout=15_000)
    expect(ui_page.get_by_text("Step 2 of 3")).to_be_visible()

    code_field.fill(totp.now())
    verify.click()

    # Step 3: the recovery codes, acknowledged before the app opens.
    expect(ui_page.get_by_text("Step 3 of 3")).to_be_visible(timeout=30_000)
    panel = ui_page.get_by_test_id("recovery-codes")
    expect(panel.locator("li")).to_have_count(10)
    codes = [li.inner_text().strip() for li in panel.locator("li").all()]
    assert all(len(c) == 19 and c.count("-") == 3 for c in codes), codes
    proceed = ui_page.get_by_role("button", name="Continue to OpenNVR")
    expect(proceed).to_be_disabled()
    ui_page.get_by_label("I have saved these codes").check()
    expect(proceed).to_be_enabled()

    # The setup is recorded for the rest of the suite before the last click,
    # so a failure past this point does not strand every later test.
    remember(config.artifacts, username=username, password=password, mfa_secret=secret)

    proceed.click()
    # Signed in: the shell, not the login form, and the setup route is gone.
    expect(ui_page.locator("nav a, aside a").first).to_be_visible(timeout=30_000)
    expect(ui_page.get_by_role("button", name="Sign in")).to_have_count(0)
    assert "first-time-setup" not in ui_page.url

    with httpx.Client(base_url=config.core.rstrip("/"), timeout=BUDGETS.REQUEST) as http:
        after = http.post(routes.api(routes.AUTH_CHECK_SETUP)).json()
    assert after.get("setup_required") is False
