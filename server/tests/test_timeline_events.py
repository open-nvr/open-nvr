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
from services.timeline_service import (  # noqa: E402
    count_events, query_events, record_track_visit,
)


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


def _camera(db, name):
    """A second camera, for the scope and tie-ordering cases."""
    cam = Camera(name=name, ip_address="10.0.0.10", port=80,
                 owner_id=db.query(User).first().id)
    db.add(cam)
    db.commit()
    db.refresh(cam)
    return cam.id


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

    # The assignment gate. A vehicle with evidence is NOT enough — the
    # camera has to carry the plate skill, or a thirty-camera site pays
    # OCR thirty times over to watch one gate.
    lpr = {"license_plate_recognition"}
    assert wants_plate("car", "ab/x.jpg", True, lpr) is True
    assert wants_plate("person", "ab/x.jpg", True, lpr) is False
    assert wants_plate("car", None, True, lpr) is False
    assert wants_plate("car", "ab/x.jpg", False, lpr) is False

    # Unassigned, or assigned to something else, or the caller could not
    # resolve the camera at all: no read. Failing CLOSED is deliberate —
    # a missed read on a misconfigured camera is visible and fixable,
    # silently reading plates nobody asked for is neither.
    assert wants_plate("car", "ab/x.jpg", True, set()) is False
    assert wants_plate("car", "ab/x.jpg", True, {"object_detection"}) is False
    assert wants_plate("car", "ab/x.jpg", True, None) is False


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


# ── paging (#451 follow-up) ─────────────────────────────────────────
#
# The rule these pin: walking `skip` through a result set must visit
# every row exactly once. That is only true if the ordering is a TOTAL
# order, which is why the `id` tiebreaker exists.


def test_skip_walks_pages_without_gaps_or_repeats(db):
    for m in range(10):
        _visit(db, start_min=m)
    whole = [r.id for r in query_events(db, limit=100)]
    paged = []
    for skip in (0, 4, 8):
        paged += [r.id for r in query_events(db, limit=4, skip=skip)]
    # List equality, not set equality: this catches a page boundary that
    # repeats a row as well as one that drops it.
    assert paged == whole


def test_ties_on_started_at_page_stably(db):
    """Six visits sharing ONE timestamp across two cameras and three
    tracks — permitted by uq_events_visit (camera, track, start).

    Without the id tiebreaker the database is free to return these in a
    different order per query, so paging would repeat and drop rows at
    random. This test is flaky by construction against the old ordering,
    which is exactly the bug it guards.
    """
    other = _camera(db, "second")
    for i in range(3):
        _visit(db, start_min=0, track_id=f"a{i}")
        _visit(db, start_min=0, cam=other, track_id=f"b{i}")
    whole = [r.id for r in query_events(db, limit=100)]
    assert len(whole) == 6
    paged = []
    for skip in (0, 2, 4):
        paged += [r.id for r in query_events(db, limit=2, skip=skip)]
    assert paged == whole
    assert whole == sorted(whole, reverse=True)   # id DESC within the tie


def test_skip_past_the_end_is_empty(db):
    _visit(db, start_min=1)
    assert query_events(db, skip=999) == []


def test_count_matches_the_rows_it_pages(db):
    for m in range(6):
        _visit(db, start_min=m, label="car" if m % 2 else "person")
    for filters in ({}, {"label": "car"}, {"label": "person"}):
        assert count_events(db, **filters) == len(
            query_events(db, limit=500, **filters))


def test_count_is_scoped_and_does_not_leak_other_cameras(db):
    """The security case: a total that ignores scope tells an operator
    how many rows exist on cameras they cannot open."""
    other = _camera(db, "not-mine")
    for m in range(3):
        _visit(db, start_min=m)
    for m in range(7):
        _visit(db, start_min=m, cam=other)
    assert count_events(db, scope={db.cam_id}) == 3
    assert count_events(db) == 10                 # superuser: unrestricted
    assert count_events(db, scope=set()) == 0     # granted nothing: nothing


# ── the HTTP envelope (nothing pinned it before) ────────────────────


@pytest.fixture
def api():
    """Just the timeline router on a bare app — enough to pin the
    response envelope, which nothing did before.

    Its own engine rather than the `db` fixture's: TestClient serves the
    request on another thread, and a plain sqlite :memory: connection
    refuses to cross one. StaticPool keeps every thread on the ONE
    connection, which is also what keeps the in-memory schema alive.

    Yields (client, session) so a test can seed rows the route will see.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import StaticPool

    import core.auth as auth_mod
    from core.database import get_db
    from routers.timeline_events import router

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    role = Role(name="admin")
    s.add(role)
    s.commit()
    owner = User(username="t", email="t@t.io", hashed_password="x",
                 role_id=role.id)
    s.add(owner)
    s.commit()
    cam = Camera(name="gate", ip_address="10.0.0.9", port=80,
                 owner_id=owner.id)
    s.add(cam)
    s.commit()
    s.refresh(cam)
    s.cam_id = cam.id

    def _fake_db():
        # A generator FUNCTION, not a lambda returning an iterator —
        # FastAPI only unwraps the former as a dependency.
        yield s

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[auth_mod.get_current_active_user] = lambda: owner
    try:
        yield TestClient(app), s
    finally:
        s.close()


def test_response_carries_page_length_and_total_separately(api):
    client, db = api
    for m in range(7):
        _visit(db, start_min=m)
    r = client.get("/events", params={"limit": 3})
    assert r.status_code == 200
    body = r.json()
    # `count` is the PAGE length and `total` the whole set. Existing
    # clients read neither, but `count` predates this work and stays.
    assert body["count"] == len(body["events"]) == 3
    assert body["total"] == 7


def test_a_short_page_reports_an_exact_total(api):
    client, db = api
    for m in range(3):
        _visit(db, start_min=m)
    body = client.get("/events", params={"limit": 25}).json()
    assert body["count"] == 3 and body["total"] == 3


def test_paging_past_the_end_reports_the_true_total(api):
    """The resolve_total trap, through the route: an empty page at a big
    skip must not report the skip as the total."""
    client, db = api
    for m in range(4):
        _visit(db, start_min=m)
    body = client.get("/events", params={"skip": 100, "limit": 25}).json()
    assert body["events"] == [] and body["count"] == 0
    assert body["total"] == 4


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": 501}, {"skip": -1},
])
def test_out_of_range_paging_is_rejected(api, params):
    """Previously `limit=1000` was silently trimmed to 500 — harmless
    until skip existed, then it steps over rows 500-999 in silence."""
    client, _db = api
    assert client.get("/events", params=params).status_code == 422
