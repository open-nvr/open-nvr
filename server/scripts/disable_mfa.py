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

"""Reset a user's two-factor sign-in from the host.

For when nobody who can use the web UI's "Reset 2FA" action is available,
typically the only administrator having lost their authenticator:

    docker exec -it opennvr_core python /app/server/scripts/disable_mfa.py <username>

Does exactly what the UI action does (services.mfa_service.reset_mfa): the
second factor is removed, every browser session of the user ends, and their
next password sign-in asks them to enrol a new authenticator. Recorded in the
audit log as ``mfa.cli_reset``.

Until that user signs in and enrols, their password alone gets them as far as
the enrolment screen, so run it when they are ready to do that.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import SessionLocal  # noqa: E402
from models import User  # noqa: E402
from services import mfa_service  # noqa: E402
from services.audit_service import write_audit_log  # noqa: E402


def disable_user_mfa(username: str) -> bool:
    """Reset ``username``'s two-factor sign-in. True on success."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        if not user:
            print(f"❌ User '{username}' not found")
            return False
        try:
            details = mfa_service.reset_mfa(db, user)
        except mfa_service.MfaResetRefused as exc:
            print(f"ℹ️  {exc}")
            return False
        try:
            write_audit_log(db, action="mfa.cli_reset", user_id=None, entity_type="user",
                            entity_id=user.id, details=details)
        except Exception as exc:  # the reset stands; say the record is missing
            print(f"⚠️  Reset done, but the audit entry failed: {exc}")
        print(f"✅ Two-factor sign-in reset for '{username}'. Their sessions have ended; "
              "at their next sign-in they set up a new authenticator.")
        return True
    except Exception as exc:
        print(f"❌ Error resetting MFA: {exc}")
        db.rollback()
        return False
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        print("usage: disable_mfa.py <username>")
        sys.exit(2)
    sys.exit(0 if disable_user_mfa(sys.argv[1].strip()) else 1)
