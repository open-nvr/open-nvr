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

    owner_id = db.query(Camera).filter(Camera.id == db.cam_id).first().owner_id
    mine = query_events(db, owner_id=owner_id)
    assert [r.camera_id for r in mine] == [db.cam_id]
    fleet = query_events(db)                      # superuser path (no scope)
    assert len(fleet) == 2


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
