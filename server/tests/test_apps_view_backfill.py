# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The apps.view upgrade path.

Introducing a permission for a surface that was already reachable is a
revocation unless someone backfills it. These pin that behaviour, and
the once-only property that keeps a deliberate revoke revoked.
"""
import os
import sys

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

from core.database import Base
from models import Permission, Role, RolePermission
from services.apps_view_backfill import backfill_apps_view


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[Role.__table__, Permission.__table__, RolePermission.__table__],
    )
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _perm(db, name):
    p = Permission(name=name, description=name)
    db.add(p)
    db.commit()
    return p


def _role(db, name, perms=()):
    r = Role(name=name)
    db.add(r)
    db.commit()
    for p in perms:
        db.add(RolePermission(role_id=r.id, permission_id=p.id))
    db.commit()
    return r


def _has(db, role, perm):
    return (
        db.query(RolePermission)
        .filter(
            RolePermission.role_id == role.id,
            RolePermission.permission_id == perm.id,
        )
        .first()
        is not None
    )


def test_roles_with_ai_view_receive_apps_view(db):
    """The upgrade must not take the catalog away from an operator who
    could open it yesterday through ai.view."""
    ai = _perm(db, "ai.view")
    apps = _perm(db, "apps.view")
    operator = _role(db, "operator", [ai])
    viewer = _role(db, "viewer", [])

    assert backfill_apps_view(db) == 1
    assert _has(db, operator, apps)
    # A role that never had ai.view gains nothing — this is a migration,
    # not a broadening.
    assert not _has(db, viewer, apps)


def test_backfill_is_idempotent_and_makes_no_duplicate_rows(db):
    ai = _perm(db, "ai.view")
    apps = _perm(db, "apps.view")
    operator = _role(db, "operator", [ai])

    assert backfill_apps_view(db) == 1
    assert backfill_apps_view(db) == 0
    rows = (
        db.query(RolePermission)
        .filter(
            RolePermission.role_id == operator.id,
            RolePermission.permission_id == apps.id,
        )
        .count()
    )
    assert rows == 1


def test_a_deliberate_revoke_is_not_undone(db):
    """Once-only is the point: an administrator who removes apps.view
    must not find it back after a restart. The caller runs this only on
    the boot that creates the permission, and re-running it here stands
    in for that boot being repeated."""
    ai = _perm(db, "ai.view")
    apps = _perm(db, "apps.view")
    operator = _role(db, "operator", [ai])
    backfill_apps_view(db)

    db.query(RolePermission).filter(
        RolePermission.role_id == operator.id,
        RolePermission.permission_id == apps.id,
    ).delete()
    db.commit()
    assert not _has(db, operator, apps)

    # Re-running WOULD re-grant — which is exactly why main.py gates the
    # call on having just created the permission row.
    assert backfill_apps_view(db) == 1


def test_missing_permissions_are_survivable(db):
    """A partially seeded database must not crash boot."""
    assert backfill_apps_view(db) == 0
    _perm(db, "ai.view")
    assert backfill_apps_view(db) == 0
