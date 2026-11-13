# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Vehicles per camera per hour, by type and colour, owner-scoped."""
from __future__ import annotations

import os
import secrets
import sys
import types as _types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_vcounts_test.db")
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
from services.camera_scope import visible_camera_ids  # noqa: E402
from services.timeline_service import vehicle_counts  # noqa: E402

NOW = datetime(2026, 10, 23, 12, 30, tzinfo=UTC)


@pytest.fixture()
def db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    role = models.Role(name="admin"); s.add(role); s.commit()
    users = {}
    for name in ("alice", "bob"):
        u = models.User(username=name, email=f"{name}@x", hashed_password="x", role_id=role.id)
        s.add(u); s.commit(); users[name] = u
    cams = {}
    for name, owner in (("gate", "alice"), ("yard", "alice"), ("lot", "bob")):
        c = models.Camera(name=name, ip_address="10.0.0.9", owner_id=users[owner].id)
        s.add(c); s.commit(); cams[name] = c

    def visit(cam, label, *, age_minutes=30.0, vtype=None, colour=None, event_type="track"):
        ev = models.TimelineEvent(camera_id=cams[cam].id, source="tier0", event_type=event_type,
                                  label=label, started_at=NOW - timedelta(minutes=age_minutes))
        s.add(ev); s.commit()
        for kind, value in (("vehicle_type", vtype), ("colour", colour)):
            if value:
                s.add(models.VisitDescriptor(event_id=ev.id, kind=kind, value=value, confidence=0.9))
        s.commit()
        return ev

    visit("gate", "car", age_minutes=10, vtype="van", colour="white")     # 12:20 bucket 12:00
    visit("gate", "car", age_minutes=20, colour="red")                    # type falls back to label
    visit("gate", "truck", age_minutes=90)                                # 11:00 bucket, no claims
    visit("yard", "motorcycle", age_minutes=5, colour="black")
    visit("lot", "car", age_minutes=5, colour="blue")                     # bob's camera
    visit("gate", "car", age_minutes=60 * 30)                             # outside a 24 h window
    visit("gate", "person", age_minutes=5)                                # not a vehicle
    visit("gate", "car", age_minutes=5, event_type="alert")               # not a track
    yield s, users, cams
    s.close()


def test_counts_by_type_and_colour_with_fallbacks(db):
    s, users, cams = db
    r = vehicle_counts(s, hours=24, scope=None, now=NOW)
    assert r["total"] == 5
    gate = next(c for c in r["per_camera"] if c["camera_id"] == cams["gate"].id)
    assert gate["total"] == 3
    assert gate["by_type"] == {"van": 1, "car": 1, "truck": 1}
    assert gate["by_colour"] == {"white": 1, "red": 1, "unknown": 1}


def test_hour_buckets_split_the_gate_camera(db):
    s, users, cams = db
    r = vehicle_counts(s, hours=24, bucket="hour", scope=None, now=NOW)
    gate_buckets = [b for b in r["buckets"] if b["camera_id"] == cams["gate"].id]
    assert [b["bucket"][11:16] for b in gate_buckets] == ["11:00", "12:00"]
    assert [b["total"] for b in gate_buckets] == [1, 2]


def test_day_bucket_and_window(db):
    s, users, cams = db
    r = vehicle_counts(s, hours=24, bucket="day", scope=None, now=NOW)
    assert all(b["bucket"].endswith("T00:00:00+00:00") for b in r["buckets"])
    wide = vehicle_counts(s, hours=24 * 7, scope=None, now=NOW)
    assert wide["total"] == 6                      # the 30-hour-old car joins


def test_owner_scoping_and_camera_filter(db):
    s, users, cams = db
    bob = vehicle_counts(s, hours=24, scope=visible_camera_ids(s, users["bob"]), now=NOW)
    assert [c["camera_id"] for c in bob["per_camera"]] == [cams["lot"].id]
    only_yard = vehicle_counts(s, hours=24, scope=None, camera_ids={cams["yard"].id}, now=NOW)
    assert only_yard["total"] == 1 and only_yard["per_camera"][0]["by_type"] == {"motorcycle": 1}


def test_empty_window_shape(db):
    s, users, cams = db
    r = vehicle_counts(s, hours=24, scope=set(), now=NOW)
    assert r == {"hours": 24, "bucket": "hour", "total": 0, "per_camera": [], "buckets": []}
