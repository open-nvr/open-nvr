# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canonical event & evidence store (RFC-0001 C1) — service-level tests."""

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
os.environ.setdefault("DATABASE_URL", "sqlite:///./_tl_test.db")
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

from core.database import Base  # noqa: E402
from models import Camera, Role, User  # noqa: E402
from services import evidence_store  # noqa: E402
from services.timeline_service import query_events, record_track_visit  # noqa: E402


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


UTC = timezone.utc
T = datetime(2026, 8, 7, 15, 0, tzinfo=UTC)  # "3pm"


@pytest.fixture
def db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    role = Role(name="admin")
    s.add(role)
    s.commit()
    owner = User(username="t", email="t@t.io", hashed_password="x", role_id=role.id)
    s.add(owner)
    s.commit()
    cam = Camera(name="gate", ip_address="10.0.0.9", port=80, owner_id=owner.id)
    s.add(cam)
    s.commit()
    s.refresh(cam)
    s.cam_id = cam.id
    try:
        yield s
    finally:
        s.close()


def _visit(db, *, start_min, end_min=None, label="person", cam=None, **kw):
    return record_track_visit(
        db, camera_id=cam or db.cam_id, label=label,
        started_at=T + timedelta(minutes=start_min),
        ended_at=None if end_min is None else T + timedelta(minutes=end_min),
        **kw,
    )


# ── write side ──────────────────────────────────────────────────────

def test_visit_row_shape(db):
    row = _visit(db, start_min=12, end_min=14, score=0.91,
                 track_id="7", stationary=False, evidence_path="ab/x.jpg")
    assert (row.source, row.event_type, row.label) == ("tier0", "track", "person")
    assert row.evidence_path == "ab/x.jpg"
    assert row.payload == {"stationary": False}


def test_label_normalized_lowercase(db):
    assert _visit(db, start_min=0, label="Person").label == "person"


# ── read side: the 3-4pm question ───────────────────────────────────

def test_window_query_uses_overlap_not_containment(db):
    _visit(db, start_min=-2, end_min=3)     # started 14:58, left 15:03 — counts
    _visit(db, start_min=12, end_min=14)    # fully inside — counts
    _visit(db, start_min=-30, end_min=-10)  # long gone — no
    _visit(db, start_min=70, end_min=75)    # after the window — no
    rows = query_events(db, label="person", from_=T, to=T + timedelta(hours=1))
    starts = sorted(r.started_at.replace(tzinfo=UTC) for r in rows)
    assert len(rows) == 2
    assert starts[0] == T + timedelta(minutes=-2)


def test_filters_by_label_and_camera(db):
    _visit(db, start_min=1, label="car")
    _visit(db, start_min=2, label="person")
    assert [r.label for r in query_events(db, label="car")] == ["car"]
    assert query_events(db, camera_id=db.cam_id + 999) == []


def test_newest_first_and_limit(db):
    for m in range(5):
        _visit(db, start_min=m)
    rows = query_events(db, limit=3)
    assert len(rows) == 3
    assert rows[0].started_at >= rows[-1].started_at


# ── evidence store ──────────────────────────────────────────────────

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _point_evidence_at(monkeypatch, tmp_path):
    """Patch the evidence module's settings reference (not the pydantic
    object — validate_assignment makes attribute patching order-dependent
    across the suite)."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        evidence_store, "settings",
        SimpleNamespace(recordings_base_path=str(tmp_path)),
    )


def test_evidence_roundtrip(tmp_path, monkeypatch):
    _point_evidence_at(monkeypatch, tmp_path)
    rel = evidence_store.save_evidence_jpeg(JPEG)
    assert rel.endswith(".jpg")
    p = evidence_store.resolve_evidence(rel)
    assert p is not None and p.read_bytes() == JPEG
    # content-addressed: same bytes, same path, no duplicate write
    assert evidence_store.save_evidence_jpeg(JPEG) == rel


def test_evidence_rejects_non_jpeg_and_oversize(tmp_path, monkeypatch):
    _point_evidence_at(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        evidence_store.save_evidence_jpeg(b"PNG-not-jpeg")
    with pytest.raises(ValueError):
        evidence_store.save_evidence_jpeg(
            b"\xff\xd8" + b"\x00" * evidence_store.MAX_EVIDENCE_BYTES)


def test_evidence_resolver_refuses_traversal(tmp_path, monkeypatch):
    _point_evidence_at(monkeypatch, tmp_path)
    (tmp_path / "secret.txt").write_text("no")
    assert evidence_store.resolve_evidence("../secret.txt") is None
    assert evidence_store.resolve_evidence("nope/missing.jpg") is None


# ── ownership scoping (cameras are owner-scoped; so is their history) ─

def test_query_scoped_to_owners_cameras(db):
    from models import User as _User
    other = _User(username="o", email="o@t.io", hashed_password="x",
                  role_id=db.query(Role).first().id)
    db.add(other)
    db.commit()
    other_cam = Camera(name="their-gate", ip_address="10.0.0.8", port=80,
                       owner_id=other.id)
    db.add(other_cam)
    db.commit()
    _visit(db, start_min=1)                       # mine
    _visit(db, start_min=2, cam=other_cam.id)     # theirs

    mine = query_events(db, scope={db.cam_id})
    assert [r.camera_id for r in mine] == [db.cam_id]
    fleet = query_events(db)                      # superuser path (no scope)
    assert len(fleet) == 2
    assert query_events(db, scope=set()) == []    # granted nothing → nothing


def test_can_access_event_mirrors_ownership(db):
    from types import SimpleNamespace

    from services.timeline_service import can_access_event
    row = _visit(db, start_min=1)
    owner_id = db.query(Camera).filter(Camera.id == db.cam_id).first().owner_id
    assert can_access_event(db, row, user=SimpleNamespace(id=owner_id, is_superuser=False))
    assert not can_access_event(db, row, user=SimpleNamespace(id=owner_id + 99, is_superuser=False))
    assert can_access_event(db, row, user=SimpleNamespace(id=0, is_superuser=True))


# ── ingest idempotency (uq_events_visit) ────────────────────────────

def test_duplicate_visit_rejected_by_unique_index(db):
    import pytest as _pytest
    from sqlalchemy.exc import IntegrityError

    _visit(db, start_min=5, end_min=7, track_id="9")
    with _pytest.raises(IntegrityError):
        _visit(db, start_min=5, end_min=7, track_id="9")
    db.rollback()


def test_null_track_ids_never_collide(db):
    # alarm/alert rows (no track) must not be blocked by the visit index
    _visit(db, start_min=5, track_id=None)
    _visit(db, start_min=5, track_id=None)   # no raise
    assert len(query_events(db)) == 2


# ── PR-C: plate enrichment ──────────────────────────────────────────

def test_plate_filter_substring_normalized(db):
    row = _visit(db, start_min=1, label="car", track_id="p1")
    row.plate_text = "KA01AB1234"
    db.commit()
    _visit(db, start_min=2, label="car", track_id="p2")
    assert [r.id for r in query_events(db, plate="ka01 ab")] == [row.id]
    assert [r.id for r in query_events(db, plate="1234")] == [row.id]
    assert query_events(db, plate="ZZ99") == []


def test_extract_plate_and_wants_plate():
    from services.plate_enrichment import extract_plate, wants_plate

    ok = {"result": {"plate_text": "ka 01 ab 1234", "accepted": True}}
    assert extract_plate(ok) == "KA01AB1234"
    assert extract_plate({"result": {"plate_text": "X", "accepted": False}}) is None
    assert extract_plate({"result": {}}) is None
    assert extract_plate(None) is None

    assert wants_plate("car", "ab/x.jpg") is True
    assert wants_plate("person", "ab/x.jpg") is False
    assert wants_plate("car", None) is False
    assert wants_plate("car", "ab/x.jpg", enabled=False) is False


# ── Partial plate reads (fragments) ────────────────────────────────
#
# A vehicle crop is the tracked box plus a margin, clamped to the frame,
# so a vehicle leaving frame yields a crop whose edge cuts the plate.
# fast_plate_ocr then reads the characters that SURVIVED and reports high
# confidence for them: "K884" (of "K884RS") scored 0.9835. Those landed in
# events.plate_text as if whole, so one Audi arrived as 66HH07, 66HH, H07
# and HHO7 — four identities, and the watchlist matched none of them.
# Confidence cannot separate partial from whole; the geometry can.

def _jpeg_of(width: int, height: int) -> bytes:
    """Smallest byte string with a readable SOF0 frame header."""
    return (bytes((0xFF, 0xD8, 0xFF, 0xC0, 0x00, 0x11, 0x08))
            + height.to_bytes(2, "big") + width.to_bytes(2, "big")
            + bytes(8))


def test_jpeg_dimensions_reads_the_sof_header():
    from services.plate_enrichment import jpeg_dimensions
    assert jpeg_dimensions(_jpeg_of(1077, 720)) == (1077, 720)
    assert jpeg_dimensions(_jpeg_of(1, 1)) == (1, 1)
    # "Cannot judge" cases must be None, never a guess.
    assert jpeg_dimensions(b"") is None
    assert jpeg_dimensions(b"not a jpeg at all") is None
    assert jpeg_dimensions(bytes((0xFF, 0xD8))) is None      # SOI, no frame
    assert jpeg_dimensions(None) is None


def test_plate_box_is_clipped_uses_measured_geometry():
    from services.plate_enrichment import plate_box_is_clipped
    # Real fragment: "K884" out of "K884RS", box flush with the right edge.
    assert plate_box_is_clipped([847, 463, 1076, 550], (1077, 720)) is True
    # Real whole read: "66HH07", 307 px clear of the nearest edge.
    assert plate_box_is_clipped([401, 307, 596, 369], (1035, 720)) is False
    # Every edge counts, not just the right one.
    assert plate_box_is_clipped([0, 100, 50, 150], (500, 500)) is True
    assert plate_box_is_clipped([100, 0, 150, 50], (500, 500)) is True
    assert plate_box_is_clipped([100, 100, 150, 500], (500, 500)) is True


def test_plate_box_is_clipped_never_invents_a_rejection():
    from services.plate_enrichment import plate_box_is_clipped
    good = [401, 307, 596, 369]
    assert plate_box_is_clipped(good, None) is False       # size unknown
    assert plate_box_is_clipped(None, (100, 100)) is False  # no box
    assert plate_box_is_clipped("nonsense", (100, 100)) is False
    assert plate_box_is_clipped([1, 2, 3], (100, 100)) is False
    assert plate_box_is_clipped(good, (0, 0)) is False


def test_extract_plate_rejects_a_clipped_read():
    from services.plate_enrichment import extract_plate
    clipped = {"result": {
        "plate_text": "K884", "confidence": 0.9835, "accepted": True,
        "plate_detection": {"found": True, "box": [847, 463, 1076, 550]},
    }}
    # High confidence and accepted=True — only the geometry says otherwise.
    assert extract_plate(clipped, image_size=(1077, 720)) is None
    # Without the crop size the check cannot run, so behaviour is unchanged.
    assert extract_plate(clipped) == "K884"


def test_extract_plate_keeps_a_whole_read():
    from services.plate_enrichment import extract_plate
    whole = {"result": {
        "plate_text": "66HH07", "confidence": 0.9993, "accepted": True,
        "plate_detection": {"found": True, "box": [401, 307, 596, 369]},
    }}
    assert extract_plate(whole, image_size=(1035, 720)) == "66HH07"
    # A response with no localisation block is unaffected by the guard.
    assert extract_plate(
        {"result": {"plate_text": "66HH07", "accepted": True}},
        image_size=(1035, 720),
    ) == "66HH07"
