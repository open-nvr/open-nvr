# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Copy me to start a new E2E test.

    cp tests/e2e/_template_test.py tests/e2e/tests/test_my_journey.py

The filename does not start with ``test_``, so pytest never collects this file
itself. Everything below is the whole contract for writing a test here — there
is no other ceremony.

Five rules, and only the first two need any thought:

1. **Take ``client``.** It is authenticated, it tags every request with your
   sandbox namespace so a failure can find your log lines, and anything you
   create through it is deleted afterwards. Do not build your own HTTP client.

2. **Wait with ``eventually`` and a named budget.** Never ``time.sleep``.
   Nothing in OpenNVR reports completion, so polling is correct — but a bare
   sleep is slow when it works and silent when it does not. The ``describe``
   string is the headline of the failure report, so make it specific.

3. **Never depend on another test's data.** Seed what you need. The suite runs
   in random order on purpose, so a test that needs a predecessor will fail —
   which is the point.

4. **Declare a marker.** ``smoke`` (fast, no detection), ``detection``,
   ``lpr`` or ``ui``. Markers are strict: a typo is a collection error, not a
   test that quietly never runs.

5. **Write no cleanup code.** If you create something the client does not
   model yet, add a helper to ``harness/client.py`` rather than a teardown
   here — then the next test gets it for free.
"""

from __future__ import annotations

import pytest

from harness import routes
from harness.budgets import BUDGETS
from harness.waiting import eventually


@pytest.mark.smoke
def test_a_camera_becomes_visible(client, sandbox, config):
    """One journey per test, named for the behaviour rather than the endpoint.

    The docstring is the first thing a reader of a failure sees, so say what
    the system is supposed to do — not which routes get called.
    """
    # --- arrange: sandbox.name() keeps this run from colliding with any other
    camera = client.create_camera(
        label="template",
        rtsp_url=f"rtsp://{config.fakecam_ip}:8554/{sandbox.name('stream')}",
        ip_address=config.fakecam_ip,
    )

    # --- act
    client.post(routes.CAMERA_PROVISION(camera["id"]))

    # --- assert: poll, because provisioning is asynchronous
    listed = eventually(
        lambda: client.json(routes.CAMERAS, params={"limit": 200}),
        until=lambda page: any(
            entry["id"] == camera["id"] for entry in page.get("cameras", [])
        ),
        budget=BUDGETS.QUICK,
        describe=f"camera {camera['id']} to appear in the camera list",
    )
    assert listed, "the camera list came back empty"

    # No teardown. The camera is deleted for you when this test ends, pass or
    # fail — see harness/sandbox.py.
