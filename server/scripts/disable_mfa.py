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

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
"""
Utility script to disable MFA for a user (for testing purposes).
"""

from core.database import SessionLocal
from models import User


def disable_user_mfa(username: str = "admin"):
    """Disable MFA for a specific user."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        if not user:
            print(f"❌ User '{username}' not found")
            return False

        if not user.mfa_enabled:
            print(f"ℹ️  MFA already disabled for user '{username}'")
            return True

        user.mfa_enabled = False
        user.mfa_secret = None  # Clear the secret
        db.commit()

        print(f"✅ MFA disabled for user '{username}'")
        return True

    except Exception as e:
        print(f"❌ Error disabling MFA: {e}")
        db.rollback()
        return False
    finally:
        db.close()


if __name__ == "__main__":
    import sys

    username = sys.argv[1] if len(sys.argv) > 1 else "admin"
    disable_user_mfa(username)
