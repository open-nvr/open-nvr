# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Copy me to start a new GUI test.

    cp tests/e2e/_template_ui_test.py tests/e2e/tests/ui/test_my_journey.py

The filename does not start with ``test_``, so pytest never collects this file
itself. (Its sibling ``_template_test.py`` is the same thing for API tests.)

Writing a GUI test here should take minutes, because three things are already
done for you:

1. **You are logged in.** ``authed_page`` seeds the session before the first
   render. Only ``ui/test_login.py`` drives the real login form.
2. **You never write a selector.** Ask for the page object you need
   (``cameras_page``, ``alerts_page``, ...) and call its methods. Selectors
   live in ``harness/selectors.py`` -- one editable line each -- and the page
   objects in ``tests/e2e/pages/`` own the clicking.
3. **Evidence is automatic.** A screenshot is taken at the end of every GUI
   test and appears in the HTML report; a failure additionally keeps a
   Playwright trace, the container logs and the audit trail.

**Give the file a basename no other test file uses.** There are no
``__init__.py`` files here, so pytest derives module names from basenames
alone -- ``tests/ui/test_rbac.py`` beside ``tests/test_rbac.py`` is a
collection error, not a silent shadowing. Naming a GUI file after what the
browser does (``test_permissions.py``, ``test_auth_pages.py``) reads better
than mirroring the API filename anyway.

Everything from the API template still applies: declare a marker, wait with
``eventually`` and a named budget rather than sleeping, never depend on another
test's data, and write no cleanup code.

## Setting up state

Use the **API** to arrange, and the **GUI** to act and assert. Creating a
camera through the form takes ten seconds and can fail for its own reasons; if
the camera is merely a precondition, make it with ``client`` and spend the
browser time on the thing you are actually testing.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from pages.base import fatal_errors

pytestmark = pytest.mark.ui


def test_a_camera_created_by_the_api_is_visible_in_the_ui(
    cameras_page, client, sandbox, config
):
    """One journey per test, named for what the operator experiences.

    This is the shape most GUI tests should take: arrange over the API, act and
    assert in the browser. It proves something no API test can -- that the page
    queries the same data the API writes, with the same scoping, and renders it.
    """
    # --- arrange (API: fast, and not what we are testing)
    camera = client.create_camera(
        label="uitemplate",
        rtsp_url=f"rtsp://{config.fakecam_ip}:8554/{sandbox.name('stream')}",
        ip_address=config.fakecam_ip,
    )

    # --- act (GUI)
    errors = cameras_page.console_errors()
    cameras_page.open()

    # --- assert
    expect(cameras_page.row(camera["name"]).first).to_be_visible(timeout=30_000)
    assert not fatal_errors(errors), f"the page threw while rendering: {errors[:3]}"

    # No teardown: the camera is deleted for you, and the screenshot is taken
    # automatically as the fixtures unwind.
