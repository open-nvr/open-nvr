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
"""

from fastapi import HTTPException, status

from core.auth import verify_totp_code
from models import User


def require_mfa_code(current_user: User, mfa_code: str | None) -> None:
    """Verify the caller's current TOTP code for sensitive actions."""
    if not current_user.mfa_secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Set up MFA before performing this action.",
        )
    if not mfa_code or not verify_totp_code(current_user.mfa_secret, mfa_code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing MFA code.",
        )
