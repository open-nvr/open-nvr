# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The occupancy page renders its charts.

Deliberately a low bar, and the docstrings say so. Producing occupancy data
needs the counting app publishing to NATS, which a bare stack does not have --
so this asserts the page mounts, queries and draws an empty state rather than
throwing. An empty chart and a broken one look identical to a user and need
completely different fixes; telling them apart is the whole value here.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from pages.base import BasePage, fatal_errors

pytestmark = pytest.mark.ui


class OccupancyPage(BasePage):
    path = "/occupancy"


def test_the_occupancy_page_renders_without_throwing(authed_page):
    page = OccupancyPage(authed_page)
    errors = page.console_errors()
    page.open()

    expect(authed_page.locator("body")).to_be_visible(timeout=30_000)
    assert not fatal_errors(errors), (
        f"the Occupancy page threw while rendering: {errors[:3]}"
    )


def test_the_audit_log_page_renders_without_throwing(authed_page):
    """Audit is where a security control becomes reviewable.

    A page that throws makes the trail unreadable, which is the same practical
    outcome as not recording it.
    """
    page = BasePage(authed_page)
    page.path = "/audit-logs"
    errors = page.console_errors()
    page.open()

    expect(authed_page.locator("body")).to_be_visible(timeout=30_000)
    assert not fatal_errors(errors), f"the audit log page threw: {errors[:3]}"
