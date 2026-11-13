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
from services.timeline_service import (TRACK, record_track_visit,  # noqa: E402
                                       resolve_visit)

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
    """A visit, written by THE REAL PRODUCER.

    This used to build a TimelineEvent by hand with
    ``event_type="visit"``. Nothing in production writes that value —
    ``record_track_visit`` writes ``"track"`` — so the fixture was the
    only source of the thing ``resolve_visit`` filtered on, and the
    whole suite proved the function worked on data the system cannot
    produce. In production every bind returned "no visit".

    Going through the producer is what makes these tests mean anything:
    a test that builds its own rows can only ever agree with itself.
    """
    return record_track_visit(
        db, camera_id=cam, label=label,
        started_at=T0 + timedelta(seconds=start_s),
        ended_at=(T0 + timedelta(seconds=start_s + dur_s)) if ended else None)


def test_the_producer_and_the_resolver_agree_on_event_type(db):
    """The guard for the bug above, stated directly.

    record_track_visit's value and resolve_visit's filter are the same
    constant now. This fails if anyone splits them again — which is how
    RFC-0003 shipped an entire feature that could never fire.
    """
    row = record_track_visit(db, camera_id=1, label="person",
                             started_at=T0,
                             ended_at=T0 + timedelta(seconds=30))

    assert row.event_type == TRACK
    got = resolve_visit(db, camera_id=1, at=T0 + timedelta(seconds=10))
    assert got["event_id"] == row.id, (
        f"a visit written by record_track_visit (event_type="
        f"{row.event_type!r}) did not bind — the producer and the "
        f"resolver disagree about what a visit is")


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


# ── the busy camera ──────────────────────────────────────────────────
#
# Containment used to be decided in Python over the 64 most recent
# visits that STARTED at or before the instant (plus the tolerance).
# That is a SILENT CEILING: on a camera where many short visits begin
# and finish while one long visit is still running, the long one is the
# OLDEST of the candidates and falls off the end of the page. The
# function then says "no visit covered the instant" — which the caller
# reads as a true miss and turns into a `nearest` guess, or into no
# claim at all.
#
# So it failed worst on the cameras with the most traffic, and it
# failed quietly. These pin that containment is a SQL predicate now, so
# the answer does not depend on how many other visits happen to be
# nearby.
#
# The shape matters: the crowd has to START BEFORE the instant and END
# BEFORE it. A crowd that starts afterwards was already excluded by the
# tolerance filter and never reached the ceiling at all — which is how
# a first draft of these tests passed against the bug they were written
# to catch.

#: Comfortably more than the 64-row page the old implementation read.
_CROWD = 90


def _crowd_of_finished_visits(db, *, before, count=_CROWD):
    """``count`` short visits, all starting and ending before ``before``.

    Every one of them is NEWER than a long visit that began earlier, so
    an ORDER BY start DESC puts the whole crowd ahead of the visit that
    actually covers the instant.
    """
    for i in range(count):
        _visit(db, start_s=10 + i, dur_s=1)


def test_a_covering_visit_is_found_behind_a_crowd_of_newer_ones(db):
    """The one that was broken.

    One long visit is in progress for ten minutes. Ninety short ones
    start and finish inside it, all before the instant in question. Only
    the long visit's span contains that instant, so it is the only
    correct answer — but it is also the oldest candidate, and the old
    query never read far enough back to see it.
    """
    long_visit = _visit(db, start_s=0, dur_s=600)
    _crowd_of_finished_visits(db, before=None)
    at = T0 + timedelta(seconds=200)   # after the crowd, inside the long visit

    got = resolve_visit(db, camera_id=1, at=at)

    assert got["event_id"] == long_visit.id, (
        "the covering visit was hidden behind newer, finished ones — "
        "containment is being decided over a page of rows again")
    assert got["binding"] == "window"


def test_a_crowd_does_not_turn_a_window_binding_into_a_nearest_guess(db):
    """The consequence, stated the way the caller meets it.

    This is the failure that actually reaches a person: a measured
    binding degrading into a guess, or into silence. `nearest` is honest
    about being a guess, which is exactly why it must not appear when
    the store knows the answer.
    """
    _visit(db, start_s=0, dur_s=600)
    _crowd_of_finished_visits(db, before=None)
    at = T0 + timedelta(seconds=200)

    got = resolve_visit(db, camera_id=1, at=at)

    assert got["binding"] == "window", (
        f"a covering visit was reported as {got['binding']!r} — the store "
        f"knew the answer and the caller was handed a guess")


def test_the_crowd_does_not_have_to_be_finished_to_hide_it(db):
    """The same ceiling with overlapping, still-open visits.

    Ninety visits that started before the instant and have not ended are
    all containing — genuinely ambiguous. The point here is that the
    refusal is reached by the predicate, not by whatever happened to fit
    on one page.
    """
    _visit(db, start_s=0, dur_s=600)
    for i in range(_CROWD):
        _visit(db, start_s=10 + i, dur_s=0, ended=False)
    at = T0 + timedelta(seconds=200)

    got = resolve_visit(db, camera_id=1, at=at)

    assert got["event_id"] is None
    assert got["reason"] == "ambiguous"


def test_ambiguity_still_refuses_when_the_crowd_really_does_overlap(db):
    """The ceiling fix must not buy recall by dropping the refusal.

    Twenty visits genuinely contain the instant. The answer is still
    nothing — and the reply names candidates, so an operator can see
    what it was torn between rather than reading a bare refusal.
    """
    at = T0 + timedelta(seconds=30)
    for i in range(20):
        _visit(db, start_s=i, dur_s=120)

    got = resolve_visit(db, camera_id=1, at=at)

    assert got["event_id"] is None
    assert got["binding"] is None
    assert got["reason"] == "ambiguous"
    assert len(got["candidates"]) > 1, "ambiguous with nothing to point at"


def test_the_nearest_arm_finds_the_closest_on_either_side(db):
    """A guard on the new two-arm query, not a reproduction.

    `nearest` is now answered by two bounded queries — the last visit to
    END before the instant, and the first to START after it — merged and
    compared by gap. This pins that both sides are actually consulted
    and that the comparison is by GAP, so a nearer earlier visit beats a
    further later one.
    """
    just_before = _visit(db, start_s=0, dur_s=9)      # ends at T0+9
    _visit(db, start_s=14, dur_s=30)                  # starts at T0+14
    at = T0 + timedelta(seconds=10)

    got = resolve_visit(db, camera_id=1, at=at)

    assert got["event_id"] == just_before.id
    assert got["binding"] == "nearest"


# ── the tolerance as a number, not as a wrong name ───────────────────
#
# DEFAULT_BIND_TOLERANCE_S is a tuning constant. Too wide, and a frame
# gets attached to the visitor before or after the one it belongs to —
# which, when the claim is a face_id, is a NAME on the wrong person.
#
# Until now the only way to find that out was for someone to notice a
# wrong name, which requires an operator who knows the visitor, looking
# at the right record, at the right time. These make it visible as a
# shape in a graph long before that: `nearest` is an admitted guess, so
# its share climbing means frames are arriving further from the visits
# they belong to, and the GAP histogram says how much further.


def _metric_value(counter, **labels) -> float:
    key = tuple(str(labels.get(n, "")) for n in counter.labelnames)
    return counter._values.get(key, 0.0)


def test_each_binding_outcome_is_counted(db):
    from services import search_metrics as m

    before = {o: _metric_value(m.BINDINGS, outcome=o)
              for o in ("window", "nearest", "ambiguous", "none")}

    _visit(db, start_s=0, dur_s=30)
    resolve_visit(db, camera_id=1, at=T0 + timedelta(seconds=10))   # window
    resolve_visit(db, camera_id=1, at=T0 + timedelta(seconds=32))   # nearest
    resolve_visit(db, camera_id=1, at=T0 + timedelta(minutes=9))    # none
    _visit(db, start_s=0, dur_s=30)
    resolve_visit(db, camera_id=1, at=T0 + timedelta(seconds=10))   # ambiguous

    for outcome in ("window", "nearest", "ambiguous", "none"):
        assert _metric_value(m.BINDINGS, outcome=outcome) == before[outcome] + 1, (
            f"{outcome} bindings are not being counted")


def test_the_gap_is_recorded_for_a_nearest_binding(db):
    """The share alone is not enough. A stable `nearest` rate whose gaps
    are creeping towards the tolerance is about to stop binding at all —
    and that symptom looks like "nothing found", not like "wrong"."""
    from services import search_metrics as m

    before = m.BIND_GAP._values.get((), {}).get("n", 0)

    _visit(db, start_s=0, dur_s=30)
    got = resolve_visit(db, camera_id=1, at=T0 + timedelta(seconds=32))

    assert got["binding"] == "nearest"
    st = m.BIND_GAP._values.get((), {})
    assert st.get("n", 0) == before + 1, "the gap behind a guess went unrecorded"


def test_a_window_binding_records_no_gap(db):
    """There is no gap — the instant was inside the visit. Recording a
    zero would put measured bindings into a histogram that exists to
    describe guesses, and drag its distribution towards a comfortable
    answer."""
    from services import search_metrics as m

    before = m.BIND_GAP._values.get((), {}).get("n", 0)

    _visit(db, start_s=0, dur_s=30)
    resolve_visit(db, camera_id=1, at=T0 + timedelta(seconds=10))

    assert m.BIND_GAP._values.get((), {}).get("n", 0) == before


def test_a_broken_metrics_collector_cannot_stop_a_binding(db, monkeypatch):
    """Metrics observe the system; they are not part of it. A doorbell
    must not fail to attach a name because a counter raised."""
    from services import search_metrics as m

    def boom(*a, **kw):
        raise RuntimeError("collector down")

    monkeypatch.setattr(m.BINDINGS, "inc", boom)
    row = _visit(db, start_s=0, dur_s=30)

    got = resolve_visit(db, camera_id=1, at=T0 + timedelta(seconds=10))

    assert got["event_id"] == row.id
    assert got["binding"] == "window"


def test_a_written_claim_records_how_its_subject_was_bound(db):
    """The other half, and the one that matters most. The counter above
    counts QUESTIONS ASKED; this counts FACTS RECORDED. A face_id
    written against a `nearest` binding is a name on a guessed subject,
    and it is countable here."""
    from services import search_metrics as m

    row = _visit(db, start_s=0, dur_s=30)
    before = _metric_value(m.DESCRIPTORS_WRITTEN, kind="face_id",
                           task="face", adapter="unknown", binding="nearest")

    apply_descriptors(db, row, [_Claim("face_id", "priya", source_task="face")],
                      binding="nearest")

    assert _metric_value(m.DESCRIPTORS_WRITTEN, kind="face_id", task="face",
                         adapter="unknown", binding="nearest") == before + 1, (
        "the binding label is being dropped again — see "
        "tests/test_metric_labels_are_declared.py")


# ── the span is the VISIT's, not the plate read's ────────────────────
#
# SEEN_AT is coalesce(observed_at, started_at), and observed_at is the
# capture time of the look a PLATE READ won on — normally later than the
# visit's start. Containment used it as the start while using the raw
# ended_at as the end, which made the effective span strictly narrower
# than the real visit.
#
# No fixture set observed_at, so every existing test exercised the
# degenerate case where coalesce falls through to started_at.


def _read_at(db, row, offset_s):
    """Give ``row`` a plate read at T0+offset, as OCR enrichment does."""
    row.observed_at = T0 + timedelta(seconds=offset_s)
    row.plate_text = "KA01AB1234"
    db.commit()
    return row


def test_a_late_plate_read_does_not_shrink_the_visit(db):
    """A visit runs 0-30s and its plate is read at +10s. An instant at
    +2s is plainly inside the visit and must bind by `window`."""
    row = _visit(db, start_s=0, dur_s=30, label="car")
    _read_at(db, row, 10)

    got = resolve_visit(db, camera_id=1, at=T0 + timedelta(seconds=2))

    assert got["event_id"] == row.id, (
        "the plate-read time was used as the visit's start — the span "
        "was shrunk to observed_at..ended_at")
    assert got["binding"] == "window", (
        f"a lookup inside a visit was reported as {got['binding']!r}")


def test_a_late_read_does_not_turn_a_lookup_into_a_guess(db):
    """The degradation this produces in practice: `window` becoming
    `nearest`, which is the exact signal the binding metrics were added
    to detect — manufactured by the function being measured."""
    row = _visit(db, start_s=0, dur_s=30, label="car")
    _read_at(db, row, 10)

    got = resolve_visit(db, camera_id=1, at=T0 + timedelta(seconds=6))

    assert got["binding"] == "window"


def test_a_read_after_the_visit_ended_still_binds(db):
    """The worst case of the same bug. With observed_at after ended_at,
    `SEEN_AT <= at AND ended_at >= at` is unsatisfiable together, so the
    visit could never bind at ANY instant."""
    row = _visit(db, start_s=0, dur_s=30, label="car")
    _read_at(db, row, 40)          # read lands after the track closed

    got = resolve_visit(db, camera_id=1, at=T0 + timedelta(seconds=15))

    assert got["event_id"] == row.id, (
        "a visit whose plate was read after it ended became unbindable")


def test_one_row_cannot_be_its_own_ambiguity(db):
    """The two near-arms keyed off different columns — `before` on
    ended_at, `after` on SEEN_AT — so a row that ended before the
    instant and was READ after it satisfied both. It appeared twice in
    `near` with identical gaps, tripped the equally-near tie check, and
    came back `ambiguous` with the same id listed twice."""
    row = _visit(db, start_s=0, dur_s=1, label="car")     # ends at T0+1
    _read_at(db, row, 4)                                  # read at T0+4
    at = T0 + timedelta(seconds=2)

    got = resolve_visit(db, camera_id=1, at=at)

    assert got["reason"] != "ambiguous", (
        f"a single candidate was reported ambiguous with itself: "
        f"{got.get('candidates')}")
    assert got["event_id"] == row.id
    assert got["binding"] == "nearest"


# ── an open visit is not zero seconds away from everything ───────────


def test_an_open_visit_that_starts_later_does_not_score_a_zero_gap(db):
    """`_gap_to` took min(|at-start|, |at-end|) and treated an open
    visit's end as `at` itself — so the second term was exactly 0.0 and
    EVERY open visit scored a perfect gap however far away it began.

    Here a closed visit ends half a second before the instant and an
    open one starts four seconds after it. The closed one is nearer.
    """
    closed = _visit(db, start_s=0, dur_s=2)               # ends at T0+2
    _visit(db, start_s=6, dur_s=0, ended=False)           # opens at T0+6
    at = T0 + timedelta(seconds=3)

    got = resolve_visit(db, camera_id=1, at=at)

    assert got["event_id"] == closed.id, (
        "an open visit that had not started yet won the nearest race "
        "with a gap of 0.0s")
    assert "1.0s" in got["reason"] or "nearest within 1" in got["reason"], (
        f"the reported gap is wrong: {got['reason']!r}")


def test_two_open_visits_starting_later_are_not_a_fake_tie(db):
    """The same zero-gap bug turning a resolvable instant into a
    refusal: two open visits both scoring 0.0 trip the tie check."""
    _visit(db, start_s=2, dur_s=0, ended=False)
    _visit(db, start_s=4, dur_s=0, ended=False)
    at = T0

    got = resolve_visit(db, camera_id=1, at=at)

    assert got["reason"] != "ambiguous", (
        "two open visits at different distances were called equally near")
    assert got["binding"] == "nearest"
