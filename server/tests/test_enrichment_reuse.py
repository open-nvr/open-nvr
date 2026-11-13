# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""RFC-0004 step 2: the same frame is not described twice.

Two visits with the same evidence path are the same pixels. The later
one takes the earlier one's caption and VQA claims and the model is not
called; a different frame, or an earlier visit with nothing to copy,
goes the normal way; the switch turns it off.
"""

from __future__ import annotations

import asyncio
import os
import secrets

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite:///./_reuse_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

from services import enrichment_reuse as reuse  # noqa: E402
from services.descriptor_enrichment import VQA_TASK  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _box(monkeypatch, *, same_frame=True, first_has_caption=True, first_vqa_ran=True, flag=True):
    """Two visits on one camera; the first already described. Returns
    (db, caption_calls, question_calls)."""
    import pathlib
    import tempfile
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import core.database
    from core.config import settings
    from core.database import Base
    from models import Camera, EventText, Role, TimelineEvent, User, VisitDescriptor
    from services import caption_enrichment as ce
    from services import descriptor_enrichment as de
    from services import evidence_store

    engine = create_engine("sqlite:///:memory:", future=True,
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, future=True)()
    db.add(Role(id=1, name="admin", description="t")); db.commit()
    db.add(User(id=1, username="o", email="o@x.test", hashed_password="x", is_active=True, role_id=1)); db.commit()
    db.add(Camera(id=1, name="c", ip_address="10.0.0.1", rtsp_url="rtsp://x/1", owner_id=1,
                  assignments=[{"skill": "image_captioning"}, {"skill": VQA_TASK}])); db.commit()
    now = datetime.now(UTC)
    for eid, rel in ((1, "ab/abc.jpg"), (2, "ab/abc.jpg" if same_frame else "cd/cde.jpg")):
        db.add(TimelineEvent(id=eid, camera_id=1, source="tier0", event_type="track", label="car",
                             started_at=now, ended_at=now + timedelta(seconds=5), evidence_path=rel,
                             payload={"enriched_by": [VQA_TASK]} if (eid == 1 and first_vqa_ran) else {}))
    db.commit()
    if first_has_caption:
        db.add(EventText(event_id=1, caption="A white van at the gate.", source="litertlm")); db.commit()
    if first_vqa_ran:
        db.add(VisitDescriptor(event_id=1, kind="colour", value="white", source_task=VQA_TASK,
                               source_adapter="litertlm")); db.commit()

    class _Shared:
        def __init__(self, s): self._s = s
        def __getattr__(self, n): return getattr(self._s, n)
        def close(self): pass

    monkeypatch.setattr(core.database, "SessionLocal", lambda: _Shared(db))
    monkeypatch.setattr(settings, reuse.SETTING, flag, raising=False)
    monkeypatch.setattr(settings, "events_caption_enrichment", True, raising=False)
    monkeypatch.setattr(settings, "events_descriptor_enrichment", True, raising=False)
    tmp = pathlib.Path(tempfile.mkdtemp()) / "e.jpg"
    tmp.write_bytes(b"\xff\xd8jpeg")
    monkeypatch.setattr(evidence_store, "resolve_evidence", lambda _p: tmp)

    async def _adapter():
        return "litertlm"

    caption_calls: list[int] = []

    async def _cap(jpeg, adapter, handle, event_id=None, *, priority="live"):
        caption_calls.append(int(event_id))
        return "A fresh sentence."

    monkeypatch.setattr(ce, "_resolve_caption_adapter", _adapter)
    monkeypatch.setattr(ce, "_caption_jpeg", _cap)

    async def _plan(_label):
        return [{"task": VQA_TASK, "healthy": True, "adapters": ["litertlm"],
                 "descriptor_kinds": ["colour", "vehicle_type"]}]

    question_calls: list[str] = []

    async def _ask(jpeg, adapter, question, handle, event_id, **kw):
        question_calls.append(question)
        return None

    monkeypatch.setattr(de, "_plan_skills", _plan)
    monkeypatch.setattr(de, "_ask", _ask)
    return db, caption_calls, question_calls


def _state(db, eid=2):
    from models import EventText, TimelineEvent, VisitDescriptor

    db.expire_all()
    row = db.get(TimelineEvent, eid)
    text = db.get(EventText, eid)
    rows = db.query(VisitDescriptor).filter(VisitDescriptor.event_id == eid).all()
    return {"caption": text.caption if text else None, "source": text.source if text else None,
            "payload": dict(row.payload or {}), "claims": sorted((d.kind, d.value) for d in rows)}


def test_the_same_frame_takes_the_earlier_caption_and_claims_without_a_call(monkeypatch):
    from services import caption_enrichment as ce
    from services import descriptor_enrichment as de

    db, caps, qs = _box(monkeypatch)
    assert _run(ce.enrich_event_caption(2)) == "done"
    assert caps == [], "the captioner was not called"
    st = _state(db)
    assert st["caption"] == "A white van at the gate." and st["source"] == "litertlm"
    assert st["claims"] == [("colour", "white")]
    assert st["payload"][reuse.MARK] == 1 and VQA_TASK in st["payload"]["enriched_by"]
    # the descriptor task then has nothing to ask either
    assert _run(de.enrich_event_descriptors(2)) is None and qs == []


def test_the_descriptor_task_reuses_on_its_own_when_it_runs_first(monkeypatch):
    from services import descriptor_enrichment as de

    db, _caps, qs = _box(monkeypatch)
    assert _run(de.enrich_event_descriptors(2)) == "done"
    assert qs == []
    st = _state(db)
    assert st["claims"] == [("colour", "white")] and st["payload"][reuse.MARK] == 1


def test_a_different_frame_goes_the_normal_way(monkeypatch):
    from services import caption_enrichment as ce
    from services import descriptor_enrichment as de

    db, caps, qs = _box(monkeypatch, same_frame=False)
    _run(ce.enrich_event_caption(2))
    _run(de.enrich_event_descriptors(2))
    assert caps == [2] and len(qs) == 2
    st = _state(db)
    assert st["caption"] == "A fresh sentence." and reuse.MARK not in st["payload"]


def test_an_earlier_visit_with_nothing_to_copy_is_no_twin(monkeypatch):
    from services import caption_enrichment as ce

    db, caps, _qs = _box(monkeypatch, first_has_caption=False, first_vqa_ran=False)
    _run(ce.enrich_event_caption(2))
    assert caps == [2]


def test_a_twin_with_claims_but_no_caption_still_gets_the_caption_called(monkeypatch):
    """Reuse what exists (the claims), ask for what does not (the words)."""
    from services import caption_enrichment as ce

    db, caps, _qs = _box(monkeypatch, first_has_caption=False, first_vqa_ran=True)
    _run(ce.enrich_event_caption(2))
    assert caps == [2]
    st = _state(db)
    assert st["caption"] == "A fresh sentence." and st["claims"] == [("colour", "white")]


def test_the_switch_turns_reuse_off(monkeypatch):
    from services import caption_enrichment as ce

    db, caps, _qs = _box(monkeypatch, flag=False)
    _run(ce.enrich_event_caption(2))
    assert caps == [2]


def test_only_an_earlier_visit_is_a_twin(monkeypatch):
    """The first of two visits that arrive together does the work; it
    must not find the second one and wait for it."""
    from models import TimelineEvent

    db, _caps, _qs = _box(monkeypatch)
    assert reuse.find_twin(db, db.get(TimelineEvent, 1)) is None
    assert reuse.find_twin(db, db.get(TimelineEvent, 2)).id == 1
