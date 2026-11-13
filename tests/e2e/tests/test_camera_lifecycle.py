# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A camera's whole life: added, provisioned, streamable, binned, purged.

This is the spine of the product. Every later journey — recording, playback,
detection, plates — starts by getting a camera into MediaMTX, so a break here
would otherwise surface as a confusing failure somewhere much further along.

These tests deliberately do **not** need a live video source. Creating a camera
and provisioning it into MediaMTX are control-plane operations: the path is
declared whether or not anything is publishing to it yet. That keeps this tier
fast and clip-free, and leaves "pixels actually arrive" to the detection tier
where a fake camera is publishing for real.

The purge test is the interesting one. Hard delete composes three independent
guards — the camera must already be binned, the caller's *current* TOTP must be
in a header, and the body must repeat an exact phrase — and they only ever meet
in a live system with a real MFA-enrolled session.
"""

from __future__ import annotations

import pytest

from harness import routes
from harness.budgets import BUDGETS
from harness.waiting import eventually

pytestmark = pytest.mark.smoke


def _rtsp(config, sandbox, label: str = "stream") -> str:
    """An RTSP URL on the fake-camera rig's address.

    It need not resolve to a live stream for these tests, but it must be a real
    IP: the camera API validates ``ip_address`` as an address, not a hostname.
    """
    return f"rtsp://{config.fakecam_ip}:8554/{sandbox.name(label)}"


def test_a_new_camera_is_listed_and_readable(client, sandbox, config):
    """Creating a camera makes it retrievable, with its details intact."""
    created = client.create_camera(
        label="listed",
        rtsp_url=_rtsp(config, sandbox),
        ip_address=config.fakecam_ip,
    )

    fetched = client.json(routes.CAMERA(created["id"]))

    assert fetched["id"] == created["id"]
    assert fetched["name"] == created["name"]
    assert fetched["rtsp_url"] == created["rtsp_url"], (
        "the stored RTSP URL differs from the one submitted — credentials or "
        "transport rewriting may have altered it"
    )

    page = client.json(routes.CAMERAS, params={"limit": 500})
    ids = {entry["id"] for entry in page.get("cameras", page.get("items", []))}
    assert created["id"] in ids, "the new camera is missing from the camera list"


def test_a_camera_provisions_into_mediamtx(client, sandbox, config):
    """A camera must reach MediaMTX, or nothing downstream can ever work.

    ``mediamtx-status`` reports two different things and the difference matters
    here (``server/routers/cameras.py``):

    * ``path_configured`` — MediaMTX has accepted the path definition. This is
      what provisioning controls, and it is true whether or not anything is
      publishing.
    * ``path_active`` — bytes are actually arriving. That needs a live source,
      so it belongs to the detection tier, not to this one.

    Asserting the first and not the second is what lets this tier run with no
    video at all.
    """
    camera = client.create_camera(
        label="provision",
        rtsp_url=_rtsp(config, sandbox),
        ip_address=config.fakecam_ip,
    )

    client.post(routes.CAMERA_PROVISION(camera["id"]))

    status = eventually(
        lambda: client.json(routes.CAMERA_MEDIAMTX_STATUS(camera["id"])),
        until=lambda payload: payload.get("path_configured") is True,
        budget=BUDGETS.STREAM_READY,
        describe=f"MediaMTX to accept the path definition for camera {camera['id']}",
    )

    assert status["path_configured"] is True
    assert status["path_status"]["status"] == "ok", (
        f"MediaMTX rejected the path: {status['path_status']}"
    )
    # Explicitly NOT asserted: path_active. Nothing is publishing to this
    # camera, so false is the correct answer and demanding true would make
    # this tier depend on video it deliberately does not have.


def test_stream_urls_are_issued_for_a_camera(client, sandbox, config):
    """The UI cannot show live video without these, so their absence is fatal."""
    camera = client.create_camera(
        label="urls",
        rtsp_url=_rtsp(config, sandbox),
        ip_address=config.fakecam_ip,
    )
    client.post(routes.CAMERA_PROVISION(camera["id"]))

    urls = eventually(
        lambda: client.json(routes.CAMERA_STREAM_URLS(camera["id"])),
        until=lambda payload: isinstance(payload, dict) and bool(payload),
        budget=BUDGETS.STREAM_READY,
        describe=f"stream URLs to be issued for camera {camera['id']}",
    )

    assert urls, "no stream URLs were returned"
    assert any(
        isinstance(value, str) and value.startswith(("http", "rtsp", "ws"))
        for value in urls.values()
    ), f"none of the returned stream URLs look like URLs: {urls}"


def test_deleting_a_camera_bins_it_rather_than_destroying_it(client, sandbox, config):
    """Delete must be recoverable.

    An NVR that discards footage on a mis-click is not one you can trust, so
    the ordinary delete has to be a soft delete: gone from the active list,
    still present in the bin.
    """
    camera = client.create_camera(
        label="bin",
        rtsp_url=_rtsp(config, sandbox),
        ip_address=config.fakecam_ip,
    )
    camera_id = camera["id"]

    client.delete(routes.CAMERA(camera_id))

    active = client.json(routes.CAMERAS, params={"limit": 500})
    active_ids = {e["id"] for e in active.get("cameras", active.get("items", []))}
    assert camera_id not in active_ids, "a deleted camera is still listed as active"

    binned = eventually(
        lambda: client.json(routes.CAMERAS_DELETED),
        until=lambda payload: any(
            entry["id"] == camera_id
            for entry in payload.get("cameras", payload.get("items", []))
        ),
        budget=BUDGETS.QUICK,
        describe=f"camera {camera_id} to appear in the bin",
    )
    assert binned, "the bin came back empty"


def test_purging_a_binned_camera_requires_all_three_guards(
    client, sandbox, config, admin
):
    """Hard delete is destructive, so it must be hard to trigger by accident.

    Each guard is checked by driving the real endpoint into it:

    * purging a camera that is not binned -> 409
    * the wrong confirmation phrase       -> 400
    * both satisfied, with a live TOTP    -> the camera is gone for good

    The MFA header is exercised implicitly throughout: every call here carries
    the session's current code, and a missing or stale one would fail the
    final purge.
    """
    camera = client.create_camera(
        label="purge",
        rtsp_url=_rtsp(config, sandbox),
        ip_address=config.fakecam_ip,
    )
    camera_id = camera["id"]
    phrase = f"hard delete {camera['name']} and it's recording"
    code = admin.totp()

    # Guard 1: it is not in the bin yet.
    not_binned = client.post(
        routes.CAMERA_HARD_DELETE(camera_id),
        json_body={"confirmation_phrase": phrase},
        headers={"X-MFA-Code": code},
        expect=None,
    )
    assert not_binned.status_code == 409, (
        "purging a camera that is not in the bin should be refused with 409, "
        f"got {not_binned.status_code}: {not_binned.text}"
    )

    client.delete(routes.CAMERA(camera_id))

    # Guard 2: the phrase has to match exactly.
    wrong_phrase = client.post(
        routes.CAMERA_HARD_DELETE(camera_id),
        json_body={"confirmation_phrase": "delete it"},
        headers={"X-MFA-Code": admin.totp()},
        expect=None,
    )
    assert wrong_phrase.status_code == 400, (
        "a mismatched confirmation phrase should be refused with 400, "
        f"got {wrong_phrase.status_code}: {wrong_phrase.text}"
    )

    # All guards satisfied.
    client.post(
        routes.CAMERA_HARD_DELETE(camera_id),
        json_body={"confirmation_phrase": phrase},
        headers={"X-MFA-Code": admin.totp()},
    )

    gone = client.get(routes.CAMERA(camera_id), expect=None)
    assert gone.status_code == 404, (
        f"the purged camera is still readable (HTTP {gone.status_code})"
    )
