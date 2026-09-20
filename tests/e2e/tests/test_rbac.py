# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who can see which cameras, and what happens when they cannot.

Camera scoping is the security property an NVR lives or dies by: footage is
the most sensitive thing the product holds, and every read surface -- the
camera list, recordings, the event timeline, evidence images -- is filtered by
what the caller is allowed to see.

That filtering is applied in a different place for each surface
(``camera_scope.visible_camera_ids``, ``permissions.get_camera_or_403``,
per-router scope calls), which is exactly the shape of thing that regresses on
one endpoint while the others stay correct. A unit test on any single one
cannot tell you the set is consistent; only asking the same question of every
surface as a real restricted user can.

These tests create their own users and grant their own permissions, so they
assert against a principal whose access is known rather than against whatever
the fleet happens to look like.
"""

from __future__ import annotations

import pytest

from harness import routes
from harness.bootstrap import generate_password

pytestmark = pytest.mark.smoke


@pytest.fixture
def viewer_role(client) -> dict:
    """The lowest-privilege shipped role: cameras.view, live.view, recordings.view."""
    roles = client.json(routes.ROLES)["roles"]
    for role in roles:
        if role["name"] == "viewer":
            return role
    pytest.skip(f"no 'viewer' role is seeded; roles are {[r['name'] for r in roles]}")


def _login(client, username: str, password: str):
    """Log in as a freshly created user and return a client acting as them.

    Users created through ``POST /users`` land with ``mfa_enabled=False`` --
    they enrol TOTP at first login through the UI's MFA wall -- so no code is
    needed here. The bootstrapped admin is the exception and the reason
    ``bootstrap.py`` computes TOTP at all.
    """
    tokens = client.login(username, password)
    return client.as_user(tokens["access_token"], tokens.get("device_token"))


def test_a_new_user_can_authenticate(client, sandbox, viewer_role):
    """The account creation path has to produce a usable login.

    Worth its own test because a user that exists but cannot log in fails
    every scoping assertion below for a reason that has nothing to do with
    scoping.
    """
    password = generate_password()
    user = client.create_user(
        label="viewer", password=password, role_id=viewer_role["id"]
    )

    as_viewer = _login(client, user["username"], password)
    me = as_viewer.json(routes.AUTH_ME)

    assert me["username"] == user["username"]
    assert not me.get("is_superuser"), "a viewer must not be created as a superuser"


def test_a_viewer_cannot_see_cameras_it_was_not_granted(client, sandbox, config, viewer_role):
    """The core scoping rule: no grant, no camera.

    The admin's camera is invisible to a viewer who was never given access.
    If this leaks, every downstream surface leaks with it.
    """
    password = generate_password()
    user = client.create_user(
        label="nogrant", password=password, role_id=viewer_role["id"]
    )
    camera = client.create_camera(
        label="private",
        rtsp_url=f"rtsp://{config.fakecam_ip}:8554/{sandbox.name('private')}",
        ip_address=config.fakecam_ip,
    )

    as_viewer = _login(client, user["username"], password)

    page = as_viewer.json(routes.CAMERAS, params={"limit": 500})
    visible = {c["id"] for c in page.get("cameras", page.get("items", []))}
    assert camera["id"] not in visible, (
        f"camera {camera['id']} is visible to a user with no grant on it"
    )

    direct = as_viewer.get(routes.CAMERA(camera["id"]), expect=None)
    assert direct.status_code in (403, 404), (
        "fetching an ungranted camera directly should be refused, got "
        f"{direct.status_code}"
    )


def test_a_granted_camera_becomes_visible(client, sandbox, config, viewer_role):
    """The grant has to actually work, in both directions.

    Asserting only the denial above would pass just as well against a system
    that shows nobody anything, so this pins the positive case: after the
    grant, the same user sees the same camera.
    """
    password = generate_password()
    user = client.create_user(
        label="granted", password=password, role_id=viewer_role["id"]
    )
    camera = client.create_camera(
        label="shared",
        rtsp_url=f"rtsp://{config.fakecam_ip}:8554/{sandbox.name('shared')}",
        ip_address=config.fakecam_ip,
    )

    as_viewer = _login(client, user["username"], password)
    before = as_viewer.get(routes.CAMERA(camera["id"]), expect=None).status_code

    client.grant_camera_permission(camera["id"], user["id"])

    after = as_viewer.get(routes.CAMERA(camera["id"]), expect=None)

    assert before in (403, 404), f"the camera was already visible before the grant ({before})"
    assert after.status_code == 200, (
        f"the camera is still not visible after an explicit grant "
        f"({after.status_code}): {after.text[:200]}"
    )


def test_a_viewer_cannot_create_cameras(client, sandbox, config, viewer_role):
    """Read-only means read-only.

    The viewer role carries ``cameras.view`` and not ``cameras.manage``, so
    the write path must refuse it. A role that can read *and* write is not a
    viewer.
    """
    password = generate_password()
    user = client.create_user(
        label="readonly", password=password, role_id=viewer_role["id"]
    )
    as_viewer = _login(client, user["username"], password)

    response = as_viewer.post(
        routes.CAMERAS,
        json_body={
            "name": sandbox.name("forbidden"),
            "ip_address": config.fakecam_ip,
            "port": 8554,
            "rtsp_url": f"rtsp://{config.fakecam_ip}:8554/{sandbox.name('forbidden')}",
        },
        params={"force": "true"},
        expect=None,
    )

    assert response.status_code in (401, 403), (
        f"a viewer was allowed to create a camera (HTTP {response.status_code})"
    )


def test_the_event_timeline_is_scoped_too(client, sandbox, config, viewer_role):
    """Scoping must hold on history, not just on the camera list.

    The timeline and its evidence images are the most sensitive read in the
    product -- they are the pictures. A camera correctly hidden from the
    camera list while its events remain queryable would be the worst kind of
    partial fix, so this asks the events surface the same question directly.
    """
    password = generate_password()
    user = client.create_user(
        label="tlscope", password=password, role_id=viewer_role["id"]
    )
    camera = client.create_camera(
        label="tlprivate",
        rtsp_url=f"rtsp://{config.fakecam_ip}:8554/{sandbox.name('tlprivate')}",
        ip_address=config.fakecam_ip,
    )

    as_viewer = _login(client, user["username"], password)

    response = as_viewer.get(
        routes.EVENTS,
        params={"camera_id": camera["id"], "limit": 50},
        expect=None,
    )

    if response.status_code == 200:
        leaked = response.json().get("events", [])
        assert not leaked, (
            f"the timeline returned {len(leaked)} event(s) for a camera the "
            "caller has no grant on"
        )
    else:
        assert response.status_code in (403, 404), (
            f"unexpected status querying an ungranted camera's events: "
            f"{response.status_code}"
        )


def test_bad_credentials_are_refused_for_a_real_account(client, sandbox, viewer_role):
    """Passwords are actually checked.

    Aimed at a throwaway user, deliberately. A failed login counts toward the
    five attempts that lock an account for three minutes, and this suite runs
    in random order -- spending the *admin's* budget could wedge an entire
    run depending on where the shuffle put this test. The user here is deleted
    moments later, so its lockout costs nothing.
    """
    password = generate_password()
    user = client.create_user(
        label="wrongpw", password=password, role_id=viewer_role["id"]
    )

    response = client.post(
        routes.AUTH_LOGIN_JSON,
        json_body={"username": user["username"], "password": password + "x"},
        auth=False,
        expect=None,
    )

    assert response.status_code in (401, 403), (
        f"a wrong password was not refused (HTTP {response.status_code})"
    )
