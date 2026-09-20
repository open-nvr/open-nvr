# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings > API Tokens, the way an operator setting up Home Assistant uses it.

The page is the only place a token's secret is ever visible, so the journey
that matters is: create one, copy what the page shows, and have THAT string
work against the API -- then revoke it on the same page and have it stop.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from harness import selectors as S
from pages.base import fatal_errors

pytestmark = pytest.mark.ui


def _as_token(client, secret: str, path: str):
    return client.get(path, expect=None, headers={"Authorization": f"Bearer {secret}"})


def test_a_token_made_on_the_page_works_until_revoked_on_the_page(
    api_tokens_page, client, sandbox
):
    page = api_tokens_page
    name = sandbox.name("ha")
    errors = page.console_errors()
    page.open()

    secret = page.create(name)
    tokens = client.json("/api-tokens")["tokens"]
    mine = next(t for t in tokens if t["name"] == name)
    sandbox.track(
        f"api token {mine['id']} ({name})",
        lambda: client.delete(f"/api-tokens/{mine['id']}", expect=None),
    )

    assert secret.startswith(f"onvr_{mine['prefix']}_"), "the page showed something else"
    # The Home Assistant preset the form starts with.
    assert set(mine["scopes"]) >= {"cameras.view", "live.view", "settings.view"}
    assert mine["camera_ids"] is None

    expect(page.row(name).first).to_be_visible(timeout=30_000)
    assert _as_token(client, secret, "/system/info").status_code == 200
    # A token never reaches the management routes, even when an admin made it.
    assert _as_token(client, secret, "/api-tokens").status_code == 403

    response = page.revoke(name)
    assert response.status < 400, f"revoke failed with {response.status}"
    # A dead secret is not left on screen for someone to copy.
    expect(page.find(S.API_TOKEN_SECRET)).to_have_count(0, timeout=10_000)
    assert _as_token(client, secret, "/system/info").status_code == 401
    assert not fatal_errors(errors), f"the page threw: {errors[:3]}"


def test_the_secret_is_not_shown_again_after_a_reload(api_tokens_page, client, sandbox):
    page = api_tokens_page
    name = sandbox.name("once")
    page.open()
    secret = page.create(name, scopes=["cameras.view"])
    mine = next(t for t in client.json("/api-tokens")["tokens"] if t["name"] == name)
    sandbox.track(
        f"api token {mine['id']} ({name})",
        lambda: client.delete(f"/api-tokens/{mine['id']}", expect=None),
    )
    assert mine["scopes"] == ["cameras.view"]

    page.open()
    expect(page.row(name).first).to_be_visible(timeout=30_000)
    assert secret not in page.page.content()
