# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""An app can search the canonical store, scoped to its own cameras.

Until this route existed there was no way for an app to ask the platform
"which visits match these words?". The operator route authenticates a
USER and scopes by what that user can see, which an app holding an
internal key can neither satisfy nor should — so every app that wanted
footage search built its own index of the same footage.

It cost more than duplication. The camera-agent's attempt to stop using
its private index called an SDK method that did not exist, against a
route that did not exist, and fell through to the index on every query,
because ``AttributeError`` and "core is unreachable" arrive at the same
``except`` branch. The repoint read as done for a month and had never
run once.

The scoping is the part that must not be wrong, so most of what follows
is about that rather than about matching.
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
os.environ.setdefault("DATABASE_URL", "sqlite:///./_app_search_test.db")

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

SITE_KEY = settings.internal_api_key
_T0 = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)


@pytest.fixture()
def client():
    # StaticPool or every connection gets its OWN in-memory database,
    # and the route's session finds no tables at all.
    engine = create_engine("sqlite://", future=True, poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    session = SessionLocal()

    session.add(Role(id=1, name="admin", description="test"))
    session.commit()
    session.add(User(id=1, username="owner", email="o@x.test",
                     hashed_password="x", is_active=True, role_id=1))
    session.commit()
    for cid, name in ((1, "Gate"), (2, "Drive")):
        session.add(Camera(id=cid, name=name, ip_address=f"10.0.0.{cid}",
                           rtsp_url=f"rtsp://x/{cid}", owner_id=1))
    session.commit()

    app = FastAPI()
    app.include_router(app_platform.router)

    def _db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db] = _db
    with TestClient(app) as tc:
        yield tc, session
    session.close()


def _visit(db, *, cam=1, minutes=0, label="car", caption=None, attributes=None,
           plate=None):
    row = TimelineEvent(
        camera_id=cam, label=label, event_type="visit", source="tier0",
        started_at=_T0 + timedelta(minutes=minutes),
        ended_at=_T0 + timedelta(minutes=minutes, seconds=20),
        plate_text=plate)
    db.add(row)
    db.flush()
    if caption is not None or attributes is not None:
        db.add(EventText(event_id=row.id, caption=caption,
                         attributes=attributes, source="test"))
    db.commit()
    return row


def _search(tc, **params):
    return tc.get("/internal/app/search", params=params,
                  headers={"X-Internal-Api-Key": SITE_KEY})


# ── it answers ───────────────────────────────────────────────────────


def test_words_match_what_an_enricher_wrote(client):
    """The whole point of `find` over `search`: "red van" reaches a visit
    nobody labelled a van, because a captioner said so."""
    tc, db = client
    _visit(db, caption="a red van at the gate", attributes="red van")
    _visit(db, caption="a white car", attributes="white car")

    body = _search(tc, text="red van").json()
    assert body["total"] == 1
    assert "red van" in body["results"][0]["caption"]


def test_the_answer_block_rides_along(client):
    """Same counted summary the operator Search page gets, so an app and
    an operator asking one question cannot be told two things."""
    tc, db = client
    _visit(db, caption="a red van", attributes="red van")
    body = _search(tc, text="red").json()

    assert body["answer"]["shown"] == 1
    assert body["answer"]["scope"] == "page"


def test_a_hit_says_whether_a_photo_was_kept(client):
    """`has_evidence` lets a caller tell "no photo" from "not fetched
    yet" without a second round trip per row."""
    tc, db = client
    _visit(db, caption="a red van")
    assert _search(tc, text="red").json()["results"][0]["has_evidence"] is False


# ── the scoping, which is the part that must not be wrong ────────────


class _AppPrincipal:
    """Stands in for services.app_keys.AppPrincipal."""

    def __init__(self, app_id):
        self.app_id = app_id


def _as_app(tc, monkeypatch, roster):
    """Run the next request as an app with this camera roster."""
    from routers import app_platform as ap

    monkeypatch.setattr(ap, "_app_roster", lambda db, principal: roster)


def test_an_app_sees_only_its_own_cameras(client, monkeypatch):
    tc, db = client
    _visit(db, cam=1, caption="a red van at the gate")
    _visit(db, cam=2, caption="a red van on the drive")

    _as_app(tc, monkeypatch, {1})
    body = _search(tc, text="red van").json()

    assert body["total"] == 1
    assert body["results"][0]["camera_id"] == 1


def test_an_app_with_no_cameras_gets_nothing_not_everything(client, monkeypatch):
    """The failure that matters, checked end to end.

    `scope=None` means UNRESTRICTED one layer down, so an app with no
    cameras and a site component with every camera arrive here one
    falsy value apart. This asserts the route's answer, deliberately
    without caring which line produces it: the short-circuit in the
    handler and `scope_query`'s empty-set predicate both give an empty
    page, and deleting either one on its own must not change what an
    app is told. The predicate itself is pinned separately, in
    test_scope_query_empty.py, since every scoped route in the server
    rests on it and not just this one.
    """
    tc, db = client
    _visit(db, cam=1, caption="a red van")
    _visit(db, cam=2, caption="a white car")

    _as_app(tc, monkeypatch, set())
    body = _search(tc, text="").json()

    assert body["total"] == 0
    assert body["results"] == []
    assert body["answer"] == {}


def test_the_platform_key_is_not_scoped(client, monkeypatch):
    """A platform component holding the site key is not an app and has
    no roster; it sees everything, which is what `None` means."""
    tc, db = client
    _visit(db, cam=1, caption="a red van")
    _visit(db, cam=2, caption="a white car")

    _as_app(tc, monkeypatch, None)
    assert _search(tc, text="").json()["total"] == 2


def test_an_explicit_camera_filter_cannot_widen_the_roster(client, monkeypatch):
    """Asking for a camera the app does not hold returns nothing, rather
    than the camera."""
    tc, db = client
    _visit(db, cam=2, caption="a red van on the drive")

    _as_app(tc, monkeypatch, {1})
    assert _search(tc, text="red", camera_id=2).json()["total"] == 0


# ── refusals and shape ───────────────────────────────────────────────


def test_it_needs_a_key(client):
    tc, _ = client
    assert tc.get("/internal/app/search", params={"text": "x"}).status_code == 401


def test_a_malformed_attr_chip_is_skipped_not_fatal(client):
    """One bad chip must not 422 a whole search — the operator route
    makes the same choice and for the same reason."""
    tc, db = client
    _visit(db, caption="a red van")
    r = _search(tc, text="red", attr=["nonsense", "colour:red"])
    assert r.status_code == 200


def test_the_window_filters(client):
    tc, db = client
    _visit(db, minutes=0, caption="early van")
    _visit(db, minutes=600, caption="late van")

    body = _search(tc, text="van",
                   **{"from": (_T0 + timedelta(minutes=300)).isoformat()}).json()
    assert body["total"] == 1
    assert "late" in body["results"][0]["caption"]
