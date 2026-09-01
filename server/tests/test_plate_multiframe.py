# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Multi-frame OCR, core half: the attempt cache, character-consensus
merging, and the ingest-time candidate sweep.

The behaviours pinned here are the round's promises:

* several diverse OCR attempts per visit, best-first, EARLY EXIT the
  moment one is accepted (the compute budget is real);
* two near-miss rejects can merge character-by-character into an
  accepted read — but a merge can never sneak under the stricter of
  the two floors;
* an early attempt's read parks in the cache and is claimed by the
  visit at ingest — but only when the attempt's timestamp falls inside
  the visit's window, so recycled track ids from a restarted worker
  can't hand yesterday's plate to today's car.
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
os.environ.setdefault("DATABASE_URL", "sqlite:///./_mf_test.db")
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
from services.plate_attempt_cache import PlateAttemptCache  # noqa: E402
from services.plate_enrichment import extract_read, merge_reads  # noqa: E402


# ── extract_read (the full-fidelity parse) ─────────────────────────


def _resp(plate="AB123CD", conf=0.9, accepted=True, floor=0.45, chars=None):
    if chars is None:
        chars = [conf] * len(plate)
    return {"result": {
        "plate_text": plate, "confidence": conf, "accepted": accepted,
        "min_confidence_applied": floor,
        "characters": [{"char": c, "confidence": p}
                       for c, p in zip(plate, chars)],
    }}


def test_extract_read_keeps_rejects_with_characters():
    read = extract_read(_resp(accepted=False, conf=0.3))
    assert read is not None
    assert read["accepted"] is False
    assert read["plate"] == "AB123CD"
    assert read["characters"] == [0.3] * 7
    assert read["floor"] == 0.45


def test_extract_read_none_on_empty_or_junk():
    assert extract_read(None) is None
    assert extract_read({"result": {"plate_text": "  "}}) is None
    assert extract_read({"result": {"plate_text": 42}}) is None


# ── merge_reads (character consensus) ──────────────────────────────


def test_merge_reconstructs_the_plate_from_two_rejects():
    """The headline case: H644LX read as H644LX (weak X) and H644LK
    (weak K) — position-wise best characters reconstruct the truth and
    the merged min-confidence clears the floor."""
    a = {"plate": "H644LX", "confidence": 0.30, "floor": 0.45,
         "accepted": False,
         "characters": [0.9, 0.9, 0.9, 0.9, 0.9, 0.30]}
    b = {"plate": "H644LK", "confidence": 0.20, "floor": 0.45,
         "accepted": False,
         "characters": [0.8, 0.8, 0.8, 0.8, 0.8, 0.20]}
    merged = merge_reads(a, b)
    assert merged is not None
    assert merged["plate"] == "H644LX"    # X (0.30) beats K (0.20)... per-char
    # positions 0-4: a's 0.9 wins; position 5: a's X at 0.30 vs b's K 0.20
    assert merged["confidence"] == pytest.approx(0.30)
    assert merged["accepted"] is False    # 0.30 < floor 0.45 — honest


def test_merge_accepts_only_above_the_stricter_floor():
    a = {"plate": "AB1", "confidence": 0.5, "floor": 0.45, "accepted": False,
         "characters": [0.9, 0.9, 0.5]}
    b = {"plate": "AB7", "confidence": 0.4, "floor": 0.75, "accepted": False,
         "characters": [0.6, 0.6, 0.9]}
    merged = merge_reads(a, b)
    assert merged["plate"] == "AB7"       # 3rd char: 0.9 beats 0.5
    assert merged["confidence"] == pytest.approx(0.9)
    assert merged["floor"] == 0.75        # the STRICTER floor governs
    assert merged["accepted"] is True


def test_merge_refuses_agreement_length_mismatch_and_charless():
    base = {"plate": "AB1", "confidence": 0.5, "floor": 0.45,
            "accepted": False, "characters": [0.5, 0.5, 0.5]}
    assert merge_reads(base, dict(base)) is None                  # same plate
    other = dict(base, plate="AB12", characters=[0.5] * 4)
    assert merge_reads(base, other) is None                       # length
    assert merge_reads(base, dict(base, plate="AB2",
                                  characters=None)) is None       # no chars
    assert merge_reads(None, base) is None


# ── the attempt cache ──────────────────────────────────────────────


def test_cache_claim_requires_window_overlap():
    cache = PlateAttemptCache()
    cache.put(1, "42", plate="H644LX", confidence=0.9, attempt_ts=1000.0)
    # A visit whose window does not contain the attempt: recycled track
    # id from a restarted worker — must NOT get yesterday's plate.
    assert cache.claim(1, "42", started_ts=5000.0, ended_ts=5100.0) is None
    # The right visit claims (and removes) it.
    got = cache.claim(1, "42", started_ts=995.0, ended_ts=1020.0)
    assert got is not None and got.plate == "H644LX"
    assert cache.claim(1, "42", started_ts=995.0, ended_ts=1020.0) is None


def test_cache_keeps_the_best_read_not_the_latest():
    cache = PlateAttemptCache()
    cache.put(1, "7", plate="GOOD1", confidence=0.9, attempt_ts=100.0)
    cache.put(1, "7", plate="WORSE", confidence=0.5, attempt_ts=101.0)
    got = cache.claim(1, "7", started_ts=90.0, ended_ts=110.0)
    assert got.plate == "GOOD1"


def test_cache_ttl_and_size_bounds():
    now = [0.0]
    cache = PlateAttemptCache(ttl_s=10.0, max_entries=3, clock=lambda: now[0])
    cache.put(1, "a", plate="A", confidence=0.9, attempt_ts=1.0)
    now[0] = 11.0                          # past TTL
    cache.put(1, "b", plate="B", confidence=0.9, attempt_ts=1.0)
    assert cache.claim(1, "a", started_ts=0.0, ended_ts=5.0) is None  # swept
    for tid in ("c", "d", "e"):
        cache.put(1, tid, plate=tid.upper(), confidence=0.9, attempt_ts=1.0)
    assert len(cache) <= 3                 # hard cap held


# ── the ingest-time candidate sweep ────────────────────────────────


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
        started_at=datetime(2026, 9, 1, 9, 59, tzinfo=timezone.utc),
    )
    s.add(row)
    s.commit()
    row_id = row.id
    s.close()
    yield SessionLocal, row_id


def _plate_of(SessionLocal, row_id):
    s = SessionLocal()
    try:
        return s.get(models.TimelineEvent, row_id).plate_text
    finally:
        s.close()


class _ScriptedOcr:
    """Monkeypatch stand-in for _ocr_jpeg: pops one scripted read per
    call and records what it was asked to OCR."""

    def __init__(self, reads):
        self.reads = list(reads)
        self.calls = []

    async def __call__(self, jpeg, camera_handle, event_id=None):
        self.calls.append((jpeg, camera_handle, event_id))
        return self.reads.pop(0) if self.reads else None


def _accepted(plate="GOOD42", conf=0.9):
    return {"plate": plate, "confidence": conf, "characters": [conf] * len(plate),
            "accepted": True, "floor": 0.45}


def _rejected(plate, chars, floor=0.45):
    return {"plate": plate, "confidence": min(chars), "characters": chars,
            "accepted": False, "floor": floor}


def test_sweep_early_exits_on_first_accepted(db, monkeypatch):
    SessionLocal, row_id = db
    ocr = _ScriptedOcr([_accepted("FIRST1"), _accepted("NEVER2")])
    monkeypatch.setattr(pe, "_ocr_jpeg", ocr)
    asyncio.run(pe.enrich_event_plate(row_id, [b"a", b"b", b"c"]))
    assert _plate_of(SessionLocal, row_id) == "FIRST1"
    assert len(ocr.calls) == 1, "early exit must stop the sweep"


def test_sweep_tries_candidates_in_order_and_respects_the_budget(db, monkeypatch):
    SessionLocal, row_id = db
    ocr = _ScriptedOcr([None, None, None, None, _accepted("LATE99")])
    monkeypatch.setattr(pe, "_ocr_jpeg", ocr)
    asyncio.run(pe.enrich_event_plate(
        row_id, [b"1", b"2", b"3", b"4", b"5", b"6"]))
    # MAX_INGEST_ATTEMPTS caps the sweep — the 5th scripted read is
    # never reached and the row stays honest-NULL.
    assert len(ocr.calls) == pe.MAX_INGEST_ATTEMPTS
    assert ocr.calls[0][0] == b"1" and ocr.calls[1][0] == b"2"
    assert _plate_of(SessionLocal, row_id) is None


def test_sweep_merges_two_rejects_into_a_read(db, monkeypatch):
    SessionLocal, row_id = db
    # Disagreement at position 5: X read at 0.60 vs K at 0.20 — the
    # merge takes X, and the merged min-confidence (0.60) clears the
    # 0.45 floor that each read alone missed.
    a = _rejected("H644LX", [0.9, 0.9, 0.9, 0.9, 0.9, 0.60])
    b = _rejected("H644LK", [0.95, 0.95, 0.95, 0.95, 0.95, 0.20])
    ocr = _ScriptedOcr([a, b])
    monkeypatch.setattr(pe, "_ocr_jpeg", ocr)
    asyncio.run(pe.enrich_event_plate(row_id, [b"1", b"2"]))
    assert _plate_of(SessionLocal, row_id) == "H644LX"
    assert len(ocr.calls) == 2


def test_sweep_writes_nothing_when_all_attempts_reject(db, monkeypatch):
    SessionLocal, row_id = db
    ocr = _ScriptedOcr([
        _rejected("AAA", [0.2, 0.2, 0.2]),
        _rejected("BBB", [0.2, 0.2, 0.2]),
    ])
    monkeypatch.setattr(pe, "_ocr_jpeg", ocr)
    asyncio.run(pe.enrich_event_plate(row_id, [b"1", b"2"]))
    assert _plate_of(SessionLocal, row_id) is None


def test_sweep_skips_rows_already_plated_by_an_early_attempt(db, monkeypatch):
    SessionLocal, row_id = db
    s = SessionLocal()
    s.get(models.TimelineEvent, row_id).plate_text = "EARLY1"
    s.commit()
    s.close()
    ocr = _ScriptedOcr([_accepted("LATE")])
    monkeypatch.setattr(pe, "_ocr_jpeg", ocr)
    asyncio.run(pe.enrich_event_plate(row_id, [b"1"]))
    assert _plate_of(SessionLocal, row_id) == "EARLY1"
    assert ocr.calls == []                 # zero OCR spent on a done row


# ── ingest + endpoint wiring (lockstep) ────────────────────────────


def test_ingest_claims_the_cache_and_passes_candidates():
    src = (_HERE / "routers" / "internal_camera_agent.py").read_text()
    assert "_attempt_cache.claim(" in src, (
        "ingest no longer claims early-attempt reads — the latency half "
        "of multi-frame OCR is disconnected")
    assert "background.add_task(enrich_event_plate, row.id, candidates or None)" in src, (
        "ingest no longer hands candidates to the enrichment sweep — the "
        "recall half of multi-frame OCR is disconnected")
    assert '@router.post("/plates/attempt"' in src, (
        "the early-attempt endpoint is gone — Tier-0's posts would 404")


def test_worker_fires_early_attempts_before_visit_lifecycle():
    src = (_HERE.parent / "detect-pipeline" / "detect_pipeline"
           / "service.py").read_text()
    assert "early_attempts.observe(result.tracks)" in src, (
        "the worker loop no longer fires early attempts — reads wait for "
        "the track to die again (minutes on a busy road)")
