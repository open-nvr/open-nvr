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

Scope, deliberately narrow
--------------------------
* **VQA only.** It is the one shipped task whose kinds are the attributes
  search filters on. ``plate`` stays with ``plate_enrichment``, which
  already does multi-frame OCR far better than one question would.
* **Two questions per visit, whatever it is.** ``colour`` and
  ``vehicle_type`` on a vehicle; ``clothing_top`` and ``carrying`` on a
  person. VQA answers one question at a time, so ``LABEL_KINDS`` is not
  a preference — it IS the per-visit inference bill, and it is capped at
  two by construction. The plan offers all four kinds on any label, so
  without the per-class split a lorry would be asked what colour its top
  is: an inference spent to store nonsense.
* **People are gated separately** (``PEOPLE_SETTING``, off by default).
  Person is the most common class on a camera by a wide margin, so
  quietly widening the label set would multiply the inference bill of
  every site that had already assigned vqa for its vehicles, without
  that site changing anything. The per-camera assignment cannot express
  "for vehicles, not for everyone who walks past"; this flag can.
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

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("descriptor_enrichment")

from services.enrichment_gate import DROPPED  # noqa: E402

#: The canonical task name (server/config/tasks.yml). The plan reports
#: canonical names since the alias fix, so this matches whether the
#: adapter advertises "vqa" or "visual_qa".
VQA_TASK = "vqa"

#: The per-camera assignment gate, same as the other two enrichers.
#: ``wants_plate`` grew one after "a thirty-camera site paid plate
#: recognition thirty times over to watch one gate"; a question per
#: vehicle has exactly that shape.
DESCRIPTOR_SKILL = VQA_TASK

#: Vehicle classes, and what is worth asking about one.
VEHICLE_LABELS = {"car", "truck", "bus", "motorcycle"}

#: The person class. Split out because it is gated separately and its
#: cost is of a different order — see ``PEOPLE_SETTING`` below.
PERSON_LABELS = {"person"}

#: Every class this enricher will ask about.
DESCRIBABLE_LABELS = VEHICLE_LABELS | PERSON_LABELS

#: The settings flag that admits people. Vehicles ride on
#: ``events_descriptor_enrichment``; people need this one as well.
#:
#: Why a second flag rather than just widening the label set: person is
#: by a long way the most common class on a camera, so asking two
#: questions per person visit multiplies the inference bill of a site
#: that already assigned vqa — without that site changing anything. The
#: per-camera assignment is the gate that stops unasked-for inference,
#: and it cannot express "I wanted this for vehicles, not for everyone
#: who walks past". This flag can.
PEOPLE_SETTING = "events_descriptor_people"

#: The second person opt-in. With it, a person visit is asked three more
#: questions (``EXTENDED_PERSON_KINDS``): five inferences a visit instead
#: of two. ``LABEL_KINDS`` keeps the two-question ceiling; ``kinds_for``
#: adds these only when the flag says so.
PEOPLE_EXTENDED_SETTING = "events_descriptor_people_extended"
EXTENDED_PERSON_KINDS: tuple[str, ...] = ("gender", "clothing_bottom", "headwear")


def kinds_for(label: str | None, *, extended: bool = False) -> tuple[str, ...]:
    """What to ask about this class: ``LABEL_KINDS`` and, for a person
    when the extended flag is on, ``EXTENDED_PERSON_KINDS`` after them."""
    lab = (label or "").lower()
    base = LABEL_KINDS.get(lab, ())
    if extended and lab in PERSON_LABELS:
        return tuple(base) + EXTENDED_PERSON_KINDS
    return tuple(base)

#: Which kinds are asked on which class, and the ceiling on cost. VQA
#: answers ONE question at a time, so this map IS the per-visit
#: inference bill: two, whatever walks or drives past.
LABEL_KINDS: dict[str, tuple[str, ...]] = {
    **{label: ("colour", "vehicle_type") for label in VEHICLE_LABELS},
    **{label: ("clothing_top", "carrying") for label in PERSON_LABELS},
}

#: Burst control lives in ``services.enrichment_gate``, shared with the
#: captioner (same model on most boxes) — see there for why the
#: semaphore that used to sit here made #583 worse, not better.


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
        "synonyms": {"gray": "grey", "dark grey": "grey", "light grey": "grey",
                     "charcoal": "grey", "navy": "blue", "dark blue": "blue",
                     "light blue": "blue", "sky blue": "blue", "crimson": "red",
                     "scarlet": "red", "tan": "brown", "khaki": "beige",
                     "cream": "white", "off-white": "white", "off white": "white",
                     "golden": "gold", "violet": "purple", "lavender": "purple",
                     "magenta": "pink", "olive": "green"},
    },
    "vehicle_type": {
        "question": (
            "What kind of vehicle is this? Answer with one word, such as "
            "car, van, truck, bus, motorcycle, pickup or suv."
        ),
        "vocabulary": [
            "car", "van", "truck", "bus", "motorcycle", "pickup", "suv",
            "lorry", "taxi", "bicycle", "scooter", "tractor", "ambulance",
            "rickshaw",
        ],
        "synonyms": {"lorry": "truck", "minivan": "van", "sedan": "car",
                     "hatchback": "car", "estate": "car", "jeep": "suv",
                     "mpv": "van", "tempo": "van", "auto": "rickshaw",
                     "autorickshaw": "rickshaw", "auto rickshaw": "rickshaw",
                     "auto-rickshaw": "rickshaw", "tuk tuk": "rickshaw",
                     "tuk-tuk": "rickshaw", "scooty": "scooter", "moped": "scooter",
                     "cab": "taxi"},
    },
    "clothing_top": {
        # Defined narrowly, as the COLOUR of the upper garment, because
        # an undefined kind is worse than a narrow one: two enrichers
        # with different ideas of what "clothing_top" means write values
        # that can never agree, and journey.py compares them as strings.
        # Colour is also the part that survives a change of camera —
        # "jacket" and "coat" are the same garment described twice.
        "question": (
            "What colour is the top the person is wearing? Answer with "
            "one word."
        ),
        "vocabulary": [
            "white", "black", "silver", "grey", "gray", "red", "blue",
            "green", "yellow", "orange", "brown", "beige", "gold",
            "maroon", "purple", "pink",
        ],
        "synonyms": {"gray": "grey", "dark grey": "grey", "light grey": "grey",
                     "charcoal": "grey", "navy": "blue", "dark blue": "blue",
                     "light blue": "blue", "sky blue": "blue", "crimson": "red",
                     "scarlet": "red", "tan": "brown", "khaki": "beige",
                     "cream": "white", "off-white": "white", "off white": "white",
                     "golden": "gold", "violet": "purple", "lavender": "purple",
                     "magenta": "pink", "olive": "green"},
    },
    "carrying": {
        "question": (
            "What is the person carrying? Answer with one word, such as "
            "backpack, bag, suitcase, box, umbrella, or nothing."
        ),
        # "nothing" is a real answer here, unlike an unreadable colour,
        # and it is stored. A person carrying nothing DISAGREES with a
        # person carrying a backpack, and journey.py can only count that
        # disagreement if both visits carry the claim — rule 1 there is
        # "missing is not mismatch", so an unstored "nothing" scores
        # neutrally when it should count against. How little a common
        # answer is worth is already handled: the surprise weighting
        # measures that from the store rather than assuming it.
        "vocabulary": [
            "nothing", "backpack", "bag", "suitcase", "box", "umbrella",
            "trolley", "bicycle", "phone", "parcel", "basket",
        ],
        "synonyms": {
            "rucksack": "backpack", "handbag": "bag", "holdall": "bag",
            "shopping bag": "bag", "carrier bag": "bag", "package": "parcel",
            "luggage": "suitcase", "case": "suitcase", "cart": "trolley",
            "pram": "trolley", "buggy": "trolley", "none": "nothing",
            "empty handed": "nothing", "empty-handed": "nothing",
            "nothing at all": "nothing",
        },
    },
    # ── extended person kinds (EVENTS_DESCRIPTOR_PEOPLE_EXTENDED) ──
    "gender": {
        "question": (
            "Is the person a man, a woman or a child? Answer with one word."
        ),
        "vocabulary": ["man", "woman", "child"],
        "synonyms": {
            "male": "man", "female": "woman", "boy": "child", "girl": "child",
            "lady": "woman", "gentleman": "man", "guy": "man", "kid": "child",
            "men": "man", "women": "woman", "children": "child",
        },
    },
    "clothing_bottom": {
        # The colour of the lower garment, defined as narrowly as
        # clothing_top and for the same reason.
        "question": (
            "What colour are the trousers, skirt or shorts the person is "
            "wearing? Answer with one word."
        ),
        "vocabulary": [
            "white", "black", "silver", "grey", "gray", "red", "blue",
            "green", "yellow", "orange", "brown", "beige", "gold",
            "maroon", "purple", "pink", "denim",
        ],
        "synonyms": {"gray": "grey", "dark grey": "grey", "light grey": "grey",
                     "jeans": "denim", "navy": "blue", "khaki": "beige"},
    },
    "headwear": {
        # "nothing" is stored, as with carrying: a bare head disagrees
        # with a helmet, and a PPE check needs the bare head counted.
        "question": (
            "What is the person wearing on their head? Answer with one "
            "word, such as helmet, cap, hat, hood, turban, scarf, or nothing."
        ),
        "vocabulary": [
            "nothing", "helmet", "cap", "hat", "hood", "turban", "scarf",
            "headscarf", "bandana", "veil", "beanie",
        ],
        "synonyms": {
            "hard hat": "helmet", "hardhat": "helmet", "safety helmet": "helmet",
            "baseball cap": "cap", "hoodie": "hood", "hijab": "headscarf",
            "dupatta": "scarf", "none": "nothing", "bare": "nothing",
            "no hat": "nothing", "nothing at all": "nothing",
        },
    },
}


def wants_descriptors(label: str | None, evidence_path: str | None,
                      enabled: bool = True,
                      camera_skills: set[str] | None = None,
                      people: bool = False) -> bool:
    """Should this visit be asked about? Pure, tested, five gates.

    ``None`` camera_skills means the caller could not resolve the camera
    and is treated as NOT assigned — failing closed, as with the other
    two enrichers. A wrong False costs a visit with fewer attributes; a
    wrong True spends inference on every vehicle of every camera on the
    site.

    ``people`` is the fifth gate and defaults OFF. A person visit is
    asked about only when a site has said so explicitly, because person
    is the most common class by a wide margin and widening the label set
    silently would multiply the bill of every site that had already
    assigned vqa for its vehicles.
    """
    lab = (label or "").lower()
    if not (enabled and evidence_path and lab in DESCRIBABLE_LABELS):
        return False
    if lab in PERSON_LABELS and not people:
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
        from services.enrichment_plan import compute_enrichment_plan

        plan = await compute_enrichment_plan(label)
    except Exception as exc:  # noqa: BLE001
        logger.debug("descriptor enrichment: plan unavailable (%s)", exc)
        return []
    skills = (plan or {}).get("skills")
    return [s for s in skills if isinstance(s, dict)] if isinstance(skills, list) else []


async def _ask(jpeg: bytes, adapter: str, question: str,
               camera_handle: str, event_id: int, *,
               priority: str = "live") -> str | None:
    """One VQA question. None on any failure."""


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
    # Same gate the captioner uses — on most boxes it is the same model
    # answering both, and one slow model gets one budget (#583).
    from services.enrichment_gate import infer_through_gate

    payload = await infer_through_gate(adapter, body, kind="vqa", log=logger,
                                       caller="descriptor enrichment",
                                       priority=priority)
    if payload is DROPPED:
        return DROPPED
    if payload is None:
        return None
    result = (payload or {}).get("result") or {}
    # VQA adapters answer in `answer`; a captioner that ignored the
    # question would reply in `caption`, and that is NOT an answer to
    # what was asked — taking it would store the scene description as
    # the vehicle's colour.
    value = result.get("answer")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _single_call_carries(db, row, already: set[str], priority: str) -> bool:
    """Will the caption task's one structured call carry this visit's
    questions? Only when the single call is on, the caption lane will run
    for this visit (its switch, its class, its camera assignment), and
    nobody has captioned it yet. A visit that already has its sentence
    — or that the captioner looked at and found nothing — is asked here
    as before, so a model that answers in prose still gets its
    attributes on the next pass."""
    from services import structured_enrichment

    if not structured_enrichment.enabled():
        return False
    from core.config import settings
    from services.caption_enrichment import CAPTION_SKILL, CAPTIONABLE_LABELS

    if priority != "requested" and not getattr(settings, "events_caption_enrichment", True):
        return False
    if (row.label or "").lower() not in CAPTIONABLE_LABELS:
        return False
    if CAPTION_SKILL in already:
        return False
    from models import Camera, EventText
    from services.skill_assignments import camera_skills

    camera = db.get(Camera, row.camera_id)
    if camera is None or CAPTION_SKILL not in camera_skills(camera):
        return False
    text = db.get(EventText, row.id)
    return text is None or not (text.caption or "").strip()


async def enrich_event_descriptors(event_id: int, *,
                                   priority: str = "live") -> str | None:
    """Background task: ask the box what it can say about this visit.

    Three phases, for the reason ``plate_enrichment`` documents: READ
    with a short session, CLOSE it, call the adapters with NO session
    held, then REOPEN to write. Holding a connection across the inference
    exhausted core's pool at roughly one visit a second.
    """
    from core.config import settings

    if priority != "requested" and not getattr(
            settings, "events_descriptor_enrichment", True):
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
        if label in PERSON_LABELS and not getattr(
                settings, PEOPLE_SETTING, False):
            # Re-checked here and not only at the gate: this is a
            # background task, so a visit queued before the flag was
            # turned off would otherwise still be paid for.
            return
        evidence_path = row.evidence_path
        already = set((row.payload or {}).get("enriched_by") or [])
        camera_handle = f"cam{row.camera_id}"
        if VQA_TASK not in already:
            # The same frame asked before (RFC-0004 step 2): its claims
            # are this visit's claims; nothing to ask.
            from services import enrichment_reuse

            twin = enrichment_reuse.find_twin(db, row)
            if twin is not None and enrichment_reuse.reuse_descriptors(db, row, twin):
                db.commit()
                return "done"
        if _single_call_carries(db, row, already, priority):
            # RFC-0004 step 1: the caption task asks the questions in the
            # same call as the sentence. Asking them here too would be
            # the duplicate the step exists to remove.
            return
        # Kinds VQA already answered on this visit. A pass whose other
        # question was DROPPED leaves VQA un-ran (so it is asked again),
        # and the retry must not pay for the answers it already has.
        from models import VisitDescriptor

        answered = {
            k for (k,) in db.query(VisitDescriptor.kind)
            .filter(VisitDescriptor.event_id == row.id,
                    VisitDescriptor.source_task == VQA_TASK).all()
        }
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

    # Three filters, and each drops a different kind of mistake. The PLAN
    # decides what this box can claim at all; KIND_QUESTIONS decides what
    # we can reduce to a countable token, because a kind we cannot
    # normalise is a row nobody can filter on; and LABEL_KINDS decides
    # what is worth asking about THIS class — the plan offers all four
    # kinds on any label, and asking a lorry what colour its top is
    # spends an inference to store nonsense.
    for_label = kinds_for(label, extended=bool(
        getattr(settings, PEOPLE_EXTENDED_SETTING, False)))
    kinds = [k for k in (vqa.get("descriptor_kinds") or [])
             if k in KIND_QUESTIONS and k in for_label and k not in answered]
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
    dropped = 0
    for kind in sorted(kinds):
        spec = KIND_QUESTIONS[kind]
        answer = await _ask(jpeg, adapter, spec["question"],
                            camera_handle, event_id, priority=priority)
        if answer is DROPPED:
            # Never reached the adapter. Not "looked and found nothing":
            # nobody looked, and recording VQA as ran would make the
            # drop permanent — every later pass would skip this visit.
            dropped += 1
            continue
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
        # Unless a question was DROPPED: then nobody has looked yet, the
        # claims that did come back are kept, and VQA is left un-ran so
        # the backfill asks again.
        apply_descriptors(db, row, claims, [VQA_TASK] if not dropped else [])
        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("descriptor enrichment: write failed for %s", event_id)
        db.rollback()
    finally:
        db.close()
    return "dropped" if dropped else "done"
