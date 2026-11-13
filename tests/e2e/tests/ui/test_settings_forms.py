# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings forms save, and the saved value is what the page shows next time.

"Saves and survives a reload" is the regression a form refactor causes most
often and notices least: the field still types, the button still says saved,
but a renamed state key or a controlled/uncontrolled swap means the value that
reaches the API -- or the value read back into the field -- is not the one the
operator typed.

Each test changes one harmless value, restores the original through the API
afterwards, and asserts only on what a reload shows.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from pages.base import BasePage, fatal_errors

pytestmark = pytest.mark.ui

MONITORING = "/system/monitoring-settings"
PASSWORD_POLICY = "/password-policy/"


def test_system_health_thresholds_save_and_survive_a_reload(authed_page, client, sandbox):
    original = client.get(MONITORING).json()
    sandbox.track("restore monitoring settings", lambda: client.put(MONITORING, json_body=original, expect=None))
    new_value = "87" if original.get("cpu_percent_threshold") != 87 else "86"

    page = BasePage(authed_page)
    page.path = "/settings/more-settings/system-health"
    errors = page.console_errors()
    page.open()

    # CPU, memory and disk share the placeholder; CPU is the first of them.
    cpu = authed_page.get_by_placeholder(re.compile(r"^90 ")).first
    expect(cpu).to_be_visible(timeout=20_000)
    cpu.fill(new_value)
    authed_page.get_by_role("button", name=re.compile(r"Save Monitoring Settings")).click()
    expect(authed_page.get_by_text("Monitoring settings saved")).to_be_visible(timeout=10_000)

    authed_page.reload()
    expect(authed_page.get_by_placeholder(re.compile(r"^90 ")).first).to_have_value(new_value, timeout=20_000)
    assert not fatal_errors(errors), f"system health page threw: {errors[:3]}"


def test_the_password_policy_saves_and_survives_a_reload(authed_page, client, sandbox):
    original = client.get(PASSWORD_POLICY).json()
    sandbox.track("restore password policy", lambda: client.put(PASSWORD_POLICY, json_body=original, expect=None))
    # History length is the one knob that cannot lock another test out.
    new_value = "7" if original.get("history_count") != 7 else "6"

    page = BasePage(authed_page)
    page.path = "/rbac/password-policy"
    errors = page.console_errors()
    page.open()

    field = authed_page.get_by_label(re.compile(r"Password history"))
    expect(field).to_be_visible(timeout=20_000)
    field.fill(new_value)
    authed_page.get_by_role("button", name=re.compile(r"^Save$")).first.click()

    # The page gives no success message today, so wait for the write itself.
    authed_page.wait_for_timeout(1_500)
    authed_page.reload()
    expect(authed_page.get_by_label(re.compile(r"Password history"))).to_have_value(new_value, timeout=20_000)
    assert not fatal_errors(errors), f"password policy page threw: {errors[:3]}"
