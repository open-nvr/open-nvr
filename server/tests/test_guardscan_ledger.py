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
        "schema": "screening.completed.v1",
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


def test_old_screenings_are_pruned(db):
    old = (datetime.now(UTC) - timedelta(days=200)).timestamp()
    apply_screening_event(envelope("old", ts=old), db=db)
    apply_screening_event(envelope("new"), db=db)

    assert prune_screenings(db) == 1
    assert [r.session_id for r in rows(db)] == ["new"]


def test_pruning_the_ledger_does_not_pretend_to_touch_the_keypoint_logs(
    db, tmp_path, monkeypatch
):
    """Core prunes ROWS. The per-frame keypoint logs are the app's, on
    the app container's own volume, and core cannot reach them.

    This test used to assert the opposite — and passed, because it
    created `<recordings>/.guardscan/old.json` itself. Nothing has ever
    written that path: the app writes to its own `session_log_dir`
    (/data/sessions). So the sweep deleted nothing in production while
    this test, the consumer's docstring and the compose comment all said
    the growth was handled. The app prunes its own directory now; the
    assertion here is that core no longer claims to.
    """
    from core.config import settings

    monkeypatch.setattr(settings, "recordings_base_path", str(tmp_path))
    stray = tmp_path / ".guardscan"
    stray.mkdir()
    (stray / "old.json").write_text("{}", encoding="utf-8")

    old = (datetime.now(UTC) - timedelta(days=200)).timestamp()
    apply_screening_event(envelope("old", ts=old), db=db)
    assert prune_screenings(db) == 1

    assert (stray / "old.json").exists(), (
        "core reached into a path it does not own — the app's logs are "
        "on the app's volume, and a sweep here can only ever be a no-op "
        "that looks like a fix")


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


# ── the report's two new aggregates ──────────────────────────────────


class _Super:
    """A superuser, which `visible_camera_ids` answers None for — i.e.
    every camera. Enough to drive the report without building an auth
    fixture for arithmetic that has nothing to do with auth."""

    id = 1
    is_superuser = True


def _report(db, **kw):
    import asyncio

    from routers.guardscan import compliance_report

    return asyncio.run(compliance_report(current_user=_Super(), db=db, **kw))


def _screened(db, *, at, verdict="compliant", missing=None, cam=3):
    """One screening in the ledger, at a given instant."""
    import json

    row = models.GuardScreening(
        session_id=secrets.token_hex(8),
        camera_id=cam,
        ended_at=at,
        verdict=verdict,
        score=100.0 if verdict == "compliant" else 50.0,
        flagged=False,
        steps_missing=json.dumps(missing or []),
    )
    db.add(row)
    db.commit()
    return row


def test_the_report_counts_which_surfaces_get_missed(db):
    """The rows have carried steps_missing since the ledger landed and
    nothing ever counted it — yet "the back is missed four times more
    than anything else" is a training instruction, where a compliance
    percentage is only a score."""
    now = datetime.now(UTC)
    for _ in range(3):
        _screened(db, at=now, verdict="partial", missing=["Back"])
    _screened(db, at=now, verdict="partial", missing=["Left arm"])
    _screened(db, at=now, verdict="incomplete", missing=["Back", "Front"])
    _screened(db, at=now)                       # complete: misses nothing

    missed = _report(db, days=7, period="day", camera_id=None,
                     tz_offset_minutes=0)["missed"]

    # Worst first — the report reads top-down.
    assert missed[0] == {"step": "Back", "count": 4}
    assert {m["step"]: m["count"] for m in missed} == {
        "Back": 4, "Left arm": 1, "Front": 1}


def test_missing_surfaces_survive_a_row_with_unreadable_json(db):
    """One malformed row must not take the whole report down."""
    now = datetime.now(UTC)
    _screened(db, at=now, verdict="partial", missing=["Back"])
    broken = _screened(db, at=now, verdict="partial")
    broken.steps_missing = "{not json"
    db.commit()

    missed = _report(db, days=7, period="day", camera_id=None,
                     tz_offset_minutes=0)["missed"]
    assert missed == [{"step": "Back", "count": 1}]


def test_the_hour_of_day_is_the_operators_hour_not_utc(db):
    """A compliance rate that collapses at closing time is a staffing
    fact, and "closing" is a local idea. Reporting 18:30 IST as 13:00
    would point a manager at the wrong shift."""
    # 13:00 UTC is 18:30 in Delhi (+05:30).
    _screened(db, at=datetime(2026, 9, 13, 13, 0, tzinfo=UTC))

    utc = _report(db, days=90, period="day", camera_id=None,
                  tz_offset_minutes=0)["hours"]
    assert [h["hour"] for h in utc] == [13]
    assert utc[0]["label"] == "13:00"

    delhi = _report(db, days=90, period="day", camera_id=None,
                    tz_offset_minutes=330)["hours"]
    assert [h["hour"] for h in delhi] == [18]
    assert delhi[0]["screenings"] == 1


def test_only_the_hours_that_saw_somebody_are_reported(db):
    """Twenty-four rows, twenty of them zero, hide the four that matter."""
    day = datetime(2026, 9, 13, tzinfo=UTC)
    _screened(db, at=day.replace(hour=9))
    _screened(db, at=day.replace(hour=9), verdict="partial", missing=["Back"])
    _screened(db, at=day.replace(hour=17))

    hours = _report(db, days=90, period="day", camera_id=None,
                    tz_offset_minutes=0)["hours"]
    assert [h["hour"] for h in hours] == [9, 17]          # chronological
    assert hours[0]["screenings"] == 2
    assert hours[0]["compliance"] == 50.0
    assert hours[1]["compliance"] == 100.0
