# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Clean plates, clean pictures: the consensus policy and the evidence
race.

What went wrong on the reporting install (a 640x360 clip of a blue
Lamborghini, plate R-197-GB):

* one accepted read was final — the early attempt fired at track-
  confirm (car smallest, farthest) and wrote R183JF at "conf=1.00";
  later, larger looks that read differently were dropped ("first
  writer wins"). Per-character probabilities saturate on blur, so the
  confidence floor filtered nothing;
* KAI-C republished every read core's own sweep made, and the bus
  consumer — which holds no image bytes — won the write race, leaving
  the row a number with no plate crop and no read frame. The UI then
  fell back to the vehicle-best frame, which on a merged track is a
  different car ("no full frame stored for this read").

The promises pinned here:

* a plate is written when the looks AGREE (edit distance ≤ 1); several
  looks that disagree write nothing; a visit with a single look still
  writes it (a lone honest read beats NULL) and says so;
* an early read is confirmed by an agreeing candidate, and overturned
  by several agreeing candidates that read otherwise;
* while a sweep owns a row the bus consumer defers to it, and a plate
  written meanwhile gets its evidence attached rather than dropped;
* a read the localiser did not find a plate for is not a read;
* a near-miss of a plate seen moments ago on the same camera is the
  same car.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
import types as _types
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_cons_test.db")
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
import services.plate_enrichment as pe  # noqa: E402
from services.plate_enrichment import (  # noqa: E402
    choose_consensus, extract_read, plate_distance,
)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.delenv("OPENNVR_PLATE_MIN_AGREEING_READS", raising=False)
    monkeypatch.delenv("OPENNVR_PLATE_DEDUP_DISTANCE", raising=False)
    with pe._sightings_lock:
        pe._recent_sightings.clear()
    with pe._sweeps_lock:
        pe._sweeping.clear()
    yield
    with pe._sightings_lock:
        pe._recent_sightings.clear()
    with pe._sweeps_lock:
        pe._sweeping.clear()


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
        started_at=datetime(2026, 9, 4, 10, 33, tzinfo=timezone.utc),
        evidence_path="ev/vehicle-best.jpg",
    )
    s.add(row)
    s.commit()
    row_id = row.id
    s.close()
    yield SessionLocal, row_id


@pytest.fixture()
def stored(monkeypatch):
    """Identity crop + captured evidence writes, keyed by fake path."""
    import services.evidence_store as es

    monkeypatch.setattr(
        pe, "crop_to_plate_box",
        lambda jpeg, box, **kw: jpeg if box else None,
    )
    seen: dict[str, bytes] = {}

    def fake_save(data: bytes) -> str:
        rel = f"xx/{data.decode()}.jpg"
        seen[rel] = data
        return rel

    monkeypatch.setattr(es, "save_evidence_jpeg", fake_save)
    return seen


def _row(SessionLocal, row_id):
    s = SessionLocal()
    try:
        r = s.get(models.TimelineEvent, row_id)
        s.expunge(r)
        return r
    finally:
        s.close()


class _ScriptedOcr:
    def __init__(self, reads):
        self.reads = list(reads)
        self.calls = []

    async def __call__(self, jpeg, camera_handle, event_id=None):
        self.calls.append((jpeg, camera_handle, event_id))
        return self.reads.pop(0) if self.reads else None


_BOX = (10.0, 20.0, 110.0, 50.0)


def _acc(plate, conf=0.9):
    return {"plate": plate, "confidence": conf,
            "characters": [conf] * len(plate), "accepted": True,
            "floor": 0.45, "box": _BOX}


def _rej(plate, chars):
    return {"plate": plate, "confidence": min(chars), "characters": chars,
            "accepted": False, "floor": 0.45, "box": _BOX}


def _sweep(row_id, reads, jpegs):
    ocr = _ScriptedOcr(reads)
    pe_ocr = pe._ocr_jpeg
    pe._ocr_jpeg = ocr
    try:
        asyncio.run(pe.enrich_event_plate(row_id, jpegs))
    finally:
        pe._ocr_jpeg = pe_ocr
    return ocr


# ── pure helpers ───────────────────────────────────────────────────


def test_plate_distance_is_bounded_levenshtein():
    assert plate_distance("R197GB", "R197GB") == 0
    assert plate_distance("R197GB", "R187GB") == 1
    assert plate_distance("R197GB", "R197G") == 1
    assert plate_distance("R197GB", "R183JF", cap=1) == 2     # cap + 1
    assert plate_distance("H644LX", "H644LK") == 1


def test_consensus_needs_agreement_when_there_were_looks_to_agree():
    a, b, c = _acc("R183JF", 1.0), _acc("L656XH", 1.0), _acc("L605HZ", 1.0)
    # three looks, three different hallucinations → nothing
    assert choose_consensus([a, b, c], min_agreeing=2, looks=3) == (None, 0)
    # two of three agree → that plate, with two votes
    read, n = choose_consensus([a, _acc("R197GB", 0.8), _acc("R197GB", 0.95)],
                               min_agreeing=2, looks=3)
    assert (read["plate"], n) == ("R197GB", 2)
    # the evidence is the most confident vote of the winning spelling
    assert read["confidence"] == 0.95


def test_consensus_clusters_near_misses_and_picks_the_common_spelling():
    votes = [_acc("H644LK", 0.7), _acc("H644LX", 0.9), _acc("H644LX", 0.6)]
    read, n = choose_consensus(votes, min_agreeing=2, looks=3)
    assert read["plate"] == "H644LX" and n == 3
    assert read["confidence"] == 0.9


def test_consensus_single_look_still_reads():
    """A visit with ONE look cannot agree with anything; its read is
    kept (marked as one look) rather than thrown away."""
    read, n = choose_consensus([_acc("R197GB")], min_agreeing=2, looks=1)
    assert read["plate"] == "R197GB" and n == 1
    assert choose_consensus([], min_agreeing=2, looks=0) == (None, 0)


def test_min_agreeing_is_operator_tunable(monkeypatch):
    assert pe.min_agreeing_reads() == 2
    monkeypatch.setenv("OPENNVR_PLATE_MIN_AGREEING_READS", "3")
    assert pe.min_agreeing_reads() == 3
    monkeypatch.setenv("OPENNVR_PLATE_MIN_AGREEING_READS", "0")
    assert pe.min_agreeing_reads() == 1
    monkeypatch.setenv("OPENNVR_PLATE_MIN_AGREEING_READS", "x")
    assert pe.min_agreeing_reads() == 2


# ── the sweep ──────────────────────────────────────────────────────


def test_sweep_writes_the_plate_the_looks_agree_on(db, stored):
    SessionLocal, row_id = db
    ocr = _sweep(row_id, [_acc("R183JF", 1.0), _acc("R197GB", 0.8),
                          _acc("R197GB", 0.95), _acc("NEVER1")],
                 [b"a", b"b", b"c", b"d"])
    r = _row(SessionLocal, row_id)
    assert r.plate_text == "R197GB"
    assert r.payload["plate_reads"] == 2
    assert r.payload["plate_source"] == "sweep"
    # evidence is the clearest agreeing look (c, conf 0.95), stored
    assert r.plate_evidence_path == "xx/c.jpg"
    assert r.plate_frame_path == "xx/c.jpg"
    # stops as soon as two agree: the 4th candidate is never OCR'd
    assert len(ocr.calls) == 3


def test_sweep_writes_nothing_when_the_looks_disagree(db, stored):
    SessionLocal, row_id = db
    _sweep(row_id, [_acc("R183JF", 1.0), _acc("L656XH", 1.0),
                    _acc("L605HZ", 1.0)], [b"a", b"b", b"c"])
    r = _row(SessionLocal, row_id)
    assert r.plate_text is None
    assert not stored


def test_sweep_single_look_writes_and_says_so(db, stored):
    SessionLocal, row_id = db
    _sweep(row_id, [_acc("R197GB")], [b"only"])
    r = _row(SessionLocal, row_id)
    assert r.plate_text == "R197GB"
    assert r.payload["plate_reads"] == 1
    assert r.plate_frame_path == "xx/only.jpg"


def test_sweep_two_looks_one_read_writes_nothing(db, stored):
    """Two looks, one read, one non-read: no agreement possible, no
    plate — the honest outcome (the read may well be a hallucination
    off the smaller look)."""
    SessionLocal, row_id = db
    _sweep(row_id, [_acc("R183JF", 1.0), None], [b"a", b"b"])
    assert _row(SessionLocal, row_id).plate_text is None


def test_merged_rejects_count_as_a_vote(db, stored):
    SessionLocal, row_id = db
    a = _rej("H644LX", [0.9, 0.9, 0.9, 0.9, 0.9, 0.60])
    b = _rej("H644LK", [0.95, 0.95, 0.95, 0.95, 0.95, 0.20])
    _sweep(row_id, [a, b, _acc("H644LX", 0.9)], [b"1", b"2", b"3"])
    r = _row(SessionLocal, row_id)
    assert r.plate_text == "H644LX"
    assert r.payload["plate_reads"] == 2


def _stamp_early(SessionLocal, row_id, plate, conf=1.0):
    s = SessionLocal()
    r = s.get(models.TimelineEvent, row_id)
    r.plate_text = plate
    pe.stamp_plate_evidence(r, "xx/early.jpg", frame_path="xx/early-frame.jpg",
                            reads=1, source="early", confidence=conf)
    s.commit()
    s.close()


def test_early_read_is_confirmed_by_one_agreeing_candidate(db, stored):
    SessionLocal, row_id = db
    _stamp_early(SessionLocal, row_id, "R197GB")
    ocr = _sweep(row_id, [_acc("R197GB", 0.8), _acc("X")], [b"a", b"b"])
    r = _row(SessionLocal, row_id)
    assert r.plate_text == "R197GB"
    assert r.payload["plate_reads"] == 2
    # keeps the early attempt's own images — nothing re-stored
    assert r.plate_frame_path == "xx/early-frame.jpg"
    assert not stored
    assert len(ocr.calls) == 1            # early + one agreeing = done


def test_early_read_is_overturned_by_two_agreeing_candidates(db, stored):
    """The install's actual failure: a track-confirm read of R183JF at
    conf=1.00, followed by larger looks that read R197GB twice."""
    SessionLocal, row_id = db
    _stamp_early(SessionLocal, row_id, "R183JF")
    _sweep(row_id, [_acc("R197GB", 0.8), _acc("R197GB", 0.9)], [b"a", b"b"])
    r = _row(SessionLocal, row_id)
    assert r.plate_text == "R197GB"
    assert r.payload["plate_reads"] == 2
    assert r.plate_frame_path == "xx/b.jpg"          # the clearer look
    assert r.plate_evidence_path == "xx/b.jpg"


def test_early_read_stands_against_a_lone_disagreeing_candidate(db, stored):
    SessionLocal, row_id = db
    _stamp_early(SessionLocal, row_id, "R183JF")
    _sweep(row_id, [_acc("R197GB", 0.8), None], [b"a", b"b"])
    r = _row(SessionLocal, row_id)
    # one look each way: nothing outranks anything; the row keeps its
    # (single) early read and no evidence is re-stored
    assert r.plate_text == "R183JF"
    assert not stored


def test_a_confirmed_consensus_is_never_reswept(db, stored):
    SessionLocal, row_id = db
    s = SessionLocal()
    r = s.get(models.TimelineEvent, row_id)
    r.plate_text = "R197GB"
    pe.stamp_plate_evidence(r, "xx/c.jpg", frame_path="xx/c.jpg",
                            reads=2, source="sweep")
    s.commit()
    s.close()
    ocr = _sweep(row_id, [_acc("ZZZ")], [b"a"])
    assert ocr.calls == []
    assert _row(SessionLocal, row_id).plate_text == "R197GB"


def test_overturned_read_that_matches_a_recent_sighting_is_retracted(db, stored):
    """The looks agree on a plate seen seconds ago on this camera — a
    fragment of the previous pass. The wrong single read comes OFF and
    the row stays an ordinary vehicle visit, not a second sighting."""
    SessionLocal, row_id = db
    pe.note_sighting(_row(SessionLocal, row_id).camera_id, "R197GB")
    _stamp_early(SessionLocal, row_id, "R183JF")
    _sweep(row_id, [_acc("R197GB", 0.8), _acc("R197GB", 0.9)], [b"a", b"b"])
    r = _row(SessionLocal, row_id)
    assert r.plate_text is None
    assert r.plate_frame_path is None and r.plate_evidence_path is None
    assert "plate_source" not in (r.payload or {})


# ── the evidence race with the bus consumer ────────────────────────


def _envelope(event_id, plate="R197GB"):
    return {"schema": "plate.recognized.v1", "camera_id": "cam1",
            "payload": {"plate_text": plate, "confidence": 0.9,
                        "event_id": event_id,
                        "plate_box": list(_BOX), "plate_box_image": [400, 300],
                        "plate_box_confidence": 0.9}}


def test_consumer_defers_to_a_pending_sweep(db):
    from services.plate_event_consumer import apply_plate_event

    SessionLocal, row_id = db
    pe.mark_sweep_pending(row_id)
    assert apply_plate_event(_envelope(row_id)) == "deferred-to-sweep"
    assert _row(SessionLocal, row_id).plate_text is None
    pe.clear_sweep_pending(row_id)
    assert apply_plate_event(_envelope(row_id)) == "applied"
    r = _row(SessionLocal, row_id)
    assert r.plate_text == "R197GB"
    assert r.payload["plate_source"] == "bus"
    assert r.payload["plate_reads"] == 1


def test_sweep_clears_its_pending_mark_even_on_no_read(db, stored):
    SessionLocal, row_id = db
    pe.mark_sweep_pending(row_id)
    _sweep(row_id, [None], [b"a"])
    assert not pe.sweep_is_pending(row_id)


def test_sweep_attaches_evidence_when_the_same_plate_landed_meanwhile(
        db, stored, monkeypatch):
    """The exact race from the log: 'already read as K884RS while OCR
    was in flight — sweep result K884RS dropped'. Same plate, same
    bytes: the row gets the crop and frame it was missing."""
    SessionLocal, row_id = db
    from services.plate_event_consumer import apply_plate_event

    async def racing_ocr(jpeg, camera_handle, event_id=None):
        # the bus consumer wins the write while the sweep is in OCR
        # (simulating an install without the pending-mark, or a foreign
        # producer's event for the same row)
        if jpeg == b"b":
            with pe._sweeps_lock:
                pe._sweeping.discard(row_id)
            assert apply_plate_event(_envelope(row_id)) == "applied"
        return _acc("R197GB", 0.8 if jpeg == b"a" else 0.9)

    monkeypatch.setattr(pe, "_ocr_jpeg", racing_ocr)
    asyncio.run(pe.enrich_event_plate(row_id, [b"a", b"b"]))
    r = _row(SessionLocal, row_id)
    assert r.plate_text == "R197GB"
    assert r.plate_frame_path == "xx/b.jpg"
    assert r.plate_evidence_path == "xx/b.jpg"
    assert r.payload["plate_reads"] == 2
    assert r.payload["plate_source"] == "sweep"


def test_sweep_consensus_replaces_a_lone_bus_write(db, stored, monkeypatch):
    SessionLocal, row_id = db
    from services.plate_event_consumer import apply_plate_event

    async def racing_ocr(jpeg, camera_handle, event_id=None):
        if jpeg == b"a":
            assert apply_plate_event(_envelope(row_id, "R183JF")) == "applied"
        return _acc("R197GB", 0.9)

    monkeypatch.setattr(pe, "_ocr_jpeg", racing_ocr)
    asyncio.run(pe.enrich_event_plate(row_id, [b"a", b"b"]))
    r = _row(SessionLocal, row_id)
    assert r.plate_text == "R197GB"
    assert r.payload["plate_reads"] == 2


# ── the localisation gate ──────────────────────────────────────────


def _resp(plate, detection):
    return {"result": {"plate_text": plate, "confidence": 0.95,
                       "accepted": True, "min_confidence_applied": 0.45,
                       "characters": [{"char": c, "confidence": 0.95}
                                      for c in plate],
                       "plate_detection": detection}}


def test_a_read_off_the_car_body_is_not_a_read(monkeypatch):
    body = {"attempted": True, "found": False, "confidence": None,
            "box": None, "image_size": [400, 300]}
    assert extract_read(_resp("L656XH", body)) is None
    assert pe.extract_plate(_resp("L656XH", body)) is None
    # OCR-only adapter: no opinion, read passes
    assert extract_read(_resp("L656XH", {"attempted": False}))["plate"] == "L656XH"
    # a localised plate passes
    found = {"attempted": True, "found": True, "confidence": 0.9,
             "box": [100, 100, 200, 130], "image_size": [400, 300]}
    assert extract_read(_resp("R197GB", found))["plate"] == "R197GB"
    # operator opt-out
    monkeypatch.setenv("OPENNVR_PLATE_REQUIRE_LOCALISATION", "0")
    assert extract_read(_resp("L656XH", body))["plate"] == "L656XH"


# ── fuzzy dedup ────────────────────────────────────────────────────


def test_a_near_miss_of_a_recent_sighting_is_the_same_car():
    pe.note_sighting(1, "R197GB", now=100.0)
    assert pe.is_duplicate_sighting(1, "R187GB", now=110.0)
    assert pe.is_duplicate_sighting(1, "R197G", now=110.0)
    assert not pe.is_duplicate_sighting(1, "R183JF", now=110.0)   # 2 edits
    assert not pe.is_duplicate_sighting(2, "R187GB", now=110.0)   # other cam
    assert not pe.is_duplicate_sighting(1, "R187GB", now=200.0)   # window


def test_dedup_distance_is_operator_tunable(monkeypatch):
    pe.note_sighting(1, "R197GB", now=100.0)
    monkeypatch.setenv("OPENNVR_PLATE_DEDUP_DISTANCE", "0")
    assert not pe.is_duplicate_sighting(1, "R187GB", now=110.0)
    monkeypatch.setenv("OPENNVR_PLATE_DEDUP_DISTANCE", "2")
    assert pe.is_duplicate_sighting(1, "R183GB", now=110.0)


# ── ingest wiring ──────────────────────────────────────────────────


def test_ingest_marks_the_sweep_pending_and_resweeps_early_reads():
    src = (_HERE / "routers" / "internal_camera_agent.py").read_text()
    assert "mark_sweep_pending(row.id)" in src
    assert 'source="early"' in src
    assert "(not row.plate_text or early_read)" in src, (
        "an early read must still hand its candidates to the sweep — "
        "they are the looks that confirm or overturn it")
