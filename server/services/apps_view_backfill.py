# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Upgrade path for the ``apps.view`` permission.

The App Catalog used to hang off ``ai.view`` — it lived in the "AI &
Detections" nav group, so that was the permission gating it. It is now
its own top-level surface with its own ``apps.view`` permission, which
is the right shape (most apps are not AI, and browsing is much weaker
than installing).

But introducing a permission is a REVOCATION for everyone who could use
the surface yesterday: the day this ships, every non-admin who could
open the catalog through ``ai.view`` loses it. So on the boot that
creates ``apps.view``, hand it to whoever already holds ``ai.view``.
Nobody gains a capability they did not effectively have, and nobody
silently loses one.

Deliberately once-only, driven by the caller creating the permission
row: running it on every boot would resurrect a grant an administrator
had deliberately revoked.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

SOURCE_PERMISSION = "ai.view"
TARGET_PERMISSION = "apps.view"


def backfill_permission(
    db: Session, source_name: str, target_name: str, *, commit: bool = True
) -> int:
    """Grant *target_name* to every role holding *source_name*.

    The general form of the apps.view upgrade rule: when a new permission
    starts gating something people could already do, hand it to whoever
    held the permission that effectively allowed it, so the upgrade neither
    revokes nor widens anyone's access. Call it ONLY on the boot that creates
    the target permission (see the module docstring).

    Returns the number of roles granted. Safe to call when either
    permission is missing (returns 0) and when a role already has the
    grant (skipped, so no duplicate RolePermission rows).
    """
    from models import Permission, RolePermission

    source = db.query(Permission).filter(Permission.name == source_name).first()
    target = db.query(Permission).filter(Permission.name == target_name).first()
    if source is None or target is None:
        logger.warning(
            "%s backfill skipped: %r=%s %r=%s",
            target_name, source_name, source is not None,
            target_name, target is not None,
        )
        return 0

    role_ids = {
        rp.role_id
        for rp in db.query(RolePermission)
        .filter(RolePermission.permission_id == source.id)
        .all()
    }
    already = {
        rp.role_id
        for rp in db.query(RolePermission)
        .filter(RolePermission.permission_id == target.id)
        .all()
    }

    granted = 0
    for role_id in sorted(role_ids - already):
        db.add(RolePermission(role_id=role_id, permission_id=target.id))
        granted += 1
    if granted:
        # commit=False lets the caller make the permission and its grants
        # one transaction (see permission_catalog.seed_new_permissions).
        db.commit() if commit else db.flush()
    return granted


def backfill_apps_view(db: Session) -> int:
    """Grant ``apps.view`` to every role holding ``ai.view``."""
    return backfill_permission(db, SOURCE_PERMISSION, TARGET_PERMISSION)
