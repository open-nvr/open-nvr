# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Plan-driven claims about a visit — what THIS box's skills can say.

The third enricher, and the first that asks rather than assumes.
``plate_enrichment`` runs a fixed model on vehicles; ``caption_enrichment``
runs a fixed set of labels through whatever captioner is registered. This
one reads ``enrichment_plan`` — the registered ∩ healthy skills and the
descriptor kinds each is expected to produce — and runs what the box
actually offers on the visit in front of it.

That is the module's own stated purpose. ``enrichment_plan`` says "so
enrichment asks rather than assumes", and ``compute_enrichment_plan``
says the plan exists so "an enricher that ran what the plan offered, and
a UI that promised a filter the plan allowed, have to be describing the
same set of skills". Until now nothing on the enrichment side read it, so
``visit_descriptors`` had no producer at all: the table, its unique
constraint, its two indexes, the ingest endpoint, and the search
service's ``attrs`` filter all existed, and the only writers in the repo
were tests. "Every red van" could never match, because no claim had ever
been written.

Scope of this first cut, deliberately narrow
--------------------------------------------
* **VQA only.** It is the one shipped task whose kinds are the attributes
  search filters on. ``plate`` stays with ``plate_enrichment``, which
  already does multi-frame OCR far better than one question would.
* **``colour`` and ``vehicle_type``, on vehicles.** A hard ceiling of two
  inferences per visit. VQA answers one question at a time, so an
  unbounded kind list is an unbounded per-visit cost.
* **``face_id`` is NOT produced here**, though the plan lists it. A name
  attached to a person, written into the platform store for every person
  on an assigned camera, is a materially larger privacy surface than an
  attribute, and the plan itself calls it "the strongest claim in the set,
  and the most sensitive". It stays with the app that the operator
  deliberately installed for it.

An answer we cannot normalise is not written
--------------------------------------------
``value`` is a short, lowercased, countable field — the model notes that
"how common a value is in this deployment is what decides whether it is
weak evidence or nearly an identifier". A VQA model answers in sentences.
Storing "the vehicle appears to be a dark red colour" as a value would
fill the filter with prose nobody can match on, so every answer is
reduced to a token from a fixed vocabulary and anything outside it is
dropped. A claim that cannot be filtered on is not worth the row.
"""

from __future__ import annotations

import asyncio as _asyncio
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("descriptor_enrichment")

#: The canonical task name (server/config/tasks.yml). The plan reports
#: canonical names since the alias fix, so this matches whether the
#: adapter advertises "vqa" or "visual_qa".
VQA_TASK = "vqa"

#: The per-camera assignment gate, same as the other two enrichers.
#: ``wants_plate`` grew one after "a thirty-camera site paid plate
#: recognition thirty times over to watch one gate"; a question per
#: vehicle has exactly that shape.
DESCRIPTOR_SKILL = VQA_TASK

#: Classes worth asking about, and the ceiling on cost. Vehicles only in
#: this cut: colour and type are the attributes an operator searches a
#: vehicle by, and a person's clothing changes between visits in a way a
#: registration plate does not.
DESCRIBABLE_LABELS = {"car", "truck", "bus", "motorcycle"}

#: Burst guard, same reasoning as the OCR and caption ones.
_VQA_CONCURRENCY = _asyncio.Semaphore(2)


@dataclass
class Claim:
    """What ``descriptor_store.apply_descriptors`` needs from us."""

    kind: str
    value: str
    confidence: float | None = None
    source_task: str | None = None
    source_adapter: str | None = None
    model_fingerprint: str | None = None


#: One question per kind, and a vocabulary to reduce the answer to.
#:
#: The question asks for one word because the vocabularies below are the
#: real contract — but a model that ignores the instruction still lands
#: correctly, since matching is done on the words IN the answer rather
#: than on the answer being a single token.
KIND_QUESTIONS: dict[str, dict[str, Any]] = {
    "colour": {
        "question": "What colour is the vehicle? Answer with one word.",
        # Deliberately coarse. "Midnight blue" and "navy" are both blue
        # to an operator scanning for a blue van, and a vocabulary that
        # splits them makes the filter worse, not better.
        "vocabulary": [
            "white", "black", "silver", "grey", "gray", "red", "blue",
            "green", "yellow", "orange", "brown", "beige", "gold",
            "maroon", "purple", "pink",
        ],
        # Answers arriving as a synonym we would otherwise drop.
        "synonyms": {"gray": "grey", "dark grey": "grey", "light grey": "grey"},
    },
    "vehicle_type": {
        "question": (
            "What kind of vehicle is this? Answer with one word, such as "
            "car, van, truck, bus, motorcycle, pickup or suv."
        ),
        "vocabulary": [
            "car", "van", "truck", "bus", "motorcycle", "pickup", "suv",
            "lorry", "taxi", "bicycle", "scooter", "tractor", "ambulance",
        ],
        "synonyms": {"lorry": "truck", "minivan": "van", "sedan": "car",
                     "hatchback": "car", "estate": "car"},
    },
}


def wants_descriptors(label: str | None, evidence_path: str | None,
                      enabled: bool = True,
                      camera_skills: set[str] | None = None) -> bool:
    """Should this visit be asked about? Pure, tested, four gates.

    ``None`` camera_skills means the caller could not resolve the camera
    and is treated as NOT assigned — failing closed, as with the other
    two enrichers. A wrong False costs a visit with fewer attributes; a
    wrong True spends inference on every vehicle of every camera on the
    site.
    """
    if not (enabled and evidence_path
            and (label or "").lower() in DESCRIBABLE_LABELS):
        return False
    return DESCRIPTOR_SKILL in (camera_skills or set())


def normalise_answer(kind: str, answer: str) -> str | None:
    """A VQA sentence reduced to one countable token, or None.

    Matching on the words IN the answer rather than requiring the whole
    answer to be a token: models say "It is a white van" however firmly
    the question asks for one word, and dropping that would throw away a
    correct claim over phrasing.
    """
    spec = KIND_QUESTIONS.get(kind)
    if not spec:
        return None
    text = (answer or "").strip().lower()
    if not text:
        return None
    synonyms: dict[str, str] = spec.get("synonyms") or {}
    # Multi-word synonyms first, so "dark grey" is not matched as "grey"
    # by the single-token pass and then re-mapped differently.
    for phrase, canonical in synonyms.items():
        if " " in phrase and phrase in text:
            return canonical
    words = {w.strip(".,;:!?'\"") for w in text.split()}
    for word in words:
        if word in synonyms:
            return synonyms[word]
    for token in spec.get("vocabulary") or []:
        if token in words:
            return synonyms.get(token, token)
    return None


async def _plan_skills(label: str | None) -> list[dict[str, Any]]:
    """The box's skills for this class, from the shared plan.

    Goes through ``compute_enrichment_plan`` rather than building its own
    view, because that function exists precisely so the enricher and the
    UI describe the same box — and it is already TTL-cached, which
    matters when this runs once per vehicle.
    """
    try:
        from routers.search import compute_enrichment_plan

        plan = await compute_enrichment_plan(label)
    except Exception as exc:  # noqa: BLE001
        logger.debug("descriptor enrichment: plan unavailable (%s)", exc)
        return []
    skills = (plan or {}).get("skills")
    return [s for s in skills if isinstance(s, dict)] if isinstance(skills, list) else []


async def _ask(jpeg: bytes, adapter: str, question: str,
               camera_handle: str, event_id: int) -> str | None:
    """One VQA question. None on any failure."""
    from core.config import settings

    import httpx

    # Deliberately NO "task" key. The camera agent learned this the hard
    # way: a VQA adapter (moondream) only answers when the task is not
    # scene_caption, while a pure captioner defaults to captioning when
    # no task is given — so omitting it is what makes a question get an
    # ANSWER rather than the same generic caption every time. Both
    # "question" and "prompt" are sent for the same reason it does.
    body: dict[str, Any] = {
        "question": question,
        "prompt": question,
        "camera_id": camera_handle,
        "event_id": int(event_id),
    }
    import base64

    body["frame_b64"] = base64.b64encode(jpeg).decode("ascii")
    try:
        async with _VQA_CONCURRENCY:
            async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
                resp = await client.post(
                    f"{settings.kai_c_url}/api/v1/infer/{adapter}",
                    json=body,
                    headers={"X-Internal-Api-Key": settings.internal_api_key},
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("descriptor enrichment: %s unreachable (%s)", adapter, exc)
        return None
    if resp.status_code != 200:
        logger.warning("descriptor enrichment: %s returned %s",
                       adapter, resp.status_code)
        return None
    try:
        result = (resp.json() or {}).get("result") or {}
    except Exception:  # noqa: BLE001
        return None
    # VQA adapters answer in `answer`; a captioner that ignored the
    # question would reply in `caption`, and that is NOT an answer to
    # what was asked — taking it would store the scene description as
    # the vehicle's colour.
    value = result.get("answer")
    return value.strip() if isinstance(value, str) and value.strip() else None


async def enrich_event_descriptors(event_id: int) -> None:
    """Background task: ask the box what it can say about this visit.

    Three phases, for the reason ``plate_enrichment`` documents: READ
    with a short session, CLOSE it, call the adapters with NO session
    held, then REOPEN to write. Holding a connection across the inference
    exhausted core's pool at roughly one visit a second.
    """
    from core.config import settings

    if not getattr(settings, "events_descriptor_enrichment", True):
        return

    # ── Phase 1: read, briefly ──────────────────────────────────────
    from core.database import SessionLocal
    from models import TimelineEvent

    db = SessionLocal()
    try:
        row = db.get(TimelineEvent, int(event_id))
        if row is None:
            return
        label = (row.label or "").lower()
        if label not in DESCRIBABLE_LABELS:
            return
        evidence_path = row.evidence_path
        already = set((row.payload or {}).get("enriched_by") or [])
        camera_handle = f"cam{row.camera_id}"
    finally:
        db.close()

    # Asked once. A re-run is a deliberate act through the endpoint, not
    # something a retried background task should pay for twice.
    if VQA_TASK in already:
        return
    if not evidence_path:
        return

    # ── Phase 2: plan, then ask — no session held ───────────────────
    skills = await _plan_skills(label)
    vqa = next((s for s in skills
                if s.get("task") == VQA_TASK and s.get("healthy")), None)
    if vqa is None:
        return
    adapters = [a for a in (vqa.get("adapters") or []) if isinstance(a, str)]
    if not adapters:
        return
    # Deterministic pick, so the same box answers with the same model and
    # a changed vocabulary is traceable to config rather than chance.
    adapter = sorted(adapters)[0]

    # Only kinds the PLAN promises AND this enricher can normalise. The
    # plan may offer clothing_top and carrying; those are person
    # attributes and out of scope for this cut, and a kind we cannot
    # reduce to a token is a row nobody can filter on.
    kinds = [k for k in (vqa.get("descriptor_kinds") or [])
             if k in KIND_QUESTIONS]
    if not kinds:
        return

    from services.evidence_store import resolve_evidence

    path = resolve_evidence(evidence_path)
    if path is None:
        return
    try:
        jpeg = path.read_bytes()
    except OSError as exc:
        logger.debug("descriptor enrichment: evidence unreadable (%s)", exc)
        return

    claims: list[Claim] = []
    for kind in sorted(kinds):
        spec = KIND_QUESTIONS[kind]
        answer = await _ask(jpeg, adapter, spec["question"],
                            camera_handle, event_id)
        if not answer:
            continue
        value = normalise_answer(kind, answer)
        if value is None:
            # Looked, got prose we cannot count. Recorded as "ran" below
            # so the row does not read as "nobody looked".
            logger.debug("descriptor enrichment: unusable %s answer %r",
                         kind, answer[:80])
            continue
        claims.append(Claim(
            kind=kind, value=value,
            # No confidence, deliberately. A VQA model returns no score,
            # and inventing one would let a guess weigh the same as a
            # measured read — which is the exact failure the confidence
            # column exists to prevent.
            confidence=None,
            source_task=VQA_TASK, source_adapter=adapter,
        ))

    # ── Phase 3: reopen and write ───────────────────────────────────
    from services.descriptor_store import apply_descriptors

    db = SessionLocal()
    try:
        row = db.get(TimelineEvent, int(event_id))
        if row is None:
            # Aged out by retention while we were asking.
            return
        # ran_tasks is written even when nothing was extracted: "looked
        # and found nothing" must be distinguishable from "never looked",
        # which is the distinction every attribute scheme gets wrong.
        apply_descriptors(db, row, claims, [VQA_TASK])
        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("descriptor enrichment: write failed for %s", event_id)
        db.rollback()
    finally:
        db.close()
