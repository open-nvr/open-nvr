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
from services.camera_scope import visible_camera_ids  # noqa: E402

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
    stats = plate_stats(s, days=7, scope=None, now=NOW)
    assert stats["total_reads"] == 4            # OLD999 + plateless excluded
    assert stats["unique_plates"] == 3
    per_cam = {e["camera_id"]: e["reads"] for e in stats["per_camera"]}
    assert per_cam == {cams["gate"].id: 2, cams["yard"].id: 1,
                       cams["lot"].id: 1}
    assert sum(d["reads"] for d in stats["per_day"]) == 4


def test_owner_scoping(db):
    s, users, cams = db
    stats = plate_stats(s, days=7, scope=visible_camera_ids(s, users["alice"]), now=NOW)
    assert stats["total_reads"] == 3            # bob's lot excluded
    assert all(e["camera_id"] != cams["lot"].id for e in stats["per_camera"])


def test_window_days(db):
    s, users, _ = db
    wide = plate_stats(s, days=90, scope=None, now=NOW)
    assert wide["total_reads"] == 5             # OLD999 now inside


# ── plate_summary (the per-plate history drill-down) ────────────────


def test_plate_summary_all_time_counts_and_range(db):
    from services.timeline_service import plate_summary

    s, users, cams = db
    got = plate_summary(s, plate="aaa 111", scope=visible_camera_ids(s, users["alice"]))
    assert got["plate"] == "AAA111"
    assert got["total_reads"] == 2
    assert got["per_camera"] == [{"camera_id": cams["gate"].id, "reads": 2}]
    # first/last seen span the two visits (2h ago .. 1h ago)
    assert got["first_seen"] < got["last_seen"]
    # ALL-time: the 30-day-old read of another plate is still visible
    old = plate_summary(s, plate="OLD999", scope=visible_camera_ids(s, users["alice"]))
    assert old["total_reads"] == 1


def test_plate_summary_owner_scoped_and_unknown_plate(db):
    from services.timeline_service import plate_summary

    s, users, _cams = db
    # bob's camera read is invisible to alice…
    assert plate_summary(s, plate="CCC333",
                         scope=visible_camera_ids(s, users["alice"]))["total_reads"] == 0
    # …and visible fleet-wide (scope=None = superuser)
    assert plate_summary(s, plate="CCC333", scope=None)["total_reads"] == 1
    # A plate never seen: zeroes, not an error.
    none = plate_summary(s, plate="ZZ00XX", scope=None)
    assert none == {"plate": "ZZ00XX", "total_reads": 0,
                    "first_seen": None, "last_seen": None, "per_camera": []}


# ── plate_sessions + gate_occupancy (gate in / gate out) ────────────


def test_plate_sessions_pairs_in_and_out(db):
    from services.timeline_service import plate_sessions

    s, users, cams = db
    # Existing fixture: AAA111 read on gate 2h ago and 1h ago (both IN).
    # Add an OUT read on yard 30 min ago → the 1h-ago entry closes.
    s.add(models.TimelineEvent(
        camera_id=cams["yard"].id, source="tier0", event_type="track",
        label="car", plate_text="AAA111",
        started_at=NOW - timedelta(minutes=30)))
    s.commit()
    got = plate_sessions(
        s, plate="AAA111",
        in_cameras=[cams["gate"].id], out_cameras=[cams["yard"].id],
        scope=visible_camera_ids(s, users["alice"]))
    assert got["inside_now"] is False
    assert len(got["sessions"]) == 2  # newest first
    closed = got["sessions"][0]
    assert closed["entry_camera_id"] == cams["gate"].id
    assert closed["exit_camera_id"] == cams["yard"].id
    assert closed["duration_seconds"] == 30 * 60
    # The older entry (2h ago) had no exit before the next entry →
    # closed with a missed exit.
    missed = got["sessions"][1]
    assert missed["exited_at"] is None


def test_plate_sessions_open_session_means_inside(db):
    from services.timeline_service import plate_sessions

    s, users, cams = db
    got = plate_sessions(
        s, plate="AAA111",
        in_cameras=[cams["gate"].id], out_cameras=[cams["yard"].id],
        scope=visible_camera_ids(s, users["alice"]))
    # No OUT reads in the base fixture: latest entry is still open.
    assert got["inside_now"] is True
    assert got["sessions"][0]["exited_at"] is None


def test_gate_occupancy_counts_last_direction(db):
    from services.timeline_service import gate_occupancy

    s, users, cams = db
    # AAA111 last gate read = IN (1h ago) → inside. Add BBB222: IN 3h
    # ago then OUT 1h ago → not inside.
    s.add(models.TimelineEvent(
        camera_id=cams["gate"].id, source="tier0", event_type="track",
        label="car", plate_text="BBB222",
        started_at=NOW - timedelta(hours=3)))
    s.add(models.TimelineEvent(
        camera_id=cams["yard"].id, source="tier0", event_type="track",
        label="car", plate_text="BBB222",
        started_at=NOW - timedelta(hours=1)))
    s.commit()
    got = gate_occupancy(
        s, in_cameras=[cams["gate"].id], out_cameras=[cams["yard"].id],
        hours=24, scope=visible_camera_ids(s, users["alice"]), now=NOW)
    assert got == {"inside": 1, "plates": ["AAA111"]}


def test_gate_occupancy_needs_both_directions(db):
    from services.timeline_service import gate_occupancy

    s, users, cams = db
    assert gate_occupancy(s, in_cameras=[cams["gate"].id], out_cameras=[],
                          scope=visible_camera_ids(s, users["alice"])) == {"inside": 0, "plates": []}


# ── vehicle_report (the printable monthly report) ───────────────────


def test_vehicle_report_month_window_and_rollups(db):
    from services.timeline_service import vehicle_report

    s, users, cams = db
    # Fixture reads are around NOW (2026-08-29): AAA111 ×2 on gate,
    # BBB222 on yard, CCC333 on bob's lot, OLD999 ~30 days back
    # (2026-07-30 — the PREVIOUS month), plus a plateless row.
    got = vehicle_report(s, year=2026, month=8, scope=None)
    assert got["total_reads"] == 4          # OLD999 + plateless excluded
    assert got["unique_plates"] == 3
    plates = {p["plate"]: p for p in got["per_plate"]}
    assert plates["AAA111"]["reads"] == 2
    assert plates["AAA111"]["per_camera"] == [
        {"camera_id": cams["gate"].id, "reads": 2}]
    assert plates["AAA111"]["first_seen"] < plates["AAA111"]["last_seen"]
    assert sum(d["reads"] for d in got["per_day"]) == 4
    # July catches the old read.
    july = vehicle_report(s, year=2026, month=7, scope=None)
    assert {p["plate"] for p in july["per_plate"]} == {"OLD999"}


def test_vehicle_report_owner_scoped(db):
    from services.timeline_service import vehicle_report

    s, users, _cams = db
    got = vehicle_report(s, year=2026, month=8, scope=visible_camera_ids(s, users["alice"]))
    assert {p["plate"] for p in got["per_plate"]} == {"AAA111", "BBB222"}
    assert got["total_reads"] == 3
