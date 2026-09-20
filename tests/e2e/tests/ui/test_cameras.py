# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Adding and removing a camera the way an operator actually does it.

The API mirror of this file (``tests/test_camera_lifecycle.py``) proves the
endpoints behave. These prove a person can reach them: that the dialog opens,
the Manual tab works, the duplicate prompt can be got past, and that a delete
guarded by a native confirm actually deletes.

Those are not the same claim. Every endpoint here could be perfect while the
form silently fails to submit, and no API test would notice.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from pages.base import fatal_errors

pytestmark = pytest.mark.ui


def test_the_camera_list_renders(cameras_page, client, sandbox, config):
    """A camera created over the API appears in the table.

    The most basic agreement between the two halves of the product: the page
    queries the same data the API writes, with the same scoping, and draws it.
    """
    camera = client.create_camera(
        label="uilist",
        rtsp_url=f"rtsp://{config.fakecam_ip}:8554/{sandbox.name('uilist')}",
        ip_address=config.fakecam_ip,
    )

    errors = cameras_page.console_errors()
    cameras_page.open()

    expect(cameras_page.row(camera["name"]).first).to_be_visible(timeout=30_000)
    assert not fatal_errors(errors), f"the Cameras page threw: {errors[:3]}"


def test_a_camera_can_be_added_through_the_form(cameras_page, sandbox, config, client):
    """The whole add-camera journey, including the prompt it always hits.

    Worth doing through the UI specifically because of the duplicate guard.
    Every fake camera is served from one IP, so the second one onwards returns
    409 and the dialog shows "already added — add it again anyway?". An
    operator meets that prompt constantly; a test that only ever called the
    API with ``force=true`` would never know whether the button behind it
    works.

    The camera is created outside the sandbox's tracking here, so it is
    registered explicitly for teardown.
    """
    name = sandbox.name("uiadd")[:100]
    rtsp = f"rtsp://{config.fakecam_ip}:8554/e2e-static"

    cameras_page.open()
    cameras_page.add_camera(name=name, ip=config.fakecam_ip, rtsp=rtsp)

    expect(cameras_page.row(name).first).to_be_visible(timeout=30_000)

    # Created through the GUI, so the client never saw it -- find its id and
    # register the cleanup by hand rather than leaking it.
    from harness import routes

    listing = client.json(routes.CAMERAS, params={"limit": 500})
    made = next(
        (c for c in listing.get("cameras", listing.get("items", [])) if c["name"] == name),
        None,
    )
    assert made, f"the camera {name!r} is on screen but absent from the API listing"
    sandbox.track(
        f"camera {made['id']} ({name}) created through the UI",
        lambda: client.delete(routes.CAMERA(made["id"]), expect=None),
    )


def test_a_camera_can_be_deleted_through_the_form(cameras_page, client, sandbox, config):
    """Delete is guarded by a native ``window.confirm``.

    Playwright **dismisses** browser dialogs by default, so without an explicit
    handler the confirm is cancelled, nothing is deleted, and the test fails on
    a row that is still there with no clue that a dialog was ever involved.
    The page object registers the handler; this test proves the whole path.
    """
    camera = client.create_camera(
        label="uidel",
        rtsp_url=f"rtsp://{config.fakecam_ip}:8554/{sandbox.name('uidel')}",
        ip_address=config.fakecam_ip,
    )
    cameras_page.open()
    expect(cameras_page.row(camera["name"]).first).to_be_visible(timeout=30_000)

    cameras_page.delete_camera(camera["name"])

    expect(cameras_page.row(camera["name"])).to_have_count(0, timeout=30_000)


@pytest.mark.xfail(
    reason=(
        "the Cameras search box is inert: the UI sends q= but "
        "GET /api/v1/cameras/ has no such parameter. See the docstring."
    ),
)
def test_the_camera_search_filters_the_table(cameras_page, client, sandbox, config):
    """Search narrows the list.

    **Currently xfail: this is a real, shipped bug, not a flaky test.**

    A filter that silently does nothing looks identical to one that works when
    the list is short -- which it is on a test stack -- so this asserts the
    negative too: a camera that should NOT match disappears. That negative is
    what caught the bug; the positive half passes either way.

    The box at ``app/src/views/Cameras.tsx`` debounces the text into
    ``useCameras({ q })``, which reaches
    ``apiService.getCameras`` and is serialised onto the request. But
    ``get_cameras`` (server/routers/cameras.py) declares only ``skip``,
    ``limit`` and ``active_only``. FastAPI ignores query parameters a handler
    does not declare, and the route never reads ``request.query_params``, so
    ``q`` is dropped without a word. Nothing filters client-side either --
    the table renders ``camsQuery.data.cameras`` straight through.

    So typing narrows nothing. It *looks* alive, which is the trap: the key
    changes, a request goes out, the table dims and repopulates -- with the
    same rows. The empty-state copy even offers to clear "the current search".

    Two ways to fix it, and the test does not care which:
    accept ``q`` on the endpoint and filter on name/IP, or drop the box.
    Either turns this XPASS, which is the signal to drop the xfail. The
    assertion stays as it is -- it must start passing on its own merits.
    """
    from harness import selectors as S

    keep = client.create_camera(
        label="uifind",
        rtsp_url=f"rtsp://{config.fakecam_ip}:8554/{sandbox.name('uifind')}",
        ip_address=config.fakecam_ip,
    )
    other = client.create_camera(
        label="uihide",
        rtsp_url=f"rtsp://{config.fakecam_ip}:8554/{sandbox.name('uihide')}",
        ip_address=config.fakecam_ip,
    )

    cameras_page.open()
    expect(cameras_page.row(keep["name"]).first).to_be_visible(timeout=30_000)

    cameras_page.find(S.CAMERAS_SEARCH).fill(keep["name"])

    expect(cameras_page.row(keep["name"]).first).to_be_visible(timeout=30_000)
    expect(cameras_page.row(other["name"])).to_have_count(0, timeout=30_000)
