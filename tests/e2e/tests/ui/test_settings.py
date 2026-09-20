# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A settings form that round-trips.

Retention is the representative case: an operator types a number, saves, and
the value has to still be there afterwards. A form that posts successfully but
does not reload its own value is a bug no API test can see -- the endpoint is
perfect and the page is still wrong.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from harness import selectors as S
from pages.base import fatal_errors

pytestmark = pytest.mark.ui


def test_the_recording_settings_page_renders(recording_settings_page):
    errors = recording_settings_page.console_errors()
    recording_settings_page.open()

    expect(recording_settings_page.find(S.SAVE_RETENTION).first).to_be_visible(
        timeout=30_000
    )
    assert not fatal_errors(errors), f"Recording settings threw: {errors[:3]}"


def test_retention_saves_and_survives_a_reload(recording_settings_page, sandbox):
    """Type, save, reload, and check the value came back.

    Asserted on the PUT response rather than the snackbar: the snackbar
    auto-dismisses after five seconds, so on a loaded machine an assertion
    against it fails for reasons that have nothing to do with saving.

    The reload is the real point. Without it this would pass against a form
    that posts correctly and then shows a stale value forever.
    """
    page = recording_settings_page
    page.open()

    original = page.retention_days()
    # A value that is obviously ours and inside the 0-3650 the form allows.
    wanted = "17" if original.strip() != "17" else "23"

    page.set_retention_days(int(wanted))
    response = page.save()
    assert response.status < 400, (
        f"saving retention failed with {response.status}: {response.text()[:200]}"
    )

    # Put it back however this test ends, so a shared stack is not left with a
    # retention policy some other run did not expect.
    sandbox.track(
        f"restore retention_days to {original!r}",
        lambda: _restore(page, original),
    )

    page.open()
    expect(page.find(S.RETENTION_DAYS).first).to_have_value(wanted, timeout=30_000)


def _restore(page, original: str) -> None:
    if not original.strip():
        return
    page.open()
    page.set_retention_days(int(original))
    page.save()
