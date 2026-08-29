# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Vehicles-page aggregates: plate reads over a window, owner-scoped."""

from __future__ import annotations

import os
import secrets
import sys
import types as _types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_pstats_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

_lm = _types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules.setdefault("core.logging_config", _lm)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import models  # noqa: E402
from services.timeline_service import plate_stats  # noqa: E402

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    SessionLocal = sessionmaker(bind=eng)
    s = SessionLocal()
    role = models.Role(name="admin"); s.add(role); s.commit()
    users = {}
    for name in ("alice", "bob"):
        u = models.User(username=name, email=f"{name}@x", hashed_password="x",
                        role_id=role.id)
        s.add(u); s.commit()
        users[name] = u
    cams = {}
    for name, owner in (("gate", "alice"), ("yard", "alice"), ("lot", "bob")):
        c = models.Camera(name=name, ip_address="10.0.0.9",
                          owner_id=users[owner].id)
        s.add(c); s.commit()
        cams[name] = c
    def visit(cam, plate, *, age_hours=1.0):
        s.add(models.TimelineEvent(
            camera_id=cams[cam].id, source="tier0", event_type="track",
            label="car", plate_text=plate,
            started_at=NOW - timedelta(hours=age_hours)))
        s.commit()
    visit("gate", "AAA111")
    visit("gate", "AAA111", age_hours=2)
    visit("yard", "BBB222", age_hours=5)
    visit("lot", "CCC333")                      # bob's camera
    visit("gate", "OLD999", age_hours=24 * 30)  # outside any window
    # A plateless visit must never count.
    s.add(models.TimelineEvent(camera_id=cams["gate"].id, source="tier0",
                               event_type="track", label="person",
                               started_at=NOW - timedelta(hours=1)))
    s.commit()
    yield s, users, cams
    s.close()


def test_counts_window_and_uniques(db):
    s, users, cams = db
    stats = plate_stats(s, days=7, owner_id=None, now=NOW)
    assert stats["total_reads"] == 4            # OLD999 + plateless excluded
    assert stats["unique_plates"] == 3
    per_cam = {e["camera_id"]: e["reads"] for e in stats["per_camera"]}
    assert per_cam == {cams["gate"].id: 2, cams["yard"].id: 1,
                       cams["lot"].id: 1}
    assert sum(d["reads"] for d in stats["per_day"]) == 4


def test_owner_scoping(db):
    s, users, cams = db
    stats = plate_stats(s, days=7, owner_id=users["alice"].id, now=NOW)
    assert stats["total_reads"] == 3            # bob's lot excluded
    assert all(e["camera_id"] != cams["lot"].id for e in stats["per_camera"])


def test_window_days(db):
    s, users, _ = db
    wide = plate_stats(s, days=90, owner_id=None, now=NOW)
    assert wide["total_reads"] == 5             # OLD999 now inside
