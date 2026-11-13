# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Route a camera question before the LLM sees it.

The question space is small: what is on a camera NOW, or what CAME BY in
some window. One camera or all, one class of thing, one time phrase.
Today every one of those goes to a 1.5B model with fifteen tool schemas
and a request to compute an ISO window from the clock — the ~4k-token
prompt and the 17 s "iter 1" of the field trace, for a decision a regex
makes in a microsecond. The agent already has the regexes: they run
AFTER the model fails to ground ("forced grounding"). This runs them
FIRST, when they are sure.

Three tiers::

    0  deterministic — every slot resolves (which camera, which tool,
       which label, which window): call the tool now, then ask the LLM
       only to SAY the answer (a ~200-token compose prompt, no tools)
    1  lexical hint — the utterance resembles one tool's phrasings but a
       slot is missing or ambiguous: the full prompt goes out UNCHANGED
       (so Ollama's prefix cache holds) with one short line in the user
       turn naming the likely tool
    2  the full LLM turn, exactly as before

Tier 0 fires only when every slot resolves and the question is one
question. Anything with a side effect — arm an alarm, start a monitor,
schedule a report, stop something — never routes: those deserve the
model's confirmation behaviour. Anything about WHO (a name, "who came")
goes to the model too, which decides whether to pay for face matching.
Every routed turn logs its tier so the hit rate is visible in the logs
and the vocabulary can grow from real misses.

Why Tier 1 is lexical and not an embedding model: the hint only steers
the LLM, which still sees the whole prompt and can ignore it. A
sentence-embedding model would add ~150 MB (onnxruntime + tokenizer) to
the agent image for that. Token overlap against per-tool exemplars is
zero dependencies, deterministic, and testable; if it ever needs to be
smarter, this module is the one place to swap it.

The question-shape helpers — what a camera question looks like, past or
present, which tool answers it, which camera it names — live HERE and
camera_agent.py imports them. Not the other way round: camera_agent.py
is ``__main__`` in the container, and a module that imports it executes
the whole file a second time, on the first routed turn.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from tools import _DETERMINERS, _NOT_NAMES

logger = logging.getLogger("camera-agent.router")


# ── the shape of a camera question (shared with forced grounding) ─────

# Words that mean "this is a question about a camera / the scene". If the
# model answers an utterance containing any of these WITHOUT calling a tool,
# we force a grounding detection (see _run_conversation_turn). Positive
# matching (vs a chit-chat blocklist) avoids force-grounding closings like
# "thanks, that's all" while still catching "is anyone there?".
_CAMERA_WORDS: tuple[str, ...] = (
    "see", "look", "watch", "watching", "camera", "cam",
    "anyone", "anybody", "someone", "somebody", "nobody",
    "person", "people", "man", "woman", "kid", "child", "face",
    "door", "porch", "outside", "yard", "driveway", "garage", "street",
    # scene locations
    "gate", "window", "fence", "entrance", "hallway", "room", "kitchen",
    "lot", "lobby", "stairs", "balcony",
    "happening", "happened", "happen", "detect", "count", "package", "parcel", "delivery",
    "visitor", "visitors", "visit", "visited",
    "dog", "dogs", "cat", "cats", "animal", "car", "cars", "truck", "trucks",
    "vehicle", "bike", "people", "persons",
    # visual-attribute verbs ("what is he wearing/doing?") — these are why a
    # caption/VQA question still triggers grounding instead of a fabrication
    "wearing", "wear", "dressed", "holding", "carrying", "doing",
    "visible", "present", "moving", "movement", "motion",
)
_CAMERA_RE = re.compile(r"\b(" + "|".join(_CAMERA_WORDS) + r")\b", re.IGNORECASE)


def _looks_like_camera_question(text: str) -> bool:
    """True if the utterance is about a camera / the scene. Used only to
    decide whether to force a grounding detection when the model failed to
    call a tool itself — so a weak model can't fabricate "I see a dog"."""
    return bool(_CAMERA_RE.search(text or ""))


# Greetings, thanks, closings and "what are you" — an utterance made ONLY of
# these is small talk, whatever the model replies. Anchored: one real word
# outside the list ("thanks, and is anyone at the door?") makes it not small
# talk.
_SMALL_TALK_RE = re.compile(
    r"^(?:\s*(?:hi|hello|hey|yo|good (?:morning|afternoon|evening|night)"
    r"|thanks|thank you|thx|cheers|ok|okay|great|cool|nice|perfect|got it"
    r"|bye|goodbye|good ?bye|later|that'?s all|that is all|nothing else"
    r"|no thanks|no thank you|all good|who are you|what are you"
    r"|what can you do|what do you do|how are you(?: doing| today)?|how'?s it going"
    r"|how do you do|help|do you hear me|can you hear me|are you there|testing"
    r"|very much|a lot|so much|for now|again|there|then|and|so"
    # spoken fillers and acknowledgements (Ventuno Q session, 2026-11-09:
    # "Yeah, I'm good. Yeah. Um okay. Okay. Thank you. Bye." reached the
    # model with tools and came back as a description of the room)
    r"|um|uh|hmm|mm|yeah|yep|yes|sure|right|alright|fine|good|actually|no|nope"
    r"|i'?m (?:good|fine|okay|ok|great))"
    r"\s*[,.!?]*)+\s*$",
    re.IGNORECASE,
)

#: The user is done: a fixed goodbye, no model (the model answered "I don't
#: want to talk more" with "Understood. I will stop monitoring now" — an
#: invented action — in the office session of 2026-11-08).
_CLOSING_RE = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|thanks|thank you|alright)[,.!]?\s*)*"
    r"(?:that'?s\s+enough|enough(?:\s+for\s+now)?|stop\s+talking|leave\s+it|never\s*mind"
    r"|forget\s+it|we'?re\s+done|i'?m\s+done|that\s+will\s+be\s+all|that\s+is\s+enough"
    r"|good\s*bye|bye(?:\s*bye)?|see\s+you(?:\s+later|\s+tomorrow)?|talk\s+(?:to\s+you\s+)?later|good\s*night)"
    r"\s*[,.!?]*\s*$", re.IGNORECASE)
#: Anywhere in the utterance (spoken turns stutter and tack on thanks: "I I
#: don't want to talk more", "thank you, end the conversation"). The office
#: session of 2026-11-08: "thank you end the conversation" reached the model,
#: which called stop_alarm and said "I have stopped any currently ringing
#: alarms. Goodbye."
_CLOSING_PHRASE_RE = re.compile(
    r"\b(?:don'?t|do not|didn'?t)\s+want\s+to\s+(?:talk|chat|speak)(?:\s+(?:any\s*)?more|\s+anymore)?\b"
    r"|\b(?:end|stop|finish|close|terminate)\s+(?:the\s+|this\s+|our\s+)?(?:conversation|call|chat|session|talk)\b"
    r"|\bgood\s*bye\b|\bbye\s*bye\b",
    re.IGNORECASE)

#: The user doubts the last look ("there should be more than that", "are
#: you sure", "look again", "there are three chairs") — the answer is a
#: fresh look, not a model that agrees with the user against its own tool.
_DISPUTE_RE = re.compile(
    r"^\s*(?:no\b|nope\b|wrong\b|that'?s\s+(?:not\s+right|wrong|incorrect)|are\s+you\s+sure"
    r"|(?:please\s+)?(?:look|check|count|try)\s+again|recount|there\s+(?:should|must)\s+be"
    r"|(?:there\s+(?:are|is)\s+)?(?:more|less|fewer)\s+than\s+that|i\s+(?:can\s+)?see\s+(?:\w+\s+)?(?:more|three|four|five|two)"
    r"|(?:and\s+)?why\s+(?:(?:did|do|are|were|have)\s+you|you\s+(?:were|have|had)?\s*)\s*(?:say|said|think|saying|thought)"
    r"|there\s+(?:are|is)\s+(?:\w+)\s+\w+s?\s*[.!?]*\s*$)",
    re.IGNORECASE)


#: The last sentence of a spoken turn is a goodbye: "yeah, I'm good. okay.
#: thank you. bye" — Smart Turn joins several short utterances into one
#: turn, and only the tail says what the user wants (Ventuno Q, 2026-11-09:
#: that turn reached the model with tools and came back as "I see an empty
#: room with a wooden desk").
_CLOSING_TAIL_RE = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|alright|yeah|well|and|that'?s\s+it|that'?s\s+all)[,\s]+)*(?:thanks?(?:\s+you)?[,\s]+)?"
    r"(?:bye(?:\s*bye)?|goodbye|good\s?bye|see\s+you|good\s+night|talk\s+(?:to\s+you\s+)?later"
    r"|catch\s+you\s+later|ciao)\s*[,.!]*\s*$", re.IGNORECASE)


#: A goodbye word anywhere in an utterance that is NOTHING but small talk
#: ("Bye. Thank you.", "ok bye, thanks a lot"): the order of the pleasantries
#: does not change what was meant. "Bye. Thank you." reached the model as
#: small talk, got "You're welcome. Goodbye." back, and the session stayed
#: open (2026-11-09).
_GOODBYE_WORD_RE = re.compile(r"\b(?:bye|goodbye|good\s?bye|see\s+you|good\s+night|ciao)\b", re.IGNORECASE)


def is_closing(text: str) -> bool:
    t = text or ""
    if _CLOSING_RE.match(t) or (len(t.split()) <= 12 and _CLOSING_PHRASE_RE.search(t)):
        return True
    if _is_small_talk(t) and _GOODBYE_WORD_RE.search(t):
        return True
    if len(t.split()) <= 20:
        parts = [p.strip() for p in re.split(r"[.!?]+", t) if p.strip()]
        return bool(parts) and bool(_CLOSING_TAIL_RE.match(parts[-1]))
    return False


def is_dispute(text: str) -> bool:
    return bool(_DISPUTE_RE.match(text or ""))


# The model's reply claims to have LOOKED ("I see the front door. It's dark
# outside", "there's a person at the gate") — as opposed to offering to look
# ("I can watch the cameras for you"). "I see..." / "I see," is the filler
# acknowledgement, not a sighting.
_SIGHTING_RE = re.compile(
    r"\b(?:i (?:can )?see\b(?!\s*(?:\.|,|!|…|that\b|what\b|why\b|how\b|you\b|your\b))"
    r"|(?:camera|cam)\s*\w*\s+(?:shows|is showing)|i notice|i spot|i observe|i'?m seeing"
    r"|there (?:is|are)|there'?s|is visible|are visible|no ?one is|nobody is"
    r"|it'?s (?:currently |now )?(?:dark|light|bright|empty|quiet|clear|raining))\b",
    re.IGNORECASE,
)


def _is_small_talk(text: str) -> bool:
    """True for an utterance that is only greeting / thanks / closing /
    "what can you do" — nothing a camera could answer."""
    return bool(_SMALL_TALK_RE.match((text or "").strip()))


def _should_force_grounding(user_text: str, reply: str) -> bool:
    """Should the anti-fabrication guard force a look, given a turn where the
    model answered without calling a tool?

    The question names the camera or the scene → yes. A standing request
    (an alarm, a watch, a report, "remove it") → never: its answer is a
    tool call or a question back, and a look there turned "remove the
    alarms" into "I see two chairs" (Ventuno Q, 2026-11-09). Otherwise
    the REPLY decides, and only when it claims a sighting: that catches a
    question STT garbled ("what's on hammer 2") answered as "camera 2
    shows a person", while a reply that merely mentions cameras — "I can
    only access the camera feeds", "happy to help with your camera
    system" — is an offer or a refusal, not a fabrication. The old rule
    forced a look on any camera word in the reply, so a greeting and a
    weather question both came back as a description of the office.
    """
    if _SIDE_EFFECT_RE.search(user_text or ""):
        return False
    if _looks_like_camera_question(user_text):
        return True
    if not _looks_like_camera_question(reply):
        return False
    return bool(_SIGHTING_RE.search(reply or ""))


# "more than 3 people", "over five cars", "fewer than 2 guards" — the count a
# watch should alert on. Small models call create_monitor for these but drop
# max_count (and then reply "watching for more than 3 people" over a watch
# that will never alert), so the handler reads the number from the question.
# Not a count when a time unit follows: "count cars over 10 minutes".
_COUNT_NUM = (r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten)"
              r"(?!\s*(?:s\b|secs?\b|seconds?\b|mins?\b|minutes?\b|hrs?\b|hours?\b"
              r"|days?\b|am\b|pm\b|o'?clock\b|:|%))")
_MAX_COUNT_RE = re.compile(
    r"\b(?:more than|over|above|greater than|exceeds?|exceeding)\s+" + _COUNT_NUM + r"\b",
    re.IGNORECASE)
_MIN_COUNT_RE = re.compile(
    r"\b(?:fewer than|less than|under|below)\s+" + _COUNT_NUM + r"\b", re.IGNORECASE)


def _count_thresholds_from_text(text: str) -> dict[str, int]:
    """``{"max_count": 3}`` for "tell me when more than 3 people gather";
    ``{}`` when the question names no count."""
    words = {**_NUMBER_WORDS, "ten": "10"}
    out: dict[str, int] = {}
    for key, rx in (("max_count", _MAX_COUNT_RE), ("min_count", _MIN_COUNT_RE)):
        m = rx.search(text or "")
        if m:
            n = m.group(1).lower()
            out[key] = int(words.get(n, n))
    return out


# Presence/count questions about concrete objects ("is anyone there?",
# "how many cars?", "any people?") must be answered by the object DETECTOR
# (yolov8), NOT a scene caption — BLIP describes the scene ("a table with a
# laptop") but can't reliably answer "is there a person?". Open "what's
# there / describe it" questions, by contrast, are best served by the BLIP
# caption. _pick_forced_tool routes the forced-grounding call accordingly.
_DETECTION_WORDS: tuple[str, ...] = (
    "person", "people", "anyone", "anybody", "someone", "somebody",
    "nobody", "man", "woman", "kid", "child", "face", "count", "many",
    "car", "cars", "truck", "vehicle", "bike", "bicycle", "motorcycle",
    "dog", "cat", "animal", "package", "parcel", "delivery",
    "visitor", "visitors",
)
_DETECTION_RE = re.compile(r"\b(" + "|".join(_DETECTION_WORDS) + r")\b", re.IGNORECASE)

# Attribute / activity / appearance questions ("what is he WEARING?", "what's
# he DOING?", "DESCRIBE the scene", "what's HAPPENING?") want a scene
# description (BLIP caption, or a VQA model), NOT the object detector — even
# though they usually also contain an object noun like "man" that would
# otherwise match _DETECTION_RE. These take precedence (test-report S-4/L-3/V-3).
_DESCRIBE_WORDS: tuple[str, ...] = (
    "describe", "description", "detail", "details", "wearing", "wear", "dressed",
    "doing", "holding", "carrying", "looks like", "look like", "looking",
    "appearance", "happening", "going on", "scene", "activity", "colour", "color",
)
_DESCRIBE_RE = re.compile(r"\b(" + "|".join(_DESCRIBE_WORDS) + r")\b", re.IGNORECASE)

# Presence / count phrasing ("how many…", "are there any…", "is there a…",
# "count…") → the detector, even when the object noun is a plural the detection
# vocab doesn't list ("dogs"), or absent entirely ("are there any?").
_COUNT_RE = re.compile(
    r"\b(how many|how much|are there|is there|number of|count|anyone|anybody|"
    r"\bany\b)\b", re.IGNORECASE)


def _pick_forced_tool(text: str) -> str:
    """Choose the forced-grounding tool by question type. Description/attribute/
    activity questions → ``describe_camera`` (BLIP caption / VQA, which falls
    back to the detector if no caption adapter is registered). Object presence/
    count questions → ``detect_objects`` (yolov8). Describe takes precedence so
    'what is the man wearing?' isn't routed to the detector just because it
    contains 'man'."""
    t = text or ""
    if _DESCRIBE_RE.search(t):
        return "describe_camera"
    if _COUNT_RE.search(t) or _DETECTION_RE.search(t):
        return "detect_objects"
    return "describe_camera"


# Past-tense / history phrasing. These questions are about what HAPPENED, not
# what the current frame shows — so forced grounding must route them to the
# HISTORY tools, never the live detector. The field bug this fixes: "did you
# see a person today?" contains "person", _pick_forced_tool sent it to
# detect_objects, and the user was told about a potted plant currently in view.
_PAST_RE = re.compile(
    r"\b(did|didn't|was|were|has|have|had)\b.{0,60}?"
    r"\b(come|came|been|seen|see|visit|visited|enter|entered|arrive|arrived|"
    r"pass|passed|show(?:ed)?\s+up|stop(?:ped)?\s+by|there)\b"
    r"|\b(earlier|yesterday|last\s+night|this\s+morning|this\s+afternoon|"
    r"this\s+evening|today|tonight|ago|so\s+far)\b"
    r"|\b(last|past)\s+\d+\s*(minutes?|mins?|hours?|hrs?)\b"
    r"|\b(in|over|during)\s+the\s+(last|past)\b"
    r"|\b(recording|recordings|footage|history)\b",
    re.IGNORECASE,
)

# "last/past 30 minutes", "last 2 hours" → a relative look-back in seconds.
_QTY_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
              "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "fifteen": 15,
              "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
              "half an": 0, "half a": 0, "couple of": 2, "few": 5}
_QTY = r"(\d+|" + "|".join(sorted(_QTY_WORDS, key=len, reverse=True)) + r")"
_WINDOW_RE = re.compile(
    r"\b(?:last|past)\s+" + _QTY + r"\s*(minutes?|mins?|min|hours?|hrs?|hr|hour)\b"
    r"|\b" + _QTY + r"\s*(minutes?|mins?|min|hours?|hrs?|hr|hour)\s+ago\b",
    re.IGNORECASE,
)


def _qty(raw: str) -> float:
    """"ten" → 10, "half an" → 0.5, "3" → 3 (speech to text writes both)."""
    r = (raw or "").lower().strip()
    if r.isdigit():
        return int(r)
    if r.startswith("half"):
        return 0.5
    return _QTY_WORDS.get(r, 1)

#: "What happened", "anything", "any events": a look back at everything,
#: not at one kind of thing.
_GENERIC_PAST_RE = re.compile(
    r"\b(what\s+happened|what\s+did\s+you\s+see|anything|any\s+(events?|activity|movement|motion)|"
    r"what\s+went\s+on|any\s+alerts?)\b", re.IGNORECASE)

# Detection noun → the events-store label search_history stores visits under.
# Person-ish nouns collapse to "person"; unknown nouns fall back to "person"
# (the store's own default) rather than guessing a label it never indexes.
_HISTORY_LABELS: dict[str, str] = {
    "visitor": "person", "visitors": "person",
    "car": "car", "cars": "car", "vehicle": "car", "truck": "truck",
    "bike": "bicycle", "bicycle": "bicycle", "motorcycle": "motorcycle",
    "dog": "dog", "cat": "cat",
    # Speech-to-text hears "car" as "card" often enough that "did you see
    # any blue card" is a real utterance; nobody asks the history for
    # playing cards.
    "card": "car", "cards": "car",
}

# What a visit was DESCRIBED as — the claim vocabulary the platform's
# descriptor enricher writes (colour, vehicle type, what someone carries;
# server/services/descriptor_enrichment.py KIND_QUESTIONS) plus the
# clothing colour of a person. A word here in a past-tense question is
# passed to search_history as ``attr``, so "did you see a BLUE car" asks
# the store for blue cars — it used to ask for every car and the answer
# listed twenty-five of them, colour unmentioned.
_ATTR_WORDS: frozenset[str] = frozenset({
    # colours (vehicle colour / clothing colour — the store resolves which)
    "white", "black", "silver", "grey", "gray", "red", "blue", "green",
    "yellow", "orange", "brown", "beige", "gold", "maroon", "purple", "pink",
    # vehicle types that are not Tier-0 labels
    "van", "suv", "pickup", "taxi", "lorry", "tractor", "ambulance",
    "hatchback", "sedan", "minivan", "scooter",
    # what a person carries
    "backpack", "rucksack", "bag", "handbag", "suitcase", "luggage",
    "box", "parcel", "package", "umbrella", "trolley", "basket",
})
_ATTR_ALIASES: dict[str, str] = {"gray": "grey", "lorry": "truck", "rucksack": "backpack",
                                 "handbag": "bag", "luggage": "suitcase", "package": "parcel"}


def _attr_words(text: str) -> list[str]:
    """The description words in an utterance, in order, canonical, unique.
    A colour or a type is kept even when it is also the object's label
    word ("truck" is a label; "lorry" is a claim)."""
    out: list[str] = []
    for w in re.findall(r"[a-z]+", (text or "").lower()):
        if w in _ATTR_WORDS:
            w = _ATTR_ALIASES.get(w, w)
            if w not in out:
                out.append(w)
    return out


def _is_past_question(text: str) -> bool:
    """True when the utterance asks about history, not the current scene.

    An absolute clock range ("from 2pm to 3pm") counts as history even
    without past-tense wording — background-task queries are phrased that
    way ("check the red truck on all cameras from 2pm to 3pm")."""
    t = text or ""
    return bool(_PAST_RE.search(t) or _CLOCK_RANGE_RE.search(t))


# Absolute clock ranges: "from 2pm to 3pm", "between 1 and 2pm",
# "13:00-14:00", "2 pm till 3 pm". The first time may omit its am/pm and
# inherit it from the second ("between 1 and 2pm" → 13:00-14:00).
_CLOCK_RANGE_RE = re.compile(
    r"\b(?:from|between)?\s*"
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*"
    r"(?:to|till|until|and|[-–])\s*"
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
    re.IGNORECASE,
)


def _window_from_text(text: str, now=None
                      ) -> tuple[str | None, str | None, int]:
    """Best-effort time window from the utterance.

    Returns ``(start_iso, end_iso, window_seconds)`` — ISO times (with tz
    offset, as search_history requires) or None for an open bound, plus the
    equivalent relative seconds for recent_events. All computed from the
    LOCAL clock (the container's TZ), matching the clock line in the system
    prompt."""
    from datetime import datetime, timedelta

    t = text or ""
    now = now or datetime.now().astimezone()

    def _hhmm(h, mnt, ampm):
        h = int(h) % 24
        if ampm:
            h = h % 12 + (12 if ampm.lower() == "pm" else 0)
        return now.replace(hour=h, minute=int(mnt or 0), second=0,
                           microsecond=0)

    # "from 2pm to 3pm" — an absolute range TODAY. The field bug this
    # closes: these were the exact phrasings background tasks are given
    # ("check the red truck on all cameras from 2pm to 3pm"), and the
    # forced-grounding fallback used to drop the window entirely.
    m = _CLOCK_RANGE_RE.search(t)
    if m:
        h1, m1, ap1, h2, m2, ap2 = m.groups()
        start = _hhmm(h1, m1, ap1 or ap2)   # "between 1 and 2pm" → both pm
        end = _hhmm(h2, m2, ap2)
        if end <= start:                     # "11pm to 1am" → wraps midnight
            end += timedelta(days=1)
        return (start.isoformat(timespec="seconds"),
                end.isoformat(timespec="seconds"),
                max(60, int((now - start).total_seconds())))
    m = _WINDOW_RE.search(t)
    if m:
        qty = _qty(m.group(1) or m.group(3))
        unit = (m.group(2) or m.group(4) or "").lower()
        secs = max(60, int(qty * (3600 if unit.startswith(("hour", "hr")) else 60)))
        return ((now - timedelta(seconds=secs)).isoformat(timespec="seconds"),
                None, secs)
    lowered = t.lower()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if "yesterday" in lowered or "last night" in lowered:
        start = midnight - timedelta(days=1)
        return (start.isoformat(timespec="seconds"),
                midnight.isoformat(timespec="seconds"),
                int((now - start).total_seconds()))
    if "today" in lowered or "this morning" in lowered or "tonight" in lowered \
            or "this afternoon" in lowered or "this evening" in lowered:
        return (midnight.isoformat(timespec="seconds"), None,
                int((now - midnight).total_seconds()))
    # No parseable window: open start for search_history; recent_events gets
    # a generous hour so "did anyone come?" still looks back meaningfully.
    return None, None, 3600


def _pick_forced_call(
    text: str, cam: str, advertised: set[str], now=None
) -> tuple[str, dict[str, Any]]:
    """Choose the forced-grounding (tool, arguments) — history-aware.

    Past-tense questions go to search_history (durable events store) when it
    is advertised, else recent_events (in-memory ring) — never to a live
    detector, which can only describe the CURRENT frame. Present-tense
    questions keep the _pick_forced_tool routing. Only advertised tools are
    ever picked, so forced grounding can't call something the operator's
    enabled_tools hides from the model."""
    if _is_past_question(text):
        start_iso, end_iso, window_secs = _window_from_text(text, now=now)
        if "search_history" in advertised:
            args: dict[str, Any] = {"camera_id": cam}
            m = _DETECTION_RE.search(text or "")
            noun = (m.group(0).lower() if m else "")
            if not noun:
                # No detection noun — maybe a label word the detection
                # vocabulary does not carry (a speech-to-text "card").
                noun = next((w for w in re.findall(r"[a-z]+", (text or "").lower())
                             if w in _HISTORY_LABELS), "")
            args["label"] = _HISTORY_LABELS.get(noun, "person")
            # The description survives: "blue car" is not "car".
            attrs = [a for a in _attr_words(text) if a != args["label"]]
            if attrs:
                args["attr"] = attrs
            if start_iso:
                args["start_time"] = start_iso
            if end_iso:
                args["end_time"] = end_iso
            return "search_history", args
        if "recent_events" in advertised:
            return "recent_events", {
                "camera_id": cam, "window_seconds": window_secs,
            }
        # No history tool advertised: fall through to the live routing —
        # a wrong-tense answer beats no grounding at all, and the reply
        # honestly describes what the tool actually looked at.
    name = _pick_forced_tool(text)
    if name not in advertised and "describe_camera" in advertised:
        name = "describe_camera"
    return name, {"camera_id": cam}


# Questions about the camera ROSTER / system config ("how many cameras are
# configured?", "which cameras do you have?", "list the cameras") are about
# the SYSTEM, not what's visible — the model answers them correctly from its
# prompt context, so forced grounding must NOT override them with an
# (irrelevant) scene detection. Distinguished from scene questions by
# "camera/cam" being the noun being counted/listed, not an object inside a
# camera's view ("how many PEOPLE on the camera" stays a scene question).
_CONFIG_RE = re.compile(
    r"\b(how many|number of|which|what|list(?:\s+\w+){0,3})\s+(cameras?|cams?)\b"
    r"|\b(cameras?|cams?)\s+(are|do you|configured|connected|available|set up|online|exist)\b",
    re.IGNORECASE,
)


def _is_config_question(text: str) -> bool:
    """True for questions about the camera roster/config (how many/which
    cameras exist) rather than what's visible in one. Forced grounding skips
    these so a correct context answer isn't clobbered by a scene detection."""
    return bool(_CONFIG_RE.search(text or ""))


def _pick_camera(text: str, cameras: list[str], preferred: str | None = None) -> str:
    """Which camera did the user mean — for forced grounding, which must
    always answer: the camera the utterance NAMES (``_names_camera``),
    else the UI-selected ``preferred`` camera, else the only camera there
    is, else ALL of them (the first configured camera used to stand in for
    the fleet, and answered for the wrong door). A bare digit is never a
    camera: "in the last 2 hours" is not cam2."""
    named = _names_camera(text, cameras)
    if named is not None:
        return named
    if preferred and preferred in cameras:
        return preferred
    if len(cameras) == 1:
        return cameras[0]
    return "all"


def which_camera_question(cameras: list[str], roles: dict[str, str] | None) -> str:
    """The question the agent asks when no camera was named and looking at
    every one would be too dear: the cameras by their locations."""
    names = [(roles or {}).get(c) or c for c in cameras]
    names = [n for n in names if n]
    if len(names) <= 1:
        return "Which camera do you mean?"
    listed = ", ".join(names[:-1]) + f" or {names[-1]}"
    return f"Which camera do you mean: {listed}?"




#: Requests with a side effect or a standing shape. Never Tier 0: the
#: model's confirmation ("I'll watch the driveway and tell you…") is part
#: of the product for these.
_SIDE_EFFECT_RE = re.compile(
    r"\b(notify|alert|alarm|siren|watch|keep\s+(an\s+)?eye|monitor|remind|wake\s+me|"
    r"report|summar(y|ize|ise)|every\s+(morning|evening|hour|day|\d+)|"
    r"stop\b(?!\s+by)|cancel|disarm|arm|turn\s+(on|off)|mute|silence|schedule|"
    r"remove|delete|get\s+rid|don'?t\s+need|no\s+longer|notifications?|notified|"
    r"from\s+now\s+on|whenever|if\s+(you\s+)?(see|spot)|let\s+me\s+know|"
    r"tell\s+me\s+(when|if))\b",
    re.IGNORECASE,
)

#: WHO questions — the word, or a proper name. The model decides whether
#: to pay for face matching, and how to phrase a name it does not know.
_WHO_RE = re.compile(r"\b(who|whom|whose|recogni[sz]e|name)\b", re.IGNORECASE)
#: A capitalised word that is not the first word: "was Priya here".
#: Transcripts arrive lowercase except for sentence starts and names —
#: and, from some STT, "Camera"; those, weekdays, months, the roster's
#: words and a noun after "a"/"the" are not names (_looks_like_name;
#: the word lists are tools.py's, shared with search_history).
_NAME_RE = re.compile(r"(?<=[a-z0-9,] )[A-Z][a-z]{2,}\b")

#: Questions that belong to OTHER tools the picker below does not know
#: (recent_plates, camera_snapshot, search_footage, the apps): never
#: Tier 0 — the picker's catch-all would answer "any plates today" with
#: people, and a snapshot request with a caption.
_OTHER_TOOL_RE = re.compile(
    r"\b(plates?|number\s*plates?|licen[cs]e|registration|snapshot|screenshot|"
    r"photo|picture|image|clip|apps?)\b", re.IGNORECASE)

#: A live question the picker CAN answer: it sees, counts, or describes.
_LIVE_ASK_RE = re.compile(
    r"\b(see|look|looking|view|show|clear|empty|happening|going\s+on|anything|"
    r"anyone|anybody|someone|somebody|nobody|how\s+many|any|there)\b", re.IGNORECASE)
#: "What do you see" and its kin: a description with no specific question
#: in it, for which describe_camera's own default caption is the answer.
_GENERIC_SEE_RE = re.compile(
    r"^\s*(what|whats|what's)\s+(do\s+you\s+see|can\s+you\s+see|is\s+(happening|going\s+on)|"
    r"does\s+it\s+look\s+like)\b", re.IGNORECASE)

#: "all cameras" / "every camera" / "any camera".
_ALL_RE = re.compile(r"\b(all|every|each|any)\s+(of\s+the\s+)?(cameras?|cams?|feeds?)\b|"
                     r"\b(everywhere|anywhere)\b", re.IGNORECASE)

#: Above this the utterance is probably two questions, or a story.
_MAX_WORDS = 20


#: A description of every camera is a caption per camera (1.7 s each on an
#: NPU, far more on a CPU): up to this many cameras the router looks at
#: all of them when none is named; above it, it asks which one.
DESCRIBE_ALL_MAX = 4


@dataclass
class Decision:
    tier: int
    tool: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    hint: str | None = None
    reason: str = ""
    #: A reply the agent gives as is, with no model and no tool: the router
    #: asking which camera, when the question names none and looking at
    #: every one would be too dear.
    ask: str | None = None
    #: False when Tier 0 declined for a reason a Tier-1 hint would only
    #: make worse — a standing request, a WHO question, another tool's
    #: question: the model must decide those, unsteered.
    hintable: bool = True

    @property
    def routed(self) -> bool:
        return self.tier == 0


# ── Tier 0 ────────────────────────────────────────────────────────

_NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
                 "six": "6", "seven": "7", "eight": "8", "nine": "9"}
_ORDINAL_WORDS = {"first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5"}


def _id_pattern(cam: str) -> str:
    """``front_door`` matches "front door" and "frontdoor"; ``cam1``
    matches "cam1" and "cam 1" — as words, never inside one ("garage"
    is not in "garages", and "e" before it is not a boundary)."""
    parts = re.findall(r"[a-z]+|[0-9]+", cam.lower())
    return r"(?<![a-z0-9])" + r"\s*".join(map(re.escape, parts)) + r"(?![a-z0-9])"


def _names_camera(text: str, cameras: list[str],
                  roles: dict[str, str] | None = None) -> str | None:
    """The camera the utterance NAMES, or None: the id itself as a word
    ("the garage", "front door" for ``front_door``, "cam 1"), "camera N" /
    "camera two" / "the second camera", or the camera's role from the
    roster ("the front door"). A bare digit is never a camera — "in the
    last 2 hours" is not cam2. Among roles the LONGEST match wins, so
    "back door" is not claimed by a camera whose role is "door"."""
    t = re.sub(r"[-_]", " ", (text or "").lower())
    for cam in cameras:
        if re.search(_id_pattern(cam), t):
            return cam
    m = (re.search(r"\b(?:camera|cam)\s*(\d+|one|two|three|four|five|six|seven|eight|nine)\b", t)
         or re.search(r"\b(first|second|third|fourth|fifth)\s+(?:camera|cam)\b", t))
    if m:
        w = m.group(1)
        n = _NUMBER_WORDS.get(w) or _ORDINAL_WORDS.get(w) or w
        for cam in cameras:
            if cam.lower() in (f"cam{n}", f"camera{n}"):
                return cam
    best: tuple[int, str] | None = None
    for cam, role in (roles or {}).items():
        r = re.sub(r"[-_]", " ", (role or "").strip().lower())
        if len(r) >= 3 and cam in cameras and re.search(rf"\b{re.escape(r)}\b", t):
            if best is None or len(r) > best[0]:
                best = (len(r), cam)
    return best[1] if best else None


def _looks_like_name(text: str, cameras: list[str], roles: dict[str, str] | None) -> bool:
    """A capitalised word mid-sentence that is not a camera word or one
    of the roster's own words — 'Priya', not 'Camera' or 'Door'."""
    roster_words = {w for cam in cameras for w in re.findall(r"[a-z]+", cam.lower())}
    for role in (roles or {}).values():
        roster_words.update(re.findall(r"[a-z]+", (role or "").lower()))
    for m in _NAME_RE.finditer(text or ""):
        w = m.group(0).lower()
        if w in _NOT_NAMES or w in roster_words:
            continue
        before = (text or "")[:m.start()].rstrip().split()
        if before and before[-1].lower().strip(",") in _DETERMINERS:
            continue                                  # "a Car", "the Door"
        return True
    return False


# ── the agent itself: what is set up, is the system okay, show a camera ──

#: The things the agent keeps standing. A question ABOUT them is a
#: listing, not a request to set one up or stop one.
_STANDING_NOUN_RE = re.compile(
    r"\b(alarms?|watch(?:es)?|monitors?|reports?|notifications?|reminders?|"
    r"standing\s+requests?)\b", re.IGNORECASE)
#: "what / which / list / do I have …" — the question is asking for a list.
_LISTING_RE = re.compile(
    r"\b(what|which|list|show|tell\s+me|do\s+i\s+have|have\s+i|are\s+there|any|"
    r"how\s+many|is\s+there|read\s+(me\s+)?(back|out))\b", re.IGNORECASE)
#: Words that make it a request to SET or STOP something instead.
_SETTING_RE = re.compile(
    r"\b(if|when|whenever|every|each|after|before|between|arm|create|add|make|"
    r"schedule|start|stop|cancel|disarm|silence|mute|remove|delete|clear|turn|"
    r"notify|alert\s+me|wake|let\s+me\s+know|tell\s+me\s+(when|if))\b", re.IGNORECASE)
#: "What have I set up", "what are you watching for": every standing thing.
_SETUP_RE = re.compile(
    r"\b(what|which)\s+(have\s+i|did\s+i|do\s+i\s+have|is|are)\s+(set\s+up|running|active|"
    r"scheduled|armed)\b|\bwhat\s+are\s+you\s+(watching|monitoring|looking)\s+(out\s+)?for\b|"
    r"\bwhat('s|\s+is)\s+set\s+up\b", re.IGNORECASE)

#: System words: the cameras' connection and recording, the disk.
_STATUS_WORD_RE = re.compile(
    r"\b(offline|online|disconnected|down|recording|recorded|disk|storage|space|"
    r"healthy|health|status|working|okay|ok|fine|alright|all\s+good|problems?|"
    r"issues?|wrong|connected|up\s+and\s+running)\b", re.IGNORECASE)
_STATUS_LEAD_RE = re.compile(
    r"^\s*(is|are|any|which|how\s+much|how\s+many|what|whats|what's|do|does|has|have|"
    r"everything|all|anything)\b", re.IGNORECASE)
#: Words that make a status-looking question about the SCENE or the PAST
#: instead ("is anyone down there", "show me the recording of the gate").
_NOT_STATUS_RE = re.compile(
    r"\b(clip|footage|playback|play|history|events?|snapshot|photo|picture|"
    r"show\s+me|yesterday|last\s+night|earlier|ago)\b", re.IGNORECASE)

#: "Stop watching for that", "cancel the alarm", "remove it", "turn it
#: off": a stop request with nothing for the model to fill. The stop tools
#: pick the item themselves (a number said, else the last one set up, else
#: the only one, else they ask), so the router runs them directly. The
#: model, hinted, still answered "stop watching for that" without a call 1
#: time in 3 (Ventuno Q eval, 2026-11-09).
_STOP_REQUEST_RE = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|actually|also|and|now|hey|so|well|no|yes|then|please)[,\s]+)*"
    r"(?:please\s+)?(?:can\s+you\s+|could\s+you\s+|would\s+you\s+|let\s+me\s+|let'?s\s+|"
    r"i(?:\s+want\s+to|\s+need\s+to|\s+would\s+like\s+to|'d\s+like\s+to|\s+will|'ll)?\s+)?"
    r"(?:stop|cancel|disarm|silence|mute|remove|delete|clear|turn\s+(?:it\s+|that\s+|them\s+|the\s+\w+\s+)?off)\b",
    re.IGNORECASE)
#: A "stop" that is about the conversation or the recording, not a
#: standing request.
_NOT_STOP_RE = re.compile(
    r"\b(talk|talking|listen|listening|conversation|speak|speaking|recording|"
    r"record|video|stream|streaming|playing|playback)\b", re.IGNORECASE)
_SILENCE_RE = re.compile(r"\b(silence|mute|quiet|hush|shut\s+up)\b", re.IGNORECASE)

_SHOW_RE = re.compile(
    r"^\s*(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?(show|open|switch\s+to|pull\s+up|"
    r"bring\s+up|go\s+to|display|put\s+up)\s+(?P<rest>.+?)\s*[.!?]*\s*$", re.IGNORECASE)
_SHOW_FILLER = {"me", "the", "my", "a", "camera", "cam", "feed", "view", "on", "screen",
                "please", "now", "up", "live", "stream", "video", "to"}


def _standing_kind(text: str) -> str:
    kinds = set()
    if re.search(r"\balarms?\b", text, re.IGNORECASE):
        kinds.add("alarm")
    if re.search(r"\b(watch(?:es)?|monitors?|notifications?|reminders?)\b", text, re.IGNORECASE):
        kinds.add("monitor")
    if re.search(r"\breports?\b", text, re.IGNORECASE):
        kinds.add("report")
    return kinds.pop() if len(kinds) == 1 else "all"


def _route_stop(t: str, advertised: set[str], last_kind: str | None) -> Decision | None:
    """Tier 0 for a bare stop request: which stop tool, from the words
    (alarm words → stop_alarm, report words → stop_report, watch words →
    stop_monitor, else the kind set up last, else stop_monitor), with the
    number said as its id and silence/mute as the alarm's action."""
    # The request is its first clause: "okay then remove it, I don't need
    # any notification when a car arrives" (13 words, hinted, the model
    # skipped the call 2 times in 3 — Ventuno Q eval run 5, 2026-11-09).
    lead = re.match(r"^\s*(?:(?:ok(?:ay)?|actually|also|and|now|hey|so|well|no|yes|then|please)[,\s]+)*", t)
    body = t[lead.end():] if lead else t
    head = re.split(r"[,.;!?]|\b(?:because|since|as)\b", body, maxsplit=1)[0]
    if not _STOP_REQUEST_RE.match(head) or _NOT_STOP_RE.search(t):
        return None
    if len(re.findall(r"[A-Za-z']+", head)) > 8 or _is_past_question(head):
        return None
    alarm = bool(_ALARM_WORDS_RE.search(t))
    monitor = bool(_MONITOR_WORDS_RE.search(t)) or bool(re.search(r"\bwatching\b", t, re.IGNORECASE))
    report = bool(_REPORT_WORDS_RE.search(t))
    if alarm:
        tool, key = "stop_alarm", "alarm_id"
    elif report and not monitor:
        tool, key = "stop_report", "report_id"
    elif monitor:
        tool, key = "stop_monitor", "monitor_id"
    else:
        tool = {"alarm": "stop_alarm", "report": "stop_report"}.get(last_kind or "", "stop_monitor")
        key = {"stop_alarm": "alarm_id", "stop_report": "report_id"}.get(tool, "monitor_id")
    if tool not in advertised:
        return None
    args: dict[str, Any] = {}
    m = re.search(r"(?:#|\bnumber\s+)?\b(\d{1,3})\b", t)
    if m:
        args[key] = int(m.group(1))
    if tool == "stop_alarm":
        args["action"] = "silence" if _SILENCE_RE.search(t) else "disarm"
    return Decision(0, tool=tool, args=args, reason=f"stop request: {tool}", hintable=False)


def _route_agent_itself(t: str, cameras: list[str], roles: dict[str, str] | None,
                        advertised: set[str], preferred: str | None,
                        last_kind: str | None = None) -> Decision | None:
    """Tier 0 for questions about the agent and the system rather than
    the scene: "show me the gate" (the page's view), "what alarms do I
    have" (the standing lists), "is everything okay" / "is the gate
    recording" / "how much disk is left" (core's status). Each only
    when its tool is advertised; otherwise None and the picker goes on
    as before."""
    # "Show me the gate": the verb, then nothing but a camera's name.
    m = _SHOW_RE.match(t)
    if m and "show_camera" in advertised:
        cam = _names_camera(t, cameras, roles)
        if cam is not None:
            rest = re.sub(r"[-_]", " ", m.group("rest").lower())
            refs = [re.sub(r"[-_]", " ", cam.lower())]
            role = (roles or {}).get(cam) or ""
            if role:
                refs.append(re.sub(r"^(the|a|an)\s+", "", role.strip().lower()))
            refs.sort(key=len, reverse=True)
            for r in refs:
                rest = re.sub(rf"\b{re.escape(r)}\b", " ", rest)
            rest = re.sub(r"\b(?:camera|cam)\s*\d+\b|\b(?:first|second|third|fourth|fifth)\b", " ", rest)
            left = [w for w in re.findall(r"[a-z0-9']+", rest) if w not in _SHOW_FILLER]
            if not left:
                return Decision(0, tool="show_camera", args={"camera_id": cam},
                                reason="show a camera", hintable=False)
    # "Stop watching for that", "remove it", "cancel alarm 2".
    d = _route_stop(t, advertised, last_kind)
    if d is not None:
        return d
    # "What alarms do I have": a listing of the standing things.
    if "list_standing" in advertised and not _SETTING_RE.search(t):
        if _SETUP_RE.search(t):
            return Decision(0, tool="list_standing", args={"kind": "all"},
                            reason="what is set up", hintable=False)
        if _STANDING_NOUN_RE.search(t) and _LISTING_RE.search(t):
            return Decision(0, tool="list_standing", args={"kind": _standing_kind(t)},
                            reason="lists standing requests", hintable=False)
    # "Is everything okay", "is the gate recording", "how much disk is left".
    if ("system_status" in advertised and _STATUS_LEAD_RE.match(t) and _STATUS_WORD_RE.search(t)
            and not _NOT_STATUS_RE.search(t) and not _DETECTION_RE.search(t)
            and not _SETTING_RE.search(t) and not _STANDING_NOUN_RE.search(t)):
        args: dict[str, Any] = {}
        cam = _names_camera(t, cameras, roles)
        if cam is not None:
            args["camera_id"] = cam
        return Decision(0, tool="system_status", args=args, reason="system status", hintable=False)
    return None


def decide_tier0(text: str, *, cameras: list[str], advertised: set[str],
                 last_kind: str | None = None,
                 preferred: str | None = None, roles: dict[str, str] | None = None,
                 now=None) -> Decision | None:
    """A tool call the agent can make without asking the model, or None."""
    t = (text or "").strip()
    words = re.findall(r"[A-Za-z']+", t)
    if not t:
        return None
    # Small talk is a decision, not a question: a greeting, thanks, a
    # closing or "what can you do" reaches the model with NO tools.
    # Measured (MODELS_AND_LATENCY, "Measured"): told "always call a tool
    # before describing", granite4:micro answered "thanks, that's all" with
    # a three-tool sweep and "I can't see right now"; the guard only stops
    # a fabricated sighting, not a look nobody asked for. Decided here,
    # whatever the model, and before the camera check: a goodbye needs
    # no camera.
    if _is_small_talk(t):
        return Decision(0, reason="small talk", hintable=False)
    if not cameras:
        return None
    if len(words) > _MAX_WORDS or t.count("?") > 1:
        return Decision(2, reason="too long or two questions", hintable=False)
    # The agent and the system before the scene: "show me the gate", "what
    # alarms do I have", "is every camera recording". Before the config
    # check because "which cameras are online" is a status question, not
    # a roster one.
    d = _route_agent_itself(t, cameras, roles, advertised, preferred, last_kind)
    if d is not None:
        return d
    if _is_config_question(t):
        # Answered from the roster in the prompt. hintable=False: "how many
        # cameras do you have" resembles "how many people" word for word,
        # and a detect_objects hint here sent the model to count people
        # (golden set, 2026-11-09).
        return Decision(2, reason="roster question", hintable=False)
    if _SIDE_EFFECT_RE.search(t):
        return Decision(2, reason="side effect or standing request", hintable=False)
    if _WHO_RE.search(t) or _looks_like_name(t, cameras, roles):
        return Decision(2, reason="asks who", hintable=False)
    if _OTHER_TOOL_RE.search(t):
        return Decision(2, reason="another tool's question", hintable=False)
    if not _looks_like_camera_question(t):
        return Decision(2, reason="not a camera question", hintable=False)
    past = _is_past_question(t)
    nouns = {m.lower() for m in _DETECTION_RE.findall(t)}
    if len(nouns - {"anyone", "anybody", "someone", "somebody", "nobody",
                    "people", "person", "count", "many"}) > 1:
        return Decision(2, reason="more than one thing asked about")
    # The picker below only knows how to see, count, describe and search
    # for a class of thing. A question that names none of those is for
    # the model, not for its catch-all.
    if past and not nouns:
        # "What happened between 2pm and 3pm": no thing named, an absolute
        # window — the footage narration for that window is the answer
        # (the hinted model called recent_events with a start_time it does
        # not take, 3 of 3, Ventuno Q eval 2026-11-09).
        if _CLOCK_RANGE_RE.search(t) and _GENERIC_PAST_RE.search(t) and "describe_window" in advertised:
            start_iso, end_iso, _ = _window_from_text(t, now=now)
            cam0 = _names_camera(t, cameras, roles) if not _ALL_RE.search(t) else None
            if cam0 is None and preferred and preferred in cameras:
                cam0 = preferred
            if cam0 is None and len(cameras) == 1:
                cam0 = cameras[0]
            if cam0 is not None and start_iso and end_iso:
                return Decision(0, tool="describe_window",
                                args={"camera_id": cam0, "start_time": start_iso, "end_time": end_iso},
                                reason="what happened, clock window")
        # "What happened in the last ten minutes": no thing named, so the
        # history search (one label) is the wrong tool — the ring of recent
        # events, every label, is the answer, when the window is explicit.
        if _WINDOW_RE.search(t) and _GENERIC_PAST_RE.search(t) and "recent_events" in advertised:
            _, _, window_secs = _window_from_text(t, now=now)
            cam0 = _names_camera(t, cameras, roles) if not _ALL_RE.search(t) else None
            if cam0 is None and preferred and preferred in cameras:
                cam0 = preferred
            if cam0 is None and len(cameras) == 1:
                cam0 = cameras[0]
            return Decision(0, tool="recent_events",
                            args={"camera_id": cam0 or "__any__", "window_seconds": window_secs},
                            reason="what happened, windowed")
        return Decision(2, reason="no object named")
    if not past and not (nouns or _DESCRIBE_RE.search(t) or _LIVE_ASK_RE.search(t)):
        return Decision(2, reason="no known ask")

    # Which camera. Named, or "all", or the one the UI is on, or the only
    # one there is — or, with several cameras and no name, ALL of them:
    # the first camera in the roster used to answer for the fleet, stated
    # as if it were the whole picture (measured: wrong whenever the user
    # meant another door). Detection, counting and history over every
    # camera are cheap; a description is a caption per camera, so past
    # DESCRIBE_ALL_MAX cameras the router asks which one instead.
    if _ALL_RE.search(t):
        cam = "all"
    else:
        cam = _names_camera(t, cameras, roles)
        if cam is None:
            if preferred and preferred in cameras:
                cam = preferred
            elif len(cameras) == 1:
                cam = cameras[0]
            else:
                cam = "all"
                probe, _ = _pick_forced_call(t, cam, advertised, now=now)
                if probe == "describe_camera" and len(cameras) > DESCRIBE_ALL_MAX:
                    return Decision(2, reason="camera ambiguous",
                                    ask=which_camera_question(cameras, roles))

    tool, args = _pick_forced_call(t, cam, advertised, now=now)
    if past and tool not in ("search_history", "recent_events"):
        # No history tool to send it to; a live detector cannot answer
        # "did". The model will say so in its own words.
        return Decision(2, reason="history tool not advertised")
    if tool not in advertised:
        return Decision(2, reason=f"{tool} not advertised")
    if cam == "all":
        if tool == "search_history":
            args.pop("camera_id", None)               # history over every camera
        elif tool == "recent_events":
            args["camera_id"] = "__any__"             # the only spelling it takes
    if tool == "describe_camera" and _DESCRIBE_RE.search(t) and not _GENERIC_SEE_RE.search(t):
        # "what is the person wearing": the VLM answers THAT, not a
        # generic caption the compose model then has to guess from.
        args["question"] = t
    return Decision(0, tool=tool, args=args,
                    reason=f"{'past' if past else 'live'} question, camera {cam}")


# ── Tier 1 ────────────────────────────────────────────────────────

#: Phrasings per tool, for the lexical hint. Grow this from real misses
#: in the logs ("router: tier=2 reason=..."), not from imagination.
_EXEMPLARS: dict[str, tuple[str, ...]] = {
    "describe_camera": (
        "what do you see", "what is happening", "describe the scene",
        "what is going on", "what does it look like", "is anything there",
        "what is the person wearing", "what is he doing", "look at the camera",
    ),
    "detect_objects": (
        "is anyone there", "is anybody at the door", "how many people",
        "are there any cars", "is there a person", "count the people",
        "any vehicles", "is someone outside", "is the gate clear",
    ),
    "search_history": (
        "did anyone come", "did a car come by", "who came to the door",
        "was there anyone earlier", "has anybody been here", "which cars entered",
        "did you see a truck today", "any visitors this morning",
        "what happened last night", "show me the history",
    ),
    "recent_events": (
        "what happened recently", "anything in the last few minutes",
        "any recent events", "what just happened",
    ),
}

#: Function words only. "there", "any", "show", "camera" carry meaning
#: for a tool ("is anyone there", "show me the history") and stay.
_STOP = {"the", "a", "an", "is", "are", "was", "were", "do", "does", "did",
         "you", "it", "to", "of", "on", "at", "in", "me", "my", "i",
         "can", "could", "please", "and", "or", "by", "up"}

#: Spellings that mean the same thing to a tool. Kept tiny on purpose;
#: every value must be ONE token or it can never match.
_SYNONYMS = {
    "anybody": "anyone", "somebody": "someone", "nobody": "noone",
    "cars": "car", "trucks": "truck", "vehicles": "vehicle", "people": "person",
    "persons": "person", "came": "come", "arrived": "come", "arrive": "come",
    "entered": "come", "visited": "come", "visitors": "visitor", "seen": "see",
    "saw": "see", "happening": "happen", "happened": "happen", "recently": "recent",
    "morning": "today", "tonight": "today", "afternoon": "today",
}


_CONTRACTIONS = (("n't", " not"), ("'s", " is"), ("'re", " are"), ("'ve", " have"),
                 ("'ll", " will"), ("'d", " would"), ("'m", " am"))

_LIVE_TOOLS = {"describe_camera", "detect_objects"}
_PAST_TOOLS = {"search_history", "recent_events"}


def _tokens(s: str) -> set[str]:
    s = (s or "").lower().replace("’", "'")
    for suffix, words in _CONTRACTIONS:
        s = s.replace(suffix, words)                  # "what's" folds onto "what is"
    return {_SYNONYMS.get(w, w) for w in re.findall(r"[a-z]+", s) if w not in _STOP}


def hint_tier1(text: str, *, advertised: set[str], threshold: float = 0.6,
               margin: float = 0.15) -> Decision | None:
    """The tool the utterance most resembles, when it clearly resembles
    one: the largest share of an exemplar's content words that the
    utterance contains (synonyms folded), over that tool's exemplars.
    Only tools of the question's TENSE compete: "was anyone at the door
    earlier" resembles "is anybody at the door" word for word, and a
    hint to the live detector would be the wrong-tool iteration the
    router exists to remove."""
    q = _tokens(text)
    if not q:
        return None
    tense = _PAST_TOOLS if _is_past_question(text) else _LIVE_TOOLS
    scores: list[tuple[float, str]] = []
    for tool, phrases in _EXEMPLARS.items():
        if tool not in advertised or tool not in tense:
            continue
        best = 0.0
        for ph in phrases:
            p = _tokens(ph)
            if len(p) < 2:
                continue
            best = max(best, len(q & p) / len(p))
        scores.append((best, tool))
    if not scores:
        return None
    scores.sort(reverse=True)
    top, tool = scores[0]
    runner = scores[1][0] if len(scores) > 1 else 0.0
    if top < threshold or top - runner < margin:
        return None
    return Decision(1, hint=tool, reason=f"resembles {tool} ({top:.2f})")


# ── standing requests: which verb, by the words ───────────────────

#: An alarm is asked for with alarm words. Measured on the NPU (Gemma 4
#: E2B, 2026-11-08, 32-case eval): "let me know when a car arrives" and
#: "notify me when a person shows up" went to create_alarm 6 times of 6,
#: each with an invented "after midnight" window; "notify me when more
#: than 3 people gather" went to create_monitor every time. The words
#: decide here, the model fills the slots.
_ALARM_WORDS_RE = re.compile(
    r"\b(alarm|siren|ring|rings|loud|loudly|wake\s+me|alert\s+me\s+loud)", re.IGNORECASE)
_STOP_WORDS_RE = re.compile(
    r"\b(stop|cancel|disarm|silence|mute|turn\s+(it\s+)?off|remove|delete|clear)\b", re.IGNORECASE)
_MONITOR_WORDS_RE = re.compile(
    r"\b(notify|notifications?|notified|let\s+me\s+know|tell\s+me\s+(when|if)|watch|keep\s+(an\s+)?eye|monitor|"
    r"count(ing)?|whenever|if\s+(you\s+)?(see|spot)|alert\s+me)\b", re.IGNORECASE)
_REPORT_WORDS_RE = re.compile(
    r"\b(report|summar(y|ize|ise)|digest|every\s+(morning|evening|day|hour|night)|daily|weekly)\b",
    re.IGNORECASE)


def hint_standing(text: str, *, advertised: set[str],
                  last_kind: str | None = None) -> Decision | None:
    """The tool a standing request most likely wants, from its words:
    alarm words → create_alarm, otherwise notify/watch words →
    create_monitor; a stop word turns either into its stop tool; report
    words → create_report. Only tools that are advertised. The model
    still decides and fills the arguments; this rides in the user turn
    as a Tier-1 hint. ``last_kind`` ("alarm" | "monitor" | "report") is
    what the agent set up most recently: "remove it", "cancel that" name
    no kind, and mean that one."""
    t = text or ""
    if _NOT_STOP_RE.search(t) and _STOP_WORDS_RE.search(t):
        return None                     # "stop talking": not a standing request
    alarm = bool(_ALARM_WORDS_RE.search(t))
    stop = bool(_STOP_WORDS_RE.search(t))
    monitor = bool(_MONITOR_WORDS_RE.search(t))
    report = bool(_REPORT_WORDS_RE.search(t))
    if stop:
        if alarm:
            tool = "stop_alarm"
        elif report and not monitor:
            tool = "stop_report"
        elif monitor:
            tool = "stop_monitor"
        else:
            tool = {"alarm": "stop_alarm", "report": "stop_report"}.get(last_kind or "", "stop_monitor")
    elif report and not alarm:
        tool = "create_report"
    elif alarm:
        tool = "create_alarm"
    elif monitor:
        tool = "create_monitor"
    else:
        return None
    if tool not in advertised:
        return None
    return Decision(1, hint=tool, reason=f"standing request, {'stop' if stop else 'set'}: {tool}")


# ── the clock window a request names ──────────────────────────────

_CLOCK_RE = (r"(?:(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>a\.?m\.?|p\.?m\.?)?"
             r"|(?P<word>midnight|noon|midday))")
_BETWEEN_RE = re.compile(
    r"\b(?:between|from)\s+" + _CLOCK_RE.replace("(?P<", "(?P<a_") +
    r"\s+(?:and|to|till|until|-)\s+" + _CLOCK_RE.replace("(?P<", "(?P<b_"), re.IGNORECASE)
_AFTER_RE = re.compile(r"\b(?:after|past|from|later\s+than)\s+" + _CLOCK_RE, re.IGNORECASE)
_BEFORE_RE = re.compile(r"\b(?:before|until|till|by|earlier\s+than)\s+" + _CLOCK_RE, re.IGNORECASE)


def _clock_minutes(h, m, ap, word) -> int | None:
    if word:
        return {"midnight": 0, "noon": 720, "midday": 720}[word.lower()]
    if h is None:
        return None
    hour, minute = int(h), int(m or 0)
    ap = (ap or "").replace(".", "").lower()
    if ap and not (1 <= hour <= 12):
        return None
    if not ap and (m is None) and not (0 <= hour <= 23):
        return None
    if ap == "am" and hour == 12:
        hour = 0
    elif ap == "pm" and hour < 12:
        hour += 12
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return hour * 60 + minute


def clock_window_from_text(text: str) -> tuple[int | None, int | None, bool]:
    """(after_min, before_min, mentioned) from the clock words of a request:
    "after 6pm" → (1080, None, True); "before 6am" → (None, 360, True);
    "between 10pm and 6am" → (1320, 360, True); no clock words →
    (None, None, False). Only a clock expression counts: "tonight" and
    "at night" are not windows the agent can arm. A bare hour needs its
    keyword ("after 6" is 06:00; "6" alone is nothing)."""
    t = text or ""
    m = _BETWEEN_RE.search(t)
    if m:
        a = _clock_minutes(m.group("a_h"), m.group("a_m"), m.group("a_ap"), m.group("a_word"))
        b = _clock_minutes(m.group("b_h"), m.group("b_m"), m.group("b_ap"), m.group("b_word"))
        if a is not None and b is not None:
            return (a or None), (b or None), True
    after = before = None
    m = _AFTER_RE.search(t)
    if m:
        after = _clock_minutes(m.group("h"), m.group("m"), m.group("ap"), m.group("word"))
    m = _BEFORE_RE.search(t)
    if m:
        before = _clock_minutes(m.group("h"), m.group("m"), m.group("ap"), m.group("word"))
    mentioned = after is not None or before is not None
    # Midnight as a bound is no bound: "after midnight" is any time.
    return (after or None), (before or None), mentioned


_EVERY_RE = re.compile(
    r"\bevery\s+" + _QTY + r"?\s*(minutes?|mins?|min|hours?|hrs?|hr|hour|half\s+(?:an\s+)?hour)\b"
    r"|\b(hourly|half[-\s]hourly)\b", re.IGNORECASE)
_DAILY_RE = re.compile(
    r"\b(every\s+(morning|evening|night|day|afternoon)|daily|each\s+(morning|evening|night|day)|"
    r"(?:in\s+the\s+)?mornings?)\b", re.IGNORECASE)
_AT_RE = re.compile(r"\b(?:at|around|by)\s+" + _CLOCK_RE, re.IGNORECASE)
_PART_OF_DAY_DEFAULT = {"morning": 8 * 60, "day": 8 * 60, "afternoon": 14 * 60,
                        "evening": 18 * 60, "night": 22 * 60}


def schedule_from_text(text: str) -> tuple[int | None, int | None]:
    """(at_min, every_minutes) a report request asks for, from its words:
    "every hour" → (None, 60); "every 15 minutes" → (None, 15); "every
    morning at 7" → (420, None); "every evening" → (1080, None); "daily
    at 9pm" → (1260, None); nothing said → (None, None). The model's
    slots win when it fills them; this is for when it does not (Gemma
    put the word "every_minutes" in `at` and left every_minutes empty,
    3 of 3, Ventuno Q eval run 5)."""
    t = text or ""
    m = _EVERY_RE.search(t)
    if m:
        if m.group(3):                                    # hourly / half-hourly
            return None, 30 if "half" in m.group(3).lower() else 60
        unit = (m.group(2) or "").lower()
        if unit.startswith("half"):
            return None, 30
        qty = _qty(m.group(1)) if m.group(1) else 1
        mins = int(qty * (60 if unit.startswith(("hour", "hr")) else 1))
        return None, max(1, mins)
    at = None
    m = _AT_RE.search(t)
    if m:
        at = _clock_minutes(m.group("h"), m.group("m"), m.group("ap"), m.group("word"))
    d = _DAILY_RE.search(t)
    if d:
        if at is None:
            part = (d.group(2) or d.group(3) or "morning").lower()
            if "morning" in d.group(0).lower():
                part = "morning"
            at = _PART_OF_DAY_DEFAULT.get(part, 8 * 60)
        return at, None
    if at is not None:
        return at, None
    return None, None


# ── the one call the turn makes ───────────────────────────────────

def decide(text: str, *, cameras: list[str], advertised: set[str],
           preferred: str | None = None, roles: dict[str, str] | None = None,
           tier0: bool = True, hints: bool = True, now=None,
           last_kind: str | None = None) -> Decision:
    d = decide_tier0(text, cameras=cameras, advertised=advertised,
                     preferred=preferred, roles=roles, now=now,
                     last_kind=last_kind) if tier0 else None
    if d is not None and d.routed:
        logger.info("router: tier=0 tool=%s args=%s (%s)", d.tool, d.args, d.reason)
        return d
    if d is not None and d.ask:
        # The router's own question (which camera?) goes out as is: a hint
        # to the model would make it guess a camera instead.
        logger.info("router: ask (%s)", d.reason)
        return d
    why = d.reason if d is not None else ("tier0 off" if not tier0 else "not routable")
    if hints and d is not None and "side effect" in d.reason:
        h = hint_standing(text, advertised=advertised, last_kind=last_kind)
        if h is not None:
            logger.info("router: tier=1 hint=%s (%s)", h.hint, h.reason)
            return h
    if hints and (d is None or d.hintable):
        h = hint_tier1(text, advertised=advertised)
        if h is not None:
            logger.info("router: tier=1 hint=%s (%s; tier0: %s)", h.hint, h.reason, why)
            return h
    logger.info("router: tier=2 (%s)", why)
    return Decision(2, reason=why)


def compose_prompt(base_identity: str, operator_prompt: str, roster: str,
                   clock_line: str) -> str:
    """The short system prompt for a Tier-0 compose call: say the answer,
    nothing else. No tools, no routing guidance, no tool schemas — the
    decision is already made. The operator's own system prompt stays
    (a persona, a language, a camera never to mention): a routed turn
    must not behave differently from an un-routed one. ~200 tokens plus
    that, so re-prefilling it costs well under a second on CPU."""
    operator = (operator_prompt or "").strip()
    return (
        f"{base_identity}\n\n"
        + (f"{operator}\n\n" if operator else "")
        + f"Cameras:\n{roster}\n\n"
        "Your replies are SPOKEN ALOUD. Refer to a camera by its location, "
        "never its raw id. Answer in 1-2 short, natural sentences a person "
        "would say out loud — no ids, colons, lists, or markdown. You will be "
        "given the question and what the camera system found; state what was "
        "found, directly, in the first person. If it found nothing, say so. "
        "Never invent details that are not in what was found.\n\n"
        f"{clock_line}"
    )
