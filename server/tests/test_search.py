# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Footage search over the canonical event store — parser + API."""

from __future__ import annotations

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
#: The store's clock. REAL, because the API route parses against the wall
#: clock and cannot be told otherwise — a row written relative to a frozen
#: date stops being "today" the day after the test was written, which is
#: exactly what happened. Rows go here; only the parser sees NOW.
WALL = datetime.now(UTC).replace(microsecond=0)


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
    assert set(parsed.labels) == {"car", "bicycle"}
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
    assert plan["vqa"].descriptor_kinds == ["colour", "vehicle_type", "clothing_top", "carrying"]

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
    assert needs["clothing_top"]["state"] in ("not-on-this-box", "never-produced")


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
