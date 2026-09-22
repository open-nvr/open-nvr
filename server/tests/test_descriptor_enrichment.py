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
    # A person is not in scope for this cut — clothing changes between
    # visits in a way a vehicle's colour does not.
    assert wants_descriptors("person", evidence, True, {DESCRIPTOR_SKILL}) is False
    assert wants_descriptors("car", None, True, {DESCRIPTOR_SKILL}) is False
    assert wants_descriptors("car", evidence, False, {DESCRIPTOR_SKILL}) is False


def test_the_cost_ceiling_is_two_questions_on_vehicles_only():
    """VQA answers one question at a time, so the kind list IS the
    per-visit inference bill."""
    assert set(KIND_QUESTIONS) == {"colour", "vehicle_type"}
    assert DESCRIBABLE_LABELS == {"car", "truck", "bus", "motorcycle"}


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
    assert normalise_answer("clothing_top", "a red jacket") is None


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
