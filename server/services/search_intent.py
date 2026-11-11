# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""What a question NEEDS from the box, and whether the box has it.

"Did you see a person in a red shirt" needs a skill that describes what
people wear. "Was Varun here" needs face recognition and a name that a
visit has actually been bound to. "What is the plate of the white car"
needs a plate reader and, more to the point, only wants visits that
carry a read. None of that is a filter the parser can express by
itself, and none of it is something the operator can be expected to
know about the deployment in front of them.

So search resolves the question against the box: each word that asks
for a skill becomes a :class:`Need`, with the skill that would answer
it and the STATE of that skill here — offered and producing claims,
offered but never assigned to a camera, or not on this box at all. The
states are different fixes ("assign Visual QA to the gate camera" vs
"install a captioner"), and "nothing matched" without the state is the
confident-wrong answer this whole area exists to avoid: an empty page
for "red shirt" on a box where nothing has ever looked at a shirt.

Deterministic and vocabulary-driven, on purpose — the vocabulary IS the
set of values the descriptor enricher can write
(``descriptor_enrichment.KIND_QUESTIONS``), so a word resolves to a
kind exactly when a skill could have produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from services.descriptor_enrichment import (
    KIND_QUESTIONS, PERSON_LABELS, VEHICLE_LABELS,
)

__all__ = ["Need", "resolve_needs", "STATE_AVAILABLE", "STATE_UNASSIGNED",
           "STATE_MISSING", "STATE_PEOPLE_OFF", "PERSON_KIND", "PEOPLE_KINDS"]

PERSON_KIND = "face_id"

#: The skill is registered, healthy, and has written claims of this kind.
STATE_AVAILABLE = "available"
#: The skill is registered and healthy but no visit in scope carries a
#: claim of this kind — it was never assigned to a camera (or the flag
#: that admits people is off). The fix is a setting, not an install.
STATE_UNASSIGNED = "never-produced"
#: The skill is here and producing, but this kind is about a PERSON and
#: the setting that admits people (EVENTS_DESCRIPTOR_PEOPLE) is off: the
#: captioner is asked about vehicles only. A setting, not an install,
#: and not the app the skill came with — the old message sent people to
#: the Agent app's scene description, which was already on (measured
#: on the Ventuno Q, 2026-11-07: "blue kurta" in the caption, the notice
#: saying blue could not be searched).
STATE_PEOPLE_OFF = "people-off"
#: No healthy skill on this box produces this kind.
STATE_MISSING = "not-on-this-box"

#: Descriptor kinds written only when people descriptions are on
#: (descriptor_enrichment: clothing_top and carrying for every person
#: label; the extended kinds with the second flag).
PEOPLE_KINDS = frozenset({"clothing_top", "carrying", "gender", "clothing_bottom", "headwear"})

#: kind → the canonical task that produces it (mirrors enrichment_plan).
KIND_SKILL: dict[str, str] = {
    "colour": "vqa", "vehicle_type": "vqa", "clothing_top": "vqa",
    "carrying": "vqa", "gender": "vqa", "clothing_bottom": "vqa",
    "headwear": "vqa", PERSON_KIND: "face_recognition",
    "plate": "license_plate_recognition",
}


def canonical_value(kind: str, word: str) -> str:
    """The stored spelling of a typed attribute word: the kind's synonym
    map ("gray" → "grey", "navy" → "blue"), else the word itself."""
    spec = KIND_QUESTIONS.get(kind) or {}
    return str((spec.get("synonyms") or {}).get(word, word))


def _vocab(kind: str) -> set[str]:
    spec = KIND_QUESTIONS.get(kind) or {}
    words = set(spec.get("vocabulary") or [])
    words |= set(spec.get("synonyms") or {})
    return {w for w in words if " " not in w and w != "nothing"}


COLOUR_WORDS = _vocab("colour") | _vocab("clothing_top")
VEHICLE_TYPE_WORDS = _vocab("vehicle_type")
CARRYING_WORDS = _vocab("carrying")
GENDER_WORDS = _vocab("gender")
HEADWEAR_WORDS = _vocab("headwear")
#: Words that say the colour is about what a PERSON wears, whatever the
#: class words said: "red shirt" is clothing even when no class was named.
CLOTHING_CUES = frozenset({
    "shirt", "tshirt", "t-shirt", "tee", "top", "jacket", "coat", "hoodie",
    "hooded", "hood", "sweater", "sweatshirt", "pullover", "cardigan",
    "blazer", "jumper", "dress", "uniform", "vest", "wearing",
    "dressed", "clothes", "clothing", "blouse", "suit",
    # the garments people here actually wear and type
    "kurta", "kurti", "saree", "sari", "salwar", "kameez", "sherwani",
    "dupatta", "lungi", "dhoti", "abaya", "burqa",
})


@dataclass
class Need:
    """One thing the question asks of a skill."""

    word: str
    kind: str
    skill: str
    state: str
    #: How this kind is answered when the skill is not there: captions
    #: still carry colour words, so "red" can match a caption on a box
    #: with no VQA — the need is then advisory, not fatal.
    fallback: str | None = None
    #: Installed apps whose manifest brings this skill to the cameras they
    #: are picked for — the fix for ``never-produced`` is to pick the
    #: camera in one of these, and the UI can say which.
    apps: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"word": self.word, "kind": self.kind, "skill": self.skill,
                "state": self.state, "fallback": self.fallback, "apps": list(self.apps)}


@dataclass
class BoxAbilities:
    """What this deployment can say, as search sees it."""

    #: descriptor kinds a healthy skill offers (from the enrichment plan)
    offered_kinds: set[str] = field(default_factory=set)
    #: kinds at least one visit in scope actually carries a claim of
    claimed_kinds: set[str] = field(default_factory=set)
    #: a healthy captioner is registered (colour words can match captions)
    captions: bool = False
    #: EVENTS_DESCRIPTOR_PEOPLE: descriptors are written for people too,
    #: not just vehicles
    people_descriptors: bool = False
    #: skill → names of installed apps whose manifest brings it
    apps_by_skill: dict[str, list[str]] = field(default_factory=dict)


def _state(kind: str, box: BoxAbilities) -> str:
    if kind not in box.offered_kinds:
        return STATE_MISSING
    if kind in box.claimed_kinds:
        return STATE_AVAILABLE
    if kind in PEOPLE_KINDS and not box.people_descriptors:
        return STATE_PEOPLE_OFF
    return STATE_UNASSIGNED


def resolve_needs(
    *,
    words: Iterable[str],
    labels: Iterable[str],
    attrs: Iterable[tuple[str, str]] = (),
    wants_plate: bool = False,
    plate: str = "",
    box: BoxAbilities,
) -> list[Need]:
    """The skills this question leans on, with their state on this box.

    ``words`` are the parser's free-text words (already stripped of
    class, camera and time words); ``labels`` the classes it resolved.
    """
    labels = {str(l).lower() for l in labels}
    words = [str(w).lower() for w in words if w]
    wordset = set(words)
    person_q = bool(labels & PERSON_LABELS) or bool(wordset & CLOTHING_CUES)
    vehicle_q = bool(labels & VEHICLE_LABELS)
    needs: list[Need] = []
    seen: set[tuple[str, str]] = set()

    def _add(word: str, kind: str, fallback: str | None) -> None:
        if (word, kind) in seen:
            return
        seen.add((word, kind))
        skill = KIND_SKILL[kind]
        needs.append(Need(word=word, kind=kind, skill=skill,
                          state=_state(kind, box), fallback=fallback,
                          apps=list(box.apps_by_skill.get(skill, []))))

    caption_fallback = "captions" if box.captions else None
    for w in words:
        if w in COLOUR_WORDS:
            if person_q and not vehicle_q:
                _add(w, "clothing_top", caption_fallback)
            elif vehicle_q and not person_q:
                _add(w, "colour", caption_fallback)
            else:
                # No class named: a colour could be either. Both are
                # listed so the answer says which one this box lacks.
                _add(w, "colour", caption_fallback)
                _add(w, "clothing_top", caption_fallback)
        elif w in VEHICLE_TYPE_WORDS:
            _add(w, "vehicle_type", caption_fallback)
        elif w in CARRYING_WORDS:
            _add(w, "carrying", caption_fallback)
        elif w in GENDER_WORDS:
            _add(w, "gender", caption_fallback)
        elif w in HEADWEAR_WORDS:
            _add(w, "headwear", caption_fallback)
    for kind, value in attrs:
        if kind == PERSON_KIND:
            _add(value, PERSON_KIND, None)
    if plate or wants_plate:
        _add(plate or "plate", "plate", None)
    return needs

#: Which claim kind a vocabulary word belongs to, by the class the question
#: named. A colour is a vehicle's ``colour`` or a person's ``clothing_top``;
#: with no class named the word stays a caption word (a claim filter of the
#: wrong kind would hide the right visits).
_PERSON = frozenset({"person"})


def promote_words_to_claims(parsed, box: "BoxAbilities") -> list[tuple[str, str]]:
    """Move the parsed free words that name a claim VALUE this box actually
    holds claims of into ``parsed.attrs`` as (kind, value) filters, and
    return what was promoted. "red van" typed used to match "red" and "van"
    anywhere in a caption; where the plate or description app has written
    ``colour:red`` on visits, the claim is exact and the caption word is
    not. Words whose kind no visit in scope carries stay free text, where a
    caption can still match them. The UI chip for a promoted word is the
    same chip a picker would have made, so it can be dropped the same way."""
    words = [w for w in (parsed.text or "").split() if w]
    if not words or not box.claimed_kinds:
        return []
    labels = set(parsed.labels or [])
    person = bool(labels & _PERSON)
    vehicle = bool(labels and not person)
    promoted: list[tuple[str, str]] = []
    rest: list[str] = []
    for w in words:
        kind = None
        if w in COLOUR_WORDS:
            if person or any(c in words for c in CLOTHING_CUES):
                kind = "clothing_top"
            elif vehicle:
                kind = "colour"
        elif w in VEHICLE_TYPE_WORDS and not person:
            kind = "vehicle_type"
        elif w in CARRYING_WORDS and person:
            kind = "carrying"
        elif w in HEADWEAR_WORDS and person:
            kind = "headwear"
        elif w in GENDER_WORDS and person:
            kind = "gender"
        if kind and kind in box.claimed_kinds:
            # The claim is stored canonical ("grey", "blue"); the operator
            # typed "gray", "navy". Filter by what is stored, or a typed
            # synonym matches nothing — silently.
            value = canonical_value(kind, w)
            if (kind, value) not in parsed.attrs:
                promoted.append((kind, value))
        elif w not in CLOTHING_CUES or not promoted:
            rest.append(w)
    if promoted:
        parsed.attrs = list(parsed.attrs) + promoted
        parsed.text = " ".join(rest)
        parsed.matched["claims"] = " ".join(f"{k}:{v}" for k, v in promoted)
    return promoted
