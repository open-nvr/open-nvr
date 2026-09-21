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

__all__ = ["ParsedQuery", "parse_query", "LABEL_SYNONYMS"]

#: Words people use → the labels a detector actually emits. One word can
#: widen to several labels ("vehicle"), and several words can narrow to
#: one ("lorry" → truck). Unknown words are NOT invented into labels;
#: they fall through to free text, where a caption can still match them.
LABEL_SYNONYMS: dict[str, tuple[str, ...]] = {
    "person": ("person",), "people": ("person",), "man": ("person",),
    "woman": ("person",), "child": ("person",), "someone": ("person",),
    "somebody": ("person",), "anyone": ("person",), "pedestrian": ("person",),
    "car": ("car",), "cars": ("car",), "vehicle": ("car", "truck", "bus", "motorcycle"),
    "vehicles": ("car", "truck", "bus", "motorcycle"),
    "truck": ("truck",), "trucks": ("truck",), "lorry": ("truck",),
    "lorries": ("truck",), "van": ("truck",), "vans": ("truck",),
    "bus": ("bus",), "buses": ("bus",), "coach": ("bus",),
    "bike": ("bicycle",), "bikes": ("bicycle",), "bicycle": ("bicycle",),
    "cycle": ("bicycle",), "cyclist": ("bicycle",),
    "motorbike": ("motorcycle",), "motorcycle": ("motorcycle",),
    "scooter": ("motorcycle",),
    "bag": ("backpack", "handbag", "suitcase"), "bags": ("backpack", "handbag", "suitcase"),
    "luggage": ("backpack", "handbag", "suitcase"), "case": ("suitcase",),
    "suitcase": ("suitcase",), "backpack": ("backpack",), "rucksack": ("backpack",),
    "handbag": ("handbag",), "parcel": ("box",), "package": ("box",), "box": ("box",),
    "dog": ("dog",), "cat": ("cat",), "animal": ("dog", "cat"),
}

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
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")
#: Words that say the next token IS a registration, so it is taken as one
#: whatever it looks like.
_PLATE_CUES = {"plate", "plates", "reg", "registration", "numberplate", "licence", "license"}
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
    #: The phrase each interpretation came from, so a chip can say
    #: "yesterday → 00:00–23:59" and be removed by the operator.
    matched: dict[str, str] = field(default_factory=dict)
    #: Words that meant nothing to the parser AND nothing as text — kept
    #: so the UI can say what it ignored instead of pretending.
    ignored: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "labels": self.labels,
            "camera_ids": self.camera_ids,
            "from": self.from_.isoformat() if self.from_ else None,
            "to": self.to.isoformat() if self.to else None,
            "text": self.text,
            "plate": self.plate,
            "matched": self.matched,
            "ignored": self.ignored,
        }


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


def parse_query(
    q: str,
    *,
    cameras: dict[int, str] | None = None,
    now: datetime | None = None,
) -> ParsedQuery:
    """Parse ``q`` into filters. ``cameras`` is {id: name} for the
    caller's visible cameras — scope decides what "the dock" can mean."""
    out = ParsedQuery()
    text = (q or "").strip().lower()
    if not text:
        return out
    now = now or datetime.now().astimezone()

    out.from_, out.to, text, when = _extract_time(text, now)
    if when:
        out.matched["when"] = when

    words = [w for w in re.split(r"[^a-z0-9\-:]+", text) if w]

    if cameras:
        cam_ids, cam_words = _match_cameras(words, cameras)
        if cam_ids:
            out.camera_ids = cam_ids
            out.matched["camera"] = " ".join(dict.fromkeys(cam_words))
            words = [w for w in words if w not in set(cam_words)]

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

    keep: list[str] = []
    cued = any(w in _PLATE_CUES for w in rest)
    for w in rest:
        if w in _STOP or w in _PLATE_CUES:
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
    if out.text:
        out.matched["text"] = out.text
    return out
