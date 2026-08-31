# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Occupancy history: the consumer's decision core and the read-side
rollups behind the Occupancy page's charts."""

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
os.environ.setdefault("DATABASE_URL", "sqlite:///./_occh_test.db")
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

import core.database as cdb  # noqa: E402
import models  # noqa: E402
import services.occupancy_event_consumer as occ  # noqa: E402
from routers.occupancy import occupancy_history  # noqa: E402

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def _envelope(count, *, camera="cam1", level="normal", ts=None, **overrides):
    env = {
        "id": "evt_0123456789ab",
        "schema": "occupancy.changed.v1",
        "correlation_id": None,
        "camera_id": camera,
        "ts": (ts or NOW).isoformat(),
        "producer": "app:occupancy-counting",
        "payload": {"count": count, "level": level,
                    "max_occupancy": 25, "min_occupancy": None},
    }
    env.update(overrides)
    return env


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setitem(sys.modules, "core.database", cdb)
    monkeypatch.setitem(sys.modules, "models", models)
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(eng)
    SessionLocal = sessionmaker(bind=eng)
    monkeypatch.setattr(cdb, "SessionLocal", SessionLocal)
    s = SessionLocal()
    role = models.Role(name="admin"); s.add(role); s.commit()
    users = {}
    for name in ("alice", "bob"):
        u = models.User(username=name, email=f"{name}@x", hashed_password="x",
                        role_id=role.id)
        s.add(u); s.commit()
        users[name] = u
    cams = {}
    for name, owner in (("hall", "alice"), ("lot", "bob")):
        c = models.Camera(name=name, ip_address="10.0.0.9",
                          owner_id=users[owner].id)
        s.add(c); s.commit()
        cams[name] = c
    yield s, users, cams
    s.close()


# ── The consumer's decision core ────────────────────────────────────


def test_apply_inserts_sample(db):
    s, _users, cams = db
    assert occ.apply_occupancy_event(
        _envelope(7, camera=f"cam{cams['hall'].id}", level="over")) == "applied"
    row = s.query(models.OccupancySample).one()
    assert (row.camera_id, row.count, row.level) == (cams["hall"].id, 7, "over")
    assert row.ts is not None


def test_apply_rejects_junk(db):
    assert occ.apply_occupancy_event("nope") == "malformed"
    assert occ.apply_occupancy_event({"payload": {}}) == "malformed"
    assert occ.apply_occupancy_event(_envelope(-1)) == "malformed"
    assert occ.apply_occupancy_event(_envelope(True)) == "malformed"
    assert occ.apply_occupancy_event(_envelope(3, camera="lobby")) == "bad-camera"
    s, _u, _c = db
    assert s.query(models.OccupancySample).count() == 0


def test_retention_prunes_old_rows(db, monkeypatch):
    s, _users, cams = db
    old = NOW - timedelta(days=120)
    s.add(models.OccupancySample(camera_id=cams["hall"].id, count=1,
                                 level="normal", ts=old))
    s.commit()
    monkeypatch.setattr(occ, "_PRUNE_EVERY", 1)
    monkeypatch.setattr(occ, "_applies_since_prune", 0)
    assert occ.apply_occupancy_event(
        _envelope(2, camera=f"cam{cams['hall'].id}")) == "applied"
    counts = [r.count for r in s.query(models.OccupancySample).all()]
    assert counts == [2]  # the 120-day-old row was pruned


# ── The read-side rollups ───────────────────────────────────────────


def _sample(s, cam, count, *, hours_ago=1.0, level="normal"):
    s.add(models.OccupancySample(
        camera_id=cam.id, count=count, level=level,
        ts=NOW - timedelta(hours=hours_ago)))
    s.commit()


def test_history_buckets_and_owner_scope(db):
    s, users, cams = db
    _sample(s, cams["hall"], 4, hours_ago=1.2)
    _sample(s, cams["hall"], 8, hours_ago=1.1)   # same hour bucket
    _sample(s, cams["hall"], 2, hours_ago=5.0)
    _sample(s, cams["lot"], 30, hours_ago=1.0)   # bob's camera
    _sample(s, cams["hall"], 9, hours_ago=30.0)  # outside 24h window

    got = occupancy_history(s, hours=48, owner_id=users["alice"].id, now=NOW)
    assert got["bucket_minutes"] == 60
    assert [c["camera_id"] for c in got["cameras"]] == [cams["hall"].id]
    samples = got["cameras"][0]["samples"]
    # 3 buckets in the 48h window: 30h ago, 5h ago, ~1h ago.
    assert len(samples) == 3
    merged = samples[-1]
    assert merged["avg"] == 6.0 and merged["max"] == 8
    # Busiest hours cover 7d and only alice's cameras.
    hours = {b["hour"]: b["avg"] for b in got["busiest_hours"]}
    assert 30 not in [a for a in hours.values()]  # bob's 30 excluded


def test_history_fleet_view_and_fine_buckets(db):
    s, _users, cams = db
    _sample(s, cams["lot"], 30, hours_ago=0.1)
    got = occupancy_history(s, hours=24, owner_id=None, now=NOW)
    assert got["bucket_minutes"] == 15
    assert [c["camera_id"] for c in got["cameras"]] == [cams["lot"].id]
