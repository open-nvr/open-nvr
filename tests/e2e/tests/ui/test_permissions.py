# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a restricted user can actually see on screen.

The API mirror proves the endpoints scope correctly. This proves the UI honours
that scoping -- which is a genuinely separate risk: the shell decides what to
render from the caller's permissions, so a page can leak a camera the API would
have refused, or hide one it would have allowed.
"""

from __future__ import annotations

import pyotp
import pytest
from playwright.sync_api import expect

from harness import routes
from harness.bootstrap import enrol_mfa, generate_password
from pages import CamerasPage, Shell

pytestmark = pytest.mark.ui


@pytest.fixture
def viewer_role(client) -> dict:
    roles = client.json(routes.ROLES)["roles"]
    for role in roles:
        if role["name"] == "viewer":
            return role
    pytest.skip(f"no 'viewer' role seeded; found {[r['name'] for r in roles]}")


def _sign_in_as(page, username: str, password: str, client) -> None:
    """Replace the seeded admin session with this user's, before first render.

    Reuses the API to mint the tokens rather than driving the login form again:
    the form is covered by test_login.py, and doing it here would add ten
    seconds per test to prove something already proven.

    Enrolling MFA first is what makes the session usable in a browser at all.
    ``ProtectedShell`` renders the enrolment QR code for any user with
    ``mfa_enabled`` false, so without this a freshly created account gets a
    valid token and still never reaches a single page of the app.
    """
    secret = enrol_mfa(client, username, password)
    tokens = client.login(username, password, code=pyotp.TOTP(secret).now())
    page.add_init_script(
        "(() => { try {"
        f" localStorage.setItem('opennvr.token', {tokens['access_token']!r});"
        f" localStorage.setItem('opennvr.device_token', {tokens.get('device_token') or ''!r});"
        " } catch (e) {} })()"
    )


def test_a_viewer_sees_fewer_destinations_than_an_admin(
    authed_page, client, sandbox, viewer_role
):
    """The nav is permission-gated, and that gating has to be visible.

    An admin holds every permission, a viewer holds three. If the shell renders
    the same navigation for both, the gate is not being applied -- and every
    restricted page becomes one click away.

    ``nav_labels()`` counts the collapsible group headers as destinations, and
    has to: the four NVR links are permitted to both roles, so a reading that
    covers only links returns identical sets for an administrator and a viewer
    and can never fail. The gating lives in the groups -- AI, Security,
    Governance and Administration are dropped whole when no item in them is
    permitted.
    """
    shell = Shell(authed_page)
    shell.open()
    shell.wait_for_nav()
    admin_labels = set(shell.nav_labels())

    password = generate_password()
    user = client.create_user(
        label="uiviewer", password=password, role_id=viewer_role["id"]
    )
    _sign_in_as(authed_page, user["username"], password, client)

    shell.open()
    shell.wait_for_nav(at_least=1)
    viewer_labels = set(shell.nav_labels())

    assert viewer_labels, "the viewer sees no navigation at all"
    assert viewer_labels < admin_labels, (
        "a viewer sees the same navigation as an admin, so the shell is not "
        f"gating on permissions. admin={sorted(admin_labels)} "
        f"viewer={sorted(viewer_labels)}"
    )


def test_a_viewer_does_not_see_an_ungranted_camera(
    authed_page, client, sandbox, config, viewer_role
):
    """Scoping holds on the page, not just in the response.

    The strongest GUI-only assertion in this file: even if the API filtered
    correctly, a page that rendered from a cached or unscoped query would show
    the camera anyway.

    Two cameras, one granted, is not belt-and-braces -- it is the only version
    of this test that can fail. "The forbidden row is absent" is also true of
    the login page, the MFA enrolment screen, an error boundary and a page that
    never finished loading, so on its own it proves nothing about scoping. The
    granted row is the control: it says the viewer really did reach a populated
    Cameras table, and the other camera is missing from it by permission.
    """
    granted = client.create_camera(
        label="uigrant",
        rtsp_url=f"rtsp://{config.fakecam_ip}:8554/{sandbox.name('uigrant')}",
        ip_address=config.fakecam_ip,
    )
    hidden = client.create_camera(
        label="uiprivate",
        rtsp_url=f"rtsp://{config.fakecam_ip}:8554/{sandbox.name('uiprivate')}",
        ip_address=config.fakecam_ip,
    )
    password = generate_password()
    user = client.create_user(
        label="uinogrant", password=password, role_id=viewer_role["id"]
    )
    client.grant_camera_permission(granted["id"], user["id"], can_view=True)
    _sign_in_as(authed_page, user["username"], password, client)

    cameras = CamerasPage(authed_page)
    cameras.page.goto(cameras.path)

    expect(cameras.row(granted["name"]).first).to_be_visible(timeout=30_000)
    expect(cameras.row(hidden["name"])).to_have_count(0, timeout=30_000)
