# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The screening ledger: what core keeps, and what it reports.

The behaviour worth pinning down is the arithmetic. A compliance figure
is complete scans over ALL screenings, so the clean ones have to be
stored, a redelivered screening must not inflate the denominator, and a
day with nothing screened must not read as a perfect day.
"""
from __future__ import annotations

import os
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("INTERNAL_API_KEY", "site_" + secrets.token_hex(16))
os.environ.setdefault("SECRET_KEY", "test-secret-" + secrets.token_hex(16))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import models  # noqa: E402
from core.database import Base  # noqa: E402
from services.guardscan_event_consumer import (  # noqa: E402
    apply_screening_event,
    prune_screenings,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def envelope(session_id="s1", *, verdict="compliant", score=100.0,
             camera="cam1", ts=None, **payload):
    return {
        "schema": "guardscan.screening.v1",
        "camera_id": camera,
        "ts": ts if ts is not None else datetime.now(UTC).timestamp(),
        "producer": "guard-scan-compliance",
        "payload": {
            "session": session_id,
            "verdict": verdict,
            "score": score,
            "coverage": score,
            "steps_done": ["Left arm", "Right arm", "Front", "Back"],
            "steps_missing": [],
            "flagged": False,
            "duration_s": 42.0,
            "engaged_s": 30.0,
            "ended_by": "left",
            **payload,
        },
    }


def rows(db):
    return db.query(models.GuardScreening).order_by(
        models.GuardScreening.id).all()


# ── what gets kept ───────────────────────────────────────────────────


def test_a_clean_screening_is_recorded_too():
    """Not only the failures. Without the clean ones there is no
    denominator, and 'compliance' collapses into a complaint count."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        assert apply_screening_event(envelope(), db=session) == "applied"
        stored = rows(session)
        assert len(stored) == 1
        assert stored[0].verdict == "compliant"
        assert stored[0].camera_id == 1
    finally:
        session.close()


def test_a_redelivered_screening_does_not_count_twice(db):
    """The bus is at-least-once. A second delivery of the same screening
    would otherwise add a row and shift the day's percentage."""
    assert apply_screening_event(envelope("same"), db=db) == "applied"
    assert apply_screening_event(envelope("same"), db=db) == "duplicate"
    assert len(rows(db)) == 1


def test_a_screening_with_no_session_id_is_dropped(db):
    bad = envelope()
    bad["payload"].pop("session")
    assert apply_screening_event(bad, db=db) == "malformed"
    assert apply_screening_event({"payload": None}, db=db) == "malformed"
    assert apply_screening_event("not a dict", db=db) == "malformed"


def test_the_start_time_is_derived_so_durations_and_clocks_agree(db):
    apply_screening_event(envelope("timed", duration_s=60.0), db=db)
    row = rows(db)[0]
    assert row.started_at is not None
    assert abs((row.ended_at - row.started_at).total_seconds() - 60.0) < 1.0


def test_an_unknown_verdict_is_kept_not_dropped(db):
    """A future app version with a new grade band must not make its
    screenings vanish from the denominator."""
    apply_screening_event(envelope("new", verdict="hurried"), db=db)
    assert rows(db)[0].verdict == "hurried"


def test_a_camera_handle_core_does_not_know_still_records(db):
    apply_screening_event(envelope("odd", camera="lobby-door"), db=db)
    assert rows(db)[0].camera_id is None


# ── retention ────────────────────────────────────────────────────────


def test_old_screenings_are_pruned(db, tmp_path, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "recordings_base_path", str(tmp_path))
    keypoints = tmp_path / ".guardscan"
    keypoints.mkdir()
    (keypoints / "old.json").write_text("{}", encoding="utf-8")

    old = (datetime.now(UTC) - timedelta(days=200)).timestamp()
    apply_screening_event(envelope("old", ts=old), db=db)
    apply_screening_event(envelope("new"), db=db)

    assert prune_screenings(db) == 1
    assert [r.session_id for r in rows(db)] == ["new"]
    # The keypoint blob goes with it: it is not in the evidence store,
    # whose sweep only deletes JPEGs, so nothing else would ever remove it.
    assert not (keypoints / "old.json").exists()


# ── the arithmetic the page shows ────────────────────────────────────


def test_compliance_is_complete_scans_over_all_screenings():
    from routers.guardscan import _count, _finish, _tally

    tally = _tally()
    for verdict in ("compliant", "compliant", "compliant", "incomplete"):
        row = models.GuardScreening(session_id=verdict, verdict=verdict,
                                    score=100.0 if verdict == "compliant" else 25.0,
                                    ended_at=datetime.now(UTC), flagged=False)
        _count(tally, row)
    out = _finish(tally)
    assert out["screenings"] == 4
    assert out["compliance"] == 75.0
    assert out["mean_score"] == 81.2


def test_a_period_with_nothing_screened_has_no_compliance_figure():
    """Not 100%. Nobody walked past the camera, and a green tile over
    that is a lie an operator would act on."""
    from routers.guardscan import _finish, _tally

    out = _finish(_tally())
    assert out["screenings"] == 0
    assert out["compliance"] is None
    assert out["mean_score"] is None


def test_buckets_are_day_week_and_month():
    from routers.guardscan import _bucket

    when = datetime(2026, 9, 13, 15, 30, tzinfo=UTC)
    assert _bucket(when, "day")[0] == "2026-09-13"
    assert _bucket(when, "week")[0] == "2026-W37"
    assert _bucket(when, "month")[0] == "2026-09"


def test_a_bucket_carries_a_label_for_a_tooltip_and_one_for_an_axis():
    """A chart axis gives a bucket about forty pixels; a tooltip has the
    whole line. "September 2026" is an ellipsis in the first and the
    right answer in the second, so the server sends both — the client
    used to clip to two words, which left "Week 37," with the comma
    still attached."""
    from routers.guardscan import _bucket

    when = datetime(2026, 9, 13, 15, 30, tzinfo=UTC)
    assert _bucket(when, "day")[1:] == ("Sun 13 Sep 2026", "Sun 13")
    assert _bucket(when, "week")[1:] == ("Week 37, 2026", "W37")
    assert _bucket(when, "month")[1:] == ("September 2026", "Sep")

    # Short forms have to survive an axis: no ellipsis, no stray comma.
    for period in ("day", "week", "month"):
        short = _bucket(when, period)[2]
        assert len(short) <= 6 and not short.endswith(",")
