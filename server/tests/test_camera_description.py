# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""RFC-0004 step 3: one shared live description per camera.

The store answers when the newest described visit is fresh and nothing
newer has started; otherwise one gated look is shared by everyone asking
while it runs; and when no fresh frame can be had the answer is the
newest thing known, stamped with its age and a reason."""

from __future__ import annotations

import asyncio
import os
import secrets
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite:///./_camdesc_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest  # noqa: E402

from services import camera_description as cd  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def box(monkeypatch):
    """A camera with one described visit ended 10 s ago; the frame fetch
    and the captioner stubbed. Returns (db, state) where state records
    the stubs' calls and controls their answers."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import core.database
    from core.database import Base
    from models import Camera, EventText, Role, TimelineEvent, User, VisitDescriptor
    from services import caption_enrichment as ce

    cd.reset_for_tests()
    engine = create_engine("sqlite:///:memory:", future=True,
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, future=True)()
    db.add(Role(id=1, name="admin", description="t")); db.commit()
    db.add(User(id=1, username="o", email="o@x.test", hashed_password="x", is_active=True, role_id=1)); db.commit()
    db.add(Camera(id=23, name="gate", ip_address="10.0.0.1", rtsp_url="rtsp://x/1", owner_id=1)); db.commit()
    now = datetime.now(UTC)
    db.add(TimelineEvent(id=100, camera_id=23, source="tier0", event_type="track", label="person",
                         started_at=now - timedelta(seconds=40), ended_at=now - timedelta(seconds=10),
                         evidence_path="ab/abc.jpg"))
    db.add(EventText(event_id=100, caption="A man in a red shirt at the gate.", source="litertlm"))
    db.add(VisitDescriptor(event_id=100, kind="clothing_top", value="red", source_task="vqa", source_adapter="litertlm"))
    db.commit()

    class _Shared:
        def __init__(self, s): self._s = s
        def __getattr__(self, n): return getattr(self._s, n)
        def close(self): pass

    monkeypatch.setattr(core.database, "SessionLocal", lambda: _Shared(db))
    state = {"frames": 0, "frame": b"\xff\xd8jpeg", "captions": 0, "answer": "A fresh look.",
             "gate_delay": 0.0}

    async def _frame(camera_id):
        state["frames"] += 1
        return state["frame"]

    async def _adapter():
        return "litertlm"

    async def _cap(jpeg, adapter, handle, event_id=None, *, priority="live"):
        state["captions"] += 1
        assert priority == "requested"
        if state["gate_delay"]:
            await asyncio.sleep(state["gate_delay"])
        return state["answer"]

    monkeypatch.setattr(cd, "_latest_frame", _frame)
    monkeypatch.setattr(ce, "_resolve_caption_adapter", _adapter)
    monkeypatch.setattr(ce, "_caption_jpeg", _cap)
    return db, state


def test_a_fresh_described_visit_answers_without_a_call(box):
    db, state = box
    out = _run(cd.describe(23, max_age_s=30))
    assert out["source"] == "visit" and out["event_id"] == 100 and not out["model_called"]
    assert out["text"].startswith("A man in a red shirt") and out["attributes"] == {"clothing_top": "red"}
    assert 9 <= out["age_s"] <= 12 and state["captions"] == 0 and state["frames"] == 0


def test_an_old_visit_or_a_newer_track_means_one_look(box):
    from models import TimelineEvent

    db, state = box
    # too old for the asker
    out = _run(cd.describe(23, max_age_s=5))
    assert out["source"] == "fresh" and out["model_called"] and out["text"] == "A fresh look."
    assert state["captions"] == 1
    # the same asker again within the window: the look is reused
    out = _run(cd.describe(23, max_age_s=5))
    assert out["source"] == "fresh" and not out["model_called"] and state["captions"] == 1
    # a new visit started on the camera: the fresh look is superseded
    db.add(TimelineEvent(id=101, camera_id=23, source="tier0", event_type="track", label="car",
                         started_at=datetime.now(UTC), evidence_path="cd/cde.jpg"))
    db.commit()
    out = _run(cd.describe(23, max_age_s=60))
    assert out["model_called"] and state["captions"] == 2


def test_askers_arriving_during_a_look_share_it(box):
    db, state = box
    state["gate_delay"] = 0.05

    async def three():
        return await asyncio.gather(cd.describe(23, max_age_s=1), cd.describe(23, max_age_s=1),
                                    cd.describe(23, max_age_s=1))

    outs = _run(three())
    assert [o["text"] for o in outs] == ["A fresh look."] * 3
    assert state["captions"] == 1, "one call for three askers"
    assert cd.STATS["shared"] == 2 and cd.STATS["model_calls"] == 1


def test_no_fresh_frame_gives_the_newest_known_stamped_stale(box):
    db, state = box
    state["frame"] = None
    out = _run(cd.describe(23, max_age_s=1))
    assert out["source"] == "stale" and out["last_source"] == "visit" and out["event_id"] == 100
    assert out["reason"] == "no fresh frame from the camera" and not out["model_called"]
    assert out["age_s"] >= 9 and state["captions"] == 0


def test_a_refused_call_is_honest_too(box, monkeypatch):
    from services import caption_enrichment as ce
    from services.enrichment_gate import DROPPED

    db, state = box

    async def _dropped(jpeg, adapter, handle, event_id=None, *, priority="live"):
        return DROPPED

    monkeypatch.setattr(ce, "_caption_jpeg", _dropped)
    out = _run(cd.describe(23, max_age_s=1))
    assert out["source"] == "stale" and "yielding" in out["reason"]


def test_a_camera_with_nothing_known_says_so(box):
    db, state = box
    state["frame"] = None
    out = _run(cd.describe(99, max_age_s=30))
    assert out["source"] == "none" and out["text"] is None and out["reason"]


def test_the_internal_route_serves_it(box, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.database import get_db
    from routers import internal_camera_agent as internal_router

    db, state = box
    app = FastAPI()
    app.include_router(internal_router.router)
    app.dependency_overrides[internal_router._require_internal_key] = lambda: None
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        r = c.get("/internal/camera-agent/cameras/23/description", params={"max_age_s": 30})
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "visit" and body["camera_id"] == 23 and state["captions"] == 0
