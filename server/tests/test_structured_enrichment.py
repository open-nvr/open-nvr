# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""RFC-0004 step 1: one structured call per visit.

The cost story this guards: a vehicle visit was three captioner calls
(the sentence, then colour, then type) and a person visit up to six, all
on the same frame and the same model. With the flag on it is one, the
answers land in the same rows through the same vocabulary, and the
descriptor task does not ask again. A model that answers in prose keeps
the prose as its caption and costs nothing extra: the questions wait for
the per-question path. Off, nothing here runs.
"""

from __future__ import annotations

import asyncio
import os
import secrets

from cryptography.fernet import Fernet

# Same settings bootstrap as the sibling enrichment tests; before the imports.
os.environ.setdefault("DATABASE_URL", "sqlite:///./_structured_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest  # noqa: E402

from services import structured_enrichment as se  # noqa: E402
from services.caption_enrichment import CAPTION_SKILL  # noqa: E402
from services.descriptor_enrichment import KIND_QUESTIONS, VQA_TASK  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── the prompt ────────────────────────────────────────────────────


def test_prompt_asks_for_the_caption_and_each_kind_with_its_vocabulary():
    prompt = se.build_prompt(["colour", "vehicle_type"])
    assert '"caption"' in prompt
    for kind in ("colour", "vehicle_type"):
        assert f'"{kind}":' in prompt
        for word in KIND_QUESTIONS[kind]["vocabulary"][:3]:
            assert word in prompt
    # The per-question "one word" instruction is replaced by the JSON one.
    assert "Answer with one word" not in prompt
    # attributes first, caption last: each value is conditioned on the text
    # before it, and the caption pulled the answers toward the whole scene
    assert prompt.rstrip().endswith('{"colour": "...", "vehicle_type": "...", "caption": "..."}')
    assert "main vehicle" in prompt


def test_prompt_without_kinds_is_a_plain_caption_request():
    prompt = se.build_prompt([])
    assert "questions" not in prompt and "One of:" not in prompt
    assert prompt.rstrip().endswith('{"caption": "..."}')


def test_attributes_prompt_has_the_kinds_and_no_caption():
    prompt = se.build_attributes_prompt(["colour", "vehicle_type"])
    assert "caption" not in prompt.lower()
    assert prompt.rstrip().endswith('{"colour": "...", "vehicle_type": "..."}')
    assert "main vehicle" in prompt


# ── the answer ────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    '{"caption": "A white van", "colour": "white"}',
    'Here you go:\n```json\n{"caption": "A white van", "colour": "white"}\n```\nHope that helps.',
    'Sure. {"Caption": "A white van", "COLOUR": "white"} ',
])
def test_parse_answer_finds_the_object_in_json_fenced_or_prose(text):
    assert se.parse_answer(text) == {"caption": "A white van", "colour": "white"}


@pytest.mark.parametrize("text", ["", "A white van parked by the gate.", "[1, 2]", "{not json}"])
def test_parse_answer_is_none_without_an_object(text):
    assert se.parse_answer(text) is None


def test_interpret_json_gives_a_caption_and_normalised_claims():
    before = dict(se.STATS)
    out = se.interpret('{"caption": "A white van at the gate.", "colour": "White", '
                       '"vehicle_type": "It is a van", "clothing_top": "unknown"}',
                       ["colour", "vehicle_type", "clothing_top"], "litertlm")
    assert out.json_ok and out.caption == "A white van at the gate."
    assert {(c.kind, c.value) for c in out.claims} == {("colour", "white"), ("vehicle_type", "van")}
    assert all(c.source_task == VQA_TASK and c.source_adapter == "litertlm"
               and c.confidence is None for c in out.claims)
    assert se.STATS["json_ok"] == before["json_ok"] + 1
    assert se.STATS["claims"] == before["claims"] + 2


def test_interpret_prose_keeps_the_text_as_caption_and_no_claims():
    before = dict(se.STATS)
    out = se.interpret("A white van at the gate.", ["colour"], "litertlm")
    assert not out.json_ok and out.caption == "A white van at the gate." and out.claims == []
    assert se.STATS["json_failed"] == before["json_failed"] + 1


def test_interpret_drops_a_word_outside_the_vocabulary():
    out = se.interpret('{"caption": "x", "colour": "turquoise-ish"}', ["colour"], "a")
    assert out.json_ok and out.claims == []


# ── which kinds ───────────────────────────────────────────────────


def _plan(adapters=("litertlm",), kinds=("colour", "vehicle_type", "clothing_top", "carrying")):
    async def plan(_label):
        return [{"task": VQA_TASK, "healthy": True, "adapters": list(adapters),
                 "descriptor_kinds": list(kinds)}]
    return plan


def test_kinds_for_visit_applies_the_descriptor_filters(monkeypatch):
    from core.config import settings
    from services import descriptor_enrichment as de

    monkeypatch.setattr(de, "_plan_skills", _plan())
    monkeypatch.setattr(settings, "events_descriptor_enrichment", True, raising=False)
    monkeypatch.setattr(settings, de.PEOPLE_SETTING, False, raising=False)
    assert _run(se.kinds_for_visit("car", "litertlm")) == ["colour", "vehicle_type"]
    assert _run(se.kinds_for_visit("car", "litertlm", {"colour"})) == ["vehicle_type"]
    # people are off: no questions for a person
    assert _run(se.kinds_for_visit("person", "litertlm")) == []
    monkeypatch.setattr(settings, de.PEOPLE_SETTING, True, raising=False)
    assert _run(se.kinds_for_visit("person", "litertlm")) == ["carrying", "clothing_top"]
    # a captioner the plan does not list for VQA gets no questions
    assert _run(se.kinds_for_visit("car", "blip")) == []
    # a class nobody describes
    assert _run(se.kinds_for_visit("dog", "litertlm")) == []
    monkeypatch.setattr(settings, "events_descriptor_enrichment", False, raising=False)
    assert _run(se.kinds_for_visit("car", "litertlm")) == []


# ── the whole path ────────────────────────────────────────────────


def _box(monkeypatch, *, label="car", skills=(CAPTION_SKILL, VQA_TASK), caption_text=None):
    """One visit in an in-memory store, the gate stubbed. Returns
    (db, gate_calls, ask_calls)."""
    import pathlib
    import tempfile
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import core.database
    from core.config import settings
    from core.database import Base
    from models import Camera, EventText, Role, TimelineEvent, User
    from services import caption_enrichment as ce
    from services import descriptor_enrichment as de
    from services import enrichment_gate, evidence_store

    engine = create_engine("sqlite:///:memory:", future=True,
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, future=True)()
    db.add(Role(id=1, name="admin", description="t")); db.commit()
    db.add(User(id=1, username="o", email="o@x.test", hashed_password="x",
                is_active=True, role_id=1)); db.commit()
    db.add(Camera(id=1, name="c", ip_address="10.0.0.1", rtsp_url="rtsp://x/1", owner_id=1,
                  assignments=[{"skill": s} for s in skills]))
    db.commit()
    now = datetime.now(UTC)
    db.add(TimelineEvent(id=1, camera_id=1, source="tier0", event_type="track", label=label,
                         started_at=now, ended_at=now + timedelta(seconds=5),
                         evidence_path="e.jpg"))
    db.commit()
    if caption_text is not None:
        db.add(EventText(event_id=1, caption=caption_text, source="x")); db.commit()

    class _Shared:
        def __init__(self, s): self._s = s
        def __getattr__(self, n): return getattr(self._s, n)
        def close(self): pass

    monkeypatch.setattr(core.database, "SessionLocal", lambda: _Shared(db))
    monkeypatch.setattr(settings, "events_caption_enrichment", True, raising=False)
    monkeypatch.setattr(settings, "events_descriptor_enrichment", True, raising=False)
    monkeypatch.setattr(settings, de.PEOPLE_SETTING, False, raising=False)
    tmp = pathlib.Path(tempfile.mkdtemp()) / "e.jpg"
    tmp.write_bytes(b"\xff\xd8jpeg")
    monkeypatch.setattr(evidence_store, "resolve_evidence", lambda _p: tmp)

    async def _adapter():
        return "litertlm"

    monkeypatch.setattr(ce, "_resolve_caption_adapter", _adapter)
    monkeypatch.setattr(de, "_plan_skills", _plan())
    gate_calls: list[dict] = []
    ask_calls: list[str] = []

    async def _gate(adapter, body, **kw):
        gate_calls.append({"adapter": adapter, "kind": kw.get("kind"), "body": body})
        return {"result": {"answer": _gate.reply}}
    _gate.reply = ""
    monkeypatch.setattr(enrichment_gate, "infer_through_gate", _gate)

    async def _ask(jpeg, adapter, question, handle, event_id, **kw):
        ask_calls.append(question)
        return None
    monkeypatch.setattr(de, "_ask", _ask)
    return db, gate_calls, ask_calls, _gate


def _state(db):
    from models import EventText, TimelineEvent, VisitDescriptor

    db.expire_all()
    row = db.get(TimelineEvent, 1)
    text = db.get(EventText, 1)
    rows = db.query(VisitDescriptor).filter(VisitDescriptor.event_id == 1).all()
    return {"caption": text.caption if text else None,
            "enriched_by": sorted((row.payload or {}).get("enriched_by") or []),
            "claims": sorted((d.kind, d.value) for d in rows)}


def test_one_call_writes_the_caption_and_the_descriptors(monkeypatch):
    from core.config import settings
    from services import caption_enrichment as ce
    from services import descriptor_enrichment as de

    monkeypatch.setattr(settings, se.SETTING, True, raising=False)
    db, gate_calls, ask_calls, gate = _box(monkeypatch)
    gate.reply = '{"caption": "A white van at the gate.", "colour": "white", "vehicle_type": "van"}'
    assert _run(ce.enrich_event_caption(1)) == "done"
    assert len(gate_calls) == 1, "one call for the sentence and both attributes"
    body = gate_calls[0]["body"]
    assert gate_calls[0]["kind"] == "caption" and "task" not in body
    assert body["question"] == body["prompt"] and '"vehicle_type"' in body["prompt"]
    assert body["camera_id"] == "cam1" and body["event_id"] == 1 and body["frame_b64"]
    st = _state(db)
    assert st["caption"] == "A white van at the gate."
    assert st["claims"] == [("colour", "white"), ("vehicle_type", "van")]
    # a written caption is its own record (the EventText row); VQA is ran
    assert st["enriched_by"] == [VQA_TASK]
    # the descriptor task has nothing left to ask
    assert _run(de.enrich_event_descriptors(1)) is None
    assert ask_calls == []


def test_the_descriptor_task_leaves_a_visit_the_single_call_will_carry(monkeypatch):
    from core.config import settings
    from services import descriptor_enrichment as de

    monkeypatch.setattr(settings, se.SETTING, True, raising=False)
    db, _gate_calls, ask_calls, _gate = _box(monkeypatch)
    # no caption yet, the camera carries captioning: the caption task's call will ask
    assert _run(de.enrich_event_descriptors(1)) is None
    assert ask_calls == []
    # a visit that already has its sentence is asked as before
    from models import EventText
    db.add(EventText(event_id=1, caption="A van.", source="x")); db.commit()
    _run(de.enrich_event_descriptors(1))
    assert len(ask_calls) == 2


def test_a_camera_without_captioning_still_gets_its_questions(monkeypatch):
    from core.config import settings
    from services import descriptor_enrichment as de

    monkeypatch.setattr(settings, se.SETTING, True, raising=False)
    _db, _gate_calls, ask_calls, _gate = _box(monkeypatch, skills=(VQA_TASK,))
    _run(de.enrich_event_descriptors(1))
    assert len(ask_calls) == 2, "no caption task will run here, so the per-question path asks"


def test_a_prose_answer_is_the_caption_and_the_questions_wait(monkeypatch):
    from core.config import settings
    from services import caption_enrichment as ce
    from services import descriptor_enrichment as de

    monkeypatch.setattr(settings, se.SETTING, True, raising=False)
    db, gate_calls, ask_calls, gate = _box(monkeypatch)
    gate.reply = "A white van is parked at the gate."
    assert _run(ce.enrich_event_caption(1)) == "done"
    st = _state(db)
    assert st["caption"] == "A white van is parked at the gate."
    assert st["claims"] == [] and st["enriched_by"] == [], "VQA not recorded as ran"
    # the per-question path now has a captioned visit and asks as before
    _run(de.enrich_event_descriptors(1))
    assert len(ask_calls) == 2 and len(gate_calls) == 1


def test_an_empty_json_caption_still_records_the_look_and_the_claims(monkeypatch):
    from core.config import settings
    from services import caption_enrichment as ce

    monkeypatch.setattr(settings, se.SETTING, True, raising=False)
    db, _gate_calls, _ask_calls, gate = _box(monkeypatch)
    gate.reply = '{"caption": "", "colour": "red", "vehicle_type": "unknown"}'
    assert _run(ce.enrich_event_caption(1)) == "done"
    st = _state(db)
    assert st["caption"] is None
    assert st["claims"] == [("colour", "red")]
    assert st["enriched_by"] == sorted([CAPTION_SKILL, VQA_TASK])


def test_flag_off_is_the_old_two_task_path(monkeypatch):
    from core.config import settings
    from services import caption_enrichment as ce
    from services import descriptor_enrichment as de

    monkeypatch.setattr(settings, se.SETTING, False, raising=False)
    db, gate_calls, ask_calls, gate = _box(monkeypatch)
    captions = []

    async def _cap(jpeg, adapter, handle, event_id=None, *, priority="live"):
        captions.append(adapter)
        return "A van."
    monkeypatch.setattr(ce, "_caption_jpeg", _cap)
    _run(ce.enrich_event_caption(1))
    _run(de.enrich_event_descriptors(1))
    assert captions == ["litertlm"] and gate_calls == [] and len(ask_calls) == 2
    assert _state(db)["caption"] == "A van."
