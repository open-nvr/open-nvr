# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Claiming the admin account on a fresh install.

This is the first thing a real operator does and the first thing that can go
wrong, and it is guarded by three separate mechanisms — a one-time token, a
password policy, and mandatory TOTP — that only ever run together on a genuinely
fresh database. Unit tests exercise each in isolation; nothing else exercises
the sequence.

**These tests own no ordering slot.** The setup token is consumed the first
time it is used, so performing setup inside a test would force that test to run
first and make every other test depend on it. Instead the session fixture
performs setup once and records what it saw in ``SetupEvidence``; the tests
below assert against that record and against the live session it produced. Run
them alone, run them last, run them shuffled — same result.

On a stack that was already claimed by an earlier run, the setup-specific
assertions skip rather than fail: they need ``run.py --fresh`` to be meaningful,
and a skip says that honestly where a failure would just be noise.
"""

from __future__ import annotations

import pytest

from harness import routes
from harness.budgets import BUDGETS
from harness.waiting import eventually

pytestmark = pytest.mark.smoke


def test_admin_session_is_usable(client, admin):
    """The bootstrapped session actually authenticates against the API."""
    me = client.json(routes.AUTH_ME)

    assert me["username"] == admin.username
    assert me.get("is_active") is True, f"the admin account is not active: {me}"


def test_admin_is_a_superuser(client):
    """Setup must produce an account that can administer the system.

    An admin without privileges is a subtly broken install: login succeeds, so
    it looks fine, and every later action 403s.
    """
    me = client.json(routes.AUTH_ME)
    permissions = client.json(routes.USERS_ME_PERMISSIONS)

    assert me.get("is_superuser") or permissions, (
        "the bootstrapped admin is neither a superuser nor holds any "
        f"permissions: user={me}, permissions={permissions}"
    )


def test_setup_was_actually_required(admin):
    """A fresh stack must demand setup rather than opening up by default.

    The property under test is that OpenNVR ships with no usable default
    credentials (V-001).
    """
    evidence = admin.evidence
    if not evidence.performed_setup:
        pytest.skip(
            "this stack was already bootstrapped by an earlier run; "
            "re-run with `python tests/e2e/run.py --fresh` to exercise setup"
        )

    assert evidence.setup_was_required is True, (
        "GET /auth/check-setup reported that no setup was needed on a database "
        "that had never been claimed — that would mean the admin account is "
        "reachable without the one-time token"
    )


def test_setup_token_is_single_use(admin):
    """Replaying the token must fail.

    This is the whole point of the one-time token: it stops anyone on the LAN
    racing the operator to claim the admin account. A token that still worked
    on replay would leave that window open for the life of the process.
    """
    evidence = admin.evidence
    if not evidence.performed_setup:
        pytest.skip(
            "this stack was already bootstrapped by an earlier run; "
            "re-run with `python tests/e2e/run.py --fresh` to exercise setup"
        )

    assert evidence.token_rejected_on_reuse is True, (
        "the setup token was accepted a second time "
        f"(HTTP {evidence.token_reuse_status}) — it must be consumed on first use"
    )


def test_mfa_was_enrolled_during_setup(admin):
    """Setup must hand back a TOTP secret, and it must be the real one.

    ``/auth/login`` refuses MFA accounts outright, so a setup that enabled MFA
    without returning a working secret would lock the operator out of the
    account it just created. Proving the secret works means proving a code
    derived from it authenticates — which the session fixture already did to
    get here, so this asserts on that outcome.
    """
    if not admin.evidence.performed_setup:
        pytest.skip("stack already bootstrapped; run with --fresh")

    assert admin.evidence.mfa_secret_issued, "first-time-setup returned no MFA secret"
    assert admin.mfa_secret, "no MFA secret was retained for the session"
    assert admin.totp(), "could not derive a TOTP code from the issued secret"


def test_setup_is_closed_afterwards(client):
    """Once claimed, the account must stop advertising itself as unclaimed.

    A ``setup_required`` that stayed true would invite a second claim attempt
    against a live system.
    """
    body = eventually(
        lambda: client.post(routes.AUTH_CHECK_SETUP, auth=False).json(),
        until=lambda payload: payload.get("setup_required") is False,
        budget=BUDGETS.QUICK,
        describe="/auth/check-setup to report that setup is complete",
    )
    assert body["setup_required"] is False


def test_health_needs_no_authentication(client):
    """``/health`` must answer unauthenticated — orchestrators depend on it.

    Compose, Kubernetes and the readiness loop in ``start.sh`` all poll this
    with no credentials. Putting it behind auth would wedge every one of them.
    """
    response = client.get(routes.ABS_HEALTH, absolute=True, auth=False)

    assert response.status_code == 200
    assert response.json().get("status"), f"unexpected health payload: {response.text}"


def test_login_rejects_unknown_credentials(client, sandbox):
    """Credentials that match no account must be refused.

    Aimed at a username that does not exist, on purpose. A failed login — with
    a wrong password just as much as a wrong TOTP code — counts toward the five
    attempts that lock an account for three minutes, and this suite runs in
    random order: a test that spends a real account's attempt budget could wedge
    an entire run depending on where the shuffle put it. Password checking
    against a live account is covered in test_rbac.py, on a throwaway user that
    is deleted immediately afterwards.
    """
    response = client.post(
        routes.AUTH_LOGIN_JSON,
        json_body={
            "username": sandbox.name("nobody"),
            "password": "definitely-not-the-password",
        },
        auth=False,
        expect=None,
    )

    assert response.status_code in (401, 403), (
        f"expected the login to be refused, got {response.status_code}: "
        f"{response.text}"
    )
