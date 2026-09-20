# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Claim the admin account and hold a logged-in session.

OpenNVR ships no default password (V-001). A fresh database seeds an admin with
``password_set=False``, and every boot mints a one-time token which is printed
to stdout and **nowhere else** — no API returns it. Claiming the account means:

    GET  /auth/check-setup          -> setup_required?
    POST /auth/first-time-setup     -> {username, password, setup_token}
                                       returns the TOTP secret
    POST /auth/login-json           -> {username, password, code}

That third step is not optional. ``first-time-setup`` sets ``mfa_enabled=True``
with a real secret, and ``/auth/login`` refuses MFA accounts outright, so the
suite computes TOTP codes like a real authenticator would.

**Lockout is the trap here.** A wrong or missing MFA code counts as a failed
login; five of them lock the account for three minutes and wedge the entire
run. So this module never retries a code immediately — a retry would replay the
*same* 30-second window and fail identically, burning an attempt for nothing.
It waits for the next window, and gives up after two.

Sessions are persisted to ``.artifacts/credentials.json`` so a re-run against an
already-bootstrapped stack logs straight back in. That is what makes the normal
dev loop — edit a test, re-run it against a warm stack — take seconds.
"""

from __future__ import annotations

import json
import logging
import secrets
import string
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import httpx
import pyotp

from . import routes
from .budgets import BUDGETS

log = logging.getLogger(__name__)

#: TOTP step, in seconds. Standard, and what pyotp defaults to.
_TOTP_STEP = 30

#: The account the server seeds. server/core/config.py:default_admin_username.
ADMIN_USERNAME = "admin"


class BootstrapError(RuntimeError):
    """Setup could not be completed. Always carries the operator's next move."""


@dataclass
class SetupEvidence:
    """What the bootstrap observed, so ``test_bootstrap.py`` can assert on it.

    Recording the evidence here rather than performing setup inside a test is
    what frees the suite from a required test order: setup happens once, in a
    session fixture, and the test that checks it simply reads this.
    """

    #: False when we logged into an already-bootstrapped stack.
    performed_setup: bool = False
    #: Did GET /auth/check-setup say setup was needed?
    setup_was_required: bool | None = None
    #: Did first-time-setup return a usable TOTP secret?
    mfa_secret_issued: bool = False
    #: Did replaying the same token fail, proving it is single-use?
    token_rejected_on_reuse: bool | None = None
    #: Status code that replay returned, for the assertion message.
    token_reuse_status: int | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class AdminSession:
    """A logged-in admin, plus everything needed to log in again."""

    username: str
    password: str
    mfa_secret: str | None
    access_token: str
    refresh_token: str | None
    device_token: str | None
    evidence: SetupEvidence = field(default_factory=SetupEvidence)

    def totp(self) -> str | None:
        return pyotp.TOTP(self.mfa_secret).now() if self.mfa_secret else None


# ---------------------------------------------------------------------------
# Credential persistence
# ---------------------------------------------------------------------------
def _credentials_path(artifacts: Path) -> Path:
    return artifacts / "credentials.json"


def _save(artifacts: Path, session: AdminSession) -> None:
    artifacts.mkdir(parents=True, exist_ok=True)
    path = _credentials_path(artifacts)
    payload = {
        "username": session.username,
        "password": session.password,
        "mfa_secret": session.mfa_secret,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # Credentials for a throwaway stack, but there is no reason to leave them
    # world-readable on a shared machine.
    try:
        path.chmod(0o600)
    except OSError:
        pass  # Windows bind mounts do not always support chmod


def _load(artifacts: Path) -> dict | None:
    path = _credentials_path(artifacts)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("Ignoring unreadable %s", path)
        return None


def generate_password(length: int = 24) -> str:
    """A password that satisfies the shipped policy by construction.

    The policy (``server/models.py::PasswordPolicy``) wants >= 8 characters
    from >= 3 character classes, and ``server/scripts/init_db.py`` silently
    discards anything on its ``KNOWN_BAD_PASSWORDS`` list. Drawing 24
    characters from all four classes clears both without a retry loop — and
    randomness, not a fixed literal, is what keeps it off that list.
    """
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in candidate)
            and any(c.isupper() for c in candidate)
            and any(c.isdigit() for c in candidate)
            and any(c in "!@#$%^&*-_=+" for c in candidate)
        ):
            return candidate


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------
def ensure_admin(
    core_url: str,
    artifacts: Path,
    *,
    setup_token: str | None = None,
    token_reader=None,
) -> AdminSession:
    """Return a logged-in admin, bootstrapping the stack if it is fresh.

    Args:
        core_url: base URL of opennvr-core, e.g. ``http://opennvr-core:8000``.
        artifacts: directory holding ``credentials.json`` between runs.
        setup_token: the one-time token, if the caller already has it
            (``run.py`` scrapes it on the host and passes it through
            ``E2E_SETUP_TOKEN``).
        token_reader: fallback callable returning the token, used when
            ``setup_token`` is empty. ``compose.read_setup_token`` supplies it
            by reading the core container's logs.

    Raises:
        BootstrapError: with the operator's next action spelled out.
    """
    http = httpx.Client(base_url=core_url.rstrip("/"), timeout=BUDGETS.REQUEST)
    try:
        stored = _load(artifacts)
        if stored:
            session = _try_existing(http, stored)
            if session is not None:
                return session
            log.info("Stored credentials did not work; attempting fresh setup.")

        return _perform_setup(
            http, artifacts, setup_token=setup_token, token_reader=token_reader
        )
    finally:
        http.close()


def _try_existing(http: httpx.Client, stored: dict) -> AdminSession | None:
    """Log in with saved credentials. None if they are no longer valid."""
    username = stored.get("username") or ADMIN_USERNAME
    password = stored.get("password")
    mfa_secret = stored.get("mfa_secret")
    if not password:
        return None

    try:
        tokens = _login(http, username, password, mfa_secret)
    except BootstrapError as exc:
        log.info("Re-login with stored credentials failed: %s", exc)
        return None

    evidence = SetupEvidence(
        performed_setup=False,
        notes=["Reused credentials from a previous run against this stack."],
    )
    return AdminSession(
        username=username,
        password=password,
        mfa_secret=mfa_secret,
        access_token=tokens["access_token"],
        refresh_token=tokens.get("refresh_token"),
        device_token=tokens.get("device_token"),
        evidence=evidence,
    )


def _perform_setup(
    http: httpx.Client,
    artifacts: Path,
    *,
    setup_token: str | None,
    token_reader=None,
) -> AdminSession:
    check = http.post(routes.api(routes.AUTH_CHECK_SETUP))
    check.raise_for_status()
    body = check.json()
    setup_required = bool(body.get("setup_required"))

    if not setup_required:
        raise BootstrapError(
            "The stack says first-time setup is already complete, but this run "
            "has no working credentials for it.\n"
            "  Either point the suite at the credentials.json from the run that "
            "claimed it, or start from a clean database:\n"
            "      tests/e2e/run.py --fresh"
        )

    username = body.get("username") or ADMIN_USERNAME
    token = setup_token or (token_reader() if token_reader else None)
    if not token:
        raise BootstrapError(
            "First-time setup is required but no setup token is available.\n"
            "  The token is printed to the core container's stdout on boot and "
            "is not retrievable from any API.\n"
            "      docker logs opennvr_e2e_core | grep -A2 'setup token'\n"
            "  Then re-run with E2E_SETUP_TOKEN=<token>, or use "
            "tests/e2e/run.py which scrapes it for you."
        )

    password = generate_password()
    evidence = SetupEvidence(performed_setup=True, setup_was_required=True)

    resp = http.post(
        routes.api(routes.AUTH_FIRST_TIME_SETUP),
        json={"username": username, "password": password, "setup_token": token},
    )
    if resp.status_code == 403:
        raise BootstrapError(
            "The setup token was rejected (403).\n"
            "  It is consumed on first use and re-minted only at boot, so a "
            "stale token from an earlier run will always fail.\n"
            "  Restart core to mint a fresh one, or run with --fresh."
        )
    if resp.status_code != 200:
        raise BootstrapError(
            f"first-time-setup failed with {resp.status_code}: {resp.text}"
        )

    mfa_secret = resp.json().get("mfa_secret")
    evidence.mfa_secret_issued = bool(mfa_secret)
    if not mfa_secret:
        raise BootstrapError(
            "first-time-setup succeeded but returned no MFA secret, so no "
            "login is possible. This is a server-side contract break."
        )

    # Prove the token is single-use. Cheap here, and it is the security
    # property the whole one-time-token design exists for — worth asserting on
    # every run rather than trusting it.
    replay = http.post(
        routes.api(routes.AUTH_FIRST_TIME_SETUP),
        json={"username": username, "password": password, "setup_token": token},
    )
    evidence.token_reuse_status = replay.status_code
    evidence.token_rejected_on_reuse = replay.status_code != 200

    tokens = _login(http, username, password, mfa_secret)
    session = AdminSession(
        username=username,
        password=password,
        mfa_secret=mfa_secret,
        access_token=tokens["access_token"],
        refresh_token=tokens.get("refresh_token"),
        device_token=tokens.get("device_token"),
        evidence=evidence,
    )
    _save(artifacts, session)
    return session


def _login(
    http: httpx.Client, username: str, password: str, mfa_secret: str | None
) -> dict:
    """Log in, computing TOTP when the account has a secret.

    Retries only across a *new* TOTP window: replaying the same code inside the
    same 30-second step cannot succeed and would burn one of the five attempts
    that lock the account for three minutes.
    """
    attempts = 2 if mfa_secret else 1
    last: httpx.Response | None = None

    for attempt in range(attempts):
        if attempt:
            _sleep_to_next_totp_window()
        payload: dict[str, str] = {"username": username, "password": password}
        if mfa_secret:
            payload["code"] = pyotp.TOTP(mfa_secret).now()

        last = http.post(routes.api(routes.AUTH_LOGIN_JSON), json=payload)
        if last.status_code == 200:
            return last.json()
        if last.status_code == 423:
            raise BootstrapError(
                "The admin account is locked out.\n"
                f"  {last.text}\n"
                "  Wait for the lockout to expire (3 minutes by default) or "
                "run with --fresh to start from a clean database."
            )
        if last.status_code != 401:
            break  # 403 setup-required and friends are not worth a retry

    detail = last.text if last is not None else "no response"
    status = last.status_code if last is not None else 0
    raise BootstrapError(f"login-json failed with {status}: {detail}")


def enrol_mfa(client, username: str, password: str) -> str:
    """Take a freshly created user through MFA enrolment. Returns the secret.

    Not optional for anything that drives the browser. ``ProtectedShell``
    (app/src/main.tsx) renders ``<MFASetup/>`` for **any** signed-in user whose
    ``mfa_enabled`` is false, so a new account reaches the enrolment QR code
    and never the app shell. A UI test that skips this waits out its budget on
    a navigation that structurally cannot appear, and the failure reads as
    "the viewer sees no navigation at all" -- which is true, and nothing to do
    with permissions.

    The API layer does not care, which is why the API RBAC tests never needed
    this: ``login-json`` issues a token to an un-enrolled user quite happily.
    The gate is the SPA's.

    Enrolment is the same three calls the UI makes: log in (no code -- MFA is
    not on yet), ask for a secret, then prove possession of it.
    """
    tokens = client.login(username, password)
    as_them = client.as_user(tokens["access_token"])

    secret = as_them.post(routes.AUTH_MFA_SETUP, expect=(200,)).json()["secret"]

    # A code minted in the last moments of its step can be validated in the
    # next one and rejected. Enrolment failures are cheap to avoid and
    # expensive to debug, so spend the second.
    remaining = _TOTP_STEP - (int(time.time()) % _TOTP_STEP)
    if remaining < 5:
        time.sleep(remaining + 1)

    as_them.post(
        routes.AUTH_MFA_VERIFY,
        json_body={"code": pyotp.TOTP(secret).now()},
        expect=(200,),
    )
    return secret


def _sleep_to_next_totp_window() -> None:
    """Block until the current TOTP step rolls over, plus a small margin."""
    remaining = _TOTP_STEP - (int(time.time()) % _TOTP_STEP)
    time.sleep(remaining + 1)


__all__ = [
    "AdminSession",
    "SetupEvidence",
    "BootstrapError",
    "ensure_admin",
    "generate_password",
    "enrol_mfa",
    "ADMIN_USERNAME",
]
