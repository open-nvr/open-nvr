"""
Voice search: a spoken question, transcribed on the box, read back as
the words an operator would have typed.

Speech to text gives "show me the white car at cam twenty three with
plate k a zero one a b one two three four": the parser wants
"white car at cam 23 with plate KA01AB1234". This pass does only what
speech does to text — number words to digits, spelled-out letters and
digits to one token, the fillers of a spoken request dropped — and says
what it changed, so the box never silently hears something else.

The transcription itself is the agent's path: an adapter advertising
``speech_to_text`` (whisper on the Hexagon, Parakeet on the CPU),
reached through KAI-C like every other adapter. Nothing leaves the box.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: What an STT adapter advertises. ``audio_transcription`` is the name
#: the agent's whisper client sends; the registry's canonical name is
#: ``speech_to_text``.
STT_TASK_NAMES = {"speech_to_text", "audio_transcription", "transcribe", "asr"}
STT_TASK = "audio_transcription"

#: Spoken-request fillers a typed query never carries. Only the opening
#: phrases: "show me" mid-sentence is left alone.
_LEADING_FILLERS = (
    "ok", "okay", "um", "uh", "hey", "please", "can you", "could you",
    "show me", "find me", "find", "search for", "search", "look for",
    "look up", "give me", "i want", "i want to see", "i'd like to see",
    "let me see", "any",
)
_TRAILING_FILLERS = ("please", "thanks", "thank you")

_UNITS = {
    "zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}
#: Words that are a single spoken letter. "a" and "i" are also English
#: words, so a run has to be long enough to read as spelling (see
#: ``_SPELL_MIN``) before they count.
_LETTER_WORDS = {ch: ch for ch in "abcdefghijklmnopqrstuvwxyz"}
_LETTER_NAMES = {"bee": "b", "see": "c", "dee": "d", "gee": "g", "jay": "j", "kay": "k",
                 "el": "l", "em": "m", "en": "n", "pee": "p", "queue": "q", "are": "r",
                 "ess": "s", "tee": "t", "you": "u", "vee": "v", "ex": "x", "why": "y", "zed": "z", "zee": "z"}
_SPELL_MIN = 5

#: Count words the parser already reads ("three people"): left as words
#: when they stand before a people or group word, so "two men together"
#: stays a count rather than becoming "2 men together" (which the parser
#: also reads, but the chips would show digits the operator never said).
_COUNT_CONTEXT = {"people", "person", "persons", "men", "man", "women", "woman",
                  "guys", "visitors", "cars", "vehicles", "together", "of"}


@dataclass
class SpokenQuery:
    text: str
    changes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"query": self.text, "changes": list(self.changes)}


def _number_run(words: list[str], i: int) -> tuple[int | None, int]:
    """A number spoken as words starting at ``words[i]``: (value, words
    consumed). "twenty three" → (23, 2); "two hundred" → (200, 2);
    "zero one" is NOT a number (that is spelling, handled elsewhere)."""
    total = 0
    j = i
    if j < len(words) and words[j] in _UNITS and words[j] not in ("zero", "oh"):
        total = _UNITS[words[j]]
        j += 1
        if j < len(words) and words[j] == "hundred":
            total *= 100
            j += 1
            if j < len(words) and words[j] == "and":
                j += 1
    if j < len(words) and words[j] in _TENS and (j == i or total % 100 == 0):
        total += _TENS[words[j]]
        j += 1
        if j < len(words) and words[j] in _UNITS and 0 < _UNITS[words[j]] < 10:
            total += _UNITS[words[j]]
            j += 1
    elif j > i and total % 100 == 0 and j < len(words) and words[j] in _UNITS \
            and 0 < _UNITS[words[j]] < 20:
        total += _UNITS[words[j]]                   # "one hundred and five"
        j += 1
    return (total if j > i else None), j - i


def _spelled_run(words: list[str], i: int) -> tuple[str, int]:
    """Letters and digits spoken one by one from ``words[i]``: ("ka01ab1234", 10)."""
    out: list[str] = []
    j = i
    while j < len(words):
        w = words[j]
        if w in _LETTER_WORDS:
            out.append(w)
        elif w in _LETTER_NAMES:
            out.append(_LETTER_NAMES[w])
        elif w in _UNITS and _UNITS[w] < 10:
            out.append(str(_UNITS[w]))
        elif w.isdigit() and len(w) <= 2:
            out.append(w)
        elif w == "double" and j + 1 < len(words):
            nxt = words[j + 1]
            ch = _LETTER_WORDS.get(nxt) or _LETTER_NAMES.get(nxt) or (str(_UNITS[nxt]) if nxt in _UNITS and _UNITS[nxt] < 10 else None)
            if ch is None:
                break
            out.extend([ch, ch])
            j += 1
        else:
            break
        j += 1
    return "".join(out), j - i


def spoken_to_text(transcript: str) -> SpokenQuery:
    """The typed form of a spoken request, and what changed."""
    text = " ".join((transcript or "").lower().replace("-", " ").split())
    text = re.sub(r"[^\w\s']", " ", text)
    text = " ".join(text.split())
    changes: list[str] = []
    if not text:
        return SpokenQuery("")

    for f in sorted(_LEADING_FILLERS, key=len, reverse=True):
        if text == f or text.startswith(f + " "):
            text = text[len(f):].strip()
            changes.append(f"dropped “{f}”")
            break
    for f in _TRAILING_FILLERS:
        if text.endswith(" " + f):
            text = text[: -len(f)].strip()
            changes.append(f"dropped “{f}”")
            break

    words = text.split()
    out: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        # Spelling: a run of single letters / digits long enough to be
        # a plate or a code ("k a zero one a b one two three four").
        spelled, n = _spelled_run(words, i)
        if n >= _SPELL_MIN and any(c.isdigit() for c in spelled) and any(c.isalpha() for c in spelled):
            token = spelled.upper()
            out.append(token)
            changes.append(f"“{' '.join(words[i:i + n])}” → {token}")
            i += n
            continue
        # A number in words, unless it is a count the parser reads as
        # words ("three people").
        value, n = _number_run(words, i)
        if value is not None and n >= 1:
            following = words[i + n] if i + n < len(words) else ""
            if not (n == 1 and w in _UNITS and value < 10 and following in _COUNT_CONTEXT):
                out.append(str(value))
                if n > 1 or w not in ("one",):
                    changes.append(f"“{' '.join(words[i:i + n])}” → {value}")
                i += n
                continue
        out.append(w)
        i += 1
    text = " ".join(out)
    # "cam 23" / "camera 23" reads as the parser's camera match; "number
    # plate" is the plate cue it already knows.
    text = re.sub(r"\bnumber plate\b", "plate", text)
    return SpokenQuery(text, changes)


def transcript_from(body: Any) -> str:
    """The text an STT adapter answered, whichever key it used."""
    result = body.get("result") if isinstance(body, dict) and isinstance(body.get("result"), dict) else body
    if not isinstance(result, dict):
        return ""
    for key in ("text", "transcript", "transcription"):
        v = result.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    segments = result.get("segments")
    if isinstance(segments, list):
        return " ".join(str(s.get("text", "")).strip() for s in segments if isinstance(s, dict)).strip()
    return ""


async def resolve_stt_adapter() -> str | None:
    """A registered adapter advertising speech to text, or None."""
    from services.embed_enrichment import _resolve_producer_for

    found = await _resolve_producer_for(STT_TASK_NAMES)
    return found[0] if found else None


async def transcribe(audio_bytes: bytes, *, language: str = "en") -> tuple[str | None, str | None]:
    """``(transcript, adapter)`` through KAI-C; ``(None, None)`` with no
    adapter, ``("", adapter)`` when it heard nothing."""
    import base64

    from services.enrichment_gate import DROPPED, infer_through_gate

    adapter = await resolve_stt_adapter()
    if adapter is None:
        return None, None
    payload = {"task": STT_TASK, "audio_b64": base64.b64encode(audio_bytes).decode("ascii"),
               "language": language, "vad_filter": False}
    body = await infer_through_gate(adapter, payload, kind="stt", log=logger,
                                    caller="search voice", what="a spoken search",
                                    interactive=True)
    if body is DROPPED or body is None:
        return "", adapter
    return transcript_from(body), adapter
