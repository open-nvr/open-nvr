# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""One timestamp per plate read: ``events.observed_at`` (#451).

The failure these exist for: a plate read had no time of its own, so the
vehicle list dated it by the VISIT's start and the alarm inbox dated it
by when the app finished deciding. Two clocks for one read, drifting
apart by however long OCR and the bus took — and neither of them scrubs
to the frame the plate is legible in.

The rule pinned here: a row is dated by the LOOK the read won on, or not
at all. A wrong observed time is worse than none, because readers fall
back to started_at when it is NULL and that fallback is at least honest
about what it is.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(32))
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())

import core.database as cdb             # noqa: E402
import models                           # noqa: E402
import services.plate_enrichment as pe   # noqa: E402

T1 = 1_788_000_061.0                    # the first look
T2 = 1_788_000_064.0                    # the second, three seconds later


@pytest.fixture(autouse=True)
def _first_read_wins(monkeypatch):
    """These pin WHEN a read is dated, not how many looks it takes to
    win one — the consensus policy has its own file. One accepted read
    wins here so each dating rule can be asserted alone.

    The sweep registry is process-global (finished sweeps hold their row
    for the echo grace), so it starts clean too.
    """
    monkeypatch.setenv("OPENNVR_PLATE_MIN_AGREEING_READS", "1")
    with pe._sweeps_lock:
        pe._sweeping.clear()


@pytest.fixture(autouse=True)
def _clean_plate_sightings():
    """The dedup sightings map is process-global on purpose (that IS the
    feature), which makes it cross-test state by accident. These tests
    read the SAME plate repeatedly, so without this every test after the
    first would have its read folded as a duplicate sighting."""
    with pe._sightings_lock:
        pe._recent_sightings.clear()
    yield
    with pe._sightings_lock:
        pe._recent_sightings.clear()


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
    role = models.Role(name="admin")
    s.add(role)
    s.commit()
    user = models.User(username="u", email="u@x", hashed_password="x",
                       role_id=role.id)
    s.add(user)
    s.commit()
    cam = models.Camera(name="c", ip_address="10.0.0.5", owner_id=user.id)
    s.add(cam)
    s.commit()
    row = models.TimelineEvent(
        camera_id=cam.id, source="tier0", event_type="track", label="car",
        # Deliberately far from either look: a test that passed by
        # accidentally reading started_at would show up as a wrong value,
        # not as a right one.
        started_at=datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc),
    )
    s.add(row)
    s.commit()
    row_id = row.id
    s.close()
    yield SessionLocal, row_id


def _epoch(dt) -> float:
    """``observed_at`` as epoch seconds, tz-normalised.

    SQLite has no timezone type, so SQLAlchemy hands back a NAIVE
    datetime carrying the UTC wall time we wrote (``DateTime(timezone=
    True)`` is advisory there; Postgres keeps the offset). Calling
    .timestamp() on that would read it as LOCAL time and silently shift
    every assertion by the runner's UTC offset.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _row(SessionLocal, row_id):
    s = SessionLocal()
    try:
        r = s.get(models.TimelineEvent, row_id)
        return r.plate_text, r.observed_at
    finally:
        s.close()


class _ScriptedOcr:
    def __init__(self, reads):
        self.reads = list(reads)
        self.calls = []

    async def __call__(self, jpeg, camera_handle, event_id=None,
                       observed_at=None):
        self.calls.append((jpeg, camera_handle, event_id, observed_at))
        return self.reads.pop(0) if self.reads else None


_BOX = (10.0, 20.0, 110.0, 50.0)


def _accepted(plate="GOOD42", conf=0.9, box=_BOX):
    return {"plate": plate, "confidence": conf,
            "characters": [conf] * len(plate), "accepted": True,
            "floor": 0.45, "box": box}


def _rejected(plate, chars, floor=0.45, box=_BOX):
    return {"plate": plate, "confidence": min(chars), "characters": chars,
            "accepted": False, "floor": floor, "box": box}


# ── the sweep dates the row by the winning look ────────────────────


def test_sweep_dates_the_row_by_the_look_the_read_won_on(db, monkeypatch):
    SessionLocal, row_id = db
    # The FIRST look is rejected, so the read is won on the second — and
    # the row must carry the second look's time, not the first's.
    ocr = _ScriptedOcr([None, _accepted("H644LX")])
    monkeypatch.setattr(pe, "_ocr_jpeg", ocr)
    asyncio.run(pe.enrich_event_plate(row_id, [b"1", b"2"], [T1, T2]))
    plate, observed = _row(SessionLocal, row_id)
    assert plate == "H644LX"
    assert observed is not None, "a read with a dated look must be dated"
    assert _epoch(observed) == T2


def test_the_capture_time_travels_to_kai_c_with_the_look(db, monkeypatch):
    """It rides the infer payload so KAI-C can echo it into
    plate.recognized.v1 — the only way an app consuming the bus (and the
    alarm it raises) can show the same moment this row will."""
    SessionLocal, row_id = db
    ocr = _ScriptedOcr([_accepted("H644LX")])
    monkeypatch.setattr(pe, "_ocr_jpeg", ocr)
    asyncio.run(pe.enrich_event_plate(row_id, [b"1"], [T1]))
    assert ocr.calls[0][3] == T1


def test_a_producer_without_stamps_leaves_the_row_undated(db, monkeypatch):
    """NULL, never a guess: readers fall back to started_at, which is at
    least honest about being the visit's start."""
    SessionLocal, row_id = db
    ocr = _ScriptedOcr([_accepted("H644LX")])
    monkeypatch.setattr(pe, "_ocr_jpeg", ocr)
    asyncio.run(pe.enrich_event_plate(row_id, [b"1", b"2"]))
    plate, observed = _row(SessionLocal, row_id)
    assert plate == "H644LX" and observed is None


def test_stamps_that_do_not_line_up_with_the_looks_are_ignored(db, monkeypatch):
    """A short list would date every read past the gap by a different
    look's clock. Drop the lot instead."""
    SessionLocal, row_id = db
    ocr = _ScriptedOcr([None, _accepted("H644LX")])
    monkeypatch.setattr(pe, "_ocr_jpeg", ocr)
    asyncio.run(pe.enrich_event_plate(row_id, [b"1", b"2"], [T1]))
    plate, observed = _row(SessionLocal, row_id)
    assert plate == "H644LX" and observed is None


def test_a_merged_read_is_dated_by_the_contributor_it_keeps(db, monkeypatch):
    """A merged plate is whole in neither look, so it is dated by the
    same contributor whose crop and box it keeps — the more confident
    one, here the first."""
    SessionLocal, row_id = db
    a = _rejected("H644LX", [0.9, 0.9, 0.9, 0.9, 0.9, 0.60])
    b = _rejected("H644LK", [0.5, 0.5, 0.5, 0.5, 0.5, 0.20])
    ocr = _ScriptedOcr([a, b])
    monkeypatch.setattr(pe, "_ocr_jpeg", ocr)
    asyncio.run(pe.enrich_event_plate(row_id, [b"1", b"2"], [T1, T2]))
    plate, observed = _row(SessionLocal, row_id)
    assert plate == "H644LX"
    assert _epoch(observed) == T1


def _with_evidence_file(SessionLocal, row_id, tmp_path, monkeypatch):
    """Give the row a real evidence JPEG so the no-candidates branch
    actually reads it instead of bailing on a missing file."""
    from services import evidence_store

    root = tmp_path / "evidence"
    (root / "cam").mkdir(parents=True)
    (root / "cam" / "best.jpg").write_bytes(b"jpegbytes")
    monkeypatch.setattr(evidence_store, "evidence_root", lambda: root)
    s = SessionLocal()
    s.get(models.TimelineEvent, row_id).evidence_path = "cam/best.jpg"
    s.commit()
    s.close()


def test_the_evidence_frame_fallback_is_dated_by_the_frame_it_read(
        db, monkeypatch, tmp_path):
    """A camera without the LPR skill retains no candidate ring, so EVERY
    one of its reads takes this branch. It used to leave observed_at null,
    which made the whole feature inert on a default install: the vehicle
    list fell back to the visit start and disagreed with the alarm again.

    The crop is a look with a time of its own — the tracker stamps the
    frame it cut it from, and Tier-0 ships that as evidence_ts.
    """
    SessionLocal, row_id = db
    _with_evidence_file(SessionLocal, row_id, tmp_path, monkeypatch)
    ocr = _ScriptedOcr([_accepted("H644LX")])
    monkeypatch.setattr(pe, "_ocr_jpeg", ocr)
    asyncio.run(pe.enrich_event_plate(row_id, None, None, T1))
    plate, observed = _row(SessionLocal, row_id)
    assert plate == "H644LX"
    assert observed is not None, "the fallback read must carry its frame's time"
    assert _epoch(observed) == pytest.approx(T1)


def test_the_fallback_is_undated_when_the_producer_sends_no_frame_time(
        db, monkeypatch, tmp_path):
    """An older Tier-0 ships no evidence_ts. The read still lands; it just
    has no observed time, and readers fall back to started_at."""
    SessionLocal, row_id = db
    _with_evidence_file(SessionLocal, row_id, tmp_path, monkeypatch)
    ocr = _ScriptedOcr([_accepted("H644LX")])
    monkeypatch.setattr(pe, "_ocr_jpeg", ocr)
    asyncio.run(pe.enrich_event_plate(row_id, None, None, None))
    plate, observed = _row(SessionLocal, row_id)
    assert plate == "H644LX"
    assert observed is None


# ── a retracted read must not leave its time behind ────────────────


def test_a_retracted_read_takes_its_timestamp_with_it():
    """clear_plate turns a row back into an ordinary vehicle visit. A
    left-behind observed_at would keep overriding started_at, so the UI
    would date the visit by a read that no longer exists."""
    class _Row:
        plate_text = "H644LX"
        plate_evidence_path = "a.jpg"
        plate_frame_path = "b.jpg"
        observed_at = datetime.now(timezone.utc)
        payload = {"stationary": False, "plate_reads": 1,
                   "plate_source": "early"}

    row = _Row()
    pe.clear_plate(row)
    assert row.plate_text is None
    assert row.observed_at is None
    # The non-plate payload survives, as it always has.
    assert row.payload == {"stationary": False}


# ── the epoch -> datetime edge cases ───────────────────────────────


@pytest.mark.parametrize("junk", [None, "nope", True, float("nan"),
                                  float("inf"), 10 ** 30])
def test_junk_stamps_never_cost_us_the_read(junk):
    """A producer's bad clock value must degrade to "undated", never
    raise into a write path that was otherwise fine."""
    out = pe.observed_dt(junk)
    assert out is None or isinstance(out, datetime)


def test_observed_dt_round_trips_an_epoch():
    assert pe.observed_dt(T1).timestamp() == T1
