# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The descriptor questions as one call (RFC-0004, what survived step 1).

A visit's descriptor questions used to go to the model one call a kind:
two for a vehicle, up to five for a person with the extended questions
on. From ``EVENTS_DESCRIPTOR_BATCH_MIN_KINDS`` kinds they go as ONE call
whose answer is a JSON object, each value reduced through
``descriptor_enrichment.normalise_answer`` into the same rows. Measured
on a Ventuno Q (2026-11-13, bench/rfc-0004-infer-once-log.md): five
questions 3.7 s as single calls, 2.2 s as one, the same answers where
the crop allows one; two kinds cost the same either way and stay single.

What was built here first and removed: one call that asked for the
caption and the attributes together. It halved the model's time per
visit and lost the specific words search depends on ("camouflage" became
"beige patterned"; glasses, gestures and the second person went
missing). Quality is not a knob, so that path is gone rather than off.
The caption call is the plain one in ``caption_enrichment``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from services.descriptor_enrichment import (
    KIND_QUESTIONS,
    VQA_TASK,
    Claim,
    normalise_answer,
)
from services.enrichment_gate import DROPPED

logger = logging.getLogger("structured_enrichment")

#: Counters for the measurement: batched calls out, answered in JSON,
#: not in JSON, claims landed.
STATS: dict[str, int] = {"calls": 0, "json_ok": 0, "json_failed": 0,
                         "claims": 0, "captions": 0}


@dataclass
class Structured:
    """What one call produced."""

    caption: str = ""
    claims: list[Claim] = field(default_factory=list)
    #: The answer parsed as JSON. False means the questions are asked
    #: one by one instead.
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


# ── the call ────────────────────────────────────────────────────────

async def ask_attributes(jpeg: bytes, adapter: str, kinds: list[str],
                         camera_handle: str, event_id: int, *,
                         priority: str = "live") -> Structured | None:
    """One gated call for the kinds alone — the batched form of the
    descriptor enricher's questions, on its VQA lane. Returns DROPPED
    when the gate refused it, None when the adapter never answered, and
    a :class:`Structured` otherwise; ``json_ok`` False means the caller
    should ask one by one."""
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
