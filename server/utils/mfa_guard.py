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
caller to present their *current* TOTP code in the X-MFA-Code header on top
of a valid session — proof of presence, not just a live token.

A wrong code counts toward the account lockout exactly like a wrong code at
sign-in, and a locked account is refused here too: otherwise a live session
could guess the six digits without limit.
"""

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy.orm import object_session

from core.auth import build_account_locked_detail, get_lockout_policy, verify_totp_code
from models import User


def _locked_until(user: User) -> datetime | None:
    """The lock's end while it is still in force, else None."""
    until = user.locked_until
    if until is None:
        return None
    if until.tzinfo is None:          # a backend that drops the zone (SQLite)
        until = until.replace(tzinfo=UTC)
    return until if until > datetime.now(UTC) else None


def require_mfa_code(current_user: User, mfa_code: str | None) -> None:
    """Verify the caller's current TOTP code for sensitive actions."""
    if not current_user.mfa_secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Set up MFA before performing this action.",
        )
    if until := _locked_until(current_user):
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=build_account_locked_detail(until),
        )
    if not mfa_code or not mfa_code.strip():
        # No code at all is a client that skipped the prompt, not a guess.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing MFA code.",
        )
    try:
        db = object_session(current_user)
    except Exception:   # not a mapped User (an API-token principal)
        db = None
    if verify_totp_code(current_user.mfa_secret, mfa_code.strip()):
        if db is not None and (current_user.failed_login_attempts or current_user.locked_until):
            current_user.failed_login_attempts = 0
            current_user.locked_until = None
            db.commit()
        return
    if db is not None:
        max_attempts, lockout_mins = get_lockout_policy(db)
        current_user.failed_login_attempts = int(current_user.failed_login_attempts or 0) + 1
        if current_user.failed_login_attempts >= max_attempts:
            current_user.locked_until = datetime.now(UTC) + timedelta(minutes=lockout_mins)
            current_user.failed_login_attempts = 0
        db.commit()
        if until := _locked_until(current_user):
            raise HTTPException(
                status_code=status.HTTP_423_LOCKED,
                detail=build_account_locked_detail(until),
            )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing MFA code.",
    )
