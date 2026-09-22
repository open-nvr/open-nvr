# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Claims become words, and the plate becomes a claim.

Two halves of the store that were never joined. ``visit_descriptors``
held what the skills said; ``event_text.attributes`` held the words
search matches — and nothing had ever written the second from the first,
so a visit could carry ``colour=red`` and ``vehicle_type=van`` and still
not answer "red van". Separately, the plate was the one thing the box
reads perfectly and the only skill output that never became a
descriptor, which left ``journey.py``'s certain anchor
(``ANCHOR_KINDS = ("plate", "face_id")``) with nothing to anchor on.

The payoff test at the bottom is the one that matters: a free-text
search matching a visit nobody captioned.
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
os.environ.setdefault("DATABASE_URL", "sqlite:///./_projection_test.db")
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
    Camera, EventText, Role, TimelineEvent, User, VisitDescriptor,
)
from services.descriptor_store import (  # noqa: E402
    PLATE_TASK, apply_descriptors, project_attributes, sync_plate_claim,
)

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
    session.add(Camera(id=1, name="Gate", ip_address="10.0.0.1",
                       rtsp_url="rtsp://x/1", owner_id=1))
    session.commit()
    yield session
    session.close()
    engine.dispose()


class _Claim:
    """What an enricher hands ``apply_descriptors``."""

    model_fingerprint = None

    def __init__(self, kind, value, confidence=None, task="vqa",
                 adapter="moondream"):
        self.kind, self.value = kind, value
        self.confidence, self.source_task, self.source_adapter = (
            confidence, task, adapter)


def _visit(db, *, label="car", minutes_ago=5, plate=None,
           caption=None) -> TimelineEvent:
    started = WALL - timedelta(minutes=minutes_ago)
    row = TimelineEvent(camera_id=1, source="tier0", event_type="track",
                        label=label, started_at=started,
                        ended_at=started + timedelta(seconds=20),
                        evidence_path="e.jpg", plate_text=plate)
    db.add(row)
    db.commit()
    db.refresh(row)
    if caption:
        db.add(EventText(event_id=row.id, caption=caption, source="blip"))
        db.commit()
    return row


def _words(db, row) -> str | None:
    db.expire_all()
    text = db.get(EventText, row.id)
    return text.attributes if text is not None else None


# ── a claim is also a word ───────────────────────────────────────────


def test_claims_project_into_searchable_words(db):
    row = _visit(db)
    apply_descriptors(db, row, [_Claim("vehicle_type", "van"),
                                _Claim("colour", "red")])
    db.commit()
    # Reading order, not insertion order: two visits with the same claims
    # must produce the same string.
    assert _words(db, row) == "red van"


def test_the_projection_is_recomputed_not_appended(db):
    """A corrected claim must not leave the old word behind — the claims
    are the source of truth and this is a projection of them."""
    row = _visit(db)
    apply_descriptors(db, row, [_Claim("colour", "red")])
    db.commit()
    assert _words(db, row) == "red"

    apply_descriptors(db, row, [_Claim("colour", "blue")])
    db.commit()
    assert _words(db, row) == "blue", "the old value is still a search word"


def test_projecting_twice_changes_nothing(db):
    row = _visit(db)
    apply_descriptors(db, row, [_Claim("colour", "white"),
                                _Claim("vehicle_type", "truck")])
    db.commit()
    first = _words(db, row)
    project_attributes(db, row)
    db.commit()
    assert _words(db, row) == first


def test_the_caption_and_its_source_are_never_touched(db):
    """``caption_enrichment`` reads ``source`` to decide whether somebody
    has already described the visit. Stamping this module's name on it
    would make every projected visit look described and silently stop it
    ever being captioned."""
    row = _visit(db, caption="a vehicle at the gate")
    apply_descriptors(db, row, [_Claim("colour", "red")])
    db.commit()

    db.expire_all()
    text = db.get(EventText, row.id)
    assert text.caption == "a vehicle at the gate"
    assert text.source == "blip"
    assert text.attributes == "red"


def test_a_row_the_projection_creates_claims_no_source(db):
    """The dangerous direction: claims arrive BEFORE any caption, so the
    projection is what creates the sidecar row. If it stamped its own
    name on ``source``, ``caption_enrichment`` would later read the visit
    as "somebody else described it" and return — and that visit would
    never be captioned at all."""
    row = _visit(db)
    apply_descriptors(db, row, [_Claim("colour", "red")])
    db.commit()

    db.expire_all()
    text = db.get(EventText, row.id)
    assert text.attributes == "red"
    assert text.source is None, (
        "caption_enrichment bails on a row whose source is somebody "
        "else's, so a projected visit would never get a caption")


def test_a_face_id_is_stored_and_filterable_but_is_not_a_search_word(db):
    """An identity is queried exactly, through the attr filter, which
    already works. Tokenising a person's name into the text column would
    make any query containing that name match them, and would widen who
    can discover it beyond the app that produced it."""
    row = _visit(db, label="person")
    apply_descriptors(db, row, [_Claim("face_id", "alice", task="face_recognition"),
                                _Claim("clothing_top", "blue")])
    db.commit()

    assert _words(db, row) == "blue"
    stored = {d.kind: d.value for d in db.query(VisitDescriptor)
              .filter(VisitDescriptor.event_id == row.id).all()}
    assert stored["face_id"] == "alice", "the claim itself must still be stored"


def test_the_plate_joins_the_words(db):
    row = _visit(db, plate="AB12CDE")
    apply_descriptors(db, row, [_Claim("colour", "white")])
    db.commit()
    assert _words(db, row) == "white ab12cde"


def test_a_visit_with_nothing_to_say_gets_no_sidecar_row(db):
    """Most visits never get text; that is why it is a sidecar and not
    columns on ``events``."""
    row = _visit(db)
    project_attributes(db, row)
    db.commit()
    assert db.get(EventText, row.id) is None


def test_retracting_every_claim_clears_the_words_but_keeps_a_caption(db):
    row = _visit(db, caption="a van at the gate")
    apply_descriptors(db, row, [_Claim("colour", "red")])
    db.commit()
    assert _words(db, row) == "red"

    db.query(VisitDescriptor).filter(VisitDescriptor.event_id == row.id).delete()
    project_attributes(db, row)
    db.commit()

    db.expire_all()
    text = db.get(EventText, row.id)
    assert text is not None and text.attributes is None
    assert text.caption == "a van at the gate"


def test_a_row_left_saying_nothing_is_deleted(db):
    row = _visit(db)
    apply_descriptors(db, row, [_Claim("colour", "red")])
    db.commit()
    db.query(VisitDescriptor).filter(VisitDescriptor.event_id == row.id).delete()
    project_attributes(db, row)
    db.commit()
    assert db.get(EventText, row.id) is None, (
        "an empty sidecar row makes 'enriched' and 'enriched to nothing' "
        "look alike")


def test_the_projection_rides_with_the_shared_writer(db):
    """Projecting inside apply_descriptors rather than at each call site
    is what stops the endpoint and the enricher disagreeing about whether
    a claim is searchable."""
    import inspect

    from services import descriptor_store

    assert "project_attributes(db, row)" in inspect.getsource(
        descriptor_store.apply_descriptors)


# ── the plate is a claim, not only a column ──────────────────────────


def test_a_plate_becomes_a_claim_with_its_measured_confidence(db):
    """Unlike a VQA answer, a plate read has a real score — and keeping
    it is the distinction the confidence column exists for."""
    row = _visit(db, plate="AB12CDE")
    row.payload = {"plate_confidence": 0.93, "plate_source": "sweep"}
    sync_plate_claim(db, row)
    db.commit()

    claim = (db.query(VisitDescriptor)
             .filter(VisitDescriptor.event_id == row.id,
                     VisitDescriptor.kind == "plate").one())
    assert claim.value == "ab12cde"
    assert claim.source_task == PLATE_TASK
    assert claim.confidence == pytest.approx(0.93)
    assert _words(db, row) == "ab12cde"


def test_a_forwarded_read_with_no_score_keeps_none(db):
    row = _visit(db, plate="XY99ZZZ")
    sync_plate_claim(db, row)
    db.commit()
    claim = (db.query(VisitDescriptor)
             .filter(VisitDescriptor.kind == "plate").one())
    assert claim.confidence is None, (
        "inventing a score would let a guess weigh like a measured read")


def test_a_retracted_plate_takes_its_claim_with_it(db):
    """clear_plate exists for reads the later looks overturned. A journey
    anchored on one would put a vehicle at a camera it was never at."""
    row = _visit(db, plate="AB12CDE")
    sync_plate_claim(db, row)
    db.commit()
    assert db.query(VisitDescriptor).filter(
        VisitDescriptor.kind == "plate").count() == 1

    row.plate_text = None
    sync_plate_claim(db, row)
    db.commit()

    assert db.query(VisitDescriptor).filter(
        VisitDescriptor.kind == "plate").count() == 0
    assert _words(db, row) is None


def test_the_plate_task_is_the_one_the_plan_advertises(db):
    """The plan says license_plate_recognition produces kind 'plate'. A
    claim written under another task name is a promise the plan makes and
    the store does not keep."""
    from services.enrichment_plan import TASK_DESCRIPTORS

    assert PLATE_TASK in TASK_DESCRIPTORS
    assert "plate" in TASK_DESCRIPTORS[PLATE_TASK]["kinds"]


def test_the_plate_is_an_identity_anchor_journey_looks_for(db):
    from services.journey import ANCHOR_KINDS

    assert "plate" in ANCHOR_KINDS


def _plate_writers(root: Path):
    """Every function that assigns ``plate_text``, and whether it also
    calls ``sync_plate_claim`` itself.

    By FUNCTION, not by file. The first version of this asked whether
    the file mentioned sync_plate_claim anywhere, which passes happily
    when one of two writers in the same module loses its call — the
    exact drift it was written to catch, one level down. A mutation
    check found that; it had been green the whole time.
    """
    import ast

    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith("tests/") or rel.startswith(".venv/"):
            continue
        try:
            tree = ast.parse(path.read_text(errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            writes = any(
                isinstance(t, ast.Attribute) and t.attr == "plate_text"
                for sub in ast.walk(node)
                if isinstance(sub, ast.Assign)
                for t in sub.targets
            )
            if not writes:
                continue
            calls = {
                c.func.id if isinstance(c.func, ast.Name) else
                (c.func.attr if isinstance(c.func, ast.Attribute) else "")
                for c in ast.walk(node) if isinstance(c, ast.Call)
            }
            yield rel, node.name, calls


#: Writers that do NOT sync themselves because their caller does.
#:
#: Empty on purpose, and the emptiness is the fix. ``clear_plate`` used
#: to be the entry here: it retracted a read and left the caller to drop
#: the claim two lines later. A mutation check showed that nothing would
#: have noticed if those two lines drifted apart — both the write and
#: the sync sit inside one 340-line function, so every guard that could
#: see them saw the OTHER call and passed. Rather than teach a static
#: guard to do flow analysis, clear_plate now drops the claim itself.
#:
#: Anything added back here is a pairing somebody has to remember, and
#: the test below only checks that SOME caller syncs — not that the one
#: on the failing branch does.
_SYNCS_VIA_CALLER: set[str] = set()


def test_every_writer_of_plate_text_syncs_the_claim():
    """A writer that sets the column and forgets the claim reintroduces
    exactly the gap this closes, silently — the row looks right and the
    journey anchor is missing."""
    root = Path(__file__).resolve().parents[1]
    offenders = [
        f"{rel}:{fn}"
        for rel, fn, calls in _plate_writers(root)
        if "sync_plate_claim" not in calls and fn not in _SYNCS_VIA_CALLER
    ]
    assert offenders == [], (
        f"{offenders} set plate_text without syncing the plate claim")


def test_a_writer_excused_from_syncing_is_synced_by_every_caller():
    """``clear_plate`` retracts a read, and a claim that outlives the
    read it came from is worse than no claim: journey treats a plate as
    an exact identity worth 4.0, so a retracted plate still carrying its
    claim would put a vehicle at a camera it was never at. It does not
    sync itself — its one caller does, on the next line — so that is
    what gets asserted."""
    import ast

    root = Path(__file__).resolve().parents[1]
    unsynced = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith("tests/") or rel.startswith(".venv/"):
            continue
        try:
            tree = ast.parse(path.read_text(errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [
                c.func.id if isinstance(c.func, ast.Name) else
                (c.func.attr if isinstance(c.func, ast.Attribute) else "")
                for c in ast.walk(node) if isinstance(c, ast.Call)
            ]
            for excused in _SYNCS_VIA_CALLER:
                if excused in calls and "sync_plate_claim" not in calls:
                    unsynced.append(f"{rel}:{node.name} calls {excused}")
    assert unsynced == [], (
        f"{unsynced} — a retraction whose claim is never cleared leaves "
        "journey anchored on a plate the system has already overturned")


def test_the_excused_list_names_something_real():
    """An excuse for a function that no longer exists is an excuse that
    silently covers nothing."""
    root = Path(__file__).resolve().parents[1]
    writers = {fn for _, fn, _ in _plate_writers(root)}
    missing = sorted(_SYNCS_VIA_CALLER - writers)
    assert missing == [], (
        f"{missing} are excused from syncing but no longer write "
        "plate_text; drop them from _SYNCS_VIA_CALLER")


# ── the payoff ───────────────────────────────────────────────────────


def test_a_free_text_search_now_finds_a_visit_nobody_captioned(db):
    """The whole point. Before this, the store knew the van was red and
    search still answered nothing, because the claims and the words never
    met."""
    from services.search_service import search_events

    described = _visit(db, label="car", minutes_ago=3)
    apply_descriptors(db, described, [_Claim("colour", "red"),
                                      _Claim("vehicle_type", "van")])
    other = _visit(db, label="car", minutes_ago=4)
    apply_descriptors(db, other, [_Claim("colour", "white"),
                                  _Claim("vehicle_type", "van")])
    db.commit()

    hits = search_events(db, text="red van", scope=None)
    assert [h.event.id for h in hits] == [described.id]

    # And the structured filter still answers exactly, on the same rows.
    assert [h.event.id for h in search_events(
        db, attrs=[("colour", "white")], scope=None)] == [other.id]


def test_a_plate_is_findable_as_a_word_too(db):
    from services.search_service import search_events

    row = _visit(db, label="truck", plate="AB12CDE")
    sync_plate_claim(db, row)
    db.commit()

    assert [h.event.id for h in search_events(
        db, text="ab12cde", scope=None)] == [row.id]
    assert [h.event.id for h in search_events(
        db, attrs=[("plate", "ab12cde")], scope=None)] == [row.id]
