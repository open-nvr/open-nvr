# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The main popups open, close, and leave the page working.

A UI refactor moves most popups from hand-built overlays onto a shared
``Modal``. What must survive that move, for every popup: the trigger still
opens it, its close control still closes it, and nothing throws on the way.

Escape is tried first and the close button is the fallback, because not every
popup honours Escape today. ``ESCAPE_CLOSES`` lists the ones that must -- a
popup belongs there once it is on the shared ``Modal``, and adding it is how a
refactor commits to that behaviour.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from pages.base import BasePage, fatal_errors

pytestmark = pytest.mark.ui

# What a popup looks like in the DOM: a real dialog, or one of today's
# hand-built full-screen overlays. Counted before and after opening, so a
# page that already has one (e.g. a banner) does not confuse the check.
# The :not(:has(...)) keeps it to ONE match per popup whether or not the
# popup is on the shared Modal yet: a Modal is a full-screen wrapper AROUND a
# role="dialog" box, and counting both would read as two popups.
OVERLAY = (
    '[role="dialog"], [aria-modal="true"], '
    'div.fixed.inset-0:not(:has([role="dialog"], [aria-modal="true"]))'
)

# (id, route, trigger button accessible name). Cloud's "Add Stream" is only
# enabled when a camera exists, so that case arranges one first.
NEEDS_CAMERA = {"add-stream"}
DIALOGS = [
    ("add-camera", "/cameras", re.compile(r"^Add Camera$")),
    ("add-user", "/rbac/users", re.compile(r"^Add User$")),
    ("add-role", "/rbac/roles", re.compile(r"^Add Role$")),
    ("add-integration", "/integrations", re.compile(r"^Add Integration$")),
    ("add-stream", "/cloud", re.compile(r"^Add Stream$")),
    ("live-menu", "/live", re.compile(r"^Menu$")),
    ("byok-info", "/byok", re.compile(r"Learn about BYOK")),
]

# Popups that must close on Escape. Grows as popups move onto the shared Modal.
ESCAPE_CLOSES = {"add-camera", "add-user", "add-role", "add-integration", "add-stream", "byok-info"}

CLOSE = re.compile(r"^(Cancel|Close|×|✕)$", re.IGNORECASE)


def _close(page, overlay_count_before: int, popup_id: str) -> None:
    overlays = page.locator(OVERLAY)
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    if overlays.count() <= overlay_count_before:
        return
    assert popup_id not in ESCAPE_CLOSES, f"{popup_id}: Escape no longer closes it"
    # Fall back to the popup's own close control (the last overlay is the newest).
    newest = overlays.last
    closer = newest.get_by_role("button", name=CLOSE)
    if closer.count() == 0:
        closer = newest.get_by_role("button", name=re.compile(r"close", re.I))
    closer.first.click()


@pytest.mark.parametrize("popup_id,path,trigger", DIALOGS, ids=[d[0] for d in DIALOGS])
def test_a_dialog_opens_and_closes(authed_page, client, sandbox, config, popup_id, path, trigger):
    if popup_id in NEEDS_CAMERA:
        client.create_camera(
            label="popup",
            rtsp_url=f"rtsp://{config.fakecam_ip}:8554/{sandbox.name('popup')}",
            ip_address=config.fakecam_ip,
        )
    page = BasePage(authed_page)
    page.path = path
    errors = page.console_errors()
    page.open()

    button = authed_page.get_by_role("button", name=trigger).first
    expect(button).to_be_visible(timeout=30_000)
    before = authed_page.locator(OVERLAY).count()

    button.click()
    expect(authed_page.locator(OVERLAY)).to_have_count(before + 1, timeout=10_000)

    _close(authed_page, before, popup_id)
    expect(authed_page.locator(OVERLAY)).to_have_count(before, timeout=10_000)

    # The page underneath still works: its trigger is usable again.
    expect(button).to_be_enabled()
    assert not fatal_errors(errors), f"{popup_id} threw: {errors[:3]}"


def test_the_account_menu_opens_and_closes(authed_page):
    page = BasePage(authed_page)
    errors = page.console_errors()
    page.open()

    trigger = authed_page.locator('header [aria-haspopup="menu"]').first
    trigger.click()
    sign_out = authed_page.get_by_role("menuitem", name="Sign out")
    expect(sign_out).to_be_visible(timeout=10_000)

    trigger.click()
    expect(sign_out).to_be_hidden(timeout=10_000)
    assert not fatal_errors(errors), f"account menu threw: {errors[:3]}"


def test_the_alarm_bell_panel_opens_and_closes(authed_page):
    page = BasePage(authed_page)
    errors = page.console_errors()
    page.open()

    bell = authed_page.get_by_role("button", name="Alarms").first
    bell.click()
    history = authed_page.get_by_text(re.compile(r"Open Alarms"))
    expect(history.first).to_be_visible(timeout=10_000)

    bell.click()
    expect(history.first).to_be_hidden(timeout=10_000)
    assert not fatal_errors(errors), f"alarm bell threw: {errors[:3]}"


def test_the_dashboard_add_widget_menu_opens(authed_page):
    """Customise mode, and its menu -- it once rendered clipped and invisible."""
    page = BasePage(authed_page)
    errors = page.console_errors()
    page.open()

    authed_page.get_by_role("button", name=re.compile(r"Customise")).first.click()
    add = authed_page.get_by_role("button", name=re.compile(r"Add widget"))
    expect(add).to_be_visible(timeout=10_000)
    add.click()
    # The only widget hidden by default; the menu must actually be on screen.
    expect(authed_page.get_by_role("button", name=re.compile(r"Network IDS"))).to_be_visible(
        timeout=10_000
    )
    authed_page.get_by_role("button", name=re.compile(r"^Done$")).click()
    assert not fatal_errors(errors), f"dashboard customise threw: {errors[:3]}"
