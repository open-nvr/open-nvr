# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Duplicate-sighting dedup + the clip guard on multi-frame OCR paths.

The failure this round fixes, observed live on a moving camera: track
association breaks (a pan shifts every box at once), one physical pass
fragments into several visits, and multi-frame OCR then reads every
fragment successfully — one car became nine register rows in two
minutes. The plate is the only identity that survives a broken track
and it is only known AFTER the first OCR call, so that call is the
irreducible cost; everything past it is waste. The promises pinned
here:

* the same plate on the same camera within the rolling window is ONE
  sighting — later fragments keep their visit row but no plate;
* a folded fragment spends exactly ONE OCR call (no second candidate,
  no enrichment sweep);
* the window ROLLS: every sighting (written or folded) restarts it, so
  a fragment chain collapses no matter its length, while a genuine
  return after a quiet gap is a new row;
* all racing writers apply the same rule (sweep, ingest claim, early
  attempt race-cover, bus consumer) — a policy on only some of them is
  no policy at all;
* the #378 clip guard now judges the plate box against the image the
  box was measured in (the OCR'd crop itself, or the event's own
  ``plate_box_image``), never against an unrelated evidence frame.
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
os.environ.setdefault("DATABASE_URL", "sqlite:///./_dedup_test.db")
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
    dedup_window_s, extract_read, is_duplicate_sighting, note_sighting,
)


@pytest.fixture(autouse=True)
def _first_read_wins(monkeypatch):
    """Legacy first-accepted-read-wins, so the dedup mechanics can be
    asserted on their own; consensus lives in test_plate_consensus.py.
    Fuzzy dedup is off here too — these tests pin the exact-match
    window; the fuzzy layer has its own tests in that file."""
    monkeypatch.setenv("OPENNVR_PLATE_MIN_AGREEING_READS", "1")
    monkeypatch.setenv("OPENNVR_PLATE_DEDUP_DISTANCE", "0")
    # The sweep registry is process-global too (finished sweeps keep
    # their row for the echo grace) — start clean or a sweep run by
    # another test defers this one's consumer.
    with pe._sweeps_lock:
        pe._sweeping.clear()


@pytest.fixture(autouse=True)
def _clean_sightings():
    """Every test starts with an empty sightings map — the map is
    process-global on purpose (that is the feature), which makes it
    cross-test state by accident."""
    with pe._sightings_lock:
        pe._recent_sightings.clear()
    yield
    with pe._sightings_lock:
        pe._recent_sightings.clear()


# ── the window knob ────────────────────────────────────────────────


def test_window_default_and_parsing(monkeypatch):
    monkeypatch.delenv("OPENNVR_PLATE_DEDUP_WINDOW_S", raising=False)
    assert dedup_window_s() == 30.0
    monkeypatch.setenv("OPENNVR_PLATE_DEDUP_WINDOW_S", "45.5")
    assert dedup_window_s() == 45.5
    monkeypatch.setenv("OPENNVR_PLATE_DEDUP_WINDOW_S", "0")
    assert dedup_window_s() == 0.0
    monkeypatch.setenv("OPENNVR_PLATE_DEDUP_WINDOW_S", "-5")
    assert dedup_window_s() == 0.0          # nonsense negatives = off
    monkeypatch.setenv("OPENNVR_PLATE_DEDUP_WINDOW_S", "banana")
    assert dedup_window_s() == 30.0          # garbage = default, not off


# ── the sighting window itself ─────────────────────────────────────


def test_first_sighting_is_never_a_duplicate():
    assert not is_duplicate_sighting(1, "H644LX", now=100.0)


def test_second_sighting_within_window_is_a_duplicate():
    note_sighting(1, "H644LX", now=100.0)
    assert is_duplicate_sighting(1, "H644LX", now=125.0)      # 25s < 30
    assert not is_duplicate_sighting(1, "H644LX", now=131.0)  # 31s > 30
    assert not is_duplicate_sighting(2, "H644LX", now=101.0)  # other camera


def test_window_rolls_with_every_sighting():
    """The demo1 failure shape: fragments at 0, 25, 50, 75s. A fixed
    window anchored at the first write would re-admit the 50s fragment;
    a rolling one folds the whole chain."""
    note_sighting(1, "66HH07", now=0.0)
    for t in (25.0, 50.0, 75.0):
        assert is_duplicate_sighting(1, "66HH07", now=t), f"at {t}s"
        note_sighting(1, "66HH07", now=t)   # what every fold branch does
    # The loop's genuine re-pass, minutes later, is a NEW sighting.
    assert not is_duplicate_sighting(1, "66HH07", now=75.0 + 31.0)


def test_dedup_keys_are_normalized():
    note_sighting(1, "h64 4lx", now=100.0)
    assert is_duplicate_sighting(1, "H644LX", now=101.0)


def test_window_zero_disables(monkeypatch):
    monkeypatch.setenv("OPENNVR_PLATE_DEDUP_WINDOW_S", "0")
    note_sighting(1, "H644LX", now=100.0)
    assert not is_duplicate_sighting(1, "H644LX", now=101.0)


# ── DB-backed paths ────────────────────────────────────────────────


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
    cam_id = cam.id
    rows = []
    for _ in range(2):
        row = models.TimelineEvent(
            camera_id=cam_id, source="tier0", event_type="track",
            label="car",
            started_at=datetime(2026, 9, 1, 9, 59, tzinfo=timezone.utc),
        )
        s.add(row)
        s.commit()
        rows.append(row.id)
    s.close()
    yield SessionLocal, cam_id, rows


def _plate_of(SessionLocal, row_id):
    s = SessionLocal()
    try:
        return s.get(models.TimelineEvent, row_id).plate_text
    finally:
        s.close()


class _ScriptedOcr:
    def __init__(self, reads):
        self.reads = list(reads)
        self.calls = []

    async def __call__(self, jpeg, camera_handle, event_id=None):
        self.calls.append((jpeg, camera_handle, event_id))
        return self.reads.pop(0) if self.reads else None


def _accepted(plate, conf=0.9):
    return {"plate": plate, "confidence": conf,
            "characters": [conf] * len(plate),
            "accepted": True, "floor": 0.45}


# ── the enrichment sweep folds and stops ───────────────────────────


def test_sweep_folds_duplicate_and_spends_one_call(db, monkeypatch):
    SessionLocal, cam_id, (row_id, _) = db
    note_sighting(cam_id, "H644LX")        # the car we JUST read
    ocr = _ScriptedOcr([_accepted("H644LX"), _accepted("NEVER1")])
    monkeypatch.setattr(pe, "_ocr_jpeg", ocr)
    asyncio.run(pe.enrich_event_plate(row_id, [b"a", b"b", b"c"]))
    assert _plate_of(SessionLocal, row_id) is None, (
        "duplicate sighting must not be written")
    assert len(ocr.calls) == 1, (
        "identity established — every further OCR call is waste")


def test_sweep_write_arms_the_window_for_the_next_fragment(db, monkeypatch):
    SessionLocal, cam_id, (first, second) = db
    monkeypatch.setattr(pe, "_ocr_jpeg", _ScriptedOcr([_accepted("N894JV")]))
    asyncio.run(pe.enrich_event_plate(first, [b"a"]))
    assert _plate_of(SessionLocal, first) == "N894JV"
    # The fragment that follows seconds later: read again, folded.
    monkeypatch.setattr(pe, "_ocr_jpeg", _ScriptedOcr([_accepted("N894JV")]))
    asyncio.run(pe.enrich_event_plate(second, [b"b"]))
    assert _plate_of(SessionLocal, second) is None


def test_sweep_dedup_off_writes_both(db, monkeypatch):
    SessionLocal, cam_id, (first, second) = db
    monkeypatch.setenv("OPENNVR_PLATE_DEDUP_WINDOW_S", "0")
    for row_id in (first, second):
        monkeypatch.setattr(pe, "_ocr_jpeg",
                            _ScriptedOcr([_accepted("L605HZ")]))
        asyncio.run(pe.enrich_event_plate(row_id, [b"a"]))
    assert _plate_of(SessionLocal, first) == "L605HZ"
    assert _plate_of(SessionLocal, second) == "L605HZ"


# ── ingest claim: fold + no sweep queued ───────────────────────────


def test_ingest_claim_folds_duplicate_and_queues_no_sweep(db, monkeypatch):
    from fastapi import BackgroundTasks

    from routers import internal_camera_agent as ica
    from services import plate_attempt_cache as pac

    SessionLocal, cam_id, _rows = db
    fresh = pac.PlateAttemptCache()
    monkeypatch.setattr(pac, "cache", fresh)
    note_sighting(cam_id, "R183JF")        # just read on this camera

    # Evidence present: without the fold gate, wants_plate() is true and
    # the sweep WOULD queue — this is what makes the gate load-bearing
    # even when candidate decode is skipped.
    from services import evidence_store as _ev

    monkeypatch.setattr(_ev, "save_evidence_jpeg", lambda b: "ev/frag2.jpg")

    started = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    fresh.put(cam_id, "frag-2", plate="R183JF", confidence=0.97,
              attempt_ts=started.timestamp() + 1.0)

    payload = ica.TrackEventIn(
        camera_id=cam_id, label="car", track_id="frag-2",
        started_at=started, score=0.9,
        evidence_jpeg_b64="aGVsbG8=",       # keeps wants_plate() true
        candidate_jpegs_b64=["aGVsbG8="],   # would feed a sweep
    )
    background = BackgroundTasks()
    s = SessionLocal()
    try:
        out = asyncio.run(ica.ingest_track_event(payload, background, None, s))
        row = s.get(models.TimelineEvent, out["id"])
        assert row.plate_text is None, "folded sighting must not be written"
    finally:
        s.close()
    assert background.tasks == [], (
        "identity established at claim — queuing the sweep burns up to "
        f"{pe.MAX_INGEST_ATTEMPTS} OCR calls for nothing")


def test_ingest_claim_still_writes_a_fresh_plate(db, monkeypatch):
    from fastapi import BackgroundTasks

    from routers import internal_camera_agent as ica
    from services import plate_attempt_cache as pac

    SessionLocal, cam_id, _rows = db
    fresh = pac.PlateAttemptCache()
    monkeypatch.setattr(pac, "cache", fresh)

    started = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    fresh.put(cam_id, "t-9", plate="L656XH", confidence=0.97,
              attempt_ts=started.timestamp() + 1.0)
    payload = ica.TrackEventIn(camera_id=cam_id, label="car", track_id="t-9",
                               started_at=started, score=0.9)
    s = SessionLocal()
    try:
        out = asyncio.run(
            ica.ingest_track_event(payload, BackgroundTasks(), None, s))
        assert s.get(models.TimelineEvent, out["id"]).plate_text == "L656XH"
    finally:
        s.close()
    # ...and the write armed the window for the fragment behind it.
    assert is_duplicate_sighting(cam_id, "L656XH")


# ── early attempt: park for the claim, skip the race-cover write ───


def test_early_attempt_duplicate_parks_but_never_writes_the_row(db, monkeypatch):
    from routers import internal_camera_agent as ica
    from services import plate_attempt_cache as pac

    SessionLocal, cam_id, (row_id, _) = db
    fresh = pac.PlateAttemptCache()
    monkeypatch.setattr(pac, "cache", fresh)
    note_sighting(cam_id, "66HH07")

    s = SessionLocal()
    row = s.get(models.TimelineEvent, row_id)
    row.track_id = "frag-3"
    attempt_ts = row.started_at.replace(tzinfo=timezone.utc).timestamp() + 5.0
    s.commit()
    s.close()

    async def fake_ocr(jpeg, camera_handle, event_id=None):
        return _accepted("66HH07", conf=0.98)

    monkeypatch.setattr(pe, "_ocr_jpeg", fake_ocr)
    asyncio.run(ica.run_early_plate_attempt(cam_id, "frag-3", attempt_ts, b"j"))
    # Parked — so the visit's ingest claims it with ZERO extra OCR and
    # makes the fold decision there...
    assert len(fresh) == 1
    # ...but the race-cover write is skipped: no duplicate row plate.
    assert _plate_of(SessionLocal, row_id) is None


# ── bus consumer: same policy as the synchronous writers ───────────


def _envelope(event_id, plate="H644LX", **extra):
    payload = {"plate_text": plate, "event_id": event_id, **extra}
    return {"event": "plate.recognized.v1", "payload": payload,
            "correlation_id": "t"}


def test_consumer_folds_duplicates(db):
    from services.plate_event_consumer import apply_plate_event

    SessionLocal, cam_id, (first, second) = db
    assert apply_plate_event(_envelope(first)) == "applied"
    assert _plate_of(SessionLocal, first) == "H644LX"
    assert apply_plate_event(_envelope(second)) == "duplicate"
    assert _plate_of(SessionLocal, second) is None


def test_consumer_prefers_the_events_own_image_size(db):
    """#378 in the multi-frame world: the box is measured in the OCR'd
    CANDIDATE crop, not the visit's evidence frame. An event that says
    so (``plate_box_image``) must be judged in that space — this row
    has no evidence file at all, so only the event-carried size can
    catch the clip."""
    from services.plate_event_consumer import apply_plate_event

    SessionLocal, cam_id, (first, second) = db
    clipped = _envelope(first, plate="K884",
                        plate_box=[240, 90, 399, 148],
                        plate_box_image=[400, 150])   # abuts right edge
    assert apply_plate_event(clipped) == "clipped"
    assert _plate_of(SessionLocal, first) is None

    interior = _envelope(second, plate="K884RS",
                         plate_box=[100, 40, 300, 100],
                         plate_box_image=[400, 150])
    assert apply_plate_event(interior) == "applied"
    assert _plate_of(SessionLocal, second) == "K884RS"


# ── the clip guard inside extract_read ─────────────────────────────


def _adapter_resp(plate="K884RS", box=None, image_size_unused=None):
    result = {
        "plate_text": plate, "confidence": 0.95, "accepted": True,
        "min_confidence_applied": 0.45,
        "characters": [{"char": c, "confidence": 0.95} for c in plate],
    }
    if box is not None:
        result["plate_detection"] = {"found": True, "box": box}
    return {"result": result}


def test_extract_read_rejects_clipped_reads():
    """A read whose plate box abuts the OCR'd crop's own edge is a
    fragment — not stored, and (unlike a mere reject) not mergeable:
    its characters are real but belong to the wrong plate positions."""
    clipped = extract_read(_adapter_resp("K884", box=[300, 60, 399, 110]),
                           image_size=(400, 150))
    assert clipped is None


def test_extract_read_keeps_interior_and_unsized_reads():
    interior = extract_read(_adapter_resp(box=[100, 40, 300, 100]),
                            image_size=(400, 150))
    assert interior is not None and interior["plate"] == "K884RS"
    # No image_size (caller couldn't parse the crop) = cannot judge,
    # never a rejection.
    unsized = extract_read(_adapter_resp(box=[300, 60, 399, 110]))
    assert unsized is not None


# ── #386: a badge is not a plate (false localisations) ─────────────
#
# The reported case: an Audi's four-ring badge read as "C00D" at 0.51
# against the adapter's 0.45 floor, and was written to the register as
# a vehicle's plate. Nothing could catch it. Read confidence cannot —
# the characters really are those shapes. The #378 geometry guard
# cannot — a badge sits mid-crop, nowhere near an edge. What did know
# was the localiser, which scored its own find 0.3756 where the same
# camera's genuine plates score 0.853-0.936; that number was parsed and
# discarded.


def _badge_resp(plate="C00D", det_conf=0.3756):
    """The reported response, verbatim in the fields that matter."""
    result = {
        "plate_text": plate, "confidence": 0.5143, "accepted": True,
        "min_confidence_applied": 0.45,
        "characters": [{"char": c, "confidence": 0.9} for c in plate],
        "plate_detection": {
            "attempted": True, "found": True, "confidence": det_conf,
            # Mid-crop: 9px clear of the bottom edge, so #378 is silent.
            "box": [121, 229, 233, 267], "image_size": [477, 276],
        },
    }
    return {"result": result}


def test_extract_read_rejects_a_weakly_localised_read():
    """Dropped outright, not kept as a reject: a badge is not a near
    miss at a plate, so character-merging it with anything would
    manufacture a plate out of two non-plates."""
    assert extract_read(_badge_resp(), image_size=(477, 276)) is None


def test_extract_read_keeps_the_genuine_reads_from_the_same_camera():
    """The weakest true localisation measured on the reporting install
    (0.8529) must sail through — the gate exists to catch 0.38, and a
    guard that also rejects real plates is worse than no guard."""
    read = extract_read(_badge_resp("N894JV", det_conf=0.8529),
                        image_size=(477, 276))
    assert read is not None and read["plate"] == "N894JV"


def test_the_detection_floor_is_operator_tunable(monkeypatch):
    from services.plate_enrichment import plate_detection_floor

    # A camera angle that yields habitually weak-but-correct finds can
    # lower the bar...
    monkeypatch.setenv("OPENNVR_PLATE_MIN_DETECTION_CONFIDENCE", "0.2")
    assert plate_detection_floor() == 0.2
    assert extract_read(_badge_resp(), image_size=(477, 276)) is not None
    # ...or switch the gate off entirely.
    monkeypatch.setenv("OPENNVR_PLATE_MIN_DETECTION_CONFIDENCE", "0")
    assert plate_detection_floor() == 0.0
    assert extract_read(_badge_resp(), image_size=(477, 276)) is not None
    # Junk in the environment is not a licence to stop reading plates.
    monkeypatch.setenv("OPENNVR_PLATE_MIN_DETECTION_CONFIDENCE", "yes please")
    assert plate_detection_floor() == 0.6


def test_the_gate_has_no_opinion_without_a_localiser():
    """An OCR-only adapter never localises. Gating on a field it does
    not send would silently stop every plate it reads."""
    from services.plate_enrichment import plate_detection_is_weak

    assert plate_detection_is_weak({"attempted": False}) is False
    assert plate_detection_is_weak({"found": True}) is False   # no number
    assert plate_detection_is_weak({"confidence": "0.1"}) is False
    assert plate_detection_is_weak(None) is False
    # ...but a number below the bar is an opinion, and it is "no".
    assert plate_detection_is_weak({"confidence": 0.3756}) is True


def test_extract_plate_applies_the_same_gate():
    """The two parsers are two doors into one column; #378's lesson
    applies again — a guard on one of them is no guard at all."""
    from services.plate_enrichment import extract_plate

    assert extract_plate(_badge_resp(), image_size=(477, 276)) is None
    assert extract_plate(_badge_resp("N894JV", det_conf=0.8529),
                         image_size=(477, 276)) == "N894JV"


def test_consumer_rejects_a_weakly_localised_read(db):
    """The bus writer races the synchronous one for the same column, so
    it enforces the same rule — a badge rejected by enrichment must not
    simply land here instead."""
    from services.plate_event_consumer import apply_plate_event

    SessionLocal, cam_id, (first, second) = db
    badge = _envelope(first, plate="C00D", plate_box=[121, 229, 233, 267],
                      plate_box_image=[477, 276], plate_box_confidence=0.3756)
    assert apply_plate_event(badge) == "weak-detection"
    assert _plate_of(SessionLocal, first) is None

    # A producer that predates the field carries no opinion, and an
    # absent opinion must not become a rejection.
    old = _envelope(second, plate="N894JV", plate_box=[100, 40, 300, 100],
                    plate_box_image=[477, 276])
    assert apply_plate_event(old) == "applied"
    assert _plate_of(SessionLocal, second) == "N894JV"
