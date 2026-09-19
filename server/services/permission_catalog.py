# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Permissions added after the initial seed, and how existing installs get them.

One list, used by both seeding paths so they cannot drift:

* ``scripts/init_db.py`` seeds them on a fresh install;
* ``main.py`` seeds them on the boot that upgrades an existing install.

``backfill_from`` is the behaviour-preserving rule (see
``services.apps_view_backfill.backfill_permission``). When a new permission
starts gating something people could already do, the roles that could do it
yesterday get it on the upgrade boot. ``None`` means the permission guards
a NEW ability, so only admins get it (through ``full_access``).
"""

from __future__ import annotations

from typing import NamedTuple


class NewPermission(NamedTuple):
    name: str
    description: str
    #: Grant to every role holding this permission on the boot that creates it.
    backfill_from: str | None = None


#: Home Assistant integration / API tokens (HA-004).
NEW_PERMISSIONS_HA: tuple[NewPermission, ...] = (
    # Checked by camera_settings.py since long ago but never seeded, so only
    # superusers passed. Seeding it changes nothing; granting it would widen
    # access to camera hardware settings, which is a product decision.
    NewPermission("camera_device.write", "Change settings on the camera device itself"),
    # PTZ today requires camera OWNERSHIP (or superuser) on /cameras/{id}/ptz
    # and the "manage" tier on the IP-keyed ONVIF tools. ptz.control is an
    # ADDITIONAL requirement HA-107 layers on top of those checks, never a
    # replacement, so it can only narrow who may steer a camera. It follows
    # live.view so that adding it narrows nobody who can PTZ today.
    NewPermission("ptz.control", "Move PTZ cameras and use presets", backfill_from="live.view"),
    NewPermission("events.create", "Create and end manual events"),
    NewPermission("apps.actions", "Trigger app actions through the API"),
    NewPermission("recordings.pause", "Pause recording (only when the site allows it)"),
    NewPermission("api_tokens.manage", "Create and revoke API tokens"),
)


#: Earlier late additions, seeded the same way (kept here so every
#: upgrade-path permission lives in one list).
_EARLIER_LATE_PERMISSIONS: tuple[NewPermission, ...] = (
    NewPermission("apps.install", "Install/uninstall curated App Store apps"),
    # apps.view took the App Catalog off ai.view. Creating the row alone would
    # REVOKE the catalog from everyone who could open it yesterday.
    NewPermission("apps.view", "Browse the App Catalog and view installed apps",
                  backfill_from="ai.view"),
)

UPGRADE_PATH_PERMISSIONS: tuple[NewPermission, ...] = (
    *_EARLIER_LATE_PERMISSIONS,
    *NEW_PERMISSIONS_HA,
)


def seed_new_permissions(db) -> list[tuple[str, int, str | None]]:
    """Create any upgrade-path permission that is missing, with its grants.

    Returns ``(name, roles_granted, backfill_source)`` for each permission
    created on THIS call. The backfill runs only for a permission this call
    created: running it on every boot would resurrect a grant an
    administrator deliberately revoked.
    """
    from models import Permission
    from services.apps_view_backfill import backfill_permission

    created: list[tuple[str, int, str | None]] = []
    for perm in UPGRADE_PATH_PERMISSIONS:
        if db.query(Permission).filter(Permission.name == perm.name).first():
            continue
        # The permission and its behaviour-preserving grants land in ONE
        # transaction. Committed separately, a crash or DB error between the
        # two would leave the permission existing with no grants, and the
        # once-only rule would then never grant it: a silent revocation.
        try:
            db.add(Permission(name=perm.name, description=perm.description))
            db.flush()
            granted = (
                backfill_permission(db, perm.backfill_from, perm.name, commit=False)
                if perm.backfill_from else 0
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        created.append((perm.name, granted, perm.backfill_from))
    return created
