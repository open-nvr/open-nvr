# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Footage search over the canonical event store — parser + API."""

from __future__ import annotations

import base64
import os
import secrets
import sys
import types as _types
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_search_test.db")
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
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from core.database import Base  # noqa: E402
from models import (  # noqa: E402
    Camera, EventText, Role, TimelineEvent, User, VisitDescriptor,
)
from services.search_query import parse_query  # noqa: E402
from services.search_service import (  # noqa: E402
    count_search_events, search_events,
)

UTC = timezone.utc
#: The parser's clock. FROZEN so "yesterday" and "last Tuesday" resolve to
#: dates the assertions can name.
NOW = datetime(2026, 9, 18, 15, 0, tzinfo=UTC)
#: The store's clock. REAL, because other routes read the wall clock too —
#: a row written relative to a frozen date stops being "today" the day after
#: the test was written, which is exactly what happened. Rows go here; the
#: parser tests see NOW, and the API's parser is pinned to WALL (below).
#: Kept clear of local midnight: a visit "5 minutes ago" at 00:02 is
#: yesterday's, and "today" would miss it. The furthest a "today" test
#: reaches back is 30 minutes.
WALL = datetime.now(UTC).replace(microsecond=0)
if WALL.astimezone().time() < time(0, 40):
    WALL -= timedelta(hours=1)


class _WallClock(datetime):
    """``datetime`` with ``now()`` fixed at WALL."""

    @classmethod
    def now(cls, tz=None):
        return WALL.astimezone(tz) if tz else WALL.astimezone().replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _parser_at_wall(monkeypatch):
    """The API parses against ``datetime.now()``: pin it to WALL, so the
    rows and the question share one clock and a run that crosses midnight
    does not move "today" under the rows."""
    import services.search_query as sq
    monkeypatch.setattr(sq, "datetime", _WallClock)


# ── Parser ───────────────────────────────────────────────────────────


def _p(q: str, cameras=None):
    return parse_query(q, cameras=cameras or {}, now=NOW)


def test_the_parser_reads_class_words_through_synonyms():
    assert _p("show me trucks").labels == ["truck"]
    assert _p("any lorry").labels == ["truck"]
    assert _p("a man").labels == ["person"]
    # A word that widens: "vehicle" is not one class.
    assert set(_p("vehicles").labels) == {"car", "truck", "bus", "motorcycle"}
    # A bag query covers the three COCO bag classes.
    assert set(_p("luggage").labels) == {"backpack", "handbag", "suitcase"}


def test_several_classes_mean_or_and_the_parse_says_so():
    parsed = _p("car or bike")
    # "bike" is a motorcycle on most roads here and a bicycle on others
    assert set(parsed.labels) == {"car", "bicycle", "motorcycle"}
    assert "car" in parsed.matched["what"] and "bike" in parsed.matched["what"]


def test_relative_and_named_times():
    assert _p("today").from_ == datetime(2026, 9, 18, tzinfo=UTC)
    assert _p("yesterday").from_ == datetime(2026, 9, 17, tzinfo=UTC)
    assert _p("yesterday").to == datetime(2026, 9, 18, tzinfo=UTC)
    assert _p("in the last 10 minutes").from_ == NOW - timedelta(minutes=10)
    assert _p("past 2 hours").from_ == NOW - timedelta(hours=2)
    assert _p("last 3 days").from_ == NOW - timedelta(days=3)
    # "last night" is an evening that ends this morning, not a calendar day.
    night = _p("last night")
    assert night.from_ == datetime(2026, 9, 17, 18, 0, tzinfo=UTC)
    assert night.to == datetime(2026, 9, 18, 6, 0, tzinfo=UTC)
    # A weekday means the most recent one, never today.
    assert _p("on friday").from_ == datetime(2026, 9, 11, tzinfo=UTC)
    assert _p("2026-09-01").from_ == datetime(2026, 9, 1, tzinfo=UTC)


def test_explicit_windows():
    between = _p("between 14:00 and 16:00")
    assert between.from_ == datetime(2026, 9, 18, 14, 0, tzinfo=UTC)
    assert between.to == datetime(2026, 9, 18, 16, 0, tzinfo=UTC)
    since = _p("since 09:30")
    assert since.from_ == datetime(2026, 9, 18, 9, 30, tzinfo=UTC)
    assert since.to == NOW


def test_cameras_match_on_whole_words_only():
    cams = {1: "Loading dock", 2: "Car park north", 3: "Gate 14"}
    assert _p("at the dock", cams).camera_ids == [1]
    # "car" must not pin every vehicle query to "Car park north" — that is
    # the failure that makes people stop typing camera names.
    parsed = _p("red car yesterday", cams)
    assert parsed.camera_ids == []
    assert parsed.labels == ["car"]


def test_plate_shaped_tokens_become_a_plate_filter():
    parsed = _p("ka01ab1234 yesterday")
    assert parsed.plate == "KA01AB1234"
    assert parsed.text == ""
    # Said outright, anything alphanumeric is taken as the registration.
    assert _p("plate ab12cde").plate == "AB12CDE"
    assert _p("reg ab12cde").plate == "AB12CDE"


def test_a_place_name_with_a_digit_is_not_a_plate():
    """"gate14" and "bay3" used to become plate filters, which return an
    empty list and tell the operator nothing was there — the confident
    wrong parse this parser exists to avoid. Uncued, a token needs real
    plate shape; missed either way it falls through to text, where a
    caption carrying the number still matches."""
    parsed = _p("people at gate14")
    assert parsed.plate == "" and parsed.text == "gate14"
    assert parsed.labels == ["person"]
    assert _p("bay3 truck").plate == ""
    assert _p("ab12cde yesterday").plate == ""


def test_everything_left_over_is_text_and_nothing_is_silently_dropped():
    parsed = _p("red delivery van at 2026-09-01")
    assert set(parsed.labels) == {"truck"}
    assert parsed.text == "red delivery"
    assert parsed.matched["when"] == "2026-09-01"
    assert parsed.matched["text"] == "red delivery"
    # A bare number is noise, and the parse admits to ignoring it.
    assert _p("person 42").ignored == ["42"]


def test_an_empty_query_parses_to_nothing():
    parsed = _p("   ")
    assert parsed.labels == [] and parsed.text == "" and parsed.from_ is None


# ── Store ────────────────────────────────────────────────────────────


@pytest.fixture()
def db():
    # StaticPool + check_same_thread: TestClient runs the route on
    # another thread, and a default in-memory SQLite connection refuses
    # to be used from one.
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    # Cameras need an owner, and a user needs a role — one of each
    # stands in for the fleet's.
    session.add(Role(id=1, name="admin", description="test"))
    session.commit()
    session.add(User(id=1, username="owner", email="o@x.test",
                     hashed_password="x", is_active=True, role_id=1))
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _camera(db, cam_id: int, name: str) -> Camera:
    cam = Camera(id=cam_id, name=name, ip_address=f"10.0.0.{cam_id}",
                 rtsp_url=f"rtsp://x/{cam_id}", owner_id=1)
    db.add(cam)
    db.commit()
    return cam


def _visit(db, *, camera_id=1, label="person", minutes_ago=5, caption=None,
           attributes=None, plate=None, evidence="e.jpg") -> TimelineEvent:
    started = WALL - timedelta(minutes=minutes_ago)
    row = TimelineEvent(
        camera_id=camera_id, source="tier0", event_type="track", label=label,
        started_at=started, ended_at=started + timedelta(seconds=20),
        evidence_path=evidence, plate_text=plate,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    if caption or attributes:
        db.add(EventText(event_id=row.id, caption=caption, attributes=attributes,
                         source="test"))
        db.commit()
    return row


def test_label_filter_is_an_or_across_classes(db):
    _camera(db, 1, "Dock")
    _visit(db, label="person")
    _visit(db, label="truck")
    _visit(db, label="dog")
    hits = search_events(db, labels=["person", "truck"], scope=None)
    assert {h.event.label for h in hits} == {"person", "truck"}
    assert count_search_events(db, labels=["person", "truck"], scope=None) == 2


def test_text_matches_captions_and_attributes_and_ands_across_words(db):
    _camera(db, 1, "Dock")
    _visit(db, label="truck", caption="a red truck backing up to the dock")
    _visit(db, label="truck", caption="a white van leaving")
    _visit(db, label="truck", attributes="red long-wheelbase")
    assert len(search_events(db, text="red", scope=None)) == 2
    # Two words = both must appear (in either column).
    assert len(search_events(db, text="red backing", scope=None)) == 1
    assert len(search_events(db, text="purple", scope=None)) == 0


def test_a_visit_with_no_text_still_answers_a_label_query(db):
    """The join to event_text must be OUTER, or search silently covers
    only the rows an enricher has reached."""
    _camera(db, 1, "Dock")
    _visit(db, label="truck", caption=None)
    hits = search_events(db, labels=["truck"], scope=None)
    assert len(hits) == 1 and hits[0].caption is None


def test_scope_is_the_stores_own(db):
    _camera(db, 1, "Dock")
    _camera(db, 2, "Yard")
    _visit(db, camera_id=1, label="person")
    _visit(db, camera_id=2, label="person")
    assert len(search_events(db, labels=["person"], scope=None)) == 2
    assert len(search_events(db, labels=["person"], scope={1})) == 1
    assert count_search_events(db, labels=["person"], scope={1}) == 1
    # A caller who can see nothing gets nothing, not everything.
    assert search_events(db, labels=["person"], scope=set()) == []


def test_time_window_uses_the_overlap_rule(db):
    _camera(db, 1, "Dock")
    row = _visit(db, minutes_ago=10)
    row.ended_at = WALL - timedelta(minutes=2)
    db.commit()
    # A visit that started before the window but was still running inside
    # it counts — "who was here in the last 5 minutes" means them too.
    hits = search_events(db, from_=WALL - timedelta(minutes=5), to=WALL, scope=None)
    assert len(hits) == 1


def test_paging_and_count_agree(db):
    _camera(db, 1, "Dock")
    for i in range(7):
        _visit(db, minutes_ago=i + 1)
    first = search_events(db, limit=3, skip=0, scope=None)
    second = search_events(db, limit=3, skip=3, scope=None)
    assert len(first) == 3 and len(second) == 3
    assert {h.event.id for h in first}.isdisjoint({h.event.id for h in second})
    assert count_search_events(db, scope=None) == 7
    # Newest first, with no text to rank by.
    assert first[0].event.started_at > first[-1].event.started_at


def test_sqlite_scores_are_honest(db):
    """No ranking function on this dialect — every row says 1.0 rather
    than carrying a made-up number the UI would sort by."""
    _camera(db, 1, "Dock")
    _visit(db, caption="red truck")
    assert [h.score for h in search_events(db, text="red", scope=None)] == [1.0]


def test_the_fts_expression_matches_the_migration():
    """The query and the GIN index must build the SAME expression or
    Postgres plans a sequential scan and the index is decorative."""
    from services.search_service import FTS_EXPR

    migration = (_HERE / "migrations/versions/c1d2e3f4a5b6_add_event_text_search.py").read_text()
    # The migration writes it unqualified (inside CREATE INDEX ON event_text);
    # the query qualifies the columns. Same expression, same normalisation.
    assert "to_tsvector('simple'" in migration
    normalised = FTS_EXPR.replace("event_text.", "").replace("\n", " ")
    normalised = " ".join(normalised.split())
    assert normalised in " ".join(migration.split())


# ── API ──────────────────────────────────────────────────────────────


@pytest.fixture()
def client(db):
    """The search route on the same in-memory store the service tests
    use, with auth stubbed to a superuser (scoping itself is covered at
    the service level, where it is enforced)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.auth import get_current_active_user
    from core.database import get_db
    from routers import search as search_router

    app = FastAPI()
    app.include_router(search_router.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_active_user] = lambda: db.get(User, 1)
    with TestClient(app) as c:
        yield c


def test_the_api_answers_with_what_it_understood(client, db):
    _camera(db, 1, "Loading dock")
    _visit(db, camera_id=1, label="truck", minutes_ago=0,
           caption="a red truck at the dock")
    body = client.get("/api/v1/search", params={"q": "red truck at the dock today"}).json()

    interp = body["interpretation"]
    assert interp["labels"] == ["truck"]
    assert interp["camera_ids"] == [1]
    assert interp["text"] == "red"
    assert interp["matched"]["when"] == "today"
    assert body["total"] == 1
    hit = body["results"][0]
    assert hit["camera_name"] == "Loading dock"
    assert hit["caption"] == "a red truck at the dock"
    assert hit["evidence_url"] == f"/api/v1/events/{hit['id']}/evidence"
    # The anchor is what a player opens at; the route is the UI's to build.
    assert hit["anchor"]["camera_id"] == 1
    assert hit["anchor"]["at"] == hit["started_at"]


def test_an_explicit_parameter_overrides_the_parse(client, db):
    """Correcting a chip is passing the parameter — and the response says
    which parts the caller pinned, so the UI can show them as edited."""
    _camera(db, 1, "Loading dock")
    _visit(db, camera_id=1, label="person", minutes_ago=0)
    _visit(db, camera_id=1, label="truck", minutes_ago=0)

    guessed = client.get("/api/v1/search", params={"q": "trucks today"}).json()
    assert guessed["interpretation"]["labels"] == ["truck"]
    assert guessed["total"] == 1

    fixed = client.get(
        "/api/v1/search", params={"q": "trucks today", "label": ["person"]}
    ).json()
    assert fixed["interpretation"]["labels"] == ["person"]
    assert "labels" in fixed["interpretation"]["overridden"]
    assert fixed["results"][0]["label"] == "person"


def test_an_empty_query_returns_the_most_recent_visits(client, db):
    """No words is not an error: it is "what happened lately", which is
    the right thing to show somebody who just opened the page."""
    _camera(db, 1, "Dock")
    for i in range(3):
        _visit(db, minutes_ago=i + 1)
    body = client.get("/api/v1/search").json()
    assert body["total"] == 3
    assert body["count"] == 3
    assert body["interpretation"]["labels"] == []


def test_paging_is_reported_honestly(client, db):
    _camera(db, 1, "Dock")
    for i in range(5):
        _visit(db, minutes_ago=i + 1)
    body = client.get("/api/v1/search", params={"limit": 2, "skip": 2}).json()
    assert body["count"] == 2 and body["total"] == 5


def test_parse_false_lets_a_ui_clear_a_filter_the_sentence_implied(client, db):
    """An absent parameter means "no opinion", not "none" — so a UI
    driving from edited chips turns the parser off and owns every
    filter. Without this there is no way to REMOVE a chip."""
    _camera(db, 1, "Dock")
    _visit(db, camera_id=1, label="person", minutes_ago=0)
    _visit(db, camera_id=1, label="truck", minutes_ago=0)

    narrowed = client.get("/api/v1/search", params={"q": "trucks today"}).json()
    assert narrowed["total"] == 1

    widened = client.get(
        "/api/v1/search", params={"q": "trucks today", "parse": "false"}
    ).json()
    assert widened["total"] == 2
    assert widened["interpretation"]["source"] == "explicit"
    assert widened["interpretation"]["labels"] == []


def test_a_nonsense_query_says_what_it_ignored(client, db):
    _camera(db, 1, "Dock")
    _visit(db, caption="a person walking")
    body = client.get("/api/v1/search", params={"q": "zebra 42"}).json()
    assert body["total"] == 0
    assert body["interpretation"]["text"] == "zebra"
    assert body["interpretation"]["ignored"] == ["42"]


# ── Enrichment write path ────────────────────────────────────────────


@pytest.fixture()
def internal_client(db):
    """The internal enrichment endpoint, internal-key authed as the
    detect-pipeline and the apps are."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.database import get_db
    from routers import internal_camera_agent as internal

    app = FastAPI()
    app.include_router(internal.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[internal._require_internal_key] = lambda: "test"
    with TestClient(app) as c:
        yield c


def test_enrichment_upserts_rather_than_multiplying_rows(internal_client, db):
    _camera(db, 1, "Dock")
    row = _visit(db, label="truck")
    post = lambda body: internal_client.post("/api/v1/internal/camera-agent/events/text", json=body)

    assert post({"event_id": row.id, "caption": "a van", "source": "cap-v1"}).status_code == 204
    assert len(search_events(db, text="van", scope=None)) == 1

    # A better pass overwrites its own row instead of adding a second.
    assert post({"event_id": row.id, "caption": "a red delivery van",
                 "source": "cap-v2"}).status_code == 204
    assert db.query(EventText).count() == 1
    hits = search_events(db, text="delivery", scope=None)
    assert len(hits) == 1 and hits[0].attributes is None
    assert db.get(EventText, row.id).source == "cap-v2"


def test_an_enricher_can_retract_what_it_said(internal_client, db):
    """These words are shown to an operator as fact, so there has to be a
    way to take them back."""
    _camera(db, 1, "Dock")
    row = _visit(db, label="truck")
    internal_client.post("/api/v1/internal/camera-agent/events/text",
                         json={"event_id": row.id, "caption": "a red van"})
    assert len(search_events(db, text="red", scope=None)) == 1
    internal_client.post("/api/v1/internal/camera-agent/events/text",
                         json={"event_id": row.id, "caption": "  "})
    assert db.query(EventText).count() == 0
    # The visit itself is untouched — enrichment never owns history.
    assert len(search_events(db, labels=["truck"], scope=None)) == 1


def test_posting_text_for_an_unknown_visit_is_a_404(internal_client, db):
    resp = internal_client.post("/api/v1/internal/camera-agent/events/text",
                                json={"event_id": 9999, "caption": "x"})
    assert resp.status_code == 404


# ── Descriptors: what the skills said ────────────────────────────────


def _claim(db, event_id: int, kind: str, value: str, *, task="vqa", conf=0.8):
    db.add(VisitDescriptor(event_id=event_id, kind=kind, value=value,
                           confidence=conf, source_task=task, source_adapter="test"))
    db.commit()


def test_attribute_filters_and_across_claims_on_one_visit(db):
    """"red van" must be ONE visit that is both, not a red anything and a
    van anything — which is what a join would have returned."""
    _camera(db, 1, "Dock")
    red_van = _visit(db, label="truck")
    _claim(db, red_van.id, "colour", "red")
    _claim(db, red_van.id, "vehicle_type", "van", task="vqa2")
    white_van = _visit(db, label="truck")
    _claim(db, white_van.id, "colour", "white")
    _claim(db, white_van.id, "vehicle_type", "van", task="vqa2")
    red_car = _visit(db, label="car")
    _claim(db, red_car.id, "colour", "red")

    both = search_events(db, attrs=[("colour", "red"), ("vehicle_type", "van")], scope=None)
    assert [h.event.id for h in both] == [red_van.id]
    assert count_search_events(db, attrs=[("colour", "red"), ("vehicle_type", "van")],
                               scope=None) == 1
    # And the count is not multiplied by the number of claims that matched.
    assert count_search_events(db, attrs=[("colour", "red")], scope=None) == 2


def test_results_carry_the_claims_and_who_made_them(db):
    _camera(db, 1, "Dock")
    row = _visit(db, label="truck")
    _claim(db, row.id, "colour", "red", task="vqa", conf=0.62)
    _claim(db, row.id, "plate", "ka01ab1234", task="license_plate_recognition", conf=0.97)
    hit = search_events(db, labels=["truck"], scope=None)[0]
    kinds = {c["kind"]: c for c in hit.claims}
    assert kinds["plate"]["value"] == "ka01ab1234"
    assert kinds["plate"]["task"] == "license_plate_recognition"
    # Confidence rides each claim: a 0.62 guess must not weigh like a 0.97 read.
    assert kinds["colour"]["confidence"] == 0.62


def test_a_visit_nobody_enriched_still_answers(db):
    """More skills is better; no skills must still work."""
    _camera(db, 1, "Dock")
    _visit(db, label="truck")
    assert len(search_events(db, labels=["truck"], scope=None)) == 1
    assert search_events(db, attrs=[("colour", "red")], scope=None) == []


def test_descriptor_write_is_per_skill_and_records_what_ran(internal_client, db):
    _camera(db, 1, "Dock")
    row = _visit(db, label="truck")
    post = lambda body: internal_client.post(
        "/api/v1/internal/camera-agent/events/descriptors", json=body)

    out = post({"event_id": row.id, "ran_tasks": ["vqa", "license_plate_recognition"],
                "descriptors": [
                    {"kind": "Colour", "value": "Red", "confidence": 0.6,
                     "source_task": "vqa", "source_adapter": "qwen-vl"},
                ]}).json()
    assert out["written"] == 1
    assert out["enriched_by"] == ["license_plate_recognition", "vqa"]
    # Values are folded to lowercase so "Red" and "red" count as one thing.
    assert len(search_events(db, attrs=[("colour", "red")], scope=None)) == 1

    # A better pass by the SAME skill replaces its own claim...
    post({"event_id": row.id, "descriptors": [
        {"kind": "colour", "value": "maroon", "confidence": 0.9, "source_task": "vqa"}]})
    assert db.query(VisitDescriptor).filter_by(event_id=row.id).count() == 1
    # ...while another skill's disagreement is kept, because that is
    # information about the skills, not a conflict for the schema.
    post({"event_id": row.id, "descriptors": [
        {"kind": "colour", "value": "red", "confidence": 0.5, "source_task": "colour-net"}]})
    assert db.query(VisitDescriptor).filter_by(event_id=row.id, kind="colour").count() == 2


def test_ran_tasks_separates_looked_and_saw_nothing_from_never_looked(internal_client, db):
    """The distinction every attribute-matching scheme gets wrong."""
    _camera(db, 1, "Dock")
    looked = _visit(db, label="truck")
    never = _visit(db, label="truck")
    internal_client.post("/api/v1/internal/camera-agent/events/descriptors",
                         json={"event_id": looked.id, "descriptors": [],
                               "ran_tasks": ["license_plate_recognition"]})
    db.refresh(looked)
    assert (looked.payload or {}).get("enriched_by") == ["license_plate_recognition"]
    assert (never.payload or {}) == {}


def test_descriptors_for_an_unknown_visit_are_a_404(internal_client):
    resp = internal_client.post("/api/v1/internal/camera-agent/events/descriptors",
                                json={"event_id": 4242, "descriptors": []})
    assert resp.status_code == 404


def test_the_api_filters_on_kind_value_pairs(client, db):
    _camera(db, 1, "Dock")
    row = _visit(db, label="truck")
    _claim(db, row.id, "colour", "red")
    _visit(db, label="truck")
    body = client.get("/api/v1/search", params={"attr": ["colour:red"]}).json()
    assert body["total"] == 1
    assert body["interpretation"]["attrs"] == ["colour:red"]
    assert body["results"][0]["claims"][0]["kind"] == "colour"
    # A malformed chip is skipped rather than failing the whole search.
    assert client.get("/api/v1/search", params={"attr": ["nonsense"]}).json()["total"] == 2


# ── The enrichment plan ──────────────────────────────────────────────


def test_the_plan_is_registered_and_healthy_only():
    from services.enrichment_plan import build_plan, plan_for_label

    caps = {"adapters": [
        {"name": "yolov8", "tasks": ["object_detection"]},
        {"name": "fast-plate", "tasks": ["license_plate_recognition"]},
        {"name": "qwen-vl", "tasks": ["vqa", "image_captioning"]},
    ]}
    health = {"adapters": {"yolov8": {"healthy": True}, "fast-plate": {"healthy": False},
                           "qwen-vl": {"healthy": True}}}
    plan = {s.task: s for s in build_plan(caps, health)}
    assert plan["license_plate_recognition"].healthy is False
    assert plan["vqa"].healthy is True
    assert plan["vqa"].descriptor_kinds == [
        "colour", "vehicle_type", "clothing_top", "carrying",
        "gender", "clothing_bottom", "headwear"]

    # Worth running on a person: the VQA attributes, never plate OCR.
    tasks = {s.task for s in plan_for_label(list(plan.values()), "person")}
    assert "license_plate_recognition" not in tasks
    assert "vqa" in tasks


def test_an_unreachable_or_odd_registry_degrades_to_nothing_runnable():
    """A registry that has not answered means "no extra skills right now",
    not a crash — the visit still has its class, camera and time."""
    from services.enrichment_plan import build_plan

    assert build_plan(None, None) == []
    assert build_plan({"adapters": "nonsense"}, {"adapters": 7}) == []
    # An adapter nobody reported on stays available: health is a signal
    # that something is WRONG, not a licence to disable the whole box.
    plan = build_plan({"adapters": [{"name": "yolov8", "tasks": ["object_detection"]}]}, {})
    assert plan[0].healthy is True


# ── Journeys: following one object across cameras ────────────────────


def _at(db, *, camera_id: int, label="person", at_s: float, evidence="e.jpg"):
    """A visit at a given offset from the wall clock, in seconds."""
    started = WALL + timedelta(seconds=at_s)
    row = TimelineEvent(camera_id=camera_id, source="tier0", event_type="track",
                        label=label, started_at=started,
                        ended_at=started + timedelta(seconds=5), evidence_path=evidence)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _three_cameras(db):
    _camera(db, 1, "Gate")
    _camera(db, 2, "Yard")
    _camera(db, 3, "Dock")


def test_the_camera_graph_is_learned_from_exact_identities(db):
    """A plate or a face seen on two cameras is one observed trip. Nobody
    surveys the site; the certain journeys teach the graph."""
    from models import CameraTransition
    from services.journey import learn_transitions

    _three_cameras(db)
    for i, base in enumerate((0, 600, 1200)):
        a = _at(db, camera_id=1, label="car", at_s=base)
        b = _at(db, camera_id=2, label="car", at_s=base + 60 + i)
        for row in (a, b):
            _claim(db, row.id, "plate", f"ka0{i}ab1234",
                   task="license_plate_recognition", conf=0.95)
    assert learn_transitions(db) == 1
    edge = db.query(CameraTransition).one()
    assert (edge.from_camera_id, edge.to_camera_id) == (1, 2)
    assert edge.samples == 3
    assert 55 <= edge.median_seconds <= 65


def test_the_graph_ignores_the_same_camera_and_long_gaps(db):
    from services.journey import learn_transitions

    _three_cameras(db)
    # Same camera twice is the object still there, not a trip.
    for at_s in (0, 30):
        row = _at(db, camera_id=1, label="car", at_s=at_s)
        _claim(db, row.id, "plate", "aaa111", task="license_plate_recognition")
    # An hour apart is two visits, not one journey.
    for cam, at_s in ((1, 0), (3, 3600)):
        row = _at(db, camera_id=cam, label="car", at_s=at_s)
        _claim(db, row.id, "plate", "bbb222", task="license_plate_recognition")
    assert learn_transitions(db, max_gap_seconds=900) == 0


def test_an_exact_identity_makes_the_route_certain(db):
    from services.journey import find_journey

    _three_cameras(db)
    a = _at(db, camera_id=1, label="car", at_s=0)
    b = _at(db, camera_id=2, label="car", at_s=60)
    c = _at(db, camera_id=3, label="car", at_s=140)
    for row in (a, b, c):
        _claim(db, row.id, "plate", "ka01ab1234", task="license_plate_recognition", conf=0.96)

    j = find_journey(db, event_id=a.id, scope=None)
    assert [h.event.id for h in j.hops] == [b.id, c.id]
    assert j.method == "identity"
    assert j.caveat == ""
    assert "exact identity" in j.hops[0].why[0]


def test_a_general_object_is_followed_on_what_the_skills_saw(db):
    """No plate, no face — a person in a crowd. The graph says where they
    could have gone; the descriptors say which one there is them."""
    from services.journey import find_journey, learn_transitions

    _three_cameras(db)
    # Teach the 1→2 route from vehicles carrying plates.
    for i in range(4):
        base = i * 600
        for cam, off in ((1, 0), (2, 45)):
            row = _at(db, camera_id=cam, label="car", at_s=base + off)
            _claim(db, row.id, "plate", f"zz{i}9999", task="license_plate_recognition")
    learn_transitions(db)

    anchor = _at(db, camera_id=1, label="person", at_s=5000)
    _claim(db, anchor.id, "clothing_top", "yellow-hi-vis", task="vqa", conf=0.9)
    _claim(db, anchor.id, "carrying", "toolbox", task="vqa", conf=0.8)

    match = _at(db, camera_id=2, label="person", at_s=5050)
    _claim(db, match.id, "clothing_top", "yellow-hi-vis", task="vqa", conf=0.85)
    _claim(db, match.id, "carrying", "toolbox", task="vqa", conf=0.75)

    other = _at(db, camera_id=2, label="person", at_s=5055)
    _claim(db, other.id, "clothing_top", "blue-shirt", task="vqa", conf=0.9)
    _claim(db, other.id, "carrying", "nothing", task="vqa", conf=0.7)

    j = find_journey(db, event_id=anchor.id, scope=None)
    assert j.hops and j.hops[0].event.id == match.id
    assert j.method == "evidence"
    assert "Check each" in j.caveat
    assert any("clothing_top matches" in w for w in j.hops[0].why)


def test_the_anchor_says_whether_it_kept_a_frame(db):
    """Same shape as a hop's. Without it a client cannot tell "no frame
    was kept" from "the frame failed to load", and draws a broken image
    for the first stop of every route whose evidence has aged out."""
    from services.journey import find_journey

    _three_cameras(db)
    kept = _at(db, camera_id=1, label="car", at_s=0)
    aged = _at(db, camera_id=1, label="car", at_s=10, evidence=None)

    body = find_journey(db, event_id=kept.id, scope=None).as_dict()
    assert body["anchor"]["evidence_url"] == f"/api/v1/events/{kept.id}/evidence"

    body = find_journey(db, event_id=aged.id, scope=None).as_dict()
    assert body["anchor"]["evidence_url"] is None


def test_missing_descriptors_are_not_a_mismatch(db):
    """The rule that decides whether adding a skill helps or hurts: a
    visit nobody enriched must score neutrally, never badly."""
    from services.journey import _claims, _score_pair

    _three_cameras(db)
    a = _at(db, camera_id=1, label="person", at_s=0)
    b = _at(db, camera_id=2, label="person", at_s=30)
    _claim(db, a.id, "colour", "red")
    _claim(db, a.id, "carrying", "backpack")
    _claim(db, b.id, "colour", "red")          # nobody asked b about carrying

    c = _claims(db, [a.id, b.id])
    score, why, identity = _score_pair(db, c[a.id], c[b.id], {})
    assert identity is False
    assert score > 0
    assert not any("carrying" in w for w in why)   # silence, not a penalty


def test_a_disagreement_counts_against_but_does_not_eliminate(db):
    from services.journey import _claims, _score_pair

    _three_cameras(db)
    a = _at(db, camera_id=1, label="person", at_s=0)
    b = _at(db, camera_id=2, label="person", at_s=30)
    for kind, va, vb in (("clothing_top", "red-coat", "red-coat"),
                         ("carrying", "backpack", "satchel")):
        _claim(db, a.id, kind, va, task=f"t-{kind}")
        _claim(db, b.id, kind, vb, task=f"t-{kind}")
    c = _claims(db, [a.id, b.id])
    score, why, _ = _score_pair(db, c[a.id], c[b.id], {})
    assert any("differs" in w for w in why)
    assert 0 < score < 1


def test_a_common_value_is_weaker_evidence_than_a_rare_one(db):
    """"Red" at a depot where everything is red must not link objects."""
    from services.journey import _claims, _score_pair

    _three_cameras(db)
    # Twenty red things and one lime one.
    for i in range(20):
        row = _at(db, camera_id=3, label="car", at_s=i)
        _claim(db, row.id, "colour", "red")
    rare_a = _at(db, camera_id=1, label="car", at_s=100)
    rare_b = _at(db, camera_id=2, label="car", at_s=130)
    _claim(db, rare_a.id, "colour", "lime")
    _claim(db, rare_b.id, "colour", "lime")
    common_a = _at(db, camera_id=1, label="car", at_s=200)
    common_b = _at(db, camera_id=2, label="car", at_s=230)
    _claim(db, common_a.id, "colour", "red")
    _claim(db, common_b.id, "colour", "red")

    c = _claims(db, [rare_a.id, rare_b.id, common_a.id, common_b.id])
    cache: dict = {}
    rare_score, _, _ = _score_pair(db, c[rare_a.id], c[rare_b.id], cache)
    common_score, _, _ = _score_pair(db, c[common_a.id], c[common_b.id], cache)
    assert rare_score > common_score


def test_an_impossible_trip_is_not_offered(db):
    """The right-looking object at the wrong time is not the same object."""
    from services.journey import find_journey

    _three_cameras(db)
    anchor = _at(db, camera_id=1, label="person", at_s=0)
    _claim(db, anchor.id, "clothing_top", "red-coat")
    far = _at(db, camera_id=2, label="person", at_s=60 * 60)   # an hour later
    _claim(db, far.id, "clothing_top", "red-coat")
    j = find_journey(db, event_id=anchor.id, scope=None, window_minutes=240)
    assert j.hops == []
    assert j.method == "none"


def test_a_journey_stays_inside_what_the_caller_may_see(db):
    from services.journey import find_journey

    _three_cameras(db)
    a = _at(db, camera_id=1, label="car", at_s=0)
    b = _at(db, camera_id=2, label="car", at_s=60)
    for row in (a, b):
        _claim(db, row.id, "plate", "ka01ab1234", task="license_plate_recognition")
    assert find_journey(db, event_id=a.id, scope={1, 2}).hops
    # Camera 2 not visible: the hop is not offered, and not hinted at.
    assert find_journey(db, event_id=a.id, scope={1}).hops == []
    # The anchor itself is out of scope: nothing at all.
    assert find_journey(db, event_id=a.id, scope={3}) is None


def test_the_journey_api_names_its_method_and_its_reasons(client, db):
    _three_cameras(db)
    a = _at(db, camera_id=1, label="car", at_s=0)
    b = _at(db, camera_id=2, label="car", at_s=90)
    for row in (a, b):
        _claim(db, row.id, "plate", "ka01ab1234", task="license_plate_recognition")
    body = client.get("/api/v1/search/journey", params={"event_id": a.id}).json()
    assert body["method"] == "identity"
    assert body["anchor"]["camera_name"] == "Gate"
    hop = body["hops"][0]
    assert hop["camera_name"] == "Yard"
    assert hop["transit_seconds"] == 85.0
    assert hop["evidence_url"].endswith("/evidence")
    assert hop["anchor"]["camera_id"] == 2      # where a player should open
    assert hop["why"]


def test_the_journey_api_404s_on_an_unknown_visit(client):
    assert client.get("/api/v1/search/journey", params={"event_id": 9999}).status_code == 404


# ── Metrics ──────────────────────────────────────────────────────────


@pytest.fixture()
def metrics():
    """A clean registry per test — metrics are process-lifetime, so two
    tests sharing them would make each other's assertions meaningless."""
    from services import search_metrics as M

    M.reset_for_tests()
    yield M
    M.reset_for_tests()


def _samples(text: str) -> dict:
    """Parse exposition text with the parser the platform already uses to
    read the detect-pipeline's — if that cannot read this, no scraper can."""
    from services.tier0_metrics import parse_prometheus_text

    return parse_prometheus_text(text)


def _value(samples, name, **labels):
    for s in samples:
        if s.name == name and all(s.labels.get(k) == v for k, v in labels.items()):
            return s.value
    return None


def test_query_shape_splits_the_cost_classes(metrics):
    shape = metrics.query_shape
    assert shape(labels=["truck"], camera_ids=[], text="", attrs=[]) == "structured"
    assert shape(labels=[], camera_ids=[], text="red", attrs=[]) == "text"
    assert shape(labels=["truck"], camera_ids=[], text="red", attrs=[]) == "text+structured"
    assert shape(labels=[], camera_ids=[], text="", attrs=[("colour", "red")]) == "attr"
    assert shape(labels=[], camera_ids=[], text="", attrs=[]) == "unfiltered"
    # A plate is an equality on an indexed column, so it is structure.
    assert shape(labels=[], camera_ids=[], text="", attrs=[], plate="ka01") == "structured"


def test_a_search_is_counted_by_shape_and_outcome(client, db, metrics):
    _camera(db, 1, "Loading dock")
    _visit(db, camera_id=1, label="truck", caption="a red truck at the dock")

    client.get("/api/v1/search", params={"q": "truck"})
    client.get("/api/v1/search", params={"q": "bicycle"})

    s = _samples(metrics.render())
    assert _value(s, "opennvr_search_queries_total", shape="structured", outcome="hit") == 1
    assert _value(s, "opennvr_search_queries_total", shape="structured", outcome="empty") == 1
    # Both shapes are timed, and the count query is timed separately —
    # it is the cost this API adds over a bare page query.
    assert _value(s, "opennvr_search_seconds_count", shape="structured") == 2
    assert _value(s, "opennvr_search_count_seconds_count", shape="structured") == 2


def test_the_words_the_parser_threw_away_are_counted(client, db, metrics):
    _camera(db, 1, "Loading dock")
    client.get("/api/v1/search", params={"q": "truck 42"})

    s = _samples(metrics.render())
    # "42" is a bare number: parsed, understood to be noise, and admitted to.
    assert _value(s, "opennvr_search_query_words_total", state="ignored") == 1
    assert _value(s, "opennvr_search_query_words_total", state="matched") >= 1
    assert _value(s, "opennvr_search_ignored_words_count") == 1


def test_editing_the_chips_is_recorded_as_a_refinement(client, db, metrics):
    _camera(db, 1, "Loading dock")
    _visit(db, camera_id=1, label="truck")

    # A first parse, then the same search with the chips driving it.
    client.get("/api/v1/search", params={"q": "truck"})
    client.get("/api/v1/search", params={"q": "truck", "parse": "false", "label": "truck"})
    # And a correction that still parses the sentence.
    client.get("/api/v1/search", params={"q": "truck", "label": "car"})

    s = _samples(metrics.render())
    assert _value(s, "opennvr_search_refinements_total", kind="explicit") == 1
    assert _value(s, "opennvr_search_refinements_total", kind="corrected") == 1


def test_the_rank_that_was_opened_is_the_relevance_signal(client, metrics):
    assert client.post("/api/v1/search/opened", params={"rank": 2}).status_code == 200
    client.post("/api/v1/search/opened", params={"rank": 9})

    s = _samples(metrics.render())
    assert _value(s, "opennvr_search_opened_total") == 2
    # One of the two was in the top three, both within ten.
    assert _value(s, "opennvr_search_open_rank_bucket", le="3") == 1
    assert _value(s, "opennvr_search_open_rank_bucket", le="10") == 2


def test_a_journey_reports_the_method_it_used(client, db, metrics):
    _camera(db, 1, "Gate")
    _camera(db, 2, "Yard")
    a = _visit(db, camera_id=1, label="car", minutes_ago=30, plate="KA01AB1234")
    _visit(db, camera_id=2, label="car", minutes_ago=25, plate="KA01AB1234")

    body = client.get("/api/v1/search/journey", params={"event_id": a.id}).json()
    s = _samples(metrics.render())
    assert _value(s, "opennvr_search_journeys_total", method=body["method"]) == 1
    assert _value(s, "opennvr_search_journey_hops_count") == 1


def test_coverage_says_what_search_can_see(client, db, metrics):
    _camera(db, 1, "Loading dock")
    _visit(db, camera_id=1, label="truck", caption="a red truck")
    _visit(db, camera_id=1, label="person")  # no text: invisible to word queries
    db.add(VisitDescriptor(event_id=1, kind="colour", value="red", source_task="vqa"))
    db.commit()

    from core.config import settings

    # The key the dependency actually compares against, not the one the
    # environment happened to carry when this module was imported.
    body = client.get(
        "/api/v1/search/metrics",
        headers={"X-Internal-Api-Key": settings.internal_api_key},
    )
    assert body.status_code == 200
    s = _samples(body.text)
    assert _value(s, "opennvr_search_visits") == 2
    # The gap between these two is the recall ceiling for any word query.
    assert _value(s, "opennvr_search_enriched_visits") == 1
    assert _value(s, "opennvr_search_described_visits", kind="colour") == 1


def test_the_metrics_door_is_the_site_key(client):
    assert client.get("/api/v1/search/metrics").status_code == 401
    assert client.get(
        "/api/v1/search/metrics", headers={"X-Internal-Api-Key": "wrong"}
    ).status_code == 401


def test_a_histogram_renders_cumulative_buckets(metrics):
    h = metrics.Histogram("t_seconds", "help", (1, 5, 10), ("shape",))
    for v in (0.5, 2.0, 7.0, 40.0):
        h.observe(v, {"shape": "text"})
    s = _samples("\n".join(h.render()) + "\n")
    assert _value(s, "t_seconds_bucket", shape="text", le="1") == 1
    assert _value(s, "t_seconds_bucket", shape="text", le="5") == 2
    assert _value(s, "t_seconds_bucket", shape="text", le="10") == 3
    # +Inf is the total, including the observation past the last bucket.
    assert _value(s, "t_seconds_bucket", shape="text", le="+Inf") == 4
    assert _value(s, "t_seconds_count", shape="text") == 4
    assert _value(s, "t_seconds_sum", shape="text") == 49.5


def test_metrics_never_break_a_search(client, db, metrics, monkeypatch):
    _camera(db, 1, "Loading dock")
    _visit(db, camera_id=1, label="truck")

    class Boom:
        def observe(self, *a, **k):
            raise RuntimeError("registry on fire")

    monkeypatch.setattr(metrics, "SEARCH_SECONDS", Boom())
    r = client.get("/api/v1/search", params={"q": "truck"})
    assert r.status_code == 200 and r.json()["total"] == 1


def test_skills_disagreeing_is_counted_not_hidden(internal_client, db, metrics):
    _camera(db, 1, "Dock")
    row = _visit(db, label="truck")
    post = lambda body: internal_client.post(
        "/api/v1/internal/camera-agent/events/descriptors", json=body)

    post({"event_id": row.id, "descriptors": [
        {"kind": "colour", "value": "red", "source_task": "vqa",
         "source_adapter": "qwen-vl"}]})
    # The same skill improving its own answer is not a disagreement.
    post({"event_id": row.id, "descriptors": [
        {"kind": "colour", "value": "maroon", "source_task": "vqa"}]})
    # A DIFFERENT skill saying something else is, and the row survives —
    # a rising conflict rate is how a skill going wrong becomes visible.
    post({"event_id": row.id, "descriptors": [
        {"kind": "colour", "value": "blue", "source_task": "colour-net"}]})

    s = _samples(metrics.render())
    assert _value(s, "opennvr_search_descriptor_conflicts_total", kind="colour") == 1
    assert _value(s, "opennvr_search_descriptors_written_total",
                  kind="colour", task="vqa", adapter="qwen-vl") == 1


# ── Asking the way people actually ask ───────────────────────────────


class TestConversationalQueries:
    """The box says "describe it", so people address the system. Every
    filler word that survives the parse becomes a REQUIRED substring of
    a caption — and no caption ever written contains "you see", so a
    query whose meaning was understood perfectly returns nothing. That
    is the confident-wrong-parse failure this parser exists to avoid.
    """

    def test_did_you_see_any_car_in_last_5_mins(self, client, db):
        """The exact query that returned nothing while the camera had
        been full of cars for the previous five minutes."""
        _camera(db, 1, "cam1")
        for _ in range(3):
            _visit(db, camera_id=1, label="car", minutes_ago=1)

        body = client.get("/api/v1/search",
                          params={"q": "did you see any car in last 5 mins"}).json()
        interp = body["interpretation"]
        assert interp["labels"] == ["car"]
        assert interp["matched"]["when"] == "last 5 mins"
        # The whole bug: "you see" used to land here and AND itself
        # against every caption.
        assert interp["text"] == ""
        assert body["total"] == 3

    @pytest.mark.parametrize("q", [
        "did you see any car in last 5 mins",
        "can you show me cars today",
        "have you caught any cars today",
        "tell me if there were cars today",
        "check the cameras for a car today",
    ])
    def test_the_same_question_however_it_is_phrased(self, client, db, q):
        _camera(db, 1, "cam1")
        _visit(db, camera_id=1, label="car", minutes_ago=1)
        body = client.get("/api/v1/search", params={"q": q}).json()
        assert body["interpretation"]["labels"] == ["car"]
        assert body["interpretation"]["text"] == "", (
            f"{body['interpretation']['text']!r} became a required "
            f"caption substring")
        assert body["total"] == 1

    def test_this_morning_is_a_window_not_a_phrase_to_match(self, db):
        """Its own test rather than one of the parametrized cases: the
        window is real, so a fixture written "1 minute ago" only falls
        inside it before noon and the case would pass or fail by the
        clock."""
        from services.search_query import parse_query

        parsed = parse_query("was there a car this morning",
                             cameras={1: "cam1"})
        assert parsed.labels == ["car"]
        assert parsed.text == ""
        assert parsed.matched["when"] == "this morning"

    def test_a_real_describing_word_is_still_kept(self, db):
        """The stop list must not eat the words that carry the
        description — "red" is the whole point of "red truck"."""
        from services.search_query import parse_query

        parsed = parse_query("did you see a red truck today",
                             cameras={1: "cam1"})
        assert parsed.labels == ["truck"]
        assert parsed.text == "red"

    def test_a_place_word_is_still_kept(self, db):
        from services.search_query import parse_query

        parsed = parse_query("was anyone near the loading bay last night",
                             cameras={1: "cam1"})
        assert parsed.labels == ["person"]
        assert "bay" in parsed.text


# ── Why an empty search is empty ─────────────────────────────────────


class TestWhyEmpty:
    """"Nothing matched — remove a chip" asks the operator to guess
    which one. Dropping each filter in turn and counting is cheap, runs
    only on an empty result, and turns the guess into one click."""

    def test_it_names_the_chip_that_emptied_the_search(self, client, db):
        _camera(db, 1, "cam1")
        _visit(db, camera_id=1, label="car", minutes_ago=1, caption="a car")

        body = client.get("/api/v1/search",
                          params={"q": "car today", "text": "chartreuse"}).json()
        assert body["total"] == 0
        assert body["relax"], "an empty search should say why"
        first = body["relax"][0]
        assert first["drop"] == "text"
        assert first["value"] == "chartreuse"
        assert first["would_match"] == 1

    def test_it_names_a_time_window_that_is_too_narrow(self, client, db):
        _camera(db, 1, "cam1")
        _visit(db, camera_id=1, label="car", minutes_ago=600)

        body = client.get("/api/v1/search",
                          params={"q": "car in the last 5 minutes"}).json()
        assert body["total"] == 0
        assert [r["drop"] for r in body["relax"]] == ["when"]
        assert body["relax"][0]["would_match"] == 1

    def test_it_stays_quiet_when_there_are_results(self, client, db):
        _camera(db, 1, "cam1")
        _visit(db, camera_id=1, label="car", minutes_ago=1)
        body = client.get("/api/v1/search", params={"q": "car today"}).json()
        assert body["total"] == 1
        assert body["relax"] == []

    def test_it_stays_quiet_when_no_single_chip_explains_it(self, client, db):
        """A genuinely empty store has no chip to blame, and inventing
        one would be worse than silence."""
        _camera(db, 1, "cam1")
        body = client.get("/api/v1/search", params={"q": "car today"}).json()
        assert body["total"] == 0
        assert body["relax"] == []

    def test_a_hint_failure_never_breaks_the_search(self, client, db,
                                                    monkeypatch):
        """Best-effort means best-effort: a working, empty search must
        not become a 500 because the diagnosis blew up."""
        from routers import search as search_router

        def boom(*a, **k):
            raise RuntimeError("count exploded")

        _camera(db, 1, "cam1")
        _visit(db, camera_id=1, label="car", minutes_ago=1)
        monkeypatch.setattr(search_router, "count_search_events",
                            _counting_then(boom, first=0))
        resp = client.get("/api/v1/search",
                          params={"q": "car today", "text": "nope"})
        assert resp.status_code == 200
        assert resp.json()["relax"] == []


def _counting_then(after, *, first=0):
    """count_search_events that answers `first` once, then raises."""
    calls = {"n": 0}

    def wrapped(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return first
        return after(*a, **k)

    return wrapped


# ── A leftover word must not empty the page ─────────────────────────
#
# The parser hands everything it could not interpret to the text filter,
# and that filter is an AND across words. For a word the operator meant,
# that is correct. For the residue of a sentence it is a trap, and the
# trap is not rare: "can you tell me if you seen any car in last 15 mins
# what is number of it" parses `car` and the window correctly, leaves
# `number`, and returns nothing out of 421 matching cars. The example
# question this project ships — "did a red truck come by earlier today"
# — leaves `red come earlier` and fails identically.
#
# A longer stopword list is not the fix. _STOP is already long and
# careful and was one word short; the next sentence brings a word it has
# not met either. These pin the behaviour instead: the structural parse
# stands, the residue is dropped, and the response says it happened.


def test_a_leftover_word_does_not_empty_the_page(client, db):
    """The reported query, end to end."""
    _camera(db, 1, "Gate")
    for _ in range(3):
        _visit(db, camera_id=1, label="car", minutes_ago=2, caption="a car")

    body = client.get("/api/v1/search", params={
        "q": "can you tell me if you seen any car in last 15 mins "
             "what is number of it"}).json()

    assert body["total"] == 3, (
        "the word `number` still empties a page of cars — no caption "
        "contains it, and it is the operator asking a question rather "
        "than describing the footage")
    assert body["interpretation"]["labels"] == ["car"], (
        "relaxing the words must not disturb the structural parse")


def test_the_response_admits_what_it_dropped(client, db):
    """Results the operator did not ask for need saying so.

    Returning 3 cars for a query that asked about a number, silently, is
    a different failure from returning none — it just fails later, when
    they wonder why a filter did nothing.
    """
    _camera(db, 1, "Gate")
    _visit(db, camera_id=1, label="car", minutes_ago=2, caption="a car")

    body = client.get("/api/v1/search", params={
        "q": "any car in last 15 mins what is number of it"}).json()

    assert body.get("relaxed"), "no `relaxed` block on a relaxed search"
    assert body["relaxed"]["dropped"] == "number"
    assert body["relaxed"]["without"] == 1
    # The interpretation reports what was APPLIED.
    assert body["interpretation"]["text"] == ""
    assert "number" in body["interpretation"]["ignored"], (
        "a dropped word belongs in `ignored`, the field that exists to "
        "say what the parser set aside instead of pretending")


def test_words_the_caller_passed_are_never_dropped(client, db):
    """An explicit `text=` is not residue.

    A caller who passed words meant them — a UI chip the operator typed,
    an integration filtering deliberately — and is owed the empty result
    they asked for rather than a broader one they did not.
    """
    _camera(db, 1, "Gate")
    _visit(db, camera_id=1, label="car", minutes_ago=2, caption="a car")

    body = client.get("/api/v1/search", params={
        "q": "car today", "text": "number"}).json()

    assert body["total"] == 0
    assert "relaxed" not in body
    assert body["interpretation"]["text"] == "number"


def test_a_search_that_matched_is_left_alone(client, db):
    """Never touches a working search."""
    _camera(db, 1, "Gate")
    _visit(db, camera_id=1, label="truck", minutes_ago=2,
           caption="a red truck at the dock")

    body = client.get("/api/v1/search", params={"q": "red truck today"}).json()

    assert body["total"] == 1
    assert "relaxed" not in body
    assert body["interpretation"]["text"] == "red", (
        "a word that MATCHES must keep filtering — relaxing unconditionally "
        "would make every text search return everything")


def test_structural_chips_are_not_relaxed(client, db):
    """Only the words. Dropping a label or a window answers a different
    question, which is the failure being fixed, not a fallback."""
    _camera(db, 1, "Gate")
    _visit(db, camera_id=1, label="car", minutes_ago=2, caption="a car")

    body = client.get("/api/v1/search", params={"q": "any dog today"}).json()

    assert body["total"] == 0, "a label with no matches must stay empty"
    assert "relaxed" not in body
    # The one-click offer still stands — an offer, not a substitution.
    assert any(r["drop"] == "labels" for r in body.get("relax", []))


def test_plate_number_is_one_phrase_not_a_word_to_match():
    """`numberplate` was already a cue; `plate number` is the same phrase
    with a space in it, and leaving the tail behind demanded a caption
    containing the word "number"."""
    assert parse_query("what is the plate number of the car").text == ""
    assert parse_query("registration number for that van").text == ""
    # Only beside a cue. Alone it is an ordinary word, and _STOP is not
    # where a word with two meanings belongs.
    assert parse_query("number 5 door").text == "number door"


def test_words_are_not_relaxed_when_they_were_the_whole_query(client, db):
    """Relaxing needs something to stand on.

    "zebra 42" parses to no label, no window and no camera. Dropping
    `zebra` does not broaden that search, it deletes it — and answers
    "did you see a zebra" with every visit in the database. An empty
    result is the honest answer when the words WERE the query.
    """
    _camera(db, 1, "Dock")
    _visit(db, camera_id=1, label="person", minutes_ago=2,
           caption="a person walking")

    body = client.get("/api/v1/search", params={"q": "zebra"}).json()

    assert body["total"] == 0
    assert "relaxed" not in body


# ── The internal events read: what a camera agent can ask ────────────
#
# The agent's search_history could filter by label, time, camera and
# plate and nothing else, so "did you see a blue car in the last hour"
# became "any car in the last hour" — the word `blue` dropped before the
# query was built, and the answer confidently described a different
# question. The capability existed in the user-facing search the whole
# time; the internal endpoint the agent uses simply never exposed it.


def test_the_agent_can_filter_by_what_a_skill_said(internal_client, db):
    _camera(db, 1, "Gate")
    blue = _visit(db, camera_id=1, label="car", minutes_ago=5)
    red = _visit(db, camera_id=1, label="car", minutes_ago=5)
    _claim(db, blue.id, "colour", "blue")
    _claim(db, red.id, "colour", "red")

    body = internal_client.get(
        "/api/v1/internal/camera-agent/events",
        params={"label": "car", "attr": "blue"}).json()

    assert [e["id"] for e in body["events"]] == [blue.id]
    assert body["attrs_applied"] == ["blue"]


def test_a_bare_value_survives_the_colour_spelling(internal_client, db):
    """The kind is `colour`. Every caller who has not read
    descriptor_enrichment.LABEL_KINDS will write `color`, and a
    kind-scoped query with the wrong spelling returns nothing while
    looking exactly like "no blue cars" — so a bare value matches any
    kind, and that is the spelling the tool schema advertises."""
    _camera(db, 1, "Gate")
    row = _visit(db, camera_id=1, label="car", minutes_ago=5)
    _claim(db, row.id, "colour", "blue")

    bare = internal_client.get("/api/v1/internal/camera-agent/events",
                               params={"attr": "blue"}).json()
    right = internal_client.get("/api/v1/internal/camera-agent/events",
                                params={"attr": "colour:blue"}).json()
    wrong = internal_client.get("/api/v1/internal/camera-agent/events",
                                params={"attr": "color:blue"}).json()

    assert [e["id"] for e in bare["events"]] == [row.id]
    assert [e["id"] for e in right["events"]] == [row.id]
    assert wrong["events"] == [], "an explicit kind stays exact"


def test_attributes_and_rather_than_widen(internal_client, db):
    _camera(db, 1, "Gate")
    both = _visit(db, camera_id=1, label="car", minutes_ago=5)
    one = _visit(db, camera_id=1, label="car", minutes_ago=5)
    _claim(db, both.id, "colour", "blue")
    _claim(db, both.id, "vehicle_type", "van")
    _claim(db, one.id, "colour", "blue")

    body = internal_client.get(
        "/api/v1/internal/camera-agent/events",
        params={"attr": ["blue", "van"]}).json()

    assert [e["id"] for e in body["events"]] == [both.id]


def test_an_empty_attribute_search_says_which_kind_of_empty(internal_client, db):
    """The whole point, and the reason this endpoint reports counts.

    "No blue cars" and "nothing ever looked at what colour anything was"
    are opposite answers that produce an identical empty list. An agent
    that cannot tell them apart tells an operator there was no blue car
    when the truth is that nobody asked — confidently wrong about a
    security question, which is the failure mode this project keeps
    finding and keeps having to fix.
    """
    _camera(db, 1, "Gate")
    described = _visit(db, camera_id=1, label="car", minutes_ago=5)
    _visit(db, camera_id=1, label="car", minutes_ago=5)   # never looked at
    _visit(db, camera_id=1, label="car", minutes_ago=5)   # never looked at
    _claim(db, described.id, "colour", "red")

    body = internal_client.get(
        "/api/v1/internal/camera-agent/events",
        params={"label": "car", "attr": "blue"}).json()

    assert body["events"] == []
    assert body["described"]["in_window"] == 3
    assert body["described"]["with_any_descriptor"] == 1, (
        "without this an agent cannot say 'one of the three was described "
        "and it was not blue; the other two nobody looked at'")


def test_the_counts_are_absent_when_no_attribute_was_asked(internal_client, db):
    """Two extra counts on every history read would be a cost paid by
    every caller for a feature most do not use."""
    _camera(db, 1, "Gate")
    _visit(db, camera_id=1, label="car", minutes_ago=5)

    body = internal_client.get("/api/v1/internal/camera-agent/events",
                               params={"label": "car"}).json()

    assert "described" not in body
    assert "attrs_applied" not in body
    assert len(body["events"]) == 1


# ── The people picker ────────────────────────────────────────────────
#
# A name is deliberately not a search word (descriptor_store
# ._UNPROJECTED_KINDS), so "was Varun here yesterday" typed into the box
# matches nothing and always will. The filter that answers it —
# attr=face_id:varun — has worked all along and was unreachable: you had
# to know the exact value to ask. These cover the list that makes it a
# picker.


def test_the_people_list_is_drawn_from_what_was_actually_seen(client, db):
    """Not from the recogniser's enrolment roster.

    Core has no route to that roster, and a name enrolled but never seen
    would offer a filter that can only ever return nothing — the exact
    "couldn't ask" that this endpoint exists to remove, reintroduced as
    "asked and got nothing"."""
    _camera(db, 1, "Door")
    seen = _visit(db, camera_id=1, label="person", minutes_ago=5)
    _claim(db, seen.id, "face_id", "varun", task="doorbell")
    # Somebody else was there too, and one visit nobody recognised.
    other = _visit(db, camera_id=1, label="person", minutes_ago=3)
    _claim(db, other.id, "face_id", "priya", task="doorbell")
    _visit(db, camera_id=1, label="person", minutes_ago=1)

    body = client.get("/api/v1/search/people").json()

    assert body["kind"] == "face_id"
    assert {p["value"] for p in body["people"]} == {"varun", "priya"}
    # Every entry carries the exact filter it stands for, so the UI never
    # has to rebuild the pair and get the separator wrong.
    assert {p["attr"] for p in body["people"]} == {"face_id:varun", "face_id:priya"}


def test_a_person_the_picker_offers_is_a_person_the_filter_finds(client, db):
    """The one property that matters. An offered name that returns
    nothing is worse than no picker: it reads as "they were never here"."""
    _camera(db, 1, "Door")
    row = _visit(db, camera_id=1, label="person", minutes_ago=5)
    _claim(db, row.id, "face_id", "varun", task="doorbell")

    offered = client.get("/api/v1/search/people").json()["people"]
    assert offered, "nothing to pick means nothing to prove"
    for person in offered:
        found = client.get("/api/v1/search",
                           params={"attr": person["attr"], "parse": "false"}).json()
        assert found["total"] >= 1, f"{person['attr']} was offered and matches nothing"


def test_one_visit_claimed_twice_is_one_sighting(client, db):
    """Two tasks recognising the same face on the same visit is a fact
    about the skills, not a second visit — and a count that says "2
    sightings" next to one thumbnail is a number the operator cannot
    reconcile with the page."""
    _camera(db, 1, "Door")
    row = _visit(db, camera_id=1, label="person", minutes_ago=5)
    _claim(db, row.id, "face_id", "varun", task="doorbell")
    _claim(db, row.id, "face_id", "varun", task="insightface")

    people = client.get("/api/v1/search/people").json()["people"]
    assert [p["visits"] for p in people] == [1]


def test_the_most_recently_seen_person_is_offered_first(client, db):
    _camera(db, 1, "Door")
    old = _visit(db, camera_id=1, label="person", minutes_ago=600)
    _claim(db, old.id, "face_id", "priya", task="doorbell")
    recent = _visit(db, camera_id=1, label="person", minutes_ago=2)
    _claim(db, recent.id, "face_id", "varun", task="doorbell")

    people = client.get("/api/v1/search/people").json()["people"]
    assert [p["value"] for p in people] == ["varun", "priya"]
    assert people[0]["last_seen"] is not None


def test_the_picker_only_offers_people_seen_on_cameras_you_can_see(client, db):
    """Same scope rule as the results. Offering a name from a camera
    whose footage the caller cannot open would disclose the person
    through the picker and then show them nothing — a leak and a dead
    end in one control."""
    _camera(db, 1, "Door")
    somebody_elses = Camera(id=2, name="Neighbour", ip_address="10.0.0.2",
                            rtsp_url="rtsp://x/2", owner_id=99)
    db.add(somebody_elses)
    db.commit()
    mine = _visit(db, camera_id=1, label="person", minutes_ago=5)
    _claim(db, mine.id, "face_id", "varun", task="doorbell")
    theirs = _visit(db, camera_id=2, label="person", minutes_ago=5)
    _claim(db, theirs.id, "face_id", "priya", task="doorbell")

    people = client.get("/api/v1/search/people").json()["people"]
    assert [p["value"] for p in people] == ["varun"]


def test_a_box_that_has_recognised_nobody_offers_nothing(client, db):
    """An empty list, not an error and not a placeholder. The UI renders
    no control at all, which is the honest thing: there is nothing to
    pick, and a disabled dropdown would imply there might be."""
    _camera(db, 1, "Door")
    row = _visit(db, camera_id=1, label="person", minutes_ago=5)
    _claim(db, row.id, "colour", "red")

    body = client.get("/api/v1/search/people").json()
    assert body["people"] == []


def test_names_stay_out_of_the_words_even_though_they_are_listed(client, db):
    """Listing a name for a picker must not have made it searchable as
    text. If it ever does, this test fails and the privacy rule in
    descriptor_store has been quietly undone."""
    from services.descriptor_store import project_attributes

    _camera(db, 1, "Door")
    row = _visit(db, camera_id=1, label="person", minutes_ago=5)
    _claim(db, row.id, "face_id", "varun", task="doorbell")
    _claim(db, row.id, "clothing_top", "blue")
    project_attributes(db, row)
    db.commit()

    assert client.get("/api/v1/search/people").json()["people"][0]["value"] == "varun"
    assert client.get("/api/v1/search",
                      params={"text": "varun", "parse": "false"}).json()["total"] == 0
    # …and the claim it rode in with is still a word.
    assert client.get("/api/v1/search",
                      params={"text": "blue", "parse": "false"}).json()["total"] == 1



# ── the route: names, plate questions, and what the box can answer ───


def test_a_known_name_is_searched_as_a_person_not_a_word(client, db):
    """Typing a name used to match nothing, silently, forever."""
    _camera(db, 1, "Door")
    seen = _visit(db, camera_id=1, label="person", minutes_ago=3)
    _visit(db, camera_id=1, label="person", minutes_ago=4)
    _claim(db, seen.id, "face_id", "varun-singh", task="face_recognition")
    body = client.get("/api/v1/search", params={"q": "did you see varun today"}).json()
    assert body["interpretation"]["attrs"] == ["face_id:varun-singh"]
    assert body["interpretation"]["text"] == ""
    assert [r["id"] for r in body["results"]] == [seen.id]
    need = next(n for n in body["interpretation"]["needs"] if n["kind"] == "face_id")
    assert need["skill"] == "face_recognition"


def test_asking_for_a_plate_number_returns_only_visits_with_a_read(client, db):
    _camera(db, 1, "Gate")
    read = _visit(db, camera_id=1, label="car", minutes_ago=2,
                  caption="a white car", plate="KA01AB1234")
    _visit(db, camera_id=1, label="car", minutes_ago=1, caption="a white car")
    body = client.get("/api/v1/search",
                      params={"q": "what is the plate number of the white car today"}).json()
    assert body["interpretation"]["wants_plate"] is True
    assert [r["id"] for r in body["results"]] == [read.id]
    assert body["answer"]["plates"] == ["KA01AB1234"]


def test_the_response_names_the_skill_a_question_needs(client, db):
    """"Red shirt" on a box with no clothing skill: the need is listed
    with its state, so an empty page can say why."""
    _camera(db, 1, "Door")
    _visit(db, camera_id=1, label="person", minutes_ago=3, caption="a person walking")
    body = client.get("/api/v1/search",
                      params={"q": "did you see a person in a red shirt today"}).json()
    needs = {n["kind"]: n for n in body["interpretation"]["needs"]}
    assert "clothing_top" in needs
    assert needs["clothing_top"]["word"] == "red"
    assert needs["clothing_top"]["skill"] == "vqa"
    assert needs["clothing_top"]["state"] in ("not-on-this-box", "never-produced", "people-off")


def test_a_bare_attr_word_also_matches_the_caption(db):
    """The agent asks "described as blue" with a bare word. A caption is
    a description: a visit the captioner called "a blue car" matches even
    before (or without) a colour claim. A kind-scoped chip does not."""
    _camera(db, 1, "Gate")
    captioned = _visit(db, camera_id=1, label="car", minutes_ago=3, caption="a blue car parked")
    _visit(db, camera_id=1, label="car", minutes_ago=4, caption="a white van")
    claimed = _visit(db, camera_id=1, label="car", minutes_ago=5)
    _claim(db, claimed.id, "colour", "blue")
    bare = {h.event.id for h in search_events(db, attrs=[(None, "blue")], scope=None)}
    assert bare == {captioned.id, claimed.id}
    scoped = {h.event.id for h in search_events(db, attrs=[("colour", "blue")], scope=None)}
    assert scoped == {claimed.id}


# ── "not yet" is not "no" (#583) ───────────────────────────────────────

def test_a_question_that_needs_a_description_queues_it_and_says_so(client, db, monkeypatch):
    """"red car today" on a box with a captioner: the two undescribed cars
    are queued for description now, and the answer carries how many and
    roughly how long — so an empty page can say 'not yet'."""
    from services import enrichment_plan as plan_mod
    from services import enrichment_requests as er

    async def _plan(_label=None):
        # A healthy captioner and a VQA offering colour: what the route
        # resolves `needs` from, and now the lane's offers too.
        return {"skills": [{"task": "image_captioning", "healthy": True, "adapters": ["m"]},
                           {"task": "vqa", "healthy": True, "adapters": ["m"],
                            "descriptor_kinds": ["colour", "vehicle_type"]}]}
    monkeypatch.setattr(plan_mod, "compute_enrichment_plan", _plan)
    er._reset_for_tests()
    gate = _camera(db, 1, "Gate")
    gate.assignments = [{"skill": "image_captioning"}, {"skill": "vqa"}]; db.commit()
    _visit(db, camera_id=1, label="car", minutes_ago=3, caption="a red car")
    _visit(db, camera_id=1, label="car", minutes_ago=4)
    _visit(db, camera_id=1, label="car", minutes_ago=5)

    # No "today": a window parsed at local midnight would race the clock
    # around midnight against WALL-stamped visits. The question is the point.
    body = client.get("/api/v1/search", params={"q": "red car"}).json()
    pending = body["pending"]
    # "red" needs a caption AND a colour claim: two cars lack the caption,
    # all three lack the claim — three visits need something.
    assert pending["missing"] == 3 and pending["requested"] == 3
    assert pending["needs"]["caption"] is True and "colour" in pending["needs"]["kinds"]
    assert pending["eta_s"] > 0 and pending["request_id"]

    page2 = client.get("/api/v1/search", params={"q": "red car", "skip": 1}).json()
    assert page2["pending"] is None, "paging through the same question re-scans nothing"
    again = client.get("/api/v1/search", params={"q": "red car"}).json()["pending"]
    assert again["requested"] == 0 and again["already_queued"] == 3

    off = client.get("/api/v1/search", params={"q": "red car", "describe": "false"}).json()
    assert off["pending"] is None, "describe=false answers from what exists and queues nothing"
    er._reset_for_tests()


def test_a_plain_label_and_time_search_queues_nothing(client, db, monkeypatch):
    from services import enrichment_plan as plan_mod
    from services import enrichment_requests as er

    async def _plan(_label=None):
        return {"skills": [{"task": "image_captioning", "healthy": True, "adapters": ["m"]},
                           {"task": "vqa", "healthy": True, "adapters": ["m"],
                            "descriptor_kinds": ["colour"]}]}
    monkeypatch.setattr(plan_mod, "compute_enrichment_plan", _plan)
    gate = _camera(db, 1, "Gate")
    gate.assignments = [{"skill": "image_captioning"}, {"skill": "vqa"}]; db.commit()
    _visit(db, camera_id=1, label="car", minutes_ago=4)
    body = client.get("/api/v1/search", params={"q": "car"}).json()
    assert body["pending"] is None
    assert er.queue_depth() == 0


# ── The window and the rows agree on a timezone ──────────────────────
#
# Rows are stored in UTC; the parser's window is local time. On SQLite,
# which compares the text it is given, a +05:30 window against +00:00
# rows matched nothing, so "any car in the last 5 minutes" returned 0 on
# every box outside UTC — and CI, which runs in UTC, never saw it. This
# test pins the clock to a zone that is not UTC, whatever the machine's.

def test_the_window_finds_rows_whatever_the_local_timezone(client, db, monkeypatch):
    import routers.search as route
    from zoneinfo import ZoneInfo

    # The route calls parse_query with no clock, so the parser reads the
    # machine's. Hand it WALL on a Kolkata clock instead, through the route's
    # own reference, so this holds on a UTC machine (CI) as well as on a laptop.
    kolkata = ZoneInfo("Asia/Kolkata")
    real_parse = route.parse_query
    monkeypatch.setattr(route, "parse_query",
                        lambda q, **kw: real_parse(q, now=WALL.astimezone(kolkata), **kw))

    _camera(db, 1, "cam1")
    for _ in range(3):
        _visit(db, camera_id=1, label="car", minutes_ago=1)

    body = client.get("/api/v1/search", params={"q": "any car in the last 5 mins"}).json()
    assert body["interpretation"]["matched"]["when"] == "last 5 mins"
    assert body["total"] == 3


# ── counts and groups ───────────────────────────────────────────────

def test_counts_and_groups_become_min_people_not_words():
    """'did you see the 3 persons together' used to parse to free text
    'persons together', match nothing, and relax to every visit."""
    p = _p("did you see the 3 persons together")
    assert p.labels == ["person"] and p.min_people == 3 and p.text == ""
    assert p.matched["count"] == "3 together" and p.ignored == []
    p = _p("two people")
    assert p.labels == ["person"] and p.min_people == 2 and p.text == ""
    p = _p("a group of men near the door")
    assert p.labels == ["person"] and p.min_people == 2 and p.text == "door"
    p = _p("three cars")
    assert p.labels == ["car"] and p.min_people is None, "a count of cars is not a people count"
    p = _p("person blue")
    assert p.min_people is None and p.text == "blue"
    assert "min_people" in p.as_dict()


def test_min_people_filters_on_the_pipelines_count(db):
    from services.search_service import count_search_events
    from services.timeline_service import record_track_visit
    t = NOW
    alone = record_track_visit(db, camera_id=1, label="person", started_at=t, ended_at=t,
                               people=1)
    trio = record_track_visit(db, camera_id=1, label="person", started_at=t, ended_at=t,
                              track_id="b", people=3)
    old = record_track_visit(db, camera_id=1, label="person", started_at=t, ended_at=t,
                             track_id="c")                      # before the count existed
    db.commit()
    assert trio.payload["people"] == 3 and "people" not in (old.payload or {})
    base = dict(from_=None, to=None, plate=None, source=None, scope=None, zone_id=None, has_plate=False)
    # The one counted alone is out; the trio is in; the visit with NO count
    # is unknown, not alone, and stays in for the captions and vectors to
    # judge (every two-men visit on the Ventuno Q predated the count).
    assert count_search_events(db, labels=["person"], camera_ids=None, text="", attrs=None,
                               min_people=2, **base) == 2
    assert count_search_events(db, labels=["person"], camera_ids=None, text="", attrs=None,
                               min_people=4, **base) == 1, "only the uncounted one passes a bar the trio misses"
    assert count_search_events(db, labels=["person"], camera_ids=None, text="", attrs=None,
                               min_people=None, **base) == 3


def test_a_colour_binds_to_the_thing_it_describes_in_the_text_query():
    """'blue shirt' is blue within two words of shirt, not blue anywhere
    plus shirt anywhere; 'blue' alone with a person is blue clothing."""
    from services.search_service import fts_query
    # <N> is EXACTLY N apart in Postgres, so "within two" is <-> OR <2>
    assert fts_query("blue shirt") == ("to_tsquery", "((blue <-> shirt) | (blue <2> shirt))")
    assert fts_query("light blue t-shirt") == ("to_tsquery", "light & ((blue <-> shirt) | (blue <2> shirt))")
    fn, arg = fts_query("blue")
    assert fn == "to_tsquery" and arg.startswith("((blue <-> (shirt | t | tshirt | tee | top")
    assert fts_query("beard laptop") == ("plainto_tsquery", "beard laptop")
    assert fts_query("red van") == ("to_tsquery", "((red <-> van) | (red <2> van))")
    fn, arg = fts_query("man red carrying box")
    assert fn == "to_tsquery" and arg.startswith("man & ((red <-> (shirt") and arg.endswith(")) & carrying & box")
def test_alert_words_bring_matching_app_alerts_beside_the_visits(db):
    """"loitering at the gate" has its answer in the alerts table, not in a
    visit: the words that name an alert's subject bring the matching app
    alerts in the same window; other questions bring none."""
    from types import SimpleNamespace
    from models import AppAlert
    from routers.search import _matching_alerts
    t = NOW
    db.add(AppAlert(alert_id="l1", fired_at=t - timedelta(minutes=5), severity="high",
                    title="Person loitering at gate", camera_id="cam1", source_name="loitering",
                    alert_type="loitering_detected"))
    db.add(AppAlert(alert_id="p1", fired_at=t - timedelta(minutes=4), severity="low",
                    title="Package left at door", camera_id="cam2", source_name="package-delivery",
                    alert_type="package_left"))
    db.add(AppAlert(alert_id="old", fired_at=t - timedelta(days=3), severity="high",
                    title="Person loitering at gate", camera_id="cam1", source_name="loitering"))
    db.commit()
    su = SimpleNamespace(is_superuser=True, role=None, id=1)
    win = dict(start=t - timedelta(hours=1), end=t + timedelta(hours=1), labels=[], camera_ids=[])
    assert _matching_alerts(db, None, su, words="car blue", **win) is None, "no alert word: no lane"
    got = _matching_alerts(db, None, su, words="loitering gate", **win)
    assert [a["alert_id"] for a in got] == ["l1"]
    assert got[0]["camera_id"] == 1 and got[0]["severity"] == "high"
    got = _matching_alerts(db, None, su, words="package", camera_ids=[2], start=win["start"], end=win["end"], labels=[])
    assert [a["alert_id"] for a in got] == ["p1"]
    assert _matching_alerts(db, None, su, words="package", camera_ids=[1], start=win["start"], end=win["end"], labels=[]) == []
    assert _matching_alerts(db, None, su, words="any alerts", **win) and len(_matching_alerts(db, None, su, words="alerts", **win)) == 2
# ── calendar phrases: months, years, dates ──────────────────────────

def test_month_and_year_phrases_become_windows():
    """NOW is 18 Sep 2026. A month name alone is the most recent one that
    has started; two months joined by "or" are one window; "last month",
    "this year", "N months ago" and dated days all resolve."""
    jan = _p("did you see a car in january")
    assert (jan.from_, jan.to) == (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC))
    assert jan.labels == ["car"] and jan.text == "" and jan.matched["when"] == "january"
    span = _p("anything last year somewhere in january or february")
    assert (span.from_, span.to) == (datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 3, 1, tzinfo=UTC)) or \
        (span.from_, span.to) == (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 3, 1, tzinfo=UTC))
    assert span.text == ""
    dec = _p("the van in december")                       # december has not come this year
    assert (dec.from_, dec.to) == (datetime(2025, 12, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
    lm = _p("last month")
    assert (lm.from_, lm.to) == (datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC))
    tm = _p("this month")
    assert tm.from_ == datetime(2026, 9, 1, tzinfo=UTC) and tm.to == NOW
    ly = _p("last year")
    assert (ly.from_, ly.to) == (datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
    ago = _p("two months ago")
    assert (ago.from_, ago.to) == (datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC))
    six = _p("last 6 months")
    assert six.from_ == datetime(2026, 3, 1, tzinfo=UTC) and six.to == NOW
    day = _p("on 5th november 2025 a truck")
    assert (day.from_, day.to) == (datetime(2025, 11, 5, tzinfo=UTC), datetime(2025, 11, 6, tzinfo=UTC))
    assert day.labels == ["truck"] and day.text == ""
    day2 = _p("nov 5")                                     # future this year → last year's
    assert day2.from_ == datetime(2025, 11, 5, tzinfo=UTC)
    may = _p("may")                                        # a month word that is also a verb: still a month
    assert may.from_ == datetime(2026, 5, 1, tzinfo=UTC)
    assert _p("car with plate KA01AB1234").from_ is None, "no time means all time"


# ── typos against the box's own vocabulary ──────────────────────────────


def test_a_word_one_letter_off_a_known_word_is_read_as_that_word():
    parsed = parse_query("persn at the dok", cameras={1: "Dock"}, now=NOW)
    assert parsed.labels == ["person"]
    assert parsed.camera_ids == [1]
    assert parsed.corrections == {"persn": "person", "dok": "dock"}
    assert parsed.matched["corrected"] == "persn → person, dok → dock"
    assert parsed.text == ""


def test_corrections_can_be_refused_and_never_touch_known_words_numbers_or_plates():
    literal = parse_query("persn at the dok", cameras={1: "Dock"}, now=NOW, correct=False)
    assert literal.labels == [] and literal.text == "persn dok" and literal.corrections == {}
    plain = parse_query("person at the dock gate14 KA01AB1234", cameras={1: "Dock"}, now=NOW)
    assert plain.corrections == {}
    assert plain.plate == "KA01AB1234"


def test_a_short_or_ambiguous_word_is_left_alone():
    from services.search_query import correct_word

    vocab = {"car", "cars", "cart", "truck", "person", "dock", "deck"}
    assert correct_word("van", vocab) is None, "a three-letter word only takes a near-identical match"
    assert correct_word("dok", vocab) == "dock"
    assert correct_word("cart", vocab - {"cart"}) is None, "'cart' is not 'car' (lengths differ)"
    assert correct_word("trcuk", vocab) == "truck"
    assert correct_word("dack", vocab) is None, "dock and deck are equally close: do not guess"
    assert correct_word("gate14", vocab) is None


def test_vocabulary_words_from_the_description_skills_are_corrected_too():
    parsed = parse_query("blu shrit", vocab={"blue", "shirt", "red"}, now=NOW)
    assert parsed.corrections == {"blu": "blue", "shrit": "shirt"}
    assert parsed.text == "blue shirt"


def test_the_api_says_what_it_corrected_and_correct_false_searches_the_letters(client, db):
    _camera(db, 1, "Dock")
    _visit(db, label="person", caption="a person walking")
    body = client.get("/api/v1/search", params={"q": "persn"}).json()
    assert body["interpretation"]["labels"] == ["person"]
    assert body["interpretation"]["corrections"] == {"persn": "person"}
    assert body["total"] == 1
    literal = client.get("/api/v1/search", params={"q": "persn", "correct": "false"}).json()
    assert literal["interpretation"]["corrections"] == {}
    assert literal["interpretation"]["labels"] == []


# ── plates match by OCR-equivalence class ──────────────────────────────


def test_plate_class_folds_the_letters_a_reader_confuses():
    from services.search_query import plate_class

    assert plate_class("KA-01 AB 1234") == "KA01A81234"
    assert plate_class("ka01ab1234") == plate_class("KA01A81234") == plate_class("KAO1AB1234")
    assert plate_class("") == ""


def test_a_plate_typed_with_a_confused_letter_still_finds_the_read(db):
    _camera(db, 1, "Dock")
    hit = _visit(db, label="car", plate="KA01AB1234")
    _visit(db, label="car", plate="MH12CD5678")
    for typed in ("KA01A81234", "KAO1AB1234", "ka01ab1234", "AB1234", "A81234"):
        hits = search_events(db, labels=None, camera_ids=None, text="", plate=typed, limit=10, skip=0)
        assert [h.event.id for h in hits] == [hit.id], typed
    assert search_events(db, labels=None, camera_ids=None, text="", plate="ZZ99", limit=10, skip=0) == []


# ── the vector arms hear the whole question ────────────────────────────


def test_the_parser_keeps_the_whole_corrected_question_for_the_vector_arms():
    """``text`` is the word arm's residue; ``question`` is the sentence
    the embedders get — corrected, without the time phrase and the
    camera names, with the class words, colours and counts kept."""
    parsed = parse_query("pink car at the dock yesterday", cameras={1: "Dock"}, now=NOW)
    assert parsed.labels == ["car"] and parsed.camera_ids == [1]
    assert parsed.question == "pink car at the"
    parsed = parse_query("three people together", now=NOW)
    assert parsed.min_people == 3 and parsed.text == ""
    assert parsed.question == "three people together"
    parsed = parse_query("persn in a blu shrit", vocab={"blue", "shirt"}, now=NOW)
    assert parsed.question == "person in a blue shirt"
    assert parse_query("", now=NOW).question == ""
    assert "question" in parsed.as_dict()


def test_the_route_embeds_the_question_not_the_residue(client, db, monkeypatch):
    """"pink car" leaves no residue for the word arm and used to run no
    semantic arm at all; the embedders now get the question itself.
    Explicit text= is embedded as given."""
    import routers.search as sr
    _camera(db, 1, "Dock")
    _visit(db, label="car", caption="a pink car")
    heard: list[str] = []

    async def _vectors(_db, words):
        heard.append(words)
        return None, None, "no-embeddings"

    monkeypatch.setattr(sr, "_query_vectors", _vectors)
    body = client.get("/api/v1/search", params={"q": "pink car"}).json()
    assert body["interpretation"]["labels"] == ["car"]
    assert body["interpretation"]["question"] == "pink car"
    client.get("/api/v1/search", params={"q": "pink car", "text": "lorry"})
    client.get("/api/v1/search", params={"q": "three people together at the dock"})
    assert heard == ["pink car", "lorry", "three people together at the"]


def test_the_captions_own_words_are_part_of_the_typo_vocabulary(client, db, monkeypatch):
    """"camoflage" is no filter word, so it fell through to the caption
    text and matched nothing; the captions say "camouflage" all day."""
    import routers.search as sr
    _camera(db, 1, "Dock")
    _visit(db, label="person", caption="a person in a camouflage uniform")
    _visit(db, label="person", caption="a person wearing camouflage", minutes_ago=7)
    _visit(db, label="person", caption="a person holding a kurta", minutes_ago=9)   # once: not vocabulary
    monkeypatch.setattr(sr, "_caption_vocab_cache", None)
    vocab = sr._caption_vocabulary(db)
    assert "camouflage" in vocab and "person" in vocab
    assert "kurta" not in vocab, "a one-off word is as likely a typo as vocabulary"
    assert "uniform" not in vocab
    body = client.get("/api/v1/search", params={"q": "camoflage"}).json()
    assert body["interpretation"]["corrections"] == {"camoflage": "camouflage"}
    assert body["total"] == 2
    # Cached: a new caption is not seen until the TTL passes.
    _visit(db, label="person", caption="a zebra and another zebra", minutes_ago=11)
    assert "zebra" not in sr._caption_vocabulary(db)
    monkeypatch.setattr(sr, "_caption_vocab_cache", None)
    assert "zebra" in sr._caption_vocabulary(db)


def test_the_fusion_weights_come_from_the_settings_and_fall_back_to_one(monkeypatch):
    import routers.search as sr
    from core.config import settings
    assert sr._arm_weights(settings) == {"text": 1.0, "vector": 1.0, "caption": 0.5}
    monkeypatch.setattr(settings, "search_caption_arm_weight", "garbage", raising=False)
    monkeypatch.setattr(settings, "search_vector_arm_weight", 0.25, raising=False)
    assert sr._arm_weights(settings) == {"text": 1.0, "vector": 0.25, "caption": 1.0}


# ── voice search ───────────────────────────────────────────────────────


def test_a_spoken_search_is_transcribed_on_the_box_and_read_back_as_a_query(client, db, monkeypatch):
    import services.search_voice as sv
    _camera(db, 1, "Dock")
    heard: list[tuple[bytes, str]] = []

    async def _transcribe(audio, *, language="en"):
        heard.append((audio, language))
        return "show me the white car at cam twenty three", "whisper"

    monkeypatch.setattr(sv, "transcribe", _transcribe)
    wav = base64.b64encode(b"RIFF" + b"\x00" * 64).decode()
    r = client.post("/api/v1/search/transcribe", json={"audio_b64": wav})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["transcript"] == "show me the white car at cam twenty three"
    assert body["query"] == "the white car at cam 23"
    assert body["changes"] == ["dropped “show me”", "“twenty three” → 23"]
    assert body["adapter"] == "whisper" and heard[0][1] == "en"
    assert heard[0][0].startswith(b"RIFF")


def test_a_spoken_search_without_an_stt_adapter_is_a_503_that_says_so(client, db, monkeypatch):
    import services.search_voice as sv

    async def _none(audio, *, language="en"):
        return None, None

    monkeypatch.setattr(sv, "transcribe", _none)
    wav = base64.b64encode(b"RIFF" + b"\x00" * 64).decode()
    r = client.post("/api/v1/search/transcribe", json={"audio_b64": wav})
    assert r.status_code == 503 and r.json()["detail"]["reason"] == "no-stt"
    assert client.post("/api/v1/search/transcribe", json={"audio_b64": "@@@@not-base64@@@@"}).status_code == 400
    big = base64.b64encode(b"\x00" * (2 * 1024 * 1024 + 1)).decode()
    assert client.post("/api/v1/search/transcribe", json={"audio_b64": big}).status_code == 413


# ── the LLM re-rank, when asked for ────────────────────────────────────


def test_the_rerank_is_off_by_default_and_moves_the_models_yes_rows_first(client, db, monkeypatch):
    import routers.search as sr
    import services.search_rerank as rr
    _camera(db, 1, "Dock")
    a = _visit(db, label="person", caption="a man with a laptop", minutes_ago=3)
    b = _visit(db, label="person", caption="two men looking at a laptop", minutes_ago=6)
    c = _visit(db, label="person", caption="a laptop on a desk", minutes_ago=9)

    async def _vectors(_db, words):
        return [1.0], None, None
    monkeypatch.setattr(sr, "_query_vectors", _vectors)
    from services import search_service
    real = search_service.search_page

    def _page(*args, **kw):
        page = real(*args, **kw)
        page.semantic = {"used": True, "arms": {"text": 3}}
        return page
    monkeypatch.setattr(sr, "search_page", _page)
    asked: list = []

    async def _rerank(query, captions, *, timeout_s=8.0):
        asked.append((query, list(captions)))
        return [2], {"asked": len(captions), "kept": 1, "ms": 12}
    monkeypatch.setattr(rr, "rerank", _rerank)

    body = client.get("/api/v1/search", params={"q": "laptop"}).json()
    assert asked == [], "off unless asked"
    assert "rerank" not in (body.get("semantic") or {})

    body = client.get("/api/v1/search", params={"q": "laptop", "rerank": 3}).json()
    assert asked and asked[0][0] == "laptop"
    assert body["semantic"]["rerank"] == {"asked": len(asked[0][1]), "kept": 1, "ms": 12}
    # the second caption the model was shown is now first; the rest keep their order
    shown = asked[0][1]
    got = [r["caption"] for r in body["results"]]
    assert got[0] == shown[1] and got[1:] == [c for c in shown if c != shown[1]]
    assert {a.id, b.id, c.id} == {r["id"] for r in body["results"]}



def test_the_route_makes_parsed_labels_soft_and_chip_labels_hard(client, db, monkeypatch):
    import routers.search as sr
    _camera(db, 1, "Dock")
    _visit(db, label="car", caption="a white pickup truck")
    seen: list[bool] = []
    from services import search_service
    real = search_service.search_page

    def _page(*args, **kw):
        seen.append(bool(kw.get("labels_soft")))
        return real(*args, **kw)
    monkeypatch.setattr(sr, "search_page", _page)

    async def _vectors(_db, words):
        return [1.0], [1.0], None
    monkeypatch.setattr(sr, "_query_vectors", _vectors)
    client.get("/api/v1/search", params={"q": "pickup truck"})
    client.get("/api/v1/search", params={"q": "pickup truck", "label": "truck"})
    client.get("/api/v1/search", params={"q": "tarpaulin"})
    assert seen[0] is True, "parsed class: soft"
    assert seen[1] is False, "a label= chip: a wall"
    assert seen[2] is False, "no class word: nothing to soften"


def test_a_class_nobody_detected_is_said_when_the_vectors_answer_anyway(client, db, monkeypatch):
    """"a dog" on a site with no dog detections now returns the visits
    that look like one (#873). That is the honest best answer only when
    it is said: the response carries `lookalike`, and not when the class
    WAS detected, nor on a later page."""
    import routers.search as sr
    from services import search_service
    _camera(db, 1, "Dock")
    car = _visit(db, label="car", caption="a brown car that looks like a dog from behind")

    async def _vectors(_db, words):
        return [1.0], None, None
    monkeypatch.setattr(sr, "_query_vectors", _vectors)
    real = search_service.search_page

    def _page(*args, **kw):
        page = real(*args, **kw)
        from services.search_service import SearchHit
        if not page.hits:
            page = type(page)(hits=[SearchHit(event=db.get(TimelineEvent, car.id), caption="x", attributes=None, score=0.5)],
                              total=1, semantic={"used": True, "labels_soft": True, "arms": {"vector": 1}})
        return page
    monkeypatch.setattr(sr, "search_page", _page)
    body = client.get("/api/v1/search", params={"q": "a dog"}).json()
    assert body["lookalike"] == {"classes": ["dog"], "what": "dog"}
    body = client.get("/api/v1/search", params={"q": "a car"}).json()
    assert "lookalike" not in body, "the class was detected: nothing to say"
    body = client.get("/api/v1/search", params={"q": "a dog", "label": "dog"}).json()
    assert "lookalike" not in body, "a label= chip is a wall, not a softening"


def test_the_people_and_vehicles_an_operator_names_are_classes():
    """cop, guard, courier, toddler are people to the detector; a bike is
    a motorcycle on most roads here and a bicycle on others; an auto is
    read as a car or a motorcycle."""
    assert parse_query("a cop at the gate", now=NOW).labels == ["person"]
    assert parse_query("security guard", now=NOW).labels == ["person"]
    assert parse_query("the courier", now=NOW).labels == ["person"]
    assert parse_query("a toddler", now=NOW).labels == ["person"]
    assert parse_query("bike", now=NOW).labels == ["bicycle", "motorcycle"]
    assert parse_query("scooty", now=NOW).labels == ["motorcycle"]
    assert parse_query("an auto", now=NOW).labels == ["car", "motorcycle"]
    assert parse_query("jeep", now=NOW).labels == ["car"]
    assert parse_query("pickup", now=NOW).labels == ["truck", "car"]
