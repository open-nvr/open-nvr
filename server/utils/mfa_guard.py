# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
#
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""Shared TOTP re-verification guard for destructive endpoints.

Sensitive actions (user delete/activate, camera hard delete) require the
caller to present a *current* TOTP code from one of their authenticators in
the X-MFA-Code header on top of a valid session — proof of presence, not just
a live token. Changes to the account's own second factors also ask for the
account password (``require_reauth``).

A wrong code or password counts toward the account lockout exactly like a
wrong one at sign-in, and a locked account is refused here too: otherwise a
live session could guess the six digits without limit.
"""

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy.orm import object_session

from core.auth import build_account_locked_detail, get_lockout_policy, verify_password
from models import User
from services import mfa_service


def _locked_until(user: User) -> datetime | None:
    """The lock's end while it is still in force, else None."""
    until = user.locked_until
    if until is None:
        return None
    if until.tzinfo is None:          # a backend that drops the zone (SQLite)
        until = until.replace(tzinfo=UTC)
    return until if until > datetime.now(UTC) else None


def _session_of(user: User):
    try:
        return object_session(user)
    except Exception:   # not a mapped User (an API-token principal)
        return None


def _refuse_if_locked(user: User) -> None:
    if until := _locked_until(user):
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=build_account_locked_detail(until),
        )


def _count_failure(db, user: User) -> None:
    max_attempts, lockout_mins = get_lockout_policy(db)
    user.failed_login_attempts = int(user.failed_login_attempts or 0) + 1
    if user.failed_login_attempts >= max_attempts:
        user.locked_until = datetime.now(UTC) + timedelta(minutes=lockout_mins)
        user.failed_login_attempts = 0
    db.commit()
    _refuse_if_locked(user)


def _clear_failures(db, user: User) -> None:
    if user.failed_login_attempts or user.locked_until:
        user.failed_login_attempts = 0
        user.locked_until = None
    db.commit()


def require_mfa_code(current_user: User, mfa_code: str | None) -> None:
    """Verify the caller's current TOTP code for sensitive actions."""
    require_reauth(current_user, mfa_code)


def require_reauth(current_user: User, mfa_code: str | None, *,
                   password: str | None = None, need_password: bool = False,
                   allow_recovery: bool = False) -> None:
    """A current code from one of the caller's authenticators, and with
    ``need_password`` their account password as well. ``allow_recovery`` also
    takes (and uses up) a recovery code: only for the account's own
    two-factor settings, never for destructive actions."""
    db = _session_of(current_user)
    if db is None or not mfa_service.has_active_factor(db, current_user):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Set up MFA before performing this action.",
        )
    _refuse_if_locked(current_user)
    if not mfa_code or not mfa_code.strip() or (need_password and not password):
        # Nothing entered is a client that skipped the prompt, not a guess.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your password and a current MFA code are required."
            if need_password else "Invalid or missing MFA code.",
        )
    ok = (not need_password) or verify_password(password, current_user.hashed_password)
    # Password first: a wrong password must not use up a recovery code.
    ok = ok and mfa_service.verify_step_up(db, current_user, mfa_code.strip(),
                                           allow_recovery=allow_recovery)
    if ok:
        _clear_failures(db, current_user)
        return
    _count_failure(db, current_user)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Wrong password or MFA code." if need_password else "Invalid or missing MFA code.",
    )
