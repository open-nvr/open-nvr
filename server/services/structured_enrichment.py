# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""One structured inference per best frame (RFC-0004, step 1).

Today a visit costs the captioner one call for the sentence and then one
call per descriptor kind for the attributes — three for a vehicle, up to
six for a person — on the same model and the same frame. Measured on a
Ventuno Q (2026-11-12): 325 Gemma calls in ten busy minutes, 225 of them
questions, 48 % of the Hexagon, the detector at four times its floor.

This module asks once. The prompt wants a one-sentence caption and the
kinds' answers as one JSON object; the caption lands in ``event_text``
and each answer goes through ``descriptor_enrichment.normalise_answer``
into ``visit_descriptors`` — the same rows, the same vocabulary, the same
filters as the per-question path, so nothing downstream changes shape.

It is reached from ``caption_enrichment.enrich_event_caption`` when
``EVENTS_ENRICHMENT_SINGLE_CALL`` is on and the visit's class has kinds
to ask about; ``descriptor_enrichment`` then leaves such a visit alone
(the one call carries its questions) and asks as before for a visit that
already has its caption. A model that answers with no usable JSON costs
nothing extra: its text is kept as the caption, VQA is left un-ran, and
the per-question path asks later — counted in :data:`STATS` so the
measurement says how often that happens.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from services.descriptor_enrichment import (
    DESCRIBABLE_LABELS,
    KIND_QUESTIONS,
    PEOPLE_EXTENDED_SETTING,
    PEOPLE_SETTING,
    PERSON_LABELS,
    VQA_TASK,
    Claim,
    kinds_for,
    normalise_answer,
)
from services.enrichment_gate import DROPPED

logger = logging.getLogger("structured_enrichment")

SETTING = "events_enrichment_single_call"

#: Counters for the measurement: how many structured calls went out,
#: how many answered in JSON, how many did not, how many claims landed.
STATS: dict[str, int] = {"calls": 0, "json_ok": 0, "json_failed": 0,
                         "claims": 0, "captions": 0}


def enabled() -> bool:
    from core.config import settings

    return bool(getattr(settings, SETTING, False))


@dataclass
class Structured:
    """What one call produced."""

    caption: str = ""
    claims: list[Claim] = field(default_factory=list)
    #: The answer parsed as JSON. False means the text is kept as the
    #: caption and the questions are left for the per-question path.
    json_ok: bool = False


# ── the prompt ────────────────────────────────────────────────────

_ASK_RE = re.compile(r"\s*Answer with one word.*$", re.I)


def _question_for(kind: str) -> str:
    """The kind's question without its one-word instruction, which the
    JSON instruction below replaces for all of them at once."""
    text = str(KIND_QUESTIONS[kind]["question"])
    return _ASK_RE.sub("", text).strip()


SUBJECT = {"vehicle": "the main vehicle (the largest or nearest one)",
           "person": "the main person (the largest or nearest one)"}


def _subject(kinds: list[str]) -> str:
    return SUBJECT["person"] if any(k in ("clothing_top", "carrying", "gender",
                                          "clothing_bottom", "headwear") for k in kinds) \
        else SUBJECT["vehicle"]


def build_prompt(kinds: list[str]) -> str:
    """One prompt for the kinds' answers and a caption.

    The attribute keys come FIRST and the caption last. The model writes
    its JSON in key order, and every later value is conditioned on the
    earlier text: with the caption first, a frame holding a white van in
    front of a yellow ambulance produced "yellow" for the van's colour
    (measured 2026-11-13, six of six colour disagreements went that way).
    Asked first, each attribute is answered from the image, as the
    per-question path answers it. The subject is named so a crop with
    two vehicles in it is answered for the one the visit is about, and
    the caption asks for what the plain caption prompt asks for — who or
    what, what they wear, what they do — so search loses no words."""
    lines = ["Look at this camera frame."]
    if kinds:
        lines.append(f"Answer these questions about {_subject(kinds)} with one word each:")
        for kind in kinds:
            vocab = ", ".join(str(v) for v in KIND_QUESTIONS[kind].get("vocabulary") or [])
            lines.append(f'- "{kind}": {_question_for(kind)} One of: {vocab}; or "unknown".')
    lines.append('Then write one sentence (the "caption") describing who or what is in the '
                 "frame, what they wear and what they are doing. Plain words, no preamble.")
    keys = ", ".join([f'"{k}": "..."' for k in kinds] + ['"caption": "..."'])
    lines.append("Reply with a JSON object only, no other text, with exactly these keys:")
    lines.append("{" + keys + "}")
    return "\n".join(lines)


def build_attributes_prompt(kinds: list[str]) -> str:
    """The kinds' questions as one call, with no caption in it. The plain
    caption call stays as it is — its wording is what search was built
    on — and this call replaces the N single questions. Measured on a
    Ventuno Q (2026-11-13, paired runs 5 and 5b): for two kinds it costs
    what the two questions cost (1,340 against 1,476 ms, no gain); for
    the five extended person kinds 2,173 against 3,670 ms, with the same
    answers where the crop allows one — so it is used from three kinds."""
    lines = ["Look at this camera frame.",
             f"Answer these questions about {_subject(kinds)} with one word each:"]
    for kind in kinds:
        vocab = ", ".join(str(v) for v in KIND_QUESTIONS[kind].get("vocabulary") or [])
        lines.append(f'- "{kind}": {_question_for(kind)} One of: {vocab}; or "unknown".')
    keys = ", ".join(f'"{k}": "..."' for k in kinds)
    lines.append("Reply with a JSON object only, no other text, with exactly these keys:")
    lines.append("{" + keys + "}")
    return "\n".join(lines)


# ── the answer ────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def parse_answer(text: str) -> dict[str, Any] | None:
    """The JSON object in a model's reply, or None. Tolerant of a code
    fence and of prose around the object; strict about the result being
    an object with string values."""
    if not text:
        return None
    candidates = [m.group(1) for m in _FENCE_RE.finditer(text)] + [text]
    for cand in candidates:
        start, end = cand.find("{"), cand.rfind("}")
        if start < 0 or end <= start:
            continue
        try:
            obj = json.loads(cand[start:end + 1])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return {str(k).strip().lower(): v for k, v in obj.items()}
    return None


def claims_from(answers: dict[str, Any], kinds: list[str], adapter: str) -> list[Claim]:
    """The kinds' answers reduced to countable tokens; an "unknown" or a
    word outside the vocabulary is no claim, as in the per-question path."""
    out: list[Claim] = []
    for kind in kinds:
        raw = answers.get(kind)
        if not isinstance(raw, str) or not raw.strip() or raw.strip().lower() == "unknown":
            continue
        value = normalise_answer(kind, raw)
        if value is None:
            logger.debug("structured enrichment: unusable %s answer %r", kind, raw[:80])
            continue
        out.append(Claim(kind=kind, value=value, confidence=None,
                         source_task=VQA_TASK, source_adapter=adapter))
    return out


# ── what to ask this visit ────────────────────────────────────────

async def kinds_for_visit(label: str | None, adapter: str,
                          answered: set[str] | None = None) -> list[str]:
    """The descriptor kinds the single call should ask for this class
    on this adapter: the three filters of ``descriptor_enrichment``
    (the plan's promise, a question we can normalise, a kind worth asking
    this class), the people opt-in, kinds already answered left out,
    and — new here — the adapter must be one the plan lists for VQA,
    because a captioner that cannot answer a question is given the
    plain caption prompt instead."""
    from core.config import settings

    lab = (label or "").lower()
    if lab not in DESCRIBABLE_LABELS:
        return []
    if not getattr(settings, "events_descriptor_enrichment", True):
        return []
    if lab in PERSON_LABELS and not getattr(settings, PEOPLE_SETTING, False):
        return []
    from services.descriptor_enrichment import _plan_skills

    skills = await _plan_skills(lab)
    vqa = next((s for s in skills if s.get("task") == VQA_TASK and s.get("healthy")), None)
    if vqa is None or adapter not in (vqa.get("adapters") or []):
        return []
    for_label = kinds_for(lab, extended=bool(getattr(settings, PEOPLE_EXTENDED_SETTING, False)))
    answered = answered or set()
    return sorted(k for k in (vqa.get("descriptor_kinds") or [])
                  if k in KIND_QUESTIONS and k in for_label and k not in answered)


# ── the call ────────────────────────────────────────────────────────

async def ask_structured(jpeg: bytes, adapter: str, kinds: list[str],
                         camera_handle: str, event_id: int, *,
                         priority: str = "live") -> Structured | None:
    """One gated call for the caption and the kinds. Returns DROPPED when
    the gate refused it, None when the adapter never answered, and a
    :class:`Structured` otherwise — with ``json_ok`` False when the model
    answered in prose, which is then the caption and nothing more."""
    return await _ask_json(build_prompt(kinds), jpeg, adapter, kinds, camera_handle, event_id,
                           kind="caption", caller="structured enrichment", priority=priority)


async def ask_attributes(jpeg: bytes, adapter: str, kinds: list[str],
                         camera_handle: str, event_id: int, *,
                         priority: str = "live") -> Structured | None:
    """One gated call for the kinds alone — the batched form of the
    descriptor enricher's questions, on its VQA lane. DROPPED / None /
    :class:`Structured` as :func:`ask_structured`; ``json_ok`` False
    means the caller should ask one by one."""
    return await _ask_json(build_attributes_prompt(kinds), jpeg, adapter, kinds, camera_handle,
                           event_id, kind="vqa", caller="descriptor enrichment (batched)",
                           priority=priority)


async def _ask_json(prompt: str, jpeg: bytes, adapter: str, kinds: list[str],
                    camera_handle: str, event_id: int, *, kind: str, caller: str,
                    priority: str) -> Structured | None:
    import base64

    from services.enrichment_gate import infer_through_gate

    # The VQA shape — no "task" key, question and prompt both — for the
    # reason descriptor_enrichment._ask documents: that is what makes a
    # captioner answer the question instead of captioning regardless.
    body: dict[str, Any] = {
        "question": prompt,
        "prompt": prompt,
        "camera_id": camera_handle,
        "event_id": int(event_id),
        "frame_b64": base64.b64encode(jpeg).decode("ascii"),
    }
    STATS["calls"] += 1
    payload = await infer_through_gate(adapter, body, kind=kind, log=logger,
                                       caller=caller, priority=priority)
    if payload is DROPPED:
        STATS["calls"] -= 1
        return DROPPED
    if payload is None:
        return None
    result = (payload or {}).get("result")
    if not isinstance(result, dict):
        return None
    text = ""
    for key in ("answer", "caption", "text", "description"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            break
    return interpret(text, kinds, adapter)


def interpret(text: str, kinds: list[str], adapter: str) -> Structured:
    """The model's text as a caption plus claims. JSON: the caption key
    and the kinds. Prose: the text is the caption; the questions stay
    unanswered for the per-question path, and the counter says so."""
    answers = parse_answer(text)
    if answers is None:
        STATS["json_failed"] += 1
        out = Structured(caption=text, json_ok=False)
    else:
        STATS["json_ok"] += 1
        cap = answers.get("caption")
        out = Structured(caption=cap.strip() if isinstance(cap, str) else "",
                         claims=claims_from(answers, kinds, adapter), json_ok=True)
        STATS["claims"] += len(out.claims)
    if out.caption:
        STATS["captions"] += 1
    return out
