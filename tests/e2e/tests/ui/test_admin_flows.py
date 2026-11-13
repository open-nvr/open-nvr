# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""User and role administration, driven through the forms.

These screens had no GUI coverage, and they are among the first to move onto
shared dialogs and form fields. Each journey proves the form still submits
what the API expects and the list still reflects it -- the two things a
restyle of a hand-built dialog most easily breaks.

Rows made through the GUI are invisible to the sandbox (the API client did not
create them), so each test registers its own cleanup by looking the row up.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from harness import routes
from pages.base import BasePage, confirm_if_asked, fatal_errors

pytestmark = pytest.mark.ui

PASSWORD = "E2e-Passw0rd!"


def _track_user_by_name(client, sandbox, username: str) -> None:
    def undo() -> None:
        users = client.get(routes.USERS, params={"limit": 200}).json()
        for u in users.get("users", users if isinstance(users, list) else []):
            if u.get("username") == username:
                client.delete(routes.USER(u["id"]), expect=None)

    sandbox.track(f"user {username} (made in the GUI)", undo)


def _track_role_by_name(client, sandbox, name: str) -> None:
    def undo() -> None:
        data = client.get(routes.ROLES, params={"limit": 200}).json()
        roles = data.get("roles", data) if isinstance(data, dict) else data
        for r in roles:
            if r.get("name") == name:
                client.delete(f"{routes.ROLES}{r['id']}", expect=None)

    sandbox.track(f"role {name} (made in the GUI)", undo)


def test_a_user_can_be_created_through_the_form(authed_page, client, sandbox):
    username = sandbox.name("uiuser")[:50]
    _track_user_by_name(client, sandbox, username)

    page = BasePage(authed_page)
    page.path = "/rbac/users"
    errors = page.console_errors()
    page.open()

    authed_page.get_by_role("button", name="Add User").first.click()
    authed_page.get_by_label(re.compile(r"^Username")).fill(username)
    authed_page.get_by_label(re.compile(r"^Email")).fill(f"{username}@example.com")
    authed_page.get_by_label(re.compile(r"^Password")).fill(PASSWORD)
    authed_page.get_by_role("button", name="Create User").click()

    expect(authed_page.get_by_role("row").filter(has_text=username)).to_be_visible(timeout=20_000)
    assert not fatal_errors(errors), f"users page threw: {errors[:3]}"


def _any_role_id(client) -> int:
    data = client.get(routes.ROLES, params={"limit": 50}).json()
    roles = data.get("roles", data) if isinstance(data, dict) else data
    return roles[0]["id"]


def test_a_user_can_be_deleted_through_the_form(authed_page, client, admin):
    # The API requires a role; the form preselects one, the API does not.
    user = client.create_user(label="uidel", password=PASSWORD, role_id=_any_role_id(client))

    page = BasePage(authed_page)
    page.path = "/rbac/users"
    page.open()

    row = authed_page.get_by_role("row").filter(has_text=user["username"])
    expect(row).to_be_visible(timeout=20_000)
    row.get_by_role("button", name="Delete").click()

    # Deleting a user is MFA-gated: the admin's current TOTP confirms it.
    code = admin.totp()
    if code is None:
        pytest.skip("the e2e admin has no MFA secret, so the gated delete cannot be confirmed")
    authed_page.get_by_placeholder("123456").fill(code)
    # "Delete User" in the confirmation, not the row's own "Delete" behind it.
    authed_page.get_by_role("button", name=re.compile(r"^(Delete User|Confirm)$")).click()

    expect(row).to_have_count(0, timeout=20_000)


def test_a_role_can_be_created_and_deleted_through_the_form(authed_page, client, sandbox):
    name = sandbox.name("uirole")[:50]
    _track_role_by_name(client, sandbox, name)

    page = BasePage(authed_page)
    page.path = "/rbac/roles"
    errors = page.console_errors()
    page.accept_native_dialogs()
    page.open()

    authed_page.get_by_role("button", name="Add Role").first.click()
    authed_page.get_by_label(re.compile(r"^Name")).fill(name)
    authed_page.get_by_role("button", name="Create Role").click()

    row = authed_page.get_by_role("row").filter(has_text=name)
    expect(row).to_be_visible(timeout=20_000)

    row.get_by_role("button", name="Delete").click()
    confirm_if_asked(authed_page)
    expect(row).to_have_count(0, timeout=20_000)
    assert not fatal_errors(errors), f"roles page threw: {errors[:3]}"
