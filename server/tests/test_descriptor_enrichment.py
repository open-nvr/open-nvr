# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""visit_descriptors gets a producer, driven by what the box can do.

The gap this closes: the claims table, its unique constraint, its two
indexes, the ingest endpoint and the search service's ``attrs`` filter
all existed, and the only writers in the repo were tests. "Every red van"
could never match, because no claim had ever been written.

These guard three things: that the enricher runs only what the PLAN
offers, that an answer it cannot count is dropped rather than stored as
prose, and that a site which has not assigned the skill pays nothing.
"""

from __future__ import annotations

import os
import secrets

from cryptography.fernet import Fernet

# The enricher reads core.config.settings, which is a pydantic Settings
# requiring these. Same bootstrap every other server test that touches
# settings uses, and it must run BEFORE the import below.
os.environ.setdefault("DATABASE_URL", "sqlite:///./_descriptor_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest  # noqa: E402

from services.descriptor_enrichment import (  # noqa: E402
    DESCRIBABLE_LABELS,
    DESCRIPTOR_SKILL,
    KIND_QUESTIONS,
    PEOPLE_SETTING,
    VQA_TASK,
    normalise_answer,
    wants_descriptors,
)


# ── the gate ──────────────────────────────────────────────────────


def test_wants_descriptors_needs_the_assignment():
    evidence = "cam1/2026/09/22/frame.jpg"
    assert wants_descriptors("car", evidence, True, {DESCRIPTOR_SKILL}) is True
    assert wants_descriptors("car", evidence, True, set()) is False
    assert wants_descriptors("car", evidence, True, {"license_plate_recognition"}) is False
    # Unresolvable camera fails closed, as with the other two enrichers.
    assert wants_descriptors("car", evidence, True, None) is False


def test_wants_descriptors_respects_the_other_gates():
    evidence = "cam1/2026/09/22/frame.jpg"
    assert wants_descriptors("car", None, True, {DESCRIPTOR_SKILL}) is False
    assert wants_descriptors("car", evidence, False, {DESCRIPTOR_SKILL}) is False
    # A class nobody asks about stays out however the flags are set.
    assert wants_descriptors("suitcase", evidence, True, {DESCRIPTOR_SKILL},
                             True) is False


def test_the_cost_ceiling_is_two_questions_whatever_walks_past():
    """VQA answers one question at a time, so LABEL_KINDS is not a
    preference — it IS the per-visit inference bill, and the invariant
    that matters is the ceiling, not the particular kinds."""
    from services.descriptor_enrichment import LABEL_KINDS

    assert set(LABEL_KINDS) == DESCRIBABLE_LABELS, (
        "a describable class with no kinds would be asked nothing; a kind "
        "list with no class would be asked of everything")
    for label, kinds in LABEL_KINDS.items():
        assert len(kinds) <= 2, f"{label} costs {len(kinds)} inferences a visit"
        assert set(kinds) <= set(KIND_QUESTIONS), (
            f"{label} asks a kind nothing can normalise")


def test_each_class_is_asked_only_what_makes_sense_of_it():
    """The plan offers all four kinds on any label. Without the split a
    lorry gets asked what colour its top is — an inference spent to store
    nonsense."""
    from services.descriptor_enrichment import (
        LABEL_KINDS, PERSON_LABELS, VEHICLE_LABELS,
    )

    assert VEHICLE_LABELS == {"car", "truck", "bus", "motorcycle"}
    assert PERSON_LABELS == {"person"}
    for label in VEHICLE_LABELS:
        assert set(LABEL_KINDS[label]) == {"colour", "vehicle_type"}
    for label in PERSON_LABELS:
        assert set(LABEL_KINDS[label]) == {"clothing_top", "carrying"}


def test_people_are_a_separate_opt_in():
    """Person is the most common class by a wide margin, so widening the
    label set silently would multiply the bill of every site that had
    already assigned vqa for its vehicles."""
    from core.config import settings

    from services.descriptor_enrichment import PEOPLE_SETTING

    evidence = "cam1/2026/09/22/frame.jpg"
    assert getattr(settings, PEOPLE_SETTING) is False, "must default off"
    assert wants_descriptors("person", evidence, True, {DESCRIPTOR_SKILL}) is False
    assert wants_descriptors("person", evidence, True, {DESCRIPTOR_SKILL},
                             True) is True
    # The flag never bypasses the per-camera assignment.
    assert wants_descriptors("person", evidence, True, set(), True) is False
    # Vehicles are unaffected by it in either direction.
    assert wants_descriptors("car", evidence, True, {DESCRIPTOR_SKILL}) is True
    assert wants_descriptors("car", evidence, True, {DESCRIPTOR_SKILL},
                             True) is True


def test_the_ingest_path_passes_the_people_flag():
    """Defaulting the argument to False means a caller that forgets it
    simply never enriches a person — silent, and indistinguishable from
    the flag being off."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "routers/internal_camera_agent.py").read_text()
    assert "events_descriptor_people" in src, (
        "the ingest path never passes the people gate, so the setting "
        "does nothing on the live path")


def test_face_id_is_not_produced_here():
    """The plan lists it; this enricher deliberately does not write it.
    A name attached to a person in the platform store is a different
    privacy surface from an attribute, and belongs to the app an operator
    installed on purpose."""
    assert "face_id" not in KIND_QUESTIONS


# ── an answer we cannot count is not stored ───────────────────────


@pytest.mark.parametrize("answer,expected", [
    ("white", "white"),
    ("White.", "white"),
    # Models answer in sentences however firmly the question asks for one
    # word; throwing that away would drop a correct claim over phrasing.
    ("It is a white van.", "white"),
    ("The vehicle appears to be dark grey", "grey"),
    ("gray", "grey"),
])
def test_colour_answers_reduce_to_a_countable_token(answer, expected):
    assert normalise_answer("colour", answer) == expected


@pytest.mark.parametrize("answer,expected", [
    ("van", "van"),
    ("It looks like a lorry", "truck"),
    ("A sedan", "car"),
])
def test_vehicle_type_answers_reduce_too(answer, expected):
    assert normalise_answer("vehicle_type", answer) == expected


@pytest.mark.parametrize("answer,expected", [
    ("blue", "blue"),
    ("He is wearing a red jacket", "red"),
    ("a dark grey hoodie", "grey"),
])
def test_clothing_top_reduces_to_the_colour(answer, expected):
    """Defined narrowly as the COLOUR of the upper garment. An undefined
    kind is worse than a narrow one: two writers with different ideas of
    what clothing_top means produce values that can never agree, and
    journey.py compares them as strings."""
    assert normalise_answer("clothing_top", answer) == expected


@pytest.mark.parametrize("answer,expected", [
    ("backpack", "backpack"),
    ("She is carrying a rucksack", "backpack"),
    ("a shopping bag", "bag"),
    ("It looks like luggage", "suitcase"),
])
def test_carrying_reduces_to_one_object(answer, expected):
    assert normalise_answer("carrying", answer) == expected


@pytest.mark.parametrize("answer", [
    "nothing", "Nothing.", "They are empty-handed", "none",
])
def test_carrying_nothing_is_a_real_answer_and_is_kept(answer):
    """Unlike an unreadable colour, "nothing" is an observation. A person
    carrying nothing DISAGREES with a person carrying a backpack, and
    journey.py can only count that disagreement when both visits hold the
    claim — its rule 1 is "missing is not mismatch", so an unstored
    "nothing" scores neutrally where it should count against. How little
    a common answer is worth is already handled there: the surprise
    weighting measures it from the store rather than assuming it."""
    assert normalise_answer("carrying", answer) == "nothing"


@pytest.mark.parametrize("answer", [
    "",
    "I am not sure what that is",
    "The image is too blurry to tell",
    "a vehicle",
])
def test_an_answer_outside_the_vocabulary_is_dropped(answer):
    """value is a countable field — the model's own note is that how
    common a value is decides whether it is weak evidence or nearly an
    identifier. Prose in there makes the filter useless."""
    assert normalise_answer("colour", answer) is None


def test_an_unknown_kind_is_never_normalised():
    assert normalise_answer("hair_colour", "brown") is None


def test_multiword_synonyms_beat_the_single_token_pass():
    """'dark grey' must not be caught by the bare 'grey' match and then
    re-mapped differently — ordering here is load-bearing."""
    assert normalise_answer("colour", "dark grey") == "grey"


# ── plan-driven, not hardcoded ────────────────────────────────────


def test_the_task_name_is_the_canonical_one_the_plan_reports():
    """The plan reports canonical names since the alias fix, so matching
    on 'visual_qa' here would silently never fire."""
    from services.enrichment_plan import TASK_DESCRIPTORS

    assert VQA_TASK in TASK_DESCRIPTORS
    assert "colour" in TASK_DESCRIPTORS[VQA_TASK]["kinds"]


def test_every_kind_we_ask_about_is_one_the_plan_promises():
    """A kind this enricher writes but the plan never advertises is a
    claim the UI would never offer a filter for."""
    from services.enrichment_plan import TASK_DESCRIPTORS

    promised = set(TASK_DESCRIPTORS[VQA_TASK]["kinds"])
    assert set(KIND_QUESTIONS) <= promised, (
        f"{sorted(set(KIND_QUESTIONS) - promised)} is asked for but not in "
        "the plan's descriptor_kinds")


def test_the_vqa_call_sends_no_task_key_and_reads_answer(monkeypatch):
    """The camera agent learned this the hard way: a VQA adapter only
    answers when the task is NOT scene_caption, so sending a task turns
    every question into the same generic caption.

    Asserted on the request that actually goes out, not by grepping the
    source — the first version of this test matched the word "task" in
    the comment explaining why there is no task key.
    """
    import asyncio

    import httpx

    from services import descriptor_enrichment as mod

    sent: dict = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            # A captioner would answer in `caption`; taking that would
            # store the scene description as the vehicle's colour.
            return {"result": {"answer": "white", "caption": "a street"}}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            sent["url"] = url
            sent["body"] = json or {}
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())

    out = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        mod._ask(b"\xff\xd8\xffjpeg", "moondream", "What colour?", "cam1", 7))

    assert out == "white", "the answer must come from `answer`, not `caption`"
    assert "task" not in sent["body"], (
        "a task key makes moondream caption instead of answer")
    assert sent["body"]["question"] == "What colour?"
    assert sent["body"]["prompt"] == "What colour?"
    assert sent["body"]["frame_b64"], "the frame must ride along"
    assert "moondream" in sent["url"]


def _asked_questions(label: str, *, people: bool, monkeypatch) -> list[str]:
    """Run the real enricher over one visit and report what it ASKED.

    The kind selection is the whole cost story, and it lives inside
    ``enrich_event_descriptors`` — asserting on LABEL_KINDS alone would
    pass while the enricher ignored it.
    """
    import asyncio
    import pathlib
    import tempfile

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import core.database
    from core.config import settings
    from core.database import Base
    from models import Camera, Role, TimelineEvent, User
    from services import descriptor_enrichment as mod
    from services import evidence_store

    engine = create_engine("sqlite:///:memory:", future=True,
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(Role(id=1, name="admin", description="t"))
    db.commit()
    db.add(User(id=1, username="o", email="o@x.test", hashed_password="x",
                is_active=True, role_id=1))
    db.commit()
    db.add(Camera(id=1, name="c", ip_address="10.0.0.1",
                  rtsp_url="rtsp://x/1", owner_id=1))
    db.commit()
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    db.add(TimelineEvent(id=1, camera_id=1, source="tier0", event_type="track",
                         label=label, started_at=now,
                         ended_at=now + timedelta(seconds=5),
                         evidence_path="e.jpg"))
    db.commit()

    class _Shared:
        def __init__(self, s):
            self._s = s

        def __getattr__(self, n):
            return getattr(self._s, n)

        def close(self):
            return None

    monkeypatch.setattr(core.database, "SessionLocal", lambda: _Shared(db))
    monkeypatch.setattr(settings, "events_descriptor_enrichment", True,
                        raising=False)
    monkeypatch.setattr(settings, PEOPLE_SETTING, people, raising=False)

    tmp = pathlib.Path(tempfile.mkdtemp()) / "e.jpg"
    tmp.write_bytes(b"\xff\xd8\xffjpeg")
    monkeypatch.setattr(evidence_store, "resolve_evidence", lambda _p: tmp)

    async def _plan(_label):
        # Everything the plan can offer, so only the enricher's own
        # filtering decides what is asked.
        return [{"task": VQA_TASK, "healthy": True, "adapters": ["moondream"],
                 "descriptor_kinds": ["colour", "vehicle_type",
                                      "clothing_top", "carrying", "face_id"]}]

    asked: list[str] = []

    async def _ask(jpeg, adapter, question, handle, event_id):
        asked.append(question)
        return None      # nothing stored; we are counting the questions

    monkeypatch.setattr(mod, "_plan_skills", _plan)
    monkeypatch.setattr(mod, "_ask", _ask)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(mod.enrich_event_descriptors(1))
    finally:
        loop.close()
        db.close()
        engine.dispose()
    return asked


def test_a_vehicle_is_never_asked_what_colour_its_top_is(monkeypatch):
    asked = _asked_questions("truck", people=True, monkeypatch=monkeypatch)
    assert len(asked) == 2, "the per-visit bill is two questions"
    joined = " ".join(asked).lower()
    assert "vehicle" in joined
    assert "wearing" not in joined and "carrying" not in joined


def test_a_person_is_asked_the_person_questions(monkeypatch):
    asked = _asked_questions("person", people=True, monkeypatch=monkeypatch)
    assert len(asked) == 2
    joined = " ".join(asked).lower()
    assert "wearing" in joined and "carrying" in joined
    assert "vehicle" not in joined


def test_a_person_is_asked_nothing_while_the_flag_is_off(monkeypatch):
    """The background task re-checks it: a visit queued before the flag
    was turned off must not still be paid for."""
    assert _asked_questions("person", people=False, monkeypatch=monkeypatch) == []


def test_face_id_is_never_asked_even_when_the_plan_offers_it(monkeypatch):
    for label in ("person", "truck"):
        asked = _asked_questions(label, people=True, monkeypatch=monkeypatch)
        assert not any("who" in q.lower() or "name" in q.lower() for q in asked)
        assert len(asked) == 2


def test_the_ingest_path_gates_descriptors_like_the_other_enrichers():
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "routers/internal_camera_agent.py").read_text()
    assert "background.add_task(enrich_event_descriptors" in src, (
        "nothing queues the descriptor task — visit_descriptors goes back "
        "to having no producer")
    assert "wants_descriptors(" in src


def test_the_endpoint_and_the_enricher_share_one_writer():
    """Two copies of the upsert key, the conflict count and the
    ran_tasks rule would drift invisibly — a claim written one way and
    read another still looks like a claim."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    router = (root / "routers/internal_camera_agent.py").read_text()
    enricher = (root / "services/descriptor_enrichment.py").read_text()
    assert "from services.descriptor_store import apply_descriptors" in router
    assert "from services.descriptor_store import apply_descriptors" in enricher


def test_plan_skills_resolves_without_being_patched(monkeypatch):
    """Every other test here replaces ``_plan_skills``. This one runs it.

    The enricher imported ``compute_enrichment_plan`` from a module that
    never defined it; the ImportError was swallowed at DEBUG and the plan
    came back empty — so with the flag on, the adapter registered and
    the skill assigned, not one descriptor was ever written. Only KAI-C
    is stubbed here; the import path is real."""
    import asyncio
    from services import descriptor_enrichment as mod
    from services import enrichment_plan as ep
    from services import kai_c_service

    async def _caps(self):                    # the shape KAI-C really sends
        return {"adapters": {"ollamavlm": {"url": "http://ollamavlm-adapter:9009",
            "capabilities": {"tasks_advertised": ["visual_qa", "scene_caption"]}}}}

    async def _health(self):
        return {"kai_c_status": "ok", "adapters": {"ollamavlm": {"status": "ok"}}}

    monkeypatch.setattr(kai_c_service.KaiCService, "get_capabilities", _caps)
    monkeypatch.setattr(kai_c_service.KaiCService, "check_kai_c_health", _health)
    monkeypatch.setattr(ep, "CACHE", ep.PlanCache())      # not a stale plan from another test

    skills = asyncio.run(mod._plan_skills("car"))
    tasks = {s["task"] for s in skills}
    assert VQA_TASK in tasks, skills
    vqa = next(s for s in skills if s["task"] == VQA_TASK)
    assert vqa["healthy"] is True
    assert "colour" in vqa["descriptor_kinds"]
