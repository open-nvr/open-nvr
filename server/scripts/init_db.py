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
Database initialization script.
Creates initial roles and admin user for the application.
"""

from core.auth import get_password_hash
from core.config import settings
from core.database import SessionLocal, init_db
from models import Permission, Role, RolePermission, User


def create_initial_data():
    """Create initial roles and admin user."""
    db = SessionLocal()
    try:
        # Ensure roles exist
        existing_roles = {r.name: r for r in db.query(Role).all()}

        def get_or_create_role(name: str, desc: str) -> Role:
            role = existing_roles.get(name)
            if role:
                return role
            role = Role(name=name, description=desc)
            db.add(role)
            db.commit()
            db.refresh(role)
            existing_roles[name] = role
            return role

        admin_role = get_or_create_role(
            "admin", "Administrator with full access to all features"
        )
        operator_role = get_or_create_role(
            "operator", "Camera operator with camera management access"
        )
        viewer_role = get_or_create_role("viewer", "Viewer with read-only access")

        # Ensure default permissions
        existing_perms = {p.name: p for p in db.query(Permission).all()}

        def get_or_create_perm(name: str, desc: str) -> Permission:
            perm = existing_perms.get(name)
            if perm:
                return perm
            perm = Permission(name=name, description=desc)
            db.add(perm)
            db.commit()
            db.refresh(perm)
            existing_perms[name] = perm
            return perm

        # Define base permissions
        p_full_access = get_or_create_perm("full_access", "Full access to all features")

        # User management
        p_users_view = get_or_create_perm("users.view", "View users")
        p_users_manage = get_or_create_perm(
            "users.manage", "Create/update/delete users"
        )

        # Role management
        p_roles_view = get_or_create_perm("roles.view", "View roles")
        p_roles_manage = get_or_create_perm(
            "roles.manage", "Create/update/delete roles"
        )
        p_permissions_manage = get_or_create_perm(
            "permissions.manage", "Manage role permissions"
        )

        # Cameras
        p_cameras_view = get_or_create_perm("cameras.view", "View cameras list")
        p_cameras_manage = get_or_create_perm(
            "cameras.manage", "Create/update/delete cameras"
        )

        # Live view
        p_live_view = get_or_create_perm("live.view", "View live camera streams")

        # Recordings / Playback
        p_recordings_view = get_or_create_perm(
            "recordings.view", "View and playback recordings"
        )
        p_recordings_manage = get_or_create_perm(
            "recordings.manage", "Delete recordings, change settings"
        )

        # Settings
        p_settings_view = get_or_create_perm("settings.view", "View system settings")
        p_settings_manage = get_or_create_perm(
            "settings.manage", "Modify system settings"
        )

        # Audit logs
        p_audit_view = get_or_create_perm("audit.view", "View audit logs")

        # Network
        p_network_view = get_or_create_perm(
            "network.view", "View network configuration"
        )
        p_network_manage = get_or_create_perm(
            "network.manage", "Modify network settings"
        )

        # ONVIF
        p_onvif_discover = get_or_create_perm(
            "onvif.discover", "Discover ONVIF devices"
        )

        # Alerts & Incidents
        p_alerts_view = get_or_create_perm("alerts.view", "View security alerts")
        p_alerts_manage = get_or_create_perm(
            "alerts.manage", "Acknowledge/dismiss alerts"
        )

        # Integrations
        p_integrations_view = get_or_create_perm(
            "integrations.view", "View integrations"
        )
        p_integrations_manage = get_or_create_perm(
            "integrations.manage", "Configure integrations"
        )

        # Cloud
        p_cloud_view = get_or_create_perm("cloud.view", "View cloud settings")
        p_cloud_manage = get_or_create_perm("cloud.manage", "Configure cloud settings")

        # Firmware
        p_firmware_view = get_or_create_perm(
            "firmware.view", "View firmware/update status"
        )
        p_firmware_manage = get_or_create_perm(
            "firmware.manage", "Apply firmware updates"
        )

        # AI Engine
        p_ai_view = get_or_create_perm("ai.view", "View AI engine status")
        p_ai_manage = get_or_create_perm("ai.manage", "Configure AI models")

        # Compliance
        p_compliance_view = get_or_create_perm(
            "compliance.view", "View compliance reports"
        )

        # BYOK / BYOM
        p_byok_manage = get_or_create_perm(
            "byok.manage", "Manage customer encryption keys"
        )
        p_byom_manage = get_or_create_perm("byom.manage", "Manage custom AI models")

        # Helper to set role permissions (replace)
        def set_role_perms(role: Role, perm_names: list[str]):
            db.query(RolePermission).filter(RolePermission.role_id == role.id).delete()
            for n in perm_names:
                pid = existing_perms[n].id
                db.add(RolePermission(role_id=role.id, permission_id=pid))
            db.commit()

        # Assign defaults
        set_role_perms(admin_role, list(existing_perms.keys()))  # admin gets all
        set_role_perms(
            operator_role,
            [
                "cameras.view",
                "cameras.manage",
                "live.view",
                "recordings.view",
                "settings.view",
                "network.view",
                "onvif.discover",
                "alerts.view",
                "ai.view",
            ],
        )
        set_role_perms(
            viewer_role,
            [
                "cameras.view",
                "live.view",
                "recordings.view",
            ],
        )

        # Create admin user if not exists.
        #
        # V-001 (Zenodo 17261761 §3.1 / ETSI EN 303 645 unique-credential):
        # there is no shipped default password. Two operator-friendly paths
        # are supported:
        #
        #   (a) **Interactive bootstrap (default).** Leave DEFAULT_ADMIN_PASSWORD
        #       unset; the account is created with ``password_set=False`` and
        #       must be activated via the /auth/first-time-setup flow. That
        #       flow is itself gated by a one-time setup token printed below
        #       (C-1 followup) so an attacker on the management network cannot
        #       race the operator to claim the account.
        #   (b) **Provisioned bootstrap.** Set DEFAULT_ADMIN_PASSWORD explicitly
        #       (e.g. from your secrets manager during automated deployment).
        #       The account is created with ``password_set=True`` and the
        #       operator's password is honored on the first login. Without
        #       this branch the provisioned password is silently unusable —
        #       see review finding C-2.
        import secrets as _secrets
        admin_user = (
            db.query(User)
            .filter(User.username == settings.default_admin_username)
            .first()
        )

        # ISSUE-27 defense-in-depth: reject any DEFAULT_ADMIN_PASSWORD
        # that matches a documented-default, a historical .env.example
        # leak, or a common weak value. The ISSUE-26 fix blanks the
        # value in the shipped template, but operators with existing
        # ``.env`` files (or copies from old tutorials, backups, or
        # AI-generated examples) can still arrive here with a known-
        # bad password. Honoring it would defeat the secure-by-default
        # setup-token flow at runtime, even though the template fix is
        # in place. So we treat the reject list as terminal: if matched,
        # we discard the supplied value, fall through to the
        # password_set=False path, and print a loud warning explaining
        # what happened so the operator can audit their .env.
        #
        # Extend this list as new bad defaults are discovered. Keep
        # values lowercase here and compare against ``.lower()`` of the
        # supplied password so common casing tricks (``Admin``,
        # ``ADMIN``) all hit the same entry.
        KNOWN_BAD_PASSWORDS = frozenset({
            "securepass123!",      # historical .env.example default (ISSUE-26)
            "admin",
            "password",
            "password123",
            "changeme",
            "letmein",
            "opennvr",
            "default",
            "12345",
            "123456",
            "qwerty",
        })

        if not admin_user:
            initial_password = settings.default_admin_password
            if initial_password and initial_password.lower() in KNOWN_BAD_PASSWORDS:
                # Loud, explicit refusal. Print to stderr so the operator
                # sees it in their `./start.sh up` output even if stdout
                # is captured by another tool.
                import sys as _sys
                _sys.stderr.write(
                    "\n"
                    "================================================================\n"
                    " ⚠  REFUSING DEFAULT_ADMIN_PASSWORD — known-bad value (ISSUE-27)\n"
                    "----------------------------------------------------------------\n"
                    " Your .env's DEFAULT_ADMIN_PASSWORD matches a documented\n"
                    " default or a common weak password. Honoring it would mean\n"
                    " every fresh deploy lands on the same globally-known\n"
                    " credential — the V-001 anti-pattern OpenNVR positions\n"
                    " itself against (Zenodo §3.1, ETSI EN 303 645).\n"
                    "\n"
                    " Falling back to the secure setup-token flow instead.\n"
                    "\n"
                    " Fix: edit .env, blank the DEFAULT_ADMIN_PASSWORD line:\n"
                    "     DEFAULT_ADMIN_PASSWORD=\n"
                    " The token below is your one-time bootstrap credential.\n"
                    "================================================================\n"
                    "\n"
                )
                # Fall through to the password_set=False path.
                initial_password = ""
            if initial_password:
                hashed = get_password_hash(initial_password)
                password_set = True
                bootstrap_msg = (
                    "Admin user created with the provisioned "
                    "DEFAULT_ADMIN_PASSWORD. The account is immediately "
                    "usable — please ensure this value came from a secrets "
                    "manager and not a checked-in file."
                )
            else:
                # No password provided (or rejected as known-bad) — store
                # a hash of a high-entropy random value so even reading
                # the source code does not let an attacker authenticate.
                # The account stays gated by password_set=False;
                # activation goes through the one-time-token-gated
                # first-time-setup flow.
                hashed = get_password_hash(_secrets.token_urlsafe(64))
                password_set = False
                bootstrap_msg = (
                    "Admin user created without a password. Complete "
                    "/auth/first-time-setup using the setup token printed "
                    "below to enable the account."
                )

            admin_user = User(
                username=settings.default_admin_username,
                email=settings.default_admin_email,
                hashed_password=hashed,
                first_name=settings.default_admin_first_name,
                last_name=settings.default_admin_last_name,
                is_active=True,
                is_superuser=True,
                password_set=password_set,
                mfa_enabled=True,  # MFA on by default (paper §4.1, §5)
                role_id=admin_role.id,
            )
            db.add(admin_user)
            db.commit()
            print(f"Created admin user: {settings.default_admin_username}")
            print(f"IMPORTANT: {bootstrap_msg}")
        else:
            # Ensure admin role and superuser flag
            admin_user.role_id = admin_role.id
            admin_user.is_superuser = True
            db.commit()

        print(f"Roles present: {', '.join(existing_roles.keys())}")
        print(f"Permissions present: {', '.join(sorted(existing_perms.keys()))}")

    except Exception as e:
        print(f"Error creating initial data: {e}")
        db.rollback()
    finally:
        db.close()


def main():
    """Main function to initialize database."""
    print("Initializing database...")

    # Create tables
    init_db()
    print("Database tables created successfully")

    # Create initial data
    create_initial_data()
    print("Database initialization completed!")


if __name__ == "__main__":
    main()
