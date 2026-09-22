# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The back catalogue sweep — which visits it offers, and how it stops.

The two enrichers run off the INGEST path, so everything recorded before
they shipped has a row in ``events`` and nothing in ``event_text`` or
``visit_descriptors``. These guard the three things that make a sweep of
that history safe to leave running: that it offers exactly the visits the
live path would have enriched (same gate, same labels — never a second
copy), that it always moves forward so a visit which yields nothing
cannot stall the ones behind it, and that it stops.
"""

from __future__ import annotations

import asyncio
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
os.environ.setdefault("DATABASE_URL", "sqlite:///./_backfill_test.db")
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
from models import Camera, Role, TimelineEvent, User  # noqa: E402
from services import enrichment_backfill as bf  # noqa: E402

UTC = timezone.utc
WALL = datetime.now(UTC).replace(microsecond=0)


# ── fixtures ──────────────────────────────────────────────────────────


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
    yield session
    session.close()
    engine.dispose()


class _Shared:
    """One session standing in for SessionLocal.

    ``backfill_once`` opens and closes a session three times per pass on
    purpose (read short, call the adapter with none held, reopen to
    write). Closing the test's in-memory session between phases would
    drop the database, so close is the only call this intercepts.
    """

    def __init__(self, session):
        self._s = session

    def __getattr__(self, name):
        return getattr(self._s, name)

    def close(self):
        return None


@pytest.fixture()
def session_local(db, monkeypatch):
    import core.database

    monkeypatch.setattr(core.database, "SessionLocal", lambda: _Shared(db))
    return db


def _camera(db, cam_id: int, skills: list[str] | None = None) -> Camera:
    cam = Camera(id=cam_id, name=f"cam{cam_id}", ip_address=f"10.0.0.{cam_id}",
                 rtsp_url=f"rtsp://x/{cam_id}", owner_id=1,
                 assignments=[{"skill": s} for s in (skills or [])] or None)
    db.add(cam)
    db.commit()
    return cam


def _visit(db, *, event_id: int, camera_id: int, label: str,
           evidence: str | None = "cam1/2026/09/22/frame.jpg",
           minutes_ago: int = 0, plate: str | None = None) -> TimelineEvent:
    started = WALL - timedelta(minutes=minutes_ago)
    row = TimelineEvent(
        id=event_id, camera_id=camera_id, source="tier0", event_type="track",
        label=label, started_at=started,
        ended_at=started + timedelta(seconds=20), evidence_path=evidence,
        plate_text=plate,
    )
    db.add(row)
    db.commit()
    return row


class _Claim:
    model_fingerprint = None
    confidence = None
    source_adapter = "moondream"
    source_task = "vqa"

    def __init__(self, kind, value):
        self.kind, self.value = kind, value


def _raw_claim(db, row, kind: str, value: str) -> None:
    """A claim written the way the store held them BEFORE the projection
    existed — straight into the table, no words."""
    from models import VisitDescriptor

    db.add(VisitDescriptor(event_id=row.id, kind=kind, value=value,
                           source_task="vqa"))
    db.commit()


# ── which visits get offered ─────────────────────────────────────────


def test_the_walk_is_newest_first_and_the_cursor_is_exclusive(db):
    """Descending, because an operator searching 'last Tuesday' wants the
    last fortnight long before last spring — and retention is deleting
    the oldest rows anyway."""
    _camera(db, 1, ["image_captioning"])
    for i in (10, 11, 12, 13):
        _visit(db, event_id=i, camera_id=1, label="car")

    ids = [i["event_id"] for i in bf.plan_batch(db, None, 10)]
    assert ids == [13, 12, 11, 10]

    ids = [i["event_id"] for i in bf.plan_batch(db, 12, 10)]
    assert ids == [11, 10], "before_id must be exclusive or a pass repeats itself"

    assert len(bf.plan_batch(db, None, 2)) == 2


def test_the_gate_is_the_ingest_path_gate_not_a_copy(db):
    """Each camera is offered exactly the work its assignment bought.
    A second opinion here would make search answer differently for last
    week and last hour with nothing in the UI to explain it."""
    _camera(db, 1, [])                       # nothing assigned
    _camera(db, 2, ["image_captioning"])     # captions only
    _camera(db, 3, ["vqa"])                  # claims only
    _camera(db, 4, ["image_captioning", "vqa"])
    for cam in (1, 2, 3, 4):
        _visit(db, event_id=cam, camera_id=cam, label="car")

    by_id = {i["event_id"]: i for i in bf.plan_batch(db, None, 10)}
    assert (by_id[1]["caption"], by_id[1]["descriptors"]) == (False, False)
    assert (by_id[2]["caption"], by_id[2]["descriptors"]) == (True, False)
    assert (by_id[3]["caption"], by_id[3]["descriptors"]) == (False, True)
    assert (by_id[4]["caption"], by_id[4]["descriptors"]) == (True, True)


def test_a_person_is_captioned_but_never_questioned(db):
    """DESCRIBABLE_LABELS is vehicles only — clothing changes between
    visits in a way a vehicle's colour does not."""
    _camera(db, 1, ["image_captioning", "vqa"])
    _visit(db, event_id=1, camera_id=1, label="person")

    item = bf.plan_batch(db, None, 10)[0]
    assert item["caption"] is True
    assert item["descriptors"] is False


def test_labels_nobody_enriches_and_visits_without_evidence_are_not_offered(db):
    _camera(db, 1, ["image_captioning", "vqa"])
    _visit(db, event_id=1, camera_id=1, label="suitcase")
    _visit(db, event_id=2, camera_id=1, label="car", evidence=None)
    _visit(db, event_id=3, camera_id=1, label="car")

    assert [i["event_id"] for i in bf.plan_batch(db, None, 10)] == [3]


def test_the_label_set_is_read_from_the_enrichers(db):
    """Restating the labels here would leave history silently lagging the
    live path for exactly the class somebody just enabled."""
    from services.caption_enrichment import CAPTIONABLE_LABELS
    from services.descriptor_enrichment import DESCRIBABLE_LABELS

    assert bf._labels_of_interest() == set(CAPTIONABLE_LABELS) | set(DESCRIBABLE_LABELS)


def test_a_visit_whose_camera_is_gone_is_not_offered(db):
    """The gate is a property of the camera, so a visit with no camera
    row cannot be shown to have been assigned the skill. Failing closed,
    as wants_caption does for an unresolvable camera."""
    _camera(db, 1, ["image_captioning"])
    _visit(db, event_id=1, camera_id=1, label="car")
    _visit(db, event_id=2, camera_id=99, label="car")

    assert [i["event_id"] for i in bf.plan_batch(db, None, 10)] == [1]


# ── how a pass behaves ───────────────────────────────────────────────


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def calls(monkeypatch):
    """Record which enricher was asked about which visit."""
    seen: dict[str, list[int]] = {"caption": [], "descriptors": []}

    async def _cap(event_id, *a, **k):
        seen["caption"].append(int(event_id))

    async def _desc(event_id, *a, **k):
        seen["descriptors"].append(int(event_id))

    import services.caption_enrichment as cap_mod
    import services.descriptor_enrichment as desc_mod

    monkeypatch.setattr(cap_mod, "enrich_event_caption", _cap)
    monkeypatch.setattr(desc_mod, "enrich_event_descriptors", _desc)
    return seen


def test_a_pass_enriches_what_the_gate_allowed_and_advances_the_cursor(
        session_local, calls):
    db = session_local
    _camera(db, 1, ["image_captioning"])
    _camera(db, 2, ["vqa"])
    _visit(db, event_id=1, camera_id=1, label="car")
    _visit(db, event_id=2, camera_id=2, label="truck")

    state = _run(bf.backfill_once(batch=10, pause=0))

    assert calls["caption"] == [1], "only the camera assigned captioning"
    assert calls["descriptors"] == [2], "only the camera assigned vqa"
    assert state["cursor"] == 1, "the cursor is the LOWEST id examined"
    assert state["examined"] == 2


def test_a_second_pass_never_re_offers_a_visit(session_local, calls):
    """The enrichers skip what they have already done, but a visit they
    looked at and got nothing usable from still matches the SQL filter.
    Without the cursor the sweep would re-offer it forever and never
    reach the one behind it."""
    db = session_local
    _camera(db, 1, ["image_captioning"])
    for i in (1, 2, 3, 4):
        _visit(db, event_id=i, camera_id=1, label="car")

    _run(bf.backfill_once(batch=2, pause=0))
    assert calls["caption"] == [4, 3]

    _run(bf.backfill_once(batch=2, pause=0))
    assert calls["caption"] == [4, 3, 2, 1], "a pass must resume, not restart"

    state = _run(bf.backfill_once(batch=2, pause=0))
    assert state["done"] is True
    assert calls["caption"] == [4, 3, 2, 1], "no work after history runs out"


def test_a_finished_sweep_does_not_query_again(session_local, calls, monkeypatch):
    db = session_local
    _camera(db, 1, ["image_captioning"])
    _visit(db, event_id=1, camera_id=1, label="car")
    bf._write_state(db, {"done": True})

    def _boom(*a, **k):
        raise AssertionError("a finished sweep must not read the batch again")

    monkeypatch.setattr(bf, "plan_batch", _boom)
    assert _run(bf.backfill_once(batch=10, pause=0))["done"] is True
    assert calls["caption"] == []


def test_one_failing_visit_does_not_stall_the_ones_behind_it(
        session_local, calls, monkeypatch):
    """An unreachable adapter or an unreadable evidence file is a visit
    without words, not a stopped sweep — and it must not be retried
    forever at the cost of every visit behind it."""
    db = session_local
    _camera(db, 1, ["image_captioning"])
    for i in (1, 2, 3):
        _visit(db, event_id=i, camera_id=1, label="car")

    import services.caption_enrichment as cap_mod

    async def _explode(event_id, *a, **k):
        calls["caption"].append(int(event_id))
        raise RuntimeError("adapter unreachable")

    monkeypatch.setattr(cap_mod, "enrich_event_caption", _explode)

    state = _run(bf.backfill_once(batch=10, pause=0))
    assert calls["caption"] == [3, 2, 1], "the sweep carried on"
    assert state["cursor"] == 1, "and moved past the failure"


def test_an_enricher_switched_off_is_not_called(session_local, calls, monkeypatch):
    db = session_local
    _camera(db, 1, ["image_captioning", "vqa"])
    _visit(db, event_id=1, camera_id=1, label="car")

    from core.config import settings

    monkeypatch.setattr(settings, "events_caption_enrichment", False,
                        raising=False)
    _run(bf.backfill_once(batch=10, pause=0))

    assert calls["caption"] == []
    assert calls["descriptors"] == [1]
    # Still examined, so the cursor still advances — a disabled enricher
    # must not leave the sweep walking the same rows.
    from services import site_settings

    assert site_settings.get_json(db, bf.STATE_KEY)["cursor"] == 1


def test_the_loop_is_off_unless_an_operator_turns_it_on(session_local, calls,
                                                        monkeypatch):
    """The only enrichment path that defaults off: upgrading a running
    site must never quietly start working through its back catalogue on
    the GPU the live cameras are using."""
    db = session_local
    _camera(db, 1, ["image_captioning"])
    _visit(db, event_id=1, camera_id=1, label="car")

    from core.config import settings

    assert settings.events_enrichment_backfill is False
    _run(bf.run_backfill_loop(batch=10, pause=0, interval=0))
    assert calls["caption"] == []

    monkeypatch.setattr(settings, "events_enrichment_backfill", True,
                        raising=False)
    _run(bf.run_backfill_loop(batch=10, pause=0, interval=0))
    assert calls["caption"] == [1]


def test_the_loop_returns_when_history_is_done(session_local, calls, monkeypatch):
    """It is finite work, not a consumer — which is why main.py spawns it
    directly instead of supervising it with run_consumer_forever."""
    db = session_local
    _camera(db, 1, ["image_captioning"])
    for i in (1, 2, 3):
        _visit(db, event_id=i, camera_id=1, label="car")

    from core.config import settings

    monkeypatch.setattr(settings, "events_enrichment_backfill", True,
                        raising=False)
    _run(asyncio.wait_for(bf.run_backfill_loop(batch=1, pause=0, interval=0), 10))

    assert calls["caption"] == [3, 2, 1]
    from services import site_settings

    assert site_settings.get_json(db, bf.STATE_KEY)["done"] is True


def test_a_cursor_that_stops_moving_stops_the_loop(session_local, calls,
                                                   monkeypatch):
    """The cursor is the only thing guaranteeing forward progress. A loop
    that spins on the same batch forever would re-enrich those visits for
    the life of the process, so it stops and says so instead."""
    db = session_local
    _camera(db, 1, ["image_captioning"])
    _visit(db, event_id=1, camera_id=1, label="car")
    _visit(db, event_id=2, camera_id=1, label="car")

    # A walk that never gets past the newest visit — what an off-by-one
    # in the cursor filter would produce.
    monkeypatch.setattr(bf, "plan_batch",
                        lambda db, before, limit, people=False: [
                            {"event_id": 2, "caption": True, "descriptors": False}])

    from core.config import settings

    monkeypatch.setattr(settings, "events_enrichment_backfill", True,
                        raising=False)
    _run(asyncio.wait_for(bf.run_backfill_loop(batch=1, pause=0, interval=0), 10))

    assert calls["caption"] == [2, 2], (
        "it should notice on the second identical pass, not keep going")


def test_the_sweep_carries_the_people_gate_into_history(session_local):
    """Whatever the live path does to a person visit, the sweep must do
    to the back catalogue — and not a question more."""
    db = session_local
    _camera(db, 1, ["vqa"])
    _visit(db, event_id=1, camera_id=1, label="person")
    _visit(db, event_id=2, camera_id=1, label="car")

    off = {i["event_id"]: i for i in bf.plan_batch(db, None, 10)}
    assert off[1]["descriptors"] is False, "people are opt-in in history too"
    assert off[2]["descriptors"] is True

    on = {i["event_id"]: i for i in bf.plan_batch(db, None, 10, True)}
    assert on[1]["descriptors"] is True
    assert on[2]["descriptors"] is True


def test_a_pass_reads_the_people_setting(session_local, calls, monkeypatch):
    db = session_local
    _camera(db, 1, ["vqa"])
    _visit(db, event_id=1, camera_id=1, label="person")

    from core.config import settings

    _run(bf.backfill_once(batch=10, pause=0))
    assert calls["descriptors"] == [], "off by default, in history as well"

    bf._write_state(db, {})
    monkeypatch.setattr(settings, "events_descriptor_people", True,
                        raising=False)
    _run(bf.backfill_once(batch=10, pause=0))
    assert calls["descriptors"] == [1]


# ── the free repair ──────────────────────────────────────────────────


def test_claims_written_before_the_projection_existed_are_repaired(session_local,
                                                                   calls):
    """PR #508's claims are filterable and not findable: nothing wrote
    event_text.attributes then, and the enrichers short-circuit on a
    visit they have already handled, so only a repair reaches them."""
    db = session_local
    _camera(db, 1, [])                   # no assignment — and it must not matter
    row = _visit(db, event_id=1, camera_id=1, label="car")
    _raw_claim(db, row, "colour", "red")
    _raw_claim(db, row, "vehicle_type", "van")

    assert bf.plan_batch(db, None, 10)[0]["repair"] is True
    _run(bf.backfill_once(batch=10, pause=0))

    from models import EventText

    db.expire_all()
    assert db.get(EventText, row.id).attributes == "red van"
    assert calls["caption"] == [] and calls["descriptors"] == [], (
        "the repair spends no inference, so it is not gated by the "
        "assignment and must not call an enricher either")


def test_a_historical_plate_becomes_the_anchor_it_never_was(session_local):
    """The plate lived on the row as a column. journey.py's certain
    anchor reads descriptors, so every plate ever read was invisible to
    it."""
    db = session_local
    _camera(db, 1, [])
    row = _visit(db, event_id=1, camera_id=1, label="car", plate="AB12CDE")

    assert bf.plan_batch(db, None, 10)[0]["repair"] is True
    _run(bf.backfill_once(batch=10, pause=0))

    from models import EventText, VisitDescriptor

    db.expire_all()
    claim = (db.query(VisitDescriptor)
             .filter(VisitDescriptor.kind == "plate").one())
    assert claim.value == "ab12cde"
    assert db.get(EventText, row.id).attributes == "ab12cde"


def test_a_visit_already_projected_is_not_repaired_again(session_local):
    db = session_local
    _camera(db, 1, [])
    row = _visit(db, event_id=1, camera_id=1, label="car")

    from services.descriptor_store import apply_descriptors

    apply_descriptors(db, row, [_Claim("colour", "red")])
    db.commit()

    assert bf.plan_batch(db, None, 10)[0]["repair"] is False


def test_a_visit_with_neither_claims_nor_a_plate_is_not_repaired(session_local):
    db = session_local
    _camera(db, 1, ["image_captioning"])
    _visit(db, event_id=1, camera_id=1, label="car")

    assert bf.plan_batch(db, None, 10)[0]["repair"] is False


def test_the_sweep_runs_for_repairs_even_with_both_enrichers_off(session_local,
                                                                calls,
                                                                monkeypatch):
    """Plate reads are a third setting. A box with captions and
    descriptors off can still have a whole history of plates that never
    became claims."""
    db = session_local
    _camera(db, 1, [])
    _visit(db, event_id=1, camera_id=1, label="car", plate="AB12CDE")

    from core.config import settings

    monkeypatch.setattr(settings, "events_enrichment_backfill", True,
                        raising=False)
    monkeypatch.setattr(settings, "events_caption_enrichment", False,
                        raising=False)
    monkeypatch.setattr(settings, "events_descriptor_enrichment", False,
                        raising=False)
    _run(asyncio.wait_for(bf.run_backfill_loop(batch=10, pause=0, interval=0), 10))

    from models import VisitDescriptor

    db.expire_all()
    assert db.query(VisitDescriptor).filter(
        VisitDescriptor.kind == "plate").count() == 1
    assert calls["caption"] == [] and calls["descriptors"] == []


# ── wiring ───────────────────────────────────────────────────────────


def test_the_sweep_is_actually_started_at_boot():
    src = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    assert "from services.enrichment_backfill import run_backfill_loop" in src, (
        "nothing starts the sweep — history stays unenriched however the "
        "setting is set")
    assert 'name="enrichment-backfill"' in src, (
        "a bare create_task is only weakly referenced and the GC really "
        "does kill those mid-flight")
