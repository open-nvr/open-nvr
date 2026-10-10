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
    "happening", "detect", "count", "package", "parcel", "delivery",
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
    r"|what can you do|what do you do|how are you|how'?s it going|help"
    r"|very much|a lot|so much|for now|again|there|then|and|so)"
    r"\s*[,.!?]*)+\s*$",
    re.IGNORECASE,
)

# The model's reply claims to have LOOKED ("I see the front door. It's dark
# outside", "there's a person at the gate") — as opposed to offering to look
# ("I can watch the cameras for you"). "I see..." / "I see," is the filler
# acknowledgement, not a sighting.
_SIGHTING_RE = re.compile(
    r"\b(?:i (?:can )?see\b(?!\s*(?:\.|,|!|…))|i notice|i spot|i observe|i'?m seeing"
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

    The question names the camera or the scene → yes. Otherwise the reply
    decides: it mentions the camera or scene, which catches a question STT
    garbled ("what's on hammer 2") that the model still answered about
    "camera 2". After small talk, though, a reply mentioning cameras is
    usually an offer ("happy to help you monitor the cameras"), so there
    the reply must claim a sighting to trigger the look.
    """
    if _looks_like_camera_question(user_text):
        return True
    if not _looks_like_camera_question(reply):
        return False
    if _is_small_talk(user_text):
        return bool(_SIGHTING_RE.search(reply or ""))
    return True


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
_WINDOW_RE = re.compile(
    r"\b(?:last|past)\s+(\d+)\s*(minutes?|mins?|min|hours?|hrs?|hr)\b"
    r"|\b(\d+)\s*(minutes?|mins?|min|hours?|hrs?|hr)\s+ago\b",
    re.IGNORECASE,
)

# Detection noun → the events-store label search_history stores visits under.
# Person-ish nouns collapse to "person"; unknown nouns fall back to "person"
# (the store's own default) rather than guessing a label it never indexes.
_HISTORY_LABELS: dict[str, str] = {
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
        qty = int(m.group(1) or m.group(3))
        unit = (m.group(2) or m.group(4) or "").lower()
        secs = qty * (3600 if unit.startswith(("hour", "hr")) else 60)
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
    else the UI-selected ``preferred`` camera, else the first configured.
    A bare digit is never a camera: "in the last 2 hours" is not cam2."""
    named = _names_camera(text, cameras)
    if named is not None:
        return named
    if preferred and preferred in cameras:
        return preferred
    return cameras[0]




#: Requests with a side effect or a standing shape. Never Tier 0: the
#: model's confirmation ("I'll watch the driveway and tell you…") is part
#: of the product for these.
_SIDE_EFFECT_RE = re.compile(
    r"\b(notify|alert|alarm|siren|watch|keep\s+(an\s+)?eye|monitor|remind|"
    r"report|summar(y|ize|ise)|every\s+(morning|evening|hour|day|\d+)|"
    r"stop\b(?!\s+by)|cancel|disarm|arm|turn\s+(on|off)|mute|silence|schedule|"
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


@dataclass
class Decision:
    tier: int
    tool: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    hint: str | None = None
    reason: str = ""
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


def decide_tier0(text: str, *, cameras: list[str], advertised: set[str],
                 preferred: str | None = None, roles: dict[str, str] | None = None,
                 now=None) -> Decision | None:
    """A tool call the agent can make without asking the model, or None."""
    t = (text or "").strip()
    words = re.findall(r"[A-Za-z']+", t)
    if not t or not cameras:
        return None
    if len(words) > _MAX_WORDS or t.count("?") > 1:
        return Decision(2, reason="too long or two questions", hintable=False)
    if _is_config_question(t):
        return None                                   # answered from the roster
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
        return Decision(2, reason="no object named")
    if not past and not (nouns or _DESCRIBE_RE.search(t) or _LIVE_ASK_RE.search(t)):
        return Decision(2, reason="no known ask")

    # Which camera. Named, or "all", or the one the UI is on, or the only
    # one there is. Two cameras and no name is a guess — not Tier 0.
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
                return Decision(2, reason="camera ambiguous")

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


# ── the one call the turn makes ───────────────────────────────────

def decide(text: str, *, cameras: list[str], advertised: set[str],
           preferred: str | None = None, roles: dict[str, str] | None = None,
           tier0: bool = True, hints: bool = True, now=None) -> Decision:
    d = decide_tier0(text, cameras=cameras, advertised=advertised,
                     preferred=preferred, roles=roles, now=now) if tier0 else None
    if d is not None and d.routed:
        logger.info("router: tier=0 tool=%s args=%s (%s)", d.tool, d.args, d.reason)
        return d
    why = d.reason if d is not None else ("tier0 off" if not tier0 else "not routable")
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
