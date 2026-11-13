# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Vehicles page -- where a plate read becomes something an operator sees.

LPR is the longest chain in the product and fails silently at every link. Its
final link is this page: a plate can be read, stored and returned by the API
while the Vehicles page shows nothing, and the operator's experience is
identical to the OCR never having run.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from harness import routes
from pages.base import fatal_errors

pytestmark = pytest.mark.ui


def test_the_vehicles_page_renders(vehicles_page):
    """It mounts and draws, with or without reads.

    Marked ``ui`` rather than ``lpr`` on purpose: rendering does not need the
    OCR adapter, and this should still run on a stack with no LPR configured.
    """
    errors = vehicles_page.console_errors()
    vehicles_page.open()

    expect(vehicles_page.page.locator("body")).to_be_visible(timeout=30_000)
    assert not fatal_errors(errors), f"the Vehicles page threw: {errors[:3]}"


@pytest.mark.lpr
def test_a_plate_read_is_listed(vehicles_page, client, detectable_camera, plate_ocr):
    """A plate the API knows about is on screen.

    Skips with the rest of the LPR tier when the OCR adapter is not registered
    -- the ``plate_ocr`` fixture distinguishes "not configured here" from
    "vanished after a core restart", and only the second is a defect.
    """
    camera_id = detectable_camera["id"]
    events = client.json(
        routes.EVENTS, params={"camera_id": camera_id, "has_plate": True, "limit": 20}
    )["events"]
    read = next((e for e in events if e.get("plate_text")), None)
    if read is None:
        pytest.skip("no plate has been read yet on this stack; nothing to display")

    vehicles_page.open()

    expect(vehicles_page.plate(read["plate_text"].strip()).first).to_be_visible(
        timeout=60_000
    )
