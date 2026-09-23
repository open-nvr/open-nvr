# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Which visit is this frame looking at, and how sure are we (RFC-0003).

A frame-polling app has a camera, some bytes and an instant. It has no
``event_id``, so until now whatever it worked out from the frame had
nowhere to go — which is why smart-doorbell kept a parallel visit log,
why ``face_id`` had no producer, and why person journeys were
unreachable code.

The objection to closing the gap was never that matching is impossible.
It was smart-doorbell's, and it was right: a guessed identity written
into the shared store becomes indistinguishable, afterwards, from a
measured one. Indistinguishable is a property of the RECORD, so the
answer is to say which kind of match happened, every time.

What is pinned here is that distinction. ``window`` must only ever mean
a visit's own span contained the instant, ``nearest`` must be reachable
only when nothing did, and two candidates must bind nothing at all —
because a doorbell frame taken while two people are at the door does
not identify whose face it is, and choosing would be inventing a fact.
"""
from __future__ import annotations

import os
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("INTERNAL_API_KEY", "site_" + secrets.token_hex(16))
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_binding_test.db")

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.database import Base  # noqa: E402
from models import (Camera, Role, TimelineEvent, User,  # noqa: E402
                    VisitDescriptor)
from services.descriptor_store import BINDINGS, apply_descriptors  # noqa: E402
from services.timeline_service import resolve_visit  # noqa: E402

T0 = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", future=True, poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Role(id=1, name="admin", description="t"))
    s.commit()
    s.add(User(id=1, username="o", email="o@x.t", hashed_password="x",
               is_active=True, role_id=1))
    s.commit()
    for cid, name in ((1, "Door"), (2, "Gate")):
        s.add(Camera(id=cid, name=name, ip_address=f"10.0.0.{cid}",
                     rtsp_url=f"rtsp://x/{cid}", owner_id=1))
    s.commit()
    yield s
    s.close()


def _visit(db, *, cam=1, start_s=0, dur_s=30, label="person", ended=True):
    row = TimelineEvent(
        camera_id=cam, label=label, event_type="visit", source="tier0",
        started_at=T0 + timedelta(seconds=start_s),
        ended_at=(T0 + timedelta(seconds=start_s + dur_s)) if ended else None)
    db.add(row)
    db.commit()
    return row


# ── the three outcomes ───────────────────────────────────────────────


def test_an_instant_inside_a_visit_binds_by_window(db):
    """Not a guess. The span is core's own record of when that object
    was present, and the lookup is made by the component that owns it."""
    row = _visit(db, start_s=0, dur_s=30)
    got = resolve_visit(db, camera_id=1, at=T0 + timedelta(seconds=10))

    assert got["event_id"] == row.id
    assert got["binding"] == "window"


def test_an_instant_just_outside_binds_by_nearest_and_says_so(db):
    """This one IS a guess. It is allowed, and it is labelled, so a
    caller that must not act on a guess can refuse it in one check."""
    row = _visit(db, start_s=0, dur_s=30)
    got = resolve_visit(db, camera_id=1, at=T0 + timedelta(seconds=32))

    assert got["event_id"] == row.id
    assert got["binding"] == "nearest"


def test_an_instant_far_from_everything_binds_nothing(db):
    _visit(db, start_s=0, dur_s=30)
    got = resolve_visit(db, camera_id=1, at=T0 + timedelta(minutes=5))

    assert got["event_id"] is None
    assert got["binding"] is None
    assert got["reason"] == "no visit"


def test_two_visits_covering_the_instant_bind_nothing(db):
    """THE case worth being stubborn about.

    Two people at the door at once. The frame genuinely does not say
    whose face it is, and picking the longer or the newer visit would
    be inventing a fact that reads, afterwards, exactly like a measured
    one. Ambiguity is an answer.
    """
    a = _visit(db, start_s=0, dur_s=60)
    b = _visit(db, start_s=10, dur_s=60)
    got = resolve_visit(db, camera_id=1, at=T0 + timedelta(seconds=30))

    assert got["event_id"] is None, "it picked one of two candidates"
    assert got["reason"] == "ambiguous"
    assert got["candidates"] == sorted([a.id, b.id])


def test_two_equally_near_visits_also_bind_nothing(db):
    """The same refusal one step out. A tie between two guesses is not
    a better guess."""
    a = _visit(db, start_s=0, dur_s=10)      # ends at +10
    b = _visit(db, start_s=14, dur_s=10)     # starts at +14
    got = resolve_visit(db, camera_id=1, at=T0 + timedelta(seconds=12))

    assert got["event_id"] is None
    assert got["reason"] == "ambiguous"
    assert got["candidates"] == sorted([a.id, b.id])


# ── the boundaries ───────────────────────────────────────────────────


def test_an_open_visit_can_be_bound(db):
    """A visit still in progress has no ``ended_at``. A doorbell frame
    is taken DURING the visit, so if an open visit could not bind, the
    common case would be the broken one."""
    row = _visit(db, start_s=0, ended=False)
    got = resolve_visit(db, camera_id=1, at=T0 + timedelta(seconds=5))

    assert got["event_id"] == row.id
    assert got["binding"] == "window"


def test_tolerance_is_a_boundary_not_a_suggestion(db):
    _visit(db, start_s=0, dur_s=10)
    inside = resolve_visit(db, camera_id=1, at=T0 + timedelta(seconds=14),
                           tolerance_s=5)
    outside = resolve_visit(db, camera_id=1, at=T0 + timedelta(seconds=20),
                            tolerance_s=5)

    assert inside["binding"] == "nearest"
    assert outside["event_id"] is None


def test_zero_tolerance_allows_only_window(db):
    """An app that refuses to guess can say so with the tolerance
    rather than by inspecting the answer afterwards."""
    _visit(db, start_s=0, dur_s=10)
    got = resolve_visit(db, camera_id=1, at=T0 + timedelta(seconds=11),
                        tolerance_s=0)
    assert got["event_id"] is None


def test_another_camera_is_never_bound(db):
    """The visit is on the Gate; the frame came from the Door."""
    _visit(db, cam=2, start_s=0, dur_s=30)
    got = resolve_visit(db, camera_id=1, at=T0 + timedelta(seconds=10))
    assert got["event_id"] is None


def test_the_scope_is_honoured(db):
    """A camera outside the caller's roster binds nothing, even though
    the visit is there and the instant is inside it."""
    _visit(db, cam=1, start_s=0, dur_s=30)
    got = resolve_visit(db, camera_id=1, at=T0 + timedelta(seconds=10),
                        scope={2})
    assert got["event_id"] is None


def test_a_label_narrows_the_candidates(db):
    """How a face app avoids binding to the delivery van parked behind
    the visitor — and how two overlapping visits of DIFFERENT kinds
    stop being ambiguous."""
    person = _visit(db, start_s=0, dur_s=60, label="person")
    _visit(db, start_s=0, dur_s=60, label="car")

    unfiltered = resolve_visit(db, camera_id=1, at=T0 + timedelta(seconds=30))
    assert unfiltered["reason"] == "ambiguous"

    got = resolve_visit(db, camera_id=1, at=T0 + timedelta(seconds=30),
                        label="person")
    assert got["event_id"] == person.id
    assert got["binding"] == "window"


def test_a_naive_instant_is_read_as_utc(db):
    """Callers hand us whatever their clock produced. A naive value
    compared against an aware span raises, and a resolver that raises
    takes down whatever was asking."""
    row = _visit(db, start_s=0, dur_s=30)
    got = resolve_visit(db, camera_id=1,
                        at=(T0 + timedelta(seconds=10)).replace(tzinfo=None))
    assert got["event_id"] == row.id


# ── the column ───────────────────────────────────────────────────────


class _Claim:
    def __init__(self, kind, value, **kw):
        self.kind, self.value = kind, value
        self.confidence = kw.get("confidence")
        self.source_task = kw.get("source_task")
        self.source_adapter = kw.get("source_adapter")
        self.model_fingerprint = kw.get("model_fingerprint")


def test_a_claim_records_how_its_subject_was_bound(db):
    row = _visit(db)
    apply_descriptors(db, row, [_Claim("face_id", "Priya", source_task="face")],
                      binding="nearest")
    db.commit()

    got = db.query(VisitDescriptor).one()
    assert got.binding == "nearest"


def test_binding_defaults_to_direct(db):
    """Every writer before RFC-0003 held the event_id already, so the
    default is not a fallback — it is what they all were."""
    row = _visit(db)
    apply_descriptors(db, row, [_Claim("colour", "red", source_task="vqa")])
    db.commit()
    assert db.query(VisitDescriptor).one().binding == "direct"


def test_a_rerun_replaces_the_binding_too(db):
    """A doorbell that first guessed by timestamp and later matched a
    real visit must not leave the old ``nearest`` behind — the row
    would understate what is now known."""
    row = _visit(db)
    apply_descriptors(db, row, [_Claim("face_id", "Priya", source_task="face")],
                      binding="nearest")
    db.commit()
    apply_descriptors(db, row, [_Claim("face_id", "Priya", source_task="face")],
                      binding="window")
    db.commit()

    assert db.query(VisitDescriptor).one().binding == "window"


def test_an_unknown_binding_is_refused(db):
    """Not silently coerced to 'direct'. A caller passing something
    this does not understand has a bug, and defaulting it would write
    the MOST trusted value for a claim nobody can vouch for."""
    row = _visit(db)
    with pytest.raises(ValueError, match="binding must be"):
        apply_descriptors(db, row, [_Claim("colour", "red")],
                          binding="probably")


def test_there_is_no_binding_for_we_chose_between_two():
    """The vocabulary is closed on purpose. If a future caller wants a
    'guessed between candidates' value, that is a design conversation,
    not a string it can invent — the whole point is that a reader knows
    what each value means."""
    assert BINDINGS == {"direct", "window", "nearest"}


# ── an app may only claim about cameras it was given ─────────────────


def test_an_app_cannot_attach_a_claim_to_a_camera_it_does_not_hold(db,
                                                                   monkeypatch):
    """The hole RFC-0003's write path was modelled on.

    ``POST /internal/camera-agent/events/descriptors`` was written for
    core's own enricher and the camera-agent — unscoped platform
    components holding the site key. App keys later became valid on it
    too, which left any installed app able to attach a claim to any
    visit on the site. A claim can be a NAME, so this was a worse hole
    than any read, and it was open because the scoping lives one layer
    up in the app routes and nobody carried it down.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.config import settings
    from core.database import get_db
    from routers import internal_camera_agent as ica

    mine = _visit(db, cam=1)
    theirs = _visit(db, cam=2)

    app = FastAPI()
    app.include_router(ica.router)
    app.dependency_overrides[get_db] = lambda: db
    # An app holding camera 1 and nothing else.
    monkeypatch.setattr(ica, "_app_roster", lambda _db, _p: {1})
    client = TestClient(app)
    headers = {"X-Internal-Api-Key": settings.internal_api_key}

    ok = client.post("/internal/camera-agent/events/descriptors", headers=headers, json={
        "event_id": mine.id,
        "descriptors": [{"kind": "face_id", "value": "Priya"}]})
    assert ok.status_code == 200, ok.text

    refused = client.post("/internal/camera-agent/events/descriptors", headers=headers, json={
        "event_id": theirs.id,
        "descriptors": [{"kind": "face_id", "value": "Priya"}]})
    # 404, not 403: whether a visit exists on a camera this app does not
    # hold is not this app's business either.
    assert refused.status_code == 404, (
        "an app attached a name to a visit on a camera it was never given")

    names = [d.value for d in db.query(VisitDescriptor).all()]
    assert names == ["priya"], "the refused claim was written anyway"
