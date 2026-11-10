# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""A completed segment that holds no video raises recording_degraded.

Numbers are the real ones from camera 23 on the Ventuno Q (2026-11-09):
52,498,205-byte segments with a 0.0666 s duration every seven minutes, after
60 s / 7.5 MB segments all night. Run with:

    cd server && pytest tests/test_segment_health.py -v
"""

from __future__ import annotations

import os
import secrets
import sys
import types as _types
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
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

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from models import Base, CameraEvent  # noqa: E402
from services.segment_health import (  # noqa: E402
    EVENT_TYPE,
    classify_segment,
    note_segment,
)

MB = 1024 * 1024
BROKEN_BYTES = 52_498_205
BROKEN_DURATION = 0.066622223


# ── classify ────────────────────────────────────────────────────────────────

def test_camera_23s_frozen_segment_is_degenerate():
    assert classify_segment(BROKEN_DURATION, BROKEN_BYTES, 60) == "degenerate"


def test_a_normal_minute_is_normal():
    assert classify_segment(60.0, int(7.5 * MB), 60) == "normal"


def test_the_first_half_frozen_segment_is_caught_by_the_short_rule():
    # 5.9 s of real video, then the clamp: short for a 60 s setting and
    # 68 Mbit/s — heavier than any CCTV stream of that length.
    assert classify_segment(5.9325, int(50.8 * MB), 60) == "degenerate"


def test_a_stop_or_reconnect_tail_is_not_judged():
    # Short AND small: the recorder closed a file early, nothing is wrong.
    assert classify_segment(3.0, int(0.4 * MB), 60) is None


def test_a_heavy_4k_camera_with_long_segments_is_not_libelled():
    # 16 Mbit/s for 7 minutes = 840 MB: big, long, sane bitrate -> normal.
    assert classify_segment(420.0, 840 * MB, 420) == "normal"
    # Its 30 s partial at a camera reboot: 60 MB at 16 Mbit/s -> no verdict.
    assert classify_segment(30.0, 60 * MB, 420) is None


def test_missing_numbers_give_no_verdict():
    assert classify_segment(None, BROKEN_BYTES, 60) is None
    assert classify_segment(BROKEN_DURATION, None, 60) is None
    assert classify_segment(60.0, 8 * MB, 0) is None


# ── note_segment: one active event, cleared by the first healthy segment ───

class _Cam:
    id = 23
    name = "CTPL CP Plus"
    config = None


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine, tables=[CameraEvent.__table__])
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _events(db):
    return db.query(CameraEvent).order_by(CameraEvent.id).all()


def test_degenerate_segments_raise_one_active_event_then_recover(db):
    now = datetime(2026, 11, 9, 2, 3, 52, tzinfo=UTC)
    first = note_segment(db, _Cam(), BROKEN_DURATION, BROKEN_BYTES, now=now)
    assert first is not None
    assert first["event_type"] == EVENT_TYPE
    assert first["event_state"] == "active"
    assert first["camera_id"] == 23 and first["camera_name"] == "CTPL CP Plus"
    assert "50 MB" in first["description"] and "0.07 s" in first["description"]
    assert len(first["description"]) <= 200

    # Six more broken segments: the alarm is already active, nothing new.
    for _ in range(6):
        assert note_segment(db, _Cam(), BROKEN_DURATION, BROKEN_BYTES, now=now) is None
    assert [e.event_state for e in _events(db)] == ["active"]

    # The relay was restarted: a real 60 s segment closes the alarm.
    back = note_segment(db, _Cam(), 60.0, int(7.5 * MB), now=now)
    assert back is not None and back["event_state"] == "inactive"
    assert [e.event_state for e in _events(db)] == ["active", "inactive"]

    # Healthy segments afterwards are silent.
    assert note_segment(db, _Cam(), 60.0, int(7.5 * MB), now=now) is None


def test_healthy_segments_with_no_alarm_write_nothing(db):
    assert note_segment(db, _Cam(), 60.0, int(7.5 * MB)) is None
    assert note_segment(db, _Cam(), 2.0, int(0.2 * MB)) is None
    assert _events(db) == []


def test_degraded_cameras_is_the_set_whose_latest_event_is_active(db):
    """The list's badge and the agent's status read ask one question per
    page: whose segments arrive but hold no video right now."""
    from services.segment_health import degraded_cameras

    def ev(cam, state, when):
        db.add(CameraEvent(camera_id=cam, event_type=EVENT_TYPE, event_state=state,
                           description="x", occurred_at=when))
        db.commit()

    t0 = datetime(2026, 11, 9, 7, 0, tzinfo=UTC)
    ev(1, "active", t0)                      # camera 1: degraded, never recovered
    ev(2, "active", t0)                      # camera 2: degraded, then recovered
    ev(2, "inactive", t0.replace(hour=8))
    ev(3, "inactive", t0)                    # camera 3: only ever a recovery
    assert degraded_cameras(db, [1, 2, 3, 4]) == {1}
    assert degraded_cameras(db, []) == set()
    assert degraded_cameras(db, [4]) == set()
