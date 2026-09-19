# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Permissions added for the Home Assistant integration (HA-004).

The rule: seeding a permission must preserve behaviour. Nobody gains or
loses an ability on upgrade. Pinned here:

* a fresh install seeds all six, and the role defaults give ``ptz.control``
  to the operator and viewer roles and nothing new beyond admin.
  ``ptz.control`` is an extra requirement on top of PTZ's existing ownership
  check, so granting it with live.view narrows nobody and widens nothing;
* an upgraded install gets ``ptz.control`` on exactly the roles that hold
  ``live.view``, and ``camera_device.write`` on no role;
* the backfill runs once: a grant an admin revokes afterwards stays revoked
  on the next boot;
* the older apps.install / apps.view rules still behave as before;
* a permission and its grants are created in one transaction.
"""

from __future__ import annotations

import os
import sys

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

from core.database import Base  # noqa: E402
from models import Permission, Role, RolePermission  # noqa: E402
from services.permission_catalog import (  # noqa: E402
    NEW_PERMISSIONS_HA,
    seed_new_permissions,
)

HA_NAMES = {p.name for p in NEW_PERMISSIONS_HA}


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


def _perms_of(db, role) -> set[str]:
    return {
        p.name
        for p in db.query(Permission)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .filter(RolePermission.role_id == role.id)
    }


def test_catalog_lists_the_six_ha_permissions():
    assert HA_NAMES == {
        "camera_device.write", "ptz.control", "events.create",
        "apps.actions", "recordings.pause", "api_tokens.manage",
    }
    # Only ptz.control gates something people could already do.
    assert {p.name: p.backfill_from for p in NEW_PERMISSIONS_HA
            if p.backfill_from} == {"ptz.control": "live.view"}


def _pre_ha_install(db):
    """A database as the previous release left it."""
    names = ("full_access", "cameras.view", "cameras.manage", "live.view",
             "recordings.view", "ai.view", "apps.install", "apps.view")
    p = {n: _perm(db, n) for n in names}
    roles = {
        "admin": _role(db, "admin", [p["full_access"]]),
        "operator": _role(db, "operator", [p["cameras.view"], p["cameras.manage"],
                                           p["live.view"], p["ai.view"],
                                           p["apps.view"]]),
        "viewer": _role(db, "viewer", [p["cameras.view"], p["live.view"],
                                       p["recordings.view"]]),
        # A custom role that cannot watch live video: must NOT gain PTZ.
        "archivist": _role(db, "archivist", [p["recordings.view"]]),
    }
    return roles


def test_upgrade_grants_ptz_to_exactly_the_live_view_roles(db):
    roles = _pre_ha_install(db)
    created = seed_new_permissions(db)

    assert {name for name, _g, _s in created} == HA_NAMES
    assert dict((n, g) for n, g, _s in created)["ptz.control"] == 2

    assert "ptz.control" in _perms_of(db, roles["operator"])
    assert "ptz.control" in _perms_of(db, roles["viewer"])
    assert "ptz.control" not in _perms_of(db, roles["archivist"])
    # admin reaches everything through full_access, not role rows.
    assert _perms_of(db, roles["admin"]) == {"full_access"}

    # No role gains any of the other new abilities.
    for role in roles.values():
        assert not (_perms_of(db, role) & (HA_NAMES - {"ptz.control"}))


def test_upgrade_backfill_is_once_only(db):
    roles = _pre_ha_install(db)
    seed_new_permissions(db)

    # An administrator deliberately takes PTZ away from viewers...
    ptz = db.query(Permission).filter(Permission.name == "ptz.control").one()
    db.query(RolePermission).filter(
        RolePermission.role_id == roles["viewer"].id,
        RolePermission.permission_id == ptz.id,
    ).delete()
    db.commit()

    # ...and the next boot must not hand it back.
    assert seed_new_permissions(db) == []
    assert "ptz.control" not in _perms_of(db, roles["viewer"])


def test_earlier_late_permissions_keep_their_rules(db):
    ai_view = _perm(db, "ai.view")
    catalog_user = _role(db, "catalog_user", [ai_view])
    created = dict((n, g) for n, g, _s in seed_new_permissions(db))
    assert created["apps.view"] == 1 and created["apps.install"] == 0
    assert _perms_of(db, catalog_user) == {"ai.view", "apps.view"}


def test_fresh_install_seeds_and_sets_role_defaults(monkeypatch):
    """create_initial_data on an empty database (admin pre-created so the
    bootstrap-token path is not exercised here)."""
    import core.database as cdb
    import models
    from core.config import settings
    from scripts import init_db

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    monkeypatch.setattr(cdb, "SessionLocal", Session)
    monkeypatch.setattr(init_db, "SessionLocal", Session)

    s = Session()
    admin_role = Role(name="admin", description="Administrator")  # reused by the seed
    s.add(admin_role)
    s.commit()
    s.add(models.User(username=settings.default_admin_username,
                      email="admin@example.invalid", hashed_password="x",
                      is_active=True, is_superuser=True, role_id=admin_role.id))
    s.commit()
    s.close()

    init_db.create_initial_data()

    s = Session()
    try:
        names = {p.name for p in s.query(Permission).all()}
        assert HA_NAMES <= names
        by_name = {r.name: r for r in s.query(Role).all()}
        admin = _perms_of(s, by_name["admin"])
        operator = _perms_of(s, by_name["operator"])
        viewer = _perms_of(s, by_name["viewer"])
        assert HA_NAMES <= admin
        assert "ptz.control" in operator and "ptz.control" in viewer
        assert not (operator & (HA_NAMES - {"ptz.control"}))
        assert not (viewer & (HA_NAMES - {"ptz.control"}))
        # A second startup on this database creates nothing new.
        assert seed_new_permissions(s) == []
    finally:
        s.close()


def test_a_failed_backfill_leaves_no_half_seeded_permission(db, monkeypatch):
    """The permission and its grants are one transaction: if the grant fails,
    the permission is not left behind (the next boot retries both), instead
    of existing with no grants forever."""
    import services.apps_view_backfill as bf

    _pre_ha_install(db)

    def _boom(*a, **k):
        raise RuntimeError("pool timeout")

    monkeypatch.setattr(bf, "backfill_permission", _boom)
    with pytest.raises(RuntimeError):
        seed_new_permissions(db)
    assert db.query(Permission).filter(Permission.name == "ptz.control").first() is None

    monkeypatch.undo()
    created = dict((n, g) for n, g, _s in seed_new_permissions(db))
    assert created["ptz.control"] == 2   # the retry grants it properly
