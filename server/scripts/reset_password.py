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

"""Set a user's password from the host.

For when nobody who can use the web UI's password reset is available,
typically the only administrator having forgotten theirs:

    docker exec -it opennvr_core python /app/server/scripts/reset_password.py <username>
    docker exec -it opennvr_core python /app/server/scripts/reset_password.py <username> --generate

Without ``--generate`` it asks for the new password twice (nothing is
echoed). With it, a random password that meets the site's password policy
is printed once. Either way the password policy applies, every browser
session of the user ends, a lockout is cleared, and the change is recorded
in the audit log as ``auth.cli_password_reset``.

Two-factor sign-in is left as it is; reset that with ``disable_mfa.py``.
A single sign-on account is refused: its password lives at the identity
provider.
"""

import argparse
import getpass
import os
import secrets
import string
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException  # noqa: E402

from core.auth import get_password_hash  # noqa: E402
from core.database import SessionLocal  # noqa: E402
from models import PasswordPolicy, User  # noqa: E402
from services.audit_service import write_audit_log  # noqa: E402
from services.user_service import UserService  # noqa: E402

_SYMBOLS = "!#%+-=?@^_"


def generate_password(db) -> str:
    """Random, with every character class, at least 20 characters (longer if
    the policy asks for more)."""
    policy = db.query(PasswordPolicy).first()
    length = max(20, int(getattr(policy, "min_length", 0) or 0))
    classes = [string.ascii_lowercase, string.ascii_uppercase, string.digits, _SYMBOLS]
    alphabet = "".join(classes)
    chars = [secrets.choice(c) for c in classes]
    chars += [secrets.choice(alphabet) for _ in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def reset_password(username: str, new_password: str | None) -> bool:
    """Set ``username``'s password (random when ``new_password`` is None).
    True on success."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        if not user:
            print(f"❌ User '{username}' not found")
            return False
        if (user.auth_provider or "local") == "oidc":
            print(f"ℹ️  '{username}' signs in through single sign-on; "
                  "change the password at the identity provider.")
            return False
        generated = new_password is None
        password = generate_password(db) if generated else new_password
        try:
            UserService._enforce_password_policy(db, user.username, user.email, password)
        except HTTPException as exc:
            print(f"❌ {exc.detail}")
            return False
        user.hashed_password = get_password_hash(password)
        user.password_set = True
        user.token_version = int(user.token_version or 0) + 1   # every session ends
        user.failed_login_attempts = 0
        user.locked_until = None
        db.commit()
        try:
            write_audit_log(db, action="auth.cli_password_reset", user_id=None, entity_type="user",
                            entity_id=user.id, details={"username": user.username, "generated": generated})
        except Exception as exc:  # the reset stands; say the record is missing
            print(f"⚠️  Password set, but the audit entry failed: {exc}")
        print(f"✅ Password reset for '{username}'. Their sessions have ended.")
        if generated:
            print(f"   New password: {password}")
            print("   It is shown only this once; have them change it after signing in.")
        if user.mfa_enabled:
            print("   Two-factor sign-in is unchanged (reset it with disable_mfa.py if needed).")
        return True
    except Exception as exc:
        print(f"❌ Error resetting password: {exc}")
        db.rollback()
        return False
    finally:
        db.close()


def _ask_password() -> str | None:
    first = getpass.getpass("New password: ")
    if not first:
        print("❌ Empty password")
        return None
    if getpass.getpass("Repeat it: ") != first:
        print("❌ The two entries differ")
        return None
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set a user's password from the host.")
    parser.add_argument("username")
    parser.add_argument("--generate", action="store_true",
                        help="set a random password and print it, instead of asking for one")
    args = parser.parse_args(argv)
    username = args.username.strip()
    if not username:
        parser.error("username is required")
    password = None
    if not args.generate:
        password = _ask_password()
        if password is None:
            return 1
    return 0 if reset_password(username, password) else 1


if __name__ == "__main__":
    sys.exit(main())
