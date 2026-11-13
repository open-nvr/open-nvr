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

"""Two-factor sign-in: the one place that changes an account's second factor.

Today an account has a single TOTP secret on its row; ``reset_mfa`` is what
both the administrator's "Reset 2FA" action and ``scripts/disable_mfa.py``
call, so the two can never drift apart.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from models import User


class MfaResetRefused(ValueError):
    """The account has no second factor here to reset."""


def is_sso_account(user: User) -> bool:
    return (user.auth_provider or "local") == "oidc"


def reset_mfa(db: Session, target: User) -> dict:
    """Remove the account's second factor so it enrols afresh at next sign-in.

    - The secret and the flag go; the next password sign-in lands on the
      enrolment screen, and until it enrols the session reaches nothing else
      (``core.auth._refuse_until_enrolled``).
    - ``token_version`` is bumped, ending every browser session and refresh
      token the account holds (not its ``onvr_`` API tokens).
    - A single sign-on account whose provider owns MFA has nothing to reset.
      One marked single sign-on that nevertheless has TOTP is a password
      account linked by e-mail before linking was restricted: it becomes a
      local account, so it does not slip out of the enrolment requirement
      (and, for a superuser, stays refused by single sign-on).

    Commits. Returns what changed, for the audit entry.
    """
    if is_sso_account(target) and not target.mfa_enabled and not target.mfa_secret:
        raise MfaResetRefused(
            "This account signs in through single sign-on; its identity provider owns two-factor sign-in."
        )
    details = {
        "username": target.username,
        "had_mfa": bool(target.mfa_enabled and target.mfa_secret),
        "converted_to_local": is_sso_account(target),
    }
    if is_sso_account(target):
        target.auth_provider = None
    target.mfa_secret = None
    target.mfa_enabled = False
    target.token_version = int(target.token_version or 0) + 1
    db.add(target)
    db.commit()
    return details
