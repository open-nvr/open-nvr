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


@pytest.fixture(autouse=True)
def _first_read_wins(monkeypatch):
    """These tests pin the single-read mechanics (early exit, budget,
    merge, evidence). The consensus policy that sits on top — a plate
    is written when the looks AGREE — has its own file
    (test_plate_consensus.py); here it is switched to the legacy
    first-accepted-read-wins so each mechanic can be asserted alone."""
    monkeypatch.setenv("OPENNVR_PLATE_MIN_AGREEING_READS", "1")


@pytest.fixture(autouse=True)
def _clean_plate_sightings():
    """The dedup sightings map is process-global on purpose (that IS the
    feature) — which makes it cross-test state by accident. Every test
    starts clean or the dedup round changes unrelated verdicts."""
    import services.plate_enrichment as _pe

    with _pe._sightings_lock:
        _pe._recent_sightings.clear()
    yield
    with _pe._sightings_lock:
        _pe._recent_sightings.clear()



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


#: Every scripted read carries a plate box, because a real adapter
#: reports one whenever it localised a plate — and the stored evidence
#: is cropped to it (#385).
_BOX = (10.0, 20.0, 110.0, 50.0)


def _accepted(plate="GOOD42", conf=0.9, box=_BOX):
    return {"plate": plate, "confidence": conf, "characters": [conf] * len(plate),
            "accepted": True, "floor": 0.45, "box": box}


def _rejected(plate, chars, floor=0.45, box=_BOX):
    return {"plate": plate, "confidence": min(chars), "characters": chars,
            "accepted": False, "floor": floor, "box": box}


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


# ── the early-attempt background task (race cover) ─────────────────


def test_early_attempt_parks_read_and_covers_the_ingest_race(db, monkeypatch):
    """run_early_plate_attempt must (a) park an accepted read in the
    cache for the visit to claim, and (b) when the visit ALREADY landed
    unplated (the attempt raced ingest), write the plate straight onto
    the row — the cache is a waiting room, not a detour."""
    from routers import internal_camera_agent as ica
    from services import plate_attempt_cache as pac

    SessionLocal, row_id = db
    fresh = pac.PlateAttemptCache()
    monkeypatch.setattr(pac, "cache", fresh)

    # Attach a track id + window to the fixture row so the race-cover
    # query can find it.
    s = SessionLocal()
    row = s.get(models.TimelineEvent, row_id)
    row.track_id = "42"
    started = row.started_at.replace(tzinfo=timezone.utc)
    s.commit()
    s.close()

    async def fake_ocr(jpeg, camera_handle, event_id=None):
        assert camera_handle.startswith("cam")
        return _accepted("RACE99")

    monkeypatch.setattr(pe, "_ocr_jpeg", fake_ocr)
    attempt_ts = started.timestamp() + 5.0
    s = SessionLocal()
    cam_id = s.get(models.TimelineEvent, row_id).camera_id
    s.close()
    asyncio.run(ica.run_early_plate_attempt(cam_id, "42", attempt_ts, b"jpg"))

    # (a) parked for a future visit...
    assert len(fresh) == 1
    # (b) ...AND applied to the already-ingested row.
    assert _plate_of(SessionLocal, row_id) == "RACE99"


def test_early_attempt_rejected_read_parks_nothing(db, monkeypatch):
    from routers import internal_camera_agent as ica
    from services import plate_attempt_cache as pac

    SessionLocal, row_id = db
    fresh = pac.PlateAttemptCache()
    monkeypatch.setattr(pac, "cache", fresh)

    async def fake_ocr(jpeg, camera_handle, event_id=None):
        return _rejected("JUNK1", [0.1] * 5)

    monkeypatch.setattr(pe, "_ocr_jpeg", fake_ocr)
    asyncio.run(ica.run_early_plate_attempt(1, "42", 1000.0, b"jpg"))
    assert len(fresh) == 0
    assert _plate_of(SessionLocal, row_id) is None


# ── #382: the crop the plate was actually READ from ────────────────
#
# A visit stores ONE image — the vehicle-best frame, chosen for the
# biggest/sharpest VEHICLE box. Multi-frame OCR does not read it; it
# reads plate candidates, and the two are anti-correlated by
# construction (a car is biggest when closest, which is when its plate
# leaves the crop). Before this round the winning candidate's bytes were
# dropped when the sweep returned, so the Vehicles page captioned a
# correct plate with a photo that often did not contain it.


@pytest.fixture()
def identity_crop(monkeypatch):
    """Make the plate crop the identity, so a test can still name the
    bytes that were stored.

    The real ``crop_to_plate_box`` decodes JPEGs through cv2, which is
    not a test dependency; its geometry is pinned separately below. What
    every path shares — and what this keeps — is "no box, no crop".
    """
    monkeypatch.setattr(
        pe, "crop_to_plate_box",
        lambda jpeg, box, **kw: jpeg if box else None,
    )


@pytest.fixture()
def stored_crops(monkeypatch, identity_crop):
    """Capture what reaches the evidence store, keyed by its fake path.

    ``store_plate_crop`` imports ``save_evidence_jpeg`` at call time, so
    patching the module attribute is enough — and it keeps the test off
    the real JPEG-magic validation and the recordings volume.
    """
    import services.evidence_store as es

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
        return s.get(models.TimelineEvent, row_id)
    finally:
        s.close()


def test_sweep_stores_the_crop_the_winning_read_came_from(
    db, monkeypatch, stored_crops,
):
    """The THIRD candidate wins — so the third crop is the only image
    that shows this plate, and it is the one that must be stored."""
    SessionLocal, row_id = db
    ocr = _ScriptedOcr([None, None, _accepted("WIN123")])
    monkeypatch.setattr(pe, "_ocr_jpeg", ocr)
    asyncio.run(pe.enrich_event_plate(row_id, [b"one", b"two", b"three"]))

    row = _row(SessionLocal, row_id)
    assert row.plate_text == "WIN123"
    assert row.plate_evidence_path == "xx/three.jpg", (
        "stored the wrong crop — the image shown would not be the image "
        "the plate was read from (#382)")
    assert stored_crops["xx/three.jpg"] == b"three"
    # The vehicle-best frame is untouched: it is still the thumbnail.
    assert row.evidence_path is None


def test_merged_read_stores_the_clearer_contributor_and_marks_the_row(
    db, monkeypatch, stored_crops,
):
    """A merged plate appears WHOLE in neither crop. Keep the more
    confident contributor and say so, rather than passing it off as a
    clean single-frame read."""
    SessionLocal, row_id = db
    # Same disagreement as the merge test above; `a` is the more
    # confident contributor overall (0.60 vs 0.20 min-confidence).
    a = _rejected("H644LX", [0.9, 0.9, 0.9, 0.9, 0.9, 0.60])
    b = _rejected("H644LK", [0.95, 0.95, 0.95, 0.95, 0.95, 0.20])
    monkeypatch.setattr(pe, "_ocr_jpeg", _ScriptedOcr([a, b]))

    # Pre-existing payload must survive the merge stamp.
    s = SessionLocal()
    s.get(models.TimelineEvent, row_id).payload = {"stationary": False}
    s.commit()
    s.close()

    asyncio.run(pe.enrich_event_plate(row_id, [b"first", b"second"]))

    row = _row(SessionLocal, row_id)
    assert row.plate_text == "H644LX"
    assert row.plate_evidence_path == "xx/first.jpg", (
        "merged read kept the less confident contributor's crop")
    assert row.payload["plate_merged"] is True
    assert row.payload["stationary"] is False, (
        "the merge stamp replaced payload instead of updating it — "
        "stationary was collateral")


def test_fallback_path_stores_the_evidence_crop_it_read(
    db, monkeypatch, stored_crops,
):
    """With no candidates the sweep OCRs the evidence frame itself. One
    rule for every path: store the PLATE out of what was read. Before
    #385 this path stored the evidence frame's own bytes, which
    content-addressing collapsed onto evidence_path — the row then
    claimed a plate crop it did not have."""
    SessionLocal, row_id = db
    s = SessionLocal()
    s.get(models.TimelineEvent, row_id).evidence_path = "ab/abc.jpg"
    s.commit()
    s.close()

    # enrich_event_plate imports resolve_evidence at CALL time from
    # services.evidence_store, so that is the module to patch.
    import services.evidence_store as es

    monkeypatch.setattr(
        es, "resolve_evidence",
        lambda rel: _types.SimpleNamespace(read_bytes=lambda: b"evidence"),
    )
    monkeypatch.setattr(pe, "_ocr_jpeg", _ScriptedOcr([_accepted("FALL01")]))
    asyncio.run(pe.enrich_event_plate(row_id, None))

    row = _row(SessionLocal, row_id)
    assert row.plate_text == "FALL01"
    assert row.plate_evidence_path == "xx/evidence.jpg", (
        "the fallback read a real frame and localised a plate in it — "
        "that plate is what the row must show")


def test_a_failed_crop_store_never_costs_us_the_plate(
    db, monkeypatch, identity_crop,
):
    """Evidence storage is best-effort. A full disk must lose the photo,
    never the read."""
    import services.evidence_store as es

    SessionLocal, row_id = db

    def boom(_data):
        raise OSError("no space left on device")

    monkeypatch.setattr(es, "save_evidence_jpeg", boom)
    monkeypatch.setattr(pe, "_ocr_jpeg", _ScriptedOcr([_accepted("KEEP01")]))
    asyncio.run(pe.enrich_event_plate(row_id, [b"x"]))

    row = _row(SessionLocal, row_id)
    assert row.plate_text == "KEEP01"
    assert row.plate_evidence_path is None


def test_early_attempt_carries_its_crop_through_to_the_row(
    db, monkeypatch, stored_crops,
):
    """The early-attempt path discarded its winning crop too. It must
    store it once and park the PATH, so both the race-cover write and a
    later ingest claim can stamp the row."""
    from routers import internal_camera_agent as ica
    from services import plate_attempt_cache as pac

    SessionLocal, row_id = db
    fresh = pac.PlateAttemptCache()
    monkeypatch.setattr(pac, "cache", fresh)

    s = SessionLocal()
    row = s.get(models.TimelineEvent, row_id)
    row.track_id = "42"
    started = row.started_at.replace(tzinfo=timezone.utc)
    cam_id = row.camera_id
    s.commit()
    s.close()

    async def fake_ocr(jpeg, camera_handle, event_id=None):
        return _accepted("EARLY7")

    monkeypatch.setattr(pe, "_ocr_jpeg", fake_ocr)
    asyncio.run(ica.run_early_plate_attempt(
        cam_id, "42", started.timestamp() + 5.0, b"earlycrop"))

    # Parked for a visit that has not landed yet...
    parked = fresh.claim(cam_id, "42",
                         started_ts=started.timestamp(),
                         ended_ts=started.timestamp() + 30)
    assert parked is not None
    assert parked.plate_evidence_path == "xx/earlycrop.jpg"
    # ...and stamped on the row the attempt raced.
    row = _row(SessionLocal, row_id)
    assert row.plate_text == "EARLY7"
    assert row.plate_evidence_path == "xx/earlycrop.jpg"



def test_ingest_claim_stamps_the_parked_crop():
    """Lockstep with the claim path: the parked crop must reach the row,
    or an early-attempt read shows the vehicle frame again."""
    src = (_HERE / "routers" / "internal_camera_agent.py").read_text()
    assert "stamp_plate_evidence(row, pending.plate_evidence_path," in src, (
        "the ingest claim no longer stamps the early attempt's crop — "
        "those rows fall back to the vehicle-best frame (#382)")
    assert "frame_path=pending.plate_frame_path" in src, (
        "the ingest claim drops the frame the plate was read from — the "
        "row then has no image proving WHICH car the number came off")


def test_events_api_exposes_the_plate_crop():
    src = (_HERE / "routers" / "timeline_events.py").read_text()
    assert '"plate_evidence_url"' in src, (
        "the events payload no longer offers the plate crop — the UI "
        "cannot show it")
    assert '@router.get("/events/{event_id}/plate-evidence")' in src, (
        "the plate-evidence route is gone — the UI would 404")


# ── #385: the stored evidence is the PLATE, not the car ─────────────
#
# The attempts we OCR are VEHICLE crops — Tier-0 tracks cars, not
# plates — so #382's "store the frame the read came from" stored a car
# photo, and the UI dropped its "vehicle frame" caveat while still
# showing one. The adapter localises the plate in each attempt it
# answers; the stored evidence is now narrowed to that rectangle.


class _RecordingCrop:
    """Identity crop that remembers the (bytes, box) pairs it was
    handed — the box is the thing under test, and it must be the one
    measured in the bytes beside it."""

    def __init__(self):
        self.calls: list[tuple[bytes, object]] = []

    def __call__(self, jpeg, box, **kw):
        self.calls.append((jpeg, box))
        return jpeg if box else None


def test_sweep_crops_the_stored_evidence_to_the_winning_reads_box(
    db, monkeypatch, stored_crops,
):
    """The third candidate wins, so the third crop's OWN box is the
    rectangle to cut — a box from an earlier attempt would carve a
    different frame's coordinates out of these pixels."""
    SessionLocal, row_id = db
    losing, winning = (1.0, 2.0, 3.0, 4.0), (50.0, 60.0, 150.0, 90.0)
    monkeypatch.setattr(pe, "_ocr_jpeg", _ScriptedOcr([
        None,
        _rejected("NOPE12", [0.1] * 6, box=losing),
        _accepted("WIN123", box=winning),
    ]))
    crop = _RecordingCrop()
    monkeypatch.setattr(pe, "crop_to_plate_box", crop)

    asyncio.run(pe.enrich_event_plate(row_id, [b"one", b"two", b"three"]))

    assert _row(SessionLocal, row_id).plate_text == "WIN123"
    assert crop.calls == [(b"three", winning)], (
        "stored evidence was not cropped to the winning read's own plate "
        "box — the image shown is a car, or the wrong rectangle (#385)")


def test_merged_read_crops_with_the_kept_contributors_box(
    db, monkeypatch, stored_crops,
):
    """A merge keeps the more confident contributor's crop; the box has
    to come from the SAME attempt, since the two are different frames."""
    SessionLocal, row_id = db
    kept, dropped = (7.0, 8.0, 90.0, 30.0), (400.0, 300.0, 480.0, 322.0)
    a = _rejected("H644LX", [0.9, 0.9, 0.9, 0.9, 0.9, 0.60], box=kept)
    b = _rejected("H644LK", [0.95, 0.95, 0.95, 0.95, 0.95, 0.20], box=dropped)
    monkeypatch.setattr(pe, "_ocr_jpeg", _ScriptedOcr([a, b]))
    crop = _RecordingCrop()
    monkeypatch.setattr(pe, "crop_to_plate_box", crop)

    asyncio.run(pe.enrich_event_plate(row_id, [b"first", b"second"]))

    row = _row(SessionLocal, row_id)
    assert row.plate_text == "H644LX"
    assert crop.calls == [(b"first", kept)], (
        "the merge crossed a box with another frame's pixels (#385)")


def test_a_read_without_a_plate_box_stores_nothing(db, monkeypatch):
    """No localisation, no crop: the row keeps plate_evidence_path NULL
    so the UI shows the vehicle frame WITH its caveat. Storing the
    uncropped attempt is what made the caption lie."""
    SessionLocal, row_id = db
    monkeypatch.setattr(pe, "_ocr_jpeg",
                        _ScriptedOcr([_accepted("NOBOX1", box=None)]))
    asyncio.run(pe.enrich_event_plate(row_id, [b"vehicle-crop"]))

    row = _row(SessionLocal, row_id)
    assert row.plate_text == "NOBOX1", "a missing box must not cost the read"
    assert row.plate_evidence_path is None, (
        "stored a vehicle crop as the plate crop — the UI would drop its "
        "'vehicle frame' caveat and show a car (#385)")


def test_extract_read_carries_the_plate_box_and_drops_junk_ones():
    """The box rides the read, so the crop is always measured in the
    bytes that produced it."""
    resp = _resp()
    resp["result"]["plate_detection"] = {"found": True,
                                         "box": [121, 229, 233, 267]}
    assert extract_read(resp)["box"] == (121.0, 229.0, 233.0, 267.0)
    # No detection at all, and boxes that cannot bound anything.
    assert extract_read(_resp())["box"] is None
    for bad in ([1, 2, 3], "x", [0, 0, 0, 0], [10, 10, 5, 20], ["a", 1, 2, 3]):
        resp["result"]["plate_detection"] = {"box": bad}
        assert extract_read(resp)["box"] is None, f"accepted {bad!r}"


def test_crop_to_plate_box_narrows_the_frame_and_clamps_to_it():
    """Geometry, on real pixels: the crop is the padded box, and a plate
    near the frame edge loses margin rather than going out of bounds."""
    cv2 = pytest.importorskip("cv2", reason="crop needs opencv")
    import numpy as np

    frame = np.zeros((300, 400, 3), dtype=np.uint8)
    frame[:] = (30, 60, 90)
    ok, buf = cv2.imencode(".jpg", frame)
    assert ok
    jpeg = bytes(buf.tobytes())

    # pad = 0.08 * longer side (200) = 16, applied on every side.
    out = pe.crop_to_plate_box(jpeg, (100.0, 200.0, 300.0, 260.0))
    assert pe.jpeg_dimensions(out) == (233, 93)

    # Flush against the right/bottom edges: clamped, never wider than
    # the frame it came from.
    out = pe.crop_to_plate_box(jpeg, (380.0, 290.0, 400.0, 300.0))
    w, h = pe.jpeg_dimensions(out)
    assert w <= 400 and h <= 300 and w >= 2 and h >= 2

    # Nothing to crop, and nothing to crop it out of.
    assert pe.crop_to_plate_box(jpeg, None) is None
    assert pe.crop_to_plate_box(b"", (1.0, 1.0, 9.0, 9.0)) is None
    assert pe.crop_to_plate_box(b"not-a-jpeg", (1.0, 1.0, 9.0, 9.0)) is None


def test_the_crop_never_runs_on_the_event_loop():
    """Lockstep: storing the images decodes a full frame through cv2
    (~15ms on a 1080x720 attempt) and then writes files. Both enrichment
    paths are async, and core serves every other request on that same
    loop, so the call has to go to a thread — and both images go in ONE
    hop, which is why store_plate_images exists."""
    sweep = (_HERE / "services" / "plate_enrichment.py").read_text()
    assert "_asyncio.to_thread(\n            store_plate_images" in sweep, (
        "the ingest sweep stores its images inline again — a cv2 decode "
        "per vehicle back on the event loop")
    early = (_HERE / "routers" / "internal_camera_agent.py").read_text()
    assert "asyncio.to_thread(\n        store_plate_images" in early, (
        "the early-attempt path stores its images inline again")


# ── the sweep holds no session across OCR ───────────────────────────
#
# Holding one was what exhausted core's pool: the loop waits on
# _OCR_CONCURRENCY and then on a 15s HTTP timeout, up to
# MAX_INGEST_ATTEMPTS times, and at roughly one vehicle per second the
# tasks merely QUEUED on that semaphore pinned every connection there
# was. Letting go costs a race, so the tests below pin who wins it.


def test_the_sweep_holds_no_session_while_ocr_runs(db, monkeypatch):
    """The invariant. Counted from inside the OCR call, where the old code
    was sitting on an open transaction."""
    SessionLocal, row_id = db
    live = {"n": 0, "peak_during_ocr": 0}

    def _tracking():
        sess = SessionLocal()
        live["n"] += 1
        _close = sess.close

        def _counted_close():
            live["n"] -= 1
            _close()

        sess.close = _counted_close
        return sess

    monkeypatch.setattr(cdb, "SessionLocal", _tracking)

    class _Watching(_ScriptedOcr):
        async def __call__(self, jpeg, camera_handle, event_id=None):
            live["peak_during_ocr"] = max(live["peak_during_ocr"], live["n"])
            return await super().__call__(jpeg, camera_handle, event_id)

    monkeypatch.setattr(pe, "_ocr_jpeg", _Watching([_accepted("CLEAR1")]))
    asyncio.run(pe.enrich_event_plate(row_id, [b"a"]))
    assert _plate_of(SessionLocal, row_id) == "CLEAR1"
    assert live["peak_during_ocr"] == 0, (
        "a DB session was open while the sweep was waiting on OCR — this is "
        "the leak that pinned the pool")
    assert live["n"] == 0, "the sweep leaked a session"


def test_a_plate_written_during_ocr_is_not_overwritten(db, monkeypatch):
    """First writer wins. The other writer (an early attempt, or the ingest
    claim) read its plate off a frame we no longer hold, so we cannot prove
    ours is even the same vehicle — trading a provable pairing for an
    unprovable one is the wrong way round."""
    SessionLocal, row_id = db

    class _RacingOcr(_ScriptedOcr):
        async def __call__(self, jpeg, camera_handle, event_id=None):
            other = SessionLocal()
            try:
                other.get(models.TimelineEvent, row_id).plate_text = "EARLY1"
                other.commit()
            finally:
                other.close()
            return await super().__call__(jpeg, camera_handle, event_id)

    monkeypatch.setattr(pe, "_ocr_jpeg", _RacingOcr([_accepted("LATE99")]))
    asyncio.run(pe.enrich_event_plate(row_id, [b"a"]))
    assert _plate_of(SessionLocal, row_id) == "EARLY1"


def test_a_row_deleted_during_ocr_does_not_raise(db, monkeypatch):
    """Retention can sweep the visit while its OCR is in flight. Phase 3
    re-reads, so it must cope with the row being gone."""
    SessionLocal, row_id = db

    class _DeletingOcr(_ScriptedOcr):
        async def __call__(self, jpeg, camera_handle, event_id=None):
            other = SessionLocal()
            try:
                other.delete(other.get(models.TimelineEvent, row_id))
                other.commit()
            finally:
                other.close()
            return await super().__call__(jpeg, camera_handle, event_id)

    monkeypatch.setattr(pe, "_ocr_jpeg", _DeletingOcr([_accepted("GONE11")]))
    asyncio.run(pe.enrich_event_plate(row_id, [b"a"]))   # must not raise
    s = SessionLocal()
    try:
        assert s.get(models.TimelineEvent, row_id) is None
    finally:
        s.close()
