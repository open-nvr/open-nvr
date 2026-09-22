# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The camera graph: who learns it, and what a relearn is allowed to do.

``learn_transitions`` had no caller outside its own tests, so
``camera_transitions`` was empty on every deployment that ever ran.
Nothing was broken — every cross-camera route simply fell back to "no
learned route between these cameras yet" and scored on time alone, which
is the weakest answer the feature can give.

These guard the three things that make a nightly relearn safe to leave
running unattended: that a route the site no longer supports stops being
asserted, that a scan which saw nothing at all destroys nothing, and
that a windowed scan never gets mistaken for an incremental one.
"""

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
os.environ.setdefault("DATABASE_URL", "sqlite:///./_journey_test.db")
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

from core.database import Base  # noqa: E402
from models import (  # noqa: E402
    Camera, CameraTransition, Role, TimelineEvent, User, VisitDescriptor,
)
from services.journey import learn_transitions  # noqa: E402

UTC = timezone.utc
WALL = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:", future=True,
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    session.add(Role(id=1, name="admin", description="test"))
    session.commit()
    session.add(User(id=1, username="owner", email="o@x.test",
                     hashed_password="x", is_active=True, role_id=1))
    session.commit()
    for cam_id, name in ((1, "Gate"), (2, "Yard"), (3, "Dock")):
        session.add(Camera(id=cam_id, name=name, ip_address=f"10.0.0.{cam_id}",
                           rtsp_url=f"rtsp://x/{cam_id}", owner_id=1))
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _trip(db, *, frm: int, to: int, plate: str, at_s: float, transit: float = 60.0):
    """One certain journey: the same plate on two cameras."""
    for camera_id, offset in ((frm, 0.0), (to, transit)):
        started = WALL + timedelta(seconds=at_s + offset)
        row = TimelineEvent(camera_id=camera_id, source="tier0",
                            event_type="track", label="car",
                            started_at=started,
                            ended_at=started + timedelta(seconds=5),
                            evidence_path="e.jpg")
        db.add(row)
        db.commit()
        db.refresh(row)
        db.add(VisitDescriptor(event_id=row.id, kind="plate", value=plate,
                               confidence=0.95,
                               source_task="license_plate_recognition",
                               source_adapter="test"))
        db.commit()


def _edges(db) -> set[tuple[int, int]]:
    return {(e.from_camera_id, e.to_camera_id)
            for e in db.query(CameraTransition).all()}


# ── a relearn is authoritative, not additive ─────────────────────────


def test_a_route_the_site_no_longer_supports_stops_being_asserted(db):
    """The table exists because a route "changes when a gate is closed or
    a camera is re-aimed". An append-only graph would keep scoring the
    closed gate as a plausible next hop forever."""
    for i in range(3):
        _trip(db, frm=1, to=2, plate=f"old{i}", at_s=i * 600)
    learn_transitions(db)
    assert _edges(db) == {(1, 2)}

    # The gate closes; traffic now goes 1 -> 3. Retention has taken the
    # old evidence with it, so nothing supports 1 -> 2 any more.
    db.query(VisitDescriptor).delete()
    db.query(TimelineEvent).delete()
    db.commit()
    for i in range(3):
        _trip(db, frm=1, to=3, plate=f"new{i}", at_s=i * 600)

    learn_transitions(db)
    assert _edges(db) == {(1, 3)}, "the closed route is still in the graph"


def test_a_scan_that_saw_nothing_destroys_nothing(db):
    """Zero anchors means LPR is off, an adapter is down, or the database
    is new — "could not check", not "no route exists". Wiping the graph
    on that reading is the same mistake as treating an unenriched visit
    as a mismatch."""
    for i in range(3):
        _trip(db, frm=1, to=2, plate=f"p{i}", at_s=i * 600)
    learn_transitions(db)
    assert _edges(db) == {(1, 2)}

    db.query(VisitDescriptor).delete()
    db.commit()

    assert learn_transitions(db) == 0
    assert _edges(db) == {(1, 2)}, (
        "a site that turned LPR off just lost its whole camera graph")


def test_a_windowed_scan_never_prunes(db):
    """A window cannot speak for what it did not look at."""
    for i in range(3):
        _trip(db, frm=1, to=2, plate=f"old{i}", at_s=0 + i * 60)
    learn_transitions(db)
    for i in range(3):
        _trip(db, frm=1, to=3, plate=f"new{i}", at_s=10_000 + i * 60)

    learn_transitions(db, since=WALL + timedelta(seconds=9_000))
    assert _edges(db) == {(1, 2), (1, 3)}, (
        "the windowed run pruned an edge outside its own window")


def test_a_windowed_scan_writes_the_windows_counts_not_a_total(db):
    """The footgun this documents: a nightly since=yesterday would
    overwrite every edge's samples with one day's worth."""
    for i in range(4):
        _trip(db, frm=1, to=2, plate=f"old{i}", at_s=i * 60)
    learn_transitions(db)
    assert db.query(CameraTransition).one().samples == 4

    for i in range(1):
        _trip(db, frm=1, to=2, plate=f"new{i}", at_s=10_000)
    learn_transitions(db, since=WALL + timedelta(seconds=9_000))
    assert db.query(CameraTransition).one().samples == 1, (
        "samples is the scan's count; this is why the scheduler runs full")


def test_a_relearned_edge_is_dated_by_the_relearn(db):
    """The column has a server default and no onupdate, so without an
    explicit write a relearned edge keeps reading as first-seen and
    nothing can tell a live route from a fossil."""
    for i in range(2):
        _trip(db, frm=1, to=2, plate=f"p{i}", at_s=i * 600)
    learn_transitions(db)

    edge = db.query(CameraTransition).one()
    edge.updated_at = datetime(2020, 1, 1, tzinfo=UTC)
    db.commit()

    learn_transitions(db)
    db.expire_all()
    refreshed = db.query(CameraTransition).one().updated_at
    assert refreshed.year > 2020, "a relearn left the edge dated 2020"


def test_the_median_and_spread_come_from_the_observed_trips(db):
    for i, transit in enumerate((40.0, 60.0, 200.0)):
        _trip(db, frm=1, to=2, plate=f"p{i}", at_s=i * 600, transit=transit)
    learn_transitions(db)

    edge = db.query(CameraTransition).one()
    assert edge.samples == 3
    assert edge.median_seconds == pytest.approx(60.0, abs=2)
    # The slow tail, so a legitimate dawdle is not scored as impossible.
    assert edge.p90_seconds >= edge.median_seconds


# ── somebody actually runs it ────────────────────────────────────────


def test_the_learner_is_started_at_boot():
    """The whole defect this closes: the function existed, was tested,
    and had no caller — so the graph was empty on every deployment."""
    src = (_HERE / "main.py").read_text()
    assert "from services.journey import learn_transitions" in src, (
        "nothing learns the camera graph, so every route falls back to "
        "time-only scoring")
    assert 'name="camera-graph-learning"' in src, (
        "a bare create_task is only weakly referenced and the GC really "
        "does kill those mid-flight")


def test_it_runs_full_scans_and_off_the_event_loop():
    src = (_HERE / "main.py").read_text()
    # Bounded by the spawn that ends the block, not by a character count
    # that silently stops testing anything when the code moves.
    start = src.index("async def background_transition_learning")
    block = src[start:src.index("spawn_background", start)]
    assert "asyncio.to_thread" in block, (
        "a synchronous scan of every plate and face claim would block "
        "the event loop, which is the rule the retention sweep follows")
    assert "learn_transitions(db)" in block
    assert "since=" not in block, (
        "a windowed relearn writes the window's counts over the totals "
        "and cannot prune — the scheduler must run full scans")


def test_the_setting_exists_and_defaults_on():
    """Unlike the enrichment backfill, this spends no inference at all —
    it is one scan of claims already stored — and the feature is at its
    worst without it."""
    from core.config import settings

    assert settings.journey_transition_learning is True
