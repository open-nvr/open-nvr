# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The real SDK client against the real route, over HTTP.

Everything about this search was tested and none of it worked.

``test_app_search_route.py`` drove the route with a test client and
passed. The SDK's ``find`` was checked for sync/async parity and passed.
The camera-agent's tool was tested against a fake timeline and passed.
Then the three were run together for the first time and the first real
query returned HTTP 422, because ``query_string`` sent a list of camera
ids as ``camera_id=%5B7%5D`` — ``urlencode`` falling back to the Python
repr without ``doseq=True``.

Every component was correct. The seam between them was the only thing
nobody had exercised, and ``get_json`` turns a 422 into ``None``, which
the agent reads as "the store is unreachable" — so the failure would
have shown up in production as an outage that never happened, on a
route that was working.

So this suite makes requests with a client whose transport is the app
itself: the SDK builds the URL it would really build, FastAPI parses it
the way it really would, and nothing in between is stubbed.
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
os.environ.setdefault("DATABASE_URL", "sqlite:///./_app_search_sdk_test.db")

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.config import settings  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import Camera, EventText, Role, TimelineEvent, User  # noqa: E402
from routers import app_platform  # noqa: E402

opennvr_app_sdk = pytest.importorskip("opennvr_app_sdk")
from opennvr_app_sdk.client import TimelineAPI, _Http, query_string  # noqa: E402

_T0 = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)


class _SiteCreds:
    def headers(self):
        return {"X-Internal-Api-Key": settings.internal_api_key}


@pytest.fixture()
def timeline():
    """A real ``TimelineAPI`` whose HTTP session IS the app."""
    engine = create_engine("sqlite://", future=True, poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    db.add(Role(id=1, name="admin", description="test"))
    db.commit()
    db.add(User(id=1, username="owner", email="o@x.test",
                hashed_password="x", is_active=True, role_id=1))
    db.commit()
    for cid, name in ((7, "Dock"), (8, "Gate")):
        db.add(Camera(id=cid, name=name, ip_address=f"10.0.0.{cid}",
                      rtsp_url=f"rtsp://x/{cid}", owner_id=1))
    db.commit()

    def _visit(cam, caption, attributes, *, minutes=0, plate=None, label="truck"):
        row = TimelineEvent(
            camera_id=cam, label=label, event_type="visit", source="tier0",
            started_at=_T0 + timedelta(minutes=minutes),
            ended_at=_T0 + timedelta(minutes=minutes, seconds=30),
            plate_text=plate)
        db.add(row)
        db.flush()
        db.add(EventText(event_id=row.id, caption=caption,
                         attributes=attributes, source="test"))
        db.commit()

    _visit(7, "a red truck at the loading dock", "red truck",
           plate="KA01AB1234")
    _visit(8, "a red truck at the gate", "red truck", minutes=30)
    _visit(8, "a white car at the gate", "white car", minutes=60, label="car")

    api = FastAPI()
    api.include_router(app_platform.router, prefix="/api/v1")
    api.dependency_overrides[get_db] = lambda: db

    http = _Http.__new__(_Http)
    http.base = "http://testserver"
    http.creds = _SiteCreds()
    http.timeout = 5.0
    http._client = TestClient(api)

    yield TimelineAPI(http)
    db.close()


# ── the seam ─────────────────────────────────────────────────────────


def test_a_plain_search_reaches_the_route(timeline):
    """If this fails, nothing below means anything."""
    answer = timeline.find("red truck")
    assert answer is not None, "the SDK could not reach a route that exists"
    assert answer["total"] == 2


def test_a_repeated_parameter_survives_the_wire(timeline):
    """THE defect. A list of camera ids must arrive as a repeated query
    parameter, not as the URL-encoded repr of a Python list.

    The wrong version does not raise or return a wrong answer — it
    returns ``None``, which every caller is required to read as "the
    store could not be reached". A filtering bug disguised as an
    outage, on a route that is up.
    """
    answer = timeline.find("red truck", camera=[7])
    assert answer is not None, (
        "the store looked unreachable; it is not — check that "
        "query_string still passes doseq=True")
    assert answer["total"] == 1
    assert answer["results"][0]["camera_id"] == 7


def test_several_repeated_parameters_at_once(timeline):
    """Two ids and a label in one call: the shape an app that actually
    uses this route sends, rather than one parameter at a time."""
    answer = timeline.find("red", camera=[7, 8], label=["truck"])
    assert answer is not None
    assert answer["total"] == 2
    assert {r["camera_id"] for r in answer["results"]} == {7, 8}


def test_a_camera_handle_is_accepted_where_an_id_is(timeline):
    """Apps hold ``Camera`` objects and handles, not raw ints. If the
    handle conversion happened after the URL was built, this would 422
    the same silent way."""
    assert timeline.find("red truck", camera=["cam7"])["total"] == 1


def test_the_time_window_survives_the_wire(timeline):
    """``from`` is an alias on the route and a reserved word in Python,
    which is exactly the kind of parameter that gets lost in a rename
    and reported as an outage."""
    answer = timeline.find("red truck", start=_T0 + timedelta(minutes=15))
    assert answer is not None
    assert answer["total"] == 1
    assert answer["results"][0]["camera_id"] == 8


def test_an_attr_filter_survives_the_wire(timeline):
    """``attr`` is repeatable AND carries a colon, so it exercises both
    the list handling and the escaping."""
    answer = timeline.find("", attrs=["colour:red"])
    assert answer is not None, "an attr chip made the store look unreachable"


def test_paging_parameters_reach_the_route(timeline):
    first = timeline.find("red truck", limit=1)
    second = timeline.find("red truck", limit=1, skip=1)
    assert first["total"] == second["total"] == 2
    assert len(first["results"]) == len(second["results"]) == 1
    assert first["results"][0]["id"] != second["results"][0]["id"]


def test_the_answer_block_arrives_intact(timeline):
    """The counted summary is the reason an app calls this instead of
    counting rows itself; losing it on the wire loses the point."""
    answer = timeline.find("red truck")
    assert answer["answer"]["total"] == 2
    assert answer["answer"]["plate_count"] == 1


def test_plates_inside_reaches_the_route_over_the_wire(timeline):
    """`plates/inside` exists because an app could not reach what the
    operator's Vehicles page has had all along, so it kept its own
    ledger of who had driven in and not out. Both gate lists are
    repeated parameters, which is the shape that silently 422'd."""
    answer = timeline.plates_inside(in_cameras=[7], out_cameras=[8])
    assert answer is not None, (
        "the store looked unreachable; check the gate lists survived "
        "the wire")
    assert "entries" in answer, (
        "entries is what makes an overstay check possible without a "
        "ledger — plates alone says who, not since when")


def test_plates_inside_needs_both_directions(timeline):
    """With no exit gate, nothing can be known to be inside — and
    answering "everyone who ever drove in" would be worse than
    answering nothing."""
    answer = timeline.plates_inside(in_cameras=[7], out_cameras=[])
    assert answer == {"inside": 0, "plates": []}


# ── subject binding over the wire (RFC-0003) ─────────────────────────


def test_visit_at_resolves_over_the_wire(timeline):
    """The call a frame-polling app makes. `at` is a datetime crossing
    an HTTP boundary as a query parameter, which is the shape that
    silently 422'd for camera lists."""
    answer = timeline.visit_at(7, _T0 + timedelta(seconds=10))
    assert answer is not None, "the store looked unreachable"
    assert "binding" in answer, (
        "binding is the whole point — without it a guessed subject is "
        "indistinguishable from a measured one")


def test_a_camera_the_app_does_not_hold_binds_nothing(timeline):
    """Not a 403. Whether a visit exists on a camera this app was not
    given is not this app's business either."""
    answer = timeline.visit_at(999, _T0)
    assert answer["event_id"] is None


def test_claims_are_written_with_their_binding(timeline):
    got = timeline.visit_at(7, _T0 + timedelta(seconds=10))
    assert got["event_id"], "nothing to attach a claim to"

    out = timeline.add_claims(
        got["event_id"],
        [{"kind": "face_id", "value": "Priya", "confidence": 0.9,
          "source_task": "face_recognition"}],
        binding=got["binding"])
    assert out["written"] == 1
    assert out["binding"] == got["binding"]


def test_an_unknown_binding_is_refused_at_the_door(timeline):
    """422 from the route, not a quiet coercion to 'direct' — which
    would record the MOST trusted value for a claim nobody can vouch
    for. A write that did not happen raises rather than returning
    None, because "the claim was not recorded" has no quieter reading."""
    got = timeline.visit_at(7, _T0 + timedelta(seconds=10))
    with pytest.raises(Exception):
        timeline.add_claims(got["event_id"],
                            [{"kind": "face_id", "value": "x"}],
                            binding="probably")


# ── the encoder itself, so the reason is pinned next to the seam ─────


def test_query_string_repeats_list_values():
    assert query_string({"camera_id": [7, 8]}) == "?camera_id=7&camera_id=8"


def test_query_string_leaves_strings_alone():
    """``doseq`` special-cases ``str``; if it did not, every string
    parameter in this client would go out one character per
    repetition."""
    assert query_string({"plate": "KA01AB1234"}) == "?plate=KA01AB1234"


def test_query_string_drops_none_but_keeps_falsey_values():
    """``None`` means "not asked for". ``0`` and ``""`` are answers."""
    assert query_string({"a": None}) == ""
    assert query_string({"skip": 0}) == "?skip=0"
