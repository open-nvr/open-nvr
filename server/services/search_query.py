# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
#
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""Turn "red truck at the dock yesterday" into filters over the event store.

Deterministic on purpose. A model could parse more shapes, but this runs
on every search, on a box that may have no model at all, and — the real
reason — its output is shown back to the operator as editable chips. A
parse you can see and correct beats a cleverer one you cannot: the
characteristic failure of natural-language search is not a query it
cannot parse, it is a query it parses WRONGLY and answers confidently
with an empty list.

So every consumed word is reported in :class:`ParsedQuery.matched`, and
whatever is left over becomes free text rather than being silently
dropped. The caller (the UI, the agent, an app) can override any part by
passing it explicitly — editing a chip is exactly that.

What it understands
-------------------
* **When** — today, yesterday, last night, this morning/afternoon/evening,
  "last 10 minutes", "past 2 hours", "in the last 3 days", a weekday name
  ("on friday" = the most recent one), an ISO date, "since 14:00",
  "between 14:00 and 16:00".
* **What** — object classes, through a synonym table that maps the words
  people use ("lorry", "van", "motorbike") onto the labels a detector
  emits. Several classes mean OR, because a visit row is ONE object; a
  query that wants two things at once ("a person AND a bicycle") is a
  co-occurrence question this layer deliberately does not pretend to
  answer.
* **Where** — camera names, matched against the caller's own cameras by
  whole-word overlap, so "at the dock" finds "Loading dock" but "the"
  finds nothing.
* **Which** — a plate-shaped token (letters and digits, at least four
  characters, at least one digit) becomes a plate filter.
* **Everything else** — free text, matched against the visit's caption
  and attributes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Iterable

__all__ = ["ParsedQuery", "parse_query", "LABEL_SYNONYMS"]

#: Words people use → the labels a detector actually emits. One word can
#: widen to several labels ("vehicle"), and several words can narrow to
#: one ("lorry" → truck). Unknown words are NOT invented into labels;
#: they fall through to free text, where a caption can still match them.
LABEL_SYNONYMS: dict[str, tuple[str, ...]] = {
    "person": ("person",), "people": ("person",), "man": ("person",),
    "woman": ("person",), "child": ("person",), "someone": ("person",),
    "somebody": ("person",), "anyone": ("person",), "pedestrian": ("person",),
    # plurals and the words people actually type ("did you see the 3
    # persons together" matched nothing: "persons" was free text)
    "persons": ("person",), "men": ("person",), "women": ("person",),
    "guy": ("person",), "guys": ("person",), "lady": ("person",),
    "ladies": ("person",), "gentleman": ("person",), "boy": ("person",),
    "girl": ("person",), "boys": ("person",), "girls": ("person",),
    "kid": ("person",), "kids": ("person",), "children": ("person",),
    "visitor": ("person",), "visitors": ("person",), "intruder": ("person",),
    "stranger": ("person",), "human": ("person",), "humans": ("person",),
    "individual": ("person",), "individuals": ("person",),
    # the people an operator names by what they do or are
    "cop": ("person",), "cops": ("person",), "police": ("person",),
    "policeman": ("person",), "policemen": ("person",), "officer": ("person",),
    "guard": ("person",), "guards": ("person",), "watchman": ("person",),
    "security": ("person",), "courier": ("person",), "deliveryman": ("person",),
    "worker": ("person",), "workers": ("person",), "labourer": ("person",),
    # (not "baby", "teen", "cab": three or four letters that the typo
    # corrector would read "bay", "tee", "cam" as)
    "toddler": ("person",), "infant": ("person",),
    "teenager": ("person",), "elderly": ("person",),
    "car": ("car",), "cars": ("car",), "vehicle": ("car", "truck", "bus", "motorcycle"),
    "vehicles": ("car", "truck", "bus", "motorcycle"),
    "truck": ("truck",), "trucks": ("truck",), "lorry": ("truck",),
    "lorries": ("truck",), "van": ("truck",), "vans": ("truck",),
    "bus": ("bus",), "buses": ("bus",), "coach": ("bus",),
    # "bike" is a motorcycle on most roads here and a bicycle on others:
    # both, and the vectors sort it out.
    "bike": ("bicycle", "motorcycle"), "bikes": ("bicycle", "motorcycle"),
    "bicycle": ("bicycle",), "cycle": ("bicycle",), "cyclist": ("bicycle",),
    "motorbike": ("motorcycle",), "motorcycle": ("motorcycle",),
    "scooter": ("motorcycle",), "scooty": ("motorcycle",), "moped": ("motorcycle",),
    # an auto-rickshaw is read by the detector as a car or a motorcycle
    "auto": ("car", "motorcycle"), "rickshaw": ("car", "motorcycle"),
    "autorickshaw": ("car", "motorcycle"),
    "jeep": ("car",), "suv": ("car",), "sedan": ("car",), "hatchback": ("car",),
    "taxi": ("car",), "pickup": ("truck", "car"),
    "tempo": ("truck",), "minivan": ("truck",),
    "bag": ("backpack", "handbag", "suitcase"), "bags": ("backpack", "handbag", "suitcase"),
    "luggage": ("backpack", "handbag", "suitcase"), "case": ("suitcase",),
    "suitcase": ("suitcase",), "backpack": ("backpack",), "rucksack": ("backpack",),
    "handbag": ("handbag",), "parcel": ("box",), "package": ("box",), "box": ("box",),
    "dog": ("dog",), "cat": ("cat",), "animal": ("dog", "cat"),
}

#: How many: a number (digit or word) beside a people word, or a word
#: that means "more than one" on its own. Resolved to ``min_people``,
#: which the search answers from the pipeline's count of people in frame
#: with each visit — never from the caption, which may say "three men"
#: or nothing at all.
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "couple": 2, "pair": 2,
    "both": 2, "several": 3, "few": 3, "multiple": 2, "many": 3,
}
_GROUP_WORDS = {"together", "group", "groups", "crowd", "gathering", "gathered"}
_PEOPLE_WORDS = {w for w, labels in LABEL_SYNONYMS.items() if labels == ("person",)}

#: Words that carry no meaning here. Anything not listed survives as
#: free text, where a caption may well match it — which is why the list
#: has to cover the way people actually type into this box.
#:
#: The box says "describe it", so people address the system: "did you
#: see any car in the last 5 minutes". Every one of those words that
#: survives becomes a REQUIRED substring of a caption, and no caption
#: ever written contains "you see" — so a perfectly well-understood
#: query ("car", "last 5 minutes", both correct) returns nothing, which
#: is exactly the confident-wrong-parse failure this parser exists to
#: avoid. Three groups, for that reason:
#:
#: * articles, prepositions and conjunctions — structure, never content;
#: * asking words — the operator addressing the system rather than
#:   describing the footage ("did you see", "can you show me", "was
#:   there anything");
#: * words for the medium itself — "camera", "footage", "clip" — which
#:   name where they are looking, not what they are looking for.
_STOP = {
    # structure
    "a", "an", "the", "at", "in", "on", "of", "for", "to", "from", "by",
    "with", "and", "or", "about", "it", "its", "this", "these", "those",
    "up", "out", "over", "into", "around", "some",
    "if", "whether", "just", "only", "also", "then", "than", "as",
    "but", "so", "still", "yet", "ever", "again", "else",
    # the operator addressing the system
    "me", "my", "we", "us", "i", "you", "your", "show", "find", "get",
    "search", "look", "looking", "see", "seen", "saw", "seeing", "spot",
    "spotted", "notice", "noticed", "detect", "detected", "catch",
    "caught", "capture", "captured", "record", "recorded", "tell",
    "give", "please", "thanks", "thank", "check", "want", "need",
    "anything", "something", "any", "all", "anyone", "anybody",
    # question and auxiliary verbs
    "was", "were", "is", "are", "be", "been", "am", "has", "have", "had",
    "there", "that", "who", "whom", "what", "when", "where", "which",
    "why", "how", "did", "do", "does", "done", "can", "could", "will",
    "would", "should", "may", "might", "must", "shall",
    # the medium, not the subject
    "camera", "cameras", "cam", "footage", "video", "videos", "clip",
    "clips", "feed", "recording", "recordings", "frame", "frames",
    "near", "captured",
}

_TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_REL_RE = re.compile(
    r"\b(?:last|past|previous)\s+(\d+)?\s*(minute|min|hour|hr|day|week)s?\b"
)
#: Calendar phrases. A month name on its own is the most recent one that
#: has started (in November, "january" is this year's; "december" is last
#: year's); with a day it is that day; with a year, that year. Two months
#: joined by "or", "and", "to" or a dash are one window from the first's
#: start to the second's end — what "in January or February" means to the
#: person asking, even if a few weeks of March ride along between
#: non-adjacent months (the chip says what was searched).
_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
#: "5 november", "5th nov 2025", "november 5", "nov 5th 2025", "november",
#: "november 2025" — day and year optional either side of the month.
_MONTH_RE = re.compile(
    rf"\b(?:(?:on\s+)?(?:the\s+)?{_DAY}\s+(?:of\s+)?)?({_MONTH_ALT})\b(?:\s+{_DAY}\b)?(?:,?\s+(\d{{4}})\b)?"
)
_SPAN_RE = re.compile(
    rf"\b(?:in\s+|during\s+|around\s+|somewhere\s+in\s+)?({_MONTH_ALT})\s+(?:or|and|to|-|through|till|until)\s+({_MONTH_ALT})\b(?:\s+(\d{{4}}))?"
)
_CAL_RE = re.compile(
    r"\b(this|last|previous|past)\s+(week|month|year)\b"
)
_AGO_RE = re.compile(r"\b(\d+|a|an|one|two|three|four|five|six)\s+(week|month|year)s?\s+ago\b")
_LONG_REL_RE = re.compile(r"\b(?:last|past|previous)\s+(\d+)\s*(month|year)s?\b")
_SMALL = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


def _month_start(year: int, month: int, tz) -> datetime:
    return datetime(year, month, 1, tzinfo=tz)


def _add_months(d: datetime, n: int) -> datetime:
    y, m = d.year + (d.month - 1 + n) // 12, (d.month - 1 + n) % 12 + 1
    return d.replace(year=y, month=m, day=1)


_YEAR_HINT_RE = re.compile(r"\b(this|last|previous)\s+year\b")


def _year_hint(q: str, now: datetime) -> tuple[int | None, str]:
    """"last year ... in january": the year word qualifies the month, so it
    is read with it rather than left over as a word nobody wrote in a
    caption. Returns (year, q without the year words)."""
    m = _YEAR_HINT_RE.search(q)
    if not m:
        return None, q
    year = now.year if m.group(1) == "this" else now.year - 1
    return year, q.replace(m.group(0), " ")


def _resolve_month(name: str, year: str | None, now: datetime) -> tuple[int, int]:
    m = _MONTHS[name]
    if year:
        return int(year), m
    y = now.year if m <= now.month else now.year - 1
    return y, m
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")
#: Words that say the next token IS a registration, so it is taken as one
#: whatever it looks like.
_PLATE_CUES = {"plate", "plates", "reg", "registration", "numberplate", "licence", "license"}
#: The rest of the phrase, once a cue above has been consumed: "plate
#: NUMBER", "registration NUMBER". _PLATE_CUES already carries the
#: one-word spelling `numberplate`; these are the two-word ones, and
#: leaving the tail behind is how "what is the plate number" ends up
#: demanding a caption that contains the word "number".
#:
#: Only applied when a cue is actually present. "number" on its own is
#: not plate-speak, and a word that means one thing beside "plate" and
#: another thing alone does not belong in _STOP.
_PLATE_PHRASE_TAIL = {"number", "numbers", "no"}
#: Shape of a plate when nobody said the word: long enough, and mostly
#: digits. Without the density rule "gate14" and "bay3" become plate
#: searches that return nothing — the confident-wrong-parse failure this
#: parser exists to avoid. A plate missed this way falls through to free
#: text, where a caption carrying the number still matches.
_PLATE_RE = re.compile(r"^(?=(?:.*\d){3,})[a-z0-9][a-z0-9\-]{4,11}$", re.I)


@dataclass
class ParsedQuery:
    """What the words were taken to mean — the shape the UI renders as
    chips and the service turns into filters."""

    labels: list[str] = field(default_factory=list)
    camera_ids: list[int] = field(default_factory=list)
    from_: datetime | None = None
    to: datetime | None = None
    text: str = ""
    plate: str = ""
    #: The question named a plate without giving one — "what is the
    #: plate number of the white car" — so only visits that CARRY a read
    #: can answer it. Without this the page led with the cars nobody
    #: read, and the operator had to scroll for the one it asked about.
    wants_plate: bool = False
    #: Claims the words resolved to, as (kind, value): a name the box
    #: knows becomes ``("face_id", value)``. A name is never a caption
    #: word (see ``descriptor_store._UNPROJECTED_KINDS``), so typing one
    #: used to match nothing — silently, forever.
    attrs: list[tuple[str, str]] = field(default_factory=list)
    #: The phrase each interpretation came from, so a chip can say
    #: "yesterday → 00:00–23:59" and be removed by the operator.
    matched: dict[str, str] = field(default_factory=dict)
    #: Words that meant nothing to the parser AND nothing as text — kept
    #: so the UI can say what it ignored instead of pretending.
    ignored: list[str] = field(default_factory=list)
    #: "3 people", "two men together", "a group": at least this many people
    #: in frame with the visit. None = not asked.
    min_people: int | None = None
    #: Typed word → the vocabulary word it was read as ("persn" →
    #: "person", "dok" → "dock"). Shown, never silent, and the caller can
    #: turn it off (``correct=false``) to search the letters as typed.
    corrections: dict[str, str] = field(default_factory=dict)
    #: The question as the VECTOR arms should hear it: corrected, with
    #: the time phrase and the camera names taken out, and everything
    #: else kept — the class words, the colours, the counts. ``text`` is
    #: what is left for the WORD arm after the parser has turned those
    #: into filters, and for "pink car" or "three people together" that
    #: is nothing, which used to mean no semantic arm ran at all.
    question: str = ""

    def as_dict(self) -> dict:
        return {
            "labels": self.labels,
            "camera_ids": self.camera_ids,
            "from": self.from_.isoformat() if self.from_ else None,
            "to": self.to.isoformat() if self.to else None,
            "text": self.text,
            "plate": self.plate,
            "wants_plate": self.wants_plate,
            "attrs": [f"{k}:{v}" for k, v in self.attrs],
            "matched": self.matched,
            "ignored": self.ignored,
            "min_people": self.min_people,
            "corrections": self.corrections,
            "question": self.question,
        }


# ── typos against the box's own vocabulary ─────────────────────────────
#
# The words a search can mean are a few hundred: the class synonyms, the
# camera names, the people the box knows, the colours and kinds the
# description skills write. A word that is none of them and one letter
# away from one of them is that word mistyped — "persn", "trcuk", "dok" —
# and used to fall through to the caption text, where it matched nothing,
# silently. difflib on a vocabulary this size is microseconds; no model,
# no database extension. Rules: never touch a word the parser already
# knows, a stop word, a number or a plate; a four-letter word needs a
# closer match than a longer one ("cart" is not "car"); the best candidate
# must stand clear of the second.

def _ratio(a: str, b: str) -> float:
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()


def correct_word(word: str, vocab: set[str]) -> str | None:
    """The vocabulary word ``word`` is most likely a typo of, or None."""
    if len(word) < 3 or word in vocab or word.isdigit() or word in _STOP:
        return None
    if any(ch.isdigit() for ch in word):
        return None                              # gate14, a plate, a time
    # Shorter words need a closer match and a candidate of nearly the same
    # length: "dok" is "dock" and "blu" is "blue", but "van" is not "can"
    # and "cart" is not "car".
    if len(word) == 3:
        floor, span = 0.85, 1
    elif len(word) == 4:
        floor, span = 0.86, 1
    else:
        floor, span = 0.8, 2
    scored = sorted(
        ((_ratio(word, v), v) for v in vocab if abs(len(v) - len(word)) <= span and len(v) >= 3),
        reverse=True)
    if not scored or scored[0][0] < floor:
        return None
    if len(scored) > 1 and scored[1][0] >= scored[0][0] - 0.02 and scored[1][1] != scored[0][1]:
        return None                              # two words equally close: do not guess
    return scored[0][1]


def correct_words(words: list[str], vocab: set[str]) -> tuple[list[str], dict[str, str]]:
    """Each word corrected where :func:`correct_word` is sure; the map of
    what changed."""
    out: list[str] = []
    changed: dict[str, str] = {}
    for w in words:
        fixed = correct_word(w, vocab)
        if fixed:
            changed[w] = fixed
            out.append(fixed)
        else:
            out.append(w)
    return out, changed


#: Characters a plate reader confuses, folded to one class each so a
#: plate typed from memory still finds the read: O/Q/D read as 0, I/L
#: as 1, Z as 2, S as 5, G as 6, B as 8 (Genetec's "OCR equivalence").
PLATE_CLASS_MAP = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1",
                                 "Z": "2", "S": "5", "G": "6", "B": "8"})


def plate_class(text: str) -> str:
    """``text`` as its OCR-equivalence class: upper, no separators, the
    confusable letters folded (KA-01 AB 1234 → KA01A81234)."""
    s = "".join(ch for ch in (text or "").upper() if ch.isalnum())
    return s.translate(PLATE_CLASS_MAP)


def plate_class_sql(column):
    """The same folding as :func:`plate_class`, as a SQL expression over
    ``column`` — nested ``replace`` so it runs on Postgres and SQLite alike."""
    from sqlalchemy import func

    expr = func.upper(column)
    for a, b in (("O", "0"), ("Q", "0"), ("D", "0"), ("I", "1"), ("L", "1"),
                 ("Z", "2"), ("S", "5"), ("G", "6"), ("B", "8"), ("-", ""), (" ", "")):
        expr = func.replace(expr, a, b)
    return expr


def _day_bounds(day: datetime) -> tuple[datetime, datetime]:
    start = datetime.combine(day.date(), time.min, tzinfo=day.tzinfo)
    return start, start + timedelta(days=1)


def _extract_time(q: str, now: datetime) -> tuple[datetime | None, datetime | None, str, str]:
    """Pull a time window out of ``q``. Returns (from, to, rest, phrase)."""
    # "between 14:00 and 16:00" — explicit, so it wins over everything.
    times = _TIME_RE.findall(q)
    if "between" in q and len(times) >= 2:
        start = datetime.combine(now.date(), time(int(times[0][0]), int(times[0][1])),
                                 tzinfo=now.tzinfo)
        end = datetime.combine(now.date(), time(int(times[1][0]), int(times[1][1])),
                               tzinfo=now.tzinfo)
        if end <= start:                      # crossed midnight
            end += timedelta(days=1)
        phrase = f"between {times[0][0]}:{times[0][1]} and {times[1][0]}:{times[1][1]}"
        return start, end, _TIME_RE.sub("", q).replace("between", "").replace(" and ", " "), phrase

    m = _ISO_DATE_RE.search(q)
    if m:
        day = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                       tzinfo=now.tzinfo)
        start, end = _day_bounds(day)
        return start, end, q.replace(m.group(0), ""), m.group(0)

    m = _SPAN_RE.search(q)
    if m:
        hint, q = _year_hint(q, now)
        year = m.group(3) or (str(hint) if hint else None)
        y1, m1 = _resolve_month(m.group(1), year, now)
        y2, m2 = _resolve_month(m.group(2), year, now)
        a = _month_start(y1, m1, now.tzinfo)
        b = _add_months(_month_start(y2, m2, now.tzinfo), 1)
        if b <= a:                                    # "december or january": spans the year end
            a = _month_start(y1 - 1, m1, now.tzinfo)
        return a, b, q.replace(m.group(0), " "), m.group(0).strip()
    m = _AGO_RE.search(q)
    if m:
        n = _SMALL.get(m.group(1)) or int(m.group(1))
        unit = m.group(2)
        if unit == "week":
            start, _ = _day_bounds(now - timedelta(weeks=n))
            start -= timedelta(days=start.weekday())
            return start, start + timedelta(weeks=1), q.replace(m.group(0), " "), m.group(0)
        if unit == "month":
            a = _add_months(_month_start(now.year, now.month, now.tzinfo), -n)
            return a, _add_months(a, 1), q.replace(m.group(0), " "), m.group(0)
        a = _month_start(now.year - n, 1, now.tzinfo)
        return a, _month_start(now.year - n + 1, 1, now.tzinfo), q.replace(m.group(0), " "), m.group(0)
    m = _LONG_REL_RE.search(q)
    if m:
        n = int(m.group(1))
        if m.group(2) == "month":
            return _add_months(_month_start(now.year, now.month, now.tzinfo), -n), now, q.replace(m.group(0), " "), m.group(0)
        return _month_start(now.year - n, 1, now.tzinfo), now, q.replace(m.group(0), " "), m.group(0)
    m = _CAL_RE.search(q)
    if m:
        which, unit = m.group(1), m.group(2)
        if unit == "week":
            start, _ = _day_bounds(now)
            start -= timedelta(days=start.weekday())
            if which == "this":
                return start, now, q.replace(m.group(0), " "), m.group(0)
            return start - timedelta(weeks=1), start, q.replace(m.group(0), " "), m.group(0)
        if unit == "month":
            this = _month_start(now.year, now.month, now.tzinfo)
            if which == "this":
                return this, now, q.replace(m.group(0), " "), m.group(0)
            return _add_months(this, -1), this, q.replace(m.group(0), " "), m.group(0)
        this = _month_start(now.year, 1, now.tzinfo)
        if which == "this":
            return this, now, q.replace(m.group(0), " "), m.group(0)
        return _month_start(now.year - 1, 1, now.tzinfo), this, q.replace(m.group(0), " "), m.group(0)
    m = _MONTH_RE.search(q)
    if m and m.group(2):
        hint, q = _year_hint(q, now)
        day = m.group(1) or m.group(3)
        y, mo = _resolve_month(m.group(2), m.group(4) or (str(hint) if hint else None), now)
        if day:
            try:
                d = datetime(y, mo, int(day), tzinfo=now.tzinfo)
            except ValueError:
                d = None
            if d is not None:
                if d > now and not m.group(4):
                    d = d.replace(year=d.year - 1)
                start, end = _day_bounds(d)
                return start, end, q.replace(m.group(0), " "), m.group(0).strip()
        a = _month_start(y, mo, now.tzinfo)
        return a, _add_months(a, 1), q.replace(m.group(0), " "), m.group(0).strip()
    m = _REL_RE.search(q)
    if m:
        n = int(m.group(1) or 1)
        unit = m.group(2)
        span = {"minute": timedelta(minutes=n), "min": timedelta(minutes=n),
                "hour": timedelta(hours=n), "hr": timedelta(hours=n),
                "day": timedelta(days=n), "week": timedelta(weeks=n)}[unit]
        return now - span, now, q.replace(m.group(0), ""), m.group(0).strip()

    if "yesterday" in q:
        start, end = _day_bounds(now - timedelta(days=1))
        return start, end, q.replace("yesterday", ""), "yesterday"
    if "last night" in q:
        # 18:00 yesterday → 06:00 today: what "last night" means to
        # somebody asking at 09:00, and still right at 23:00.
        y = (now - timedelta(days=1)).date()
        start = datetime.combine(y, time(18, 0), tzinfo=now.tzinfo)
        end = datetime.combine(now.date(), time(6, 0), tzinfo=now.tzinfo)
        if end <= start:
            end = start + timedelta(hours=12)
        return start, end, q.replace("last night", ""), "last night"
    if "this morning" in q:
        start, _ = _day_bounds(now)
        return start, start + timedelta(hours=12), q.replace("this morning", ""), "this morning"
    if "this afternoon" in q:
        start, _ = _day_bounds(now)
        return (start + timedelta(hours=12), start + timedelta(hours=18),
                q.replace("this afternoon", ""), "this afternoon")
    if "this evening" in q or "tonight" in q:
        start, _ = _day_bounds(now)
        word = "this evening" if "this evening" in q else "tonight"
        return (start + timedelta(hours=18), start + timedelta(days=1),
                q.replace(word, ""), word)
    if "today" in q:
        start, end = _day_bounds(now)
        return start, end, q.replace("today", ""), "today"

    for i, name in enumerate(_WEEKDAYS):
        if name in q:
            back = (now.weekday() - i) % 7 or 7      # the most recent one
            start, end = _day_bounds(now - timedelta(days=back))
            return start, end, q.replace(name, ""), name

    m = _TIME_RE.search(q)
    if m and "since" in q:
        start = datetime.combine(now.date(), time(int(m.group(1)), int(m.group(2))),
                                 tzinfo=now.tzinfo)
        if start > now:                              # "since 23:00" said at 01:00
            start -= timedelta(days=1)
        return start, now, q.replace(m.group(0), "").replace("since", ""), f"since {m.group(0)}"
    return None, None, q, ""


def _looks_like_plate(word: str, *, cued: bool) -> bool:
    """Is this token a registration?

    Cued ("plate ab12cde"), anything alphanumeric of plausible length is
    taken at its word. Uncued, it must LOOK like one — the digit-density
    rule — because a wrong plate filter returns an empty list and tells
    the operator nothing was there, which is worse than not spotting a
    plate at all.
    """
    if not word or not any(c.isdigit() for c in word):
        return False
    if cued:
        return 3 <= len(word) <= 12 and word.replace("-", "").isalnum()
    return bool(_PLATE_RE.match(word))


def _match_cameras(words: list[str], cameras: dict[int, str]) -> tuple[list[int], list[str]]:
    """Cameras whose NAME shares a whole, non-class word with the query.

    Two rules, both learned from the same failure. Whole words, because
    substring matching makes "car" hit "Car park". And never on a CLASS
    word alone, because a camera called "Car park north" would otherwise
    capture every query about cars — the query said what it was looking
    for, not where. "at the dock", "gate 14", "north" still work, since
    those words name a place and nothing else.

    Consumed words are returned so they do not also become free text.
    """
    hits: list[int] = []
    used: list[str] = []
    for cam_id, name in cameras.items():
        name_words = {w for w in re.split(r"[^a-z0-9]+", (name or "").lower()) if len(w) > 2}
        if not name_words:
            continue
        shared = name_words.intersection(words)
        placeish = {w for w in shared if w not in LABEL_SYNONYMS}
        if placeish:
            hits.append(cam_id)
            used.extend(sorted(shared))
    return hits, used


def _match_people(words: list[str], people: Iterable[str]) -> tuple[list[str], list[str]]:
    """People whose EVERY name part appears in the query, as ``face_id``
    values. "was varun singh here" and "varun" both land on
    ``varun-singh``; "singh" alone does not name anyone in particular
    when two people share it, and one part of a two-part name is not a
    match. Consumed words are returned so they do not become free text
    that can never match a caption."""
    hits: list[str] = []
    used: list[str] = []
    wordset = set(words)
    for value in people:
        parts = [p for p in re.split(r"[^a-z0-9]+", str(value).lower()) if p]
        if not parts:
            continue
        # A one-part identity must appear whole; a multi-part one lands
        # on its first part alone as well (first names are how people
        # ask) — but only when it names exactly one person.
        if all(p in wordset for p in parts):
            hits.append(str(value)); used.extend(parts)
    if not hits:
        firsts: dict[str, list[str]] = {}
        for value in people:
            parts = [p for p in re.split(r"[^a-z0-9]+", str(value).lower()) if p]
            if len(parts) > 1 and parts[0] in wordset:
                firsts.setdefault(parts[0], []).append(str(value))
        for first, values in firsts.items():
            if len(values) == 1:
                hits.append(values[0]); used.append(first)
    return list(dict.fromkeys(hits)), list(dict.fromkeys(used))


def parse_query(
    q: str,
    *,
    cameras: dict[int, str] | None = None,
    people: Iterable[str] | None = None,
    now: datetime | None = None,
    vocab: Iterable[str] | None = None,
    correct: bool = True,
) -> ParsedQuery:
    """Parse ``q`` into filters. ``cameras`` is {id: name} for the
    caller's visible cameras — scope decides what "the dock" can mean.
    ``people`` are the ``face_id`` values visits in scope have been
    bound to (what ``/search/people`` lists): a name among the words
    becomes an attr filter instead of a caption word that cannot match.
    ``vocab`` are further words the box can act on (the colours and kinds
    its description skills write); with ``correct`` a word one letter off
    any of these is read as that word, and ``corrections`` says so."""
    out = ParsedQuery()
    text = (q or "").strip().lower()
    if not text:
        return out
    now = now or datetime.now().astimezone()

    out.from_, out.to, text, when = _extract_time(text, now)
    if when:
        out.matched["when"] = when

    words = [w for w in re.split(r"[^a-z0-9\-:]+", text) if w]
    if correct:
        known: set[str] = set(LABEL_SYNONYMS) | set(_GROUP_WORDS) | set(_NUMBER_WORDS) | set(_PLATE_CUES)
        for name in (cameras or {}).values():
            known |= {w for w in re.split(r"[^a-z0-9]+", str(name).lower()) if len(w) >= 3}
        for name in people or ():
            known |= {w for w in re.split(r"[^a-z0-9]+", str(name).lower()) if len(w) >= 3}
        known |= {str(w).lower() for w in (vocab or ()) if w}
        words, out.corrections = correct_words(words, known)
        if out.corrections:
            out.matched["corrected"] = ", ".join(f"{a} → {b}" for a, b in out.corrections.items())

    if cameras:
        cam_ids, cam_words = _match_cameras(words, cameras)
        if cam_ids:
            out.camera_ids = cam_ids
            out.matched["camera"] = " ".join(dict.fromkeys(cam_words))
            words = [w for w in words if w not in set(cam_words)]
    out.question = " ".join(words)

    # Counts and groups, before the labels pass takes the people word.
    count_words: list[str] = []
    for i, w in enumerate(words):
        n = _NUMBER_WORDS.get(w) or (int(w) if w.isdigit() and 0 < int(w) < 100 else None)
        if n and n > 1 and any(x in _PEOPLE_WORDS for x in words[i + 1:i + 3]):
            out.min_people = max(out.min_people or 0, n)
            count_words.append(w)
        elif w in _GROUP_WORDS and (out.labels or any(x in _PEOPLE_WORDS for x in words)):
            out.min_people = max(out.min_people or 0, 2)
            count_words.append(w)
    if count_words:
        out.matched["count"] = " ".join(dict.fromkeys(count_words))
        words = [w for w in words if w not in set(count_words)]
    labels: list[str] = []
    label_words: list[str] = []
    rest: list[str] = []
    for w in words:
        hit = LABEL_SYNONYMS.get(w)
        if hit:
            label_words.append(w)
            labels.extend(hit)
        else:
            rest.append(w)
    if labels:
        out.labels = list(dict.fromkeys(labels))
        out.matched["what"] = " ".join(dict.fromkeys(label_words))

    if people:
        names, name_words = _match_people(rest, people)
        if names:
            out.attrs = [("face_id", n) for n in names]
            out.matched["person"] = " ".join(name_words)
            rest = [w for w in rest if w not in set(name_words)]

    keep: list[str] = []
    cued = any(w in _PLATE_CUES for w in rest)
    for w in rest:
        if w in _STOP or w in _PLATE_CUES:
            continue
        if cued and w in _PLATE_PHRASE_TAIL:
            continue
        if not out.plate and not w.isdigit() and _looks_like_plate(w, cued=cued):
            out.plate = w.upper()
            out.matched["plate"] = w
            continue
        keep.append(w)
    # A bare number is a house number, a lane, a count — never useful as
    # a text match on its own, and it drags in every caption with a digit.
    out.ignored = [w for w in keep if w.isdigit()]
    out.text = " ".join(w for w in keep if not w.isdigit())
    if cued and not out.plate:
        out.wants_plate = True
        out.matched["plate"] = "with a plate read"
    if out.text:
        out.matched["text"] = out.text
    return out
