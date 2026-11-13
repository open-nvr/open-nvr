# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""OpenNVR Mirror — the booth page that narrates whoever stands in front of
a camera, live.

What the visitor sees: the camera's live video, boxes with a label and a
confidence on everything Tier-0 tracks, one spoken-and-written sentence
about the scene every few seconds, and a card per person (what they wear,
what they carry, an apparent age range) that follows their box. Everything
runs on the box; nothing is stored.

What this module is: the loop behind that page, with every outside call
injected (a frame, a crop, the vision model, speech, a publish) so the
policy is testable without a camera:

* one vision call at a time — the model is the bottleneck (Gemma on the
  Hexagon answers in about two seconds), so what to ask next is a choice:
  a person nobody has described yet beats a scene refresh, a scene refresh
  beats re-describing someone;
* a person is asked about once they have been in frame long enough to be
  real and large enough to be legible, then again every so often while
  they stay;
* a sentence is spoken only when it says something new, never over the
  previous one, and a newcomer gets a greeting;
* when the page goes away the loop stops — it is kept alive by the page's
  heartbeat, so a closed kiosk tab does not keep the model busy.

The page receives everything over the agent's existing ``/updates``
socket as ``{"mirror": {...}}`` messages (and the boxes as the usual
``{"tracks": ...}``), so the auth and the per-camera scope are the ones the
demo already has.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger("camera_agent.mirror")

#: How long a track must have been visible before it is worth a model
#: call: ByteTrack confirms in a few frames, a passer-by at the edge of the
#: frame is gone before the answer would arrive.
PERSON_MIN_AGE_S = 0.8
#: Smallest box (share of the frame area) the model is asked about: a
#: person 30 px tall on a 640-wide detect frame has no legible clothing.
PERSON_MIN_AREA = 0.015
#: A person present this long is described again (clothes do not change,
#: but a bag is put down, a hat comes off, a phone comes out).
PERSON_REFRESH_S = 25.0
#: Scene sentence cadence when people are present / when the view is empty.
SCENE_EVERY_S = 3.0
SCENE_IDLE_EVERY_S = 12.0
#: A card whose track has not been seen for this long is dropped.
CARD_TTL_S = 4.0
#: No heartbeat from any page for this long → the loop stops itself.
HEARTBEAT_TTL_S = 45.0
#: Two sentences this similar (word Jaccard) are the same sentence: do not
#: say it twice in a row.
SPEAK_SAME_THRESHOLD = 0.5
#: Gap kept after one utterance ends before the next starts, so speech
#: never stacks on a slow Piper.
SPEAK_MIN_GAP_S = 1.0

# The scene prompt is GROUNDED in what the detector tracks right now. Asked
# "how many people, what do they wear" on an empty room, Gemma answers with
# three people in a blue shirt, a white top and a dark jacket — every time
# (measured on the Ventuno Q, 2026-11-11). Told that nobody is there, it
# describes the room; told "2 people, 1 car", it describes those.
SCENE_PROMPT_PEOPLE = (
    "You are OpenNVR Mirror at a technology show, describing a live camera to "
    "the people in front of it. The object detector currently sees: {seen}. "
    "Describe ONLY what is actually visible, in ONE concrete sentence of at "
    "most 25 words: the people (keep the detector's count unless clearly "
    "wrong), the colour and kind of their clothes, what each holds or does "
    "(waving, phone, bag, laptop), notable objects. Never invent a person. "
    "Name colours. Do not use 'casual attire', 'setting', 'suggesting', "
    "'appears', 'seemingly'. No preamble, no quotes.")
SCENE_PROMPT_OBJECTS = (
    "You are OpenNVR Mirror at a technology show. Nobody is in front of the "
    "camera right now; the object detector sees: {seen}. In ONE sentence of at "
    "most 20 words describe those and the place, naming colours. Do not "
    "mention people at all. No preamble, no quotes.")
SCENE_PROMPT_EMPTY = (
    "You are OpenNVR Mirror at a technology show. Nobody is in front of the "
    "camera right now. In ONE sentence of at most 20 words describe the place "
    "and its notable objects, naming colours. Do not mention people at all. "
    "No preamble, no quotes.")
#: Kept for callers and tests that only need "a scene prompt".
SCENE_PROMPT = SCENE_PROMPT_PEOPLE
#: A rephrase of the sentence on screen is not shown again for this long.
CAPTION_HOLD_S = 20.0


def scene_prompt(tracks: list[dict[str, Any]]) -> str:
    """The scene question for what Tier-0 tracks at this moment."""
    counts: dict[str, int] = {}
    for t in tracks or []:
        label = str(t.get("label") or "object").replace("_", " ")
        counts[label] = counts.get(label, 0) + 1
    if not counts:
        return SCENE_PROMPT_EMPTY
    parts = []
    people = counts.get("person", 0)
    for label, n in sorted(counts.items(), key=lambda kv: (kv[0] != "person", -kv[1], kv[0])):
        plural = label if n == 1 else (label + ("s" if not label.endswith("s") else ""))
        if label == "person" and n > 1:
            plural = "people"
        parts.append(f"{n} {plural}")
    if not people:
        return SCENE_PROMPT_OBJECTS.format(seen=", ".join(parts))
    return SCENE_PROMPT_PEOPLE.format(seen=", ".join(parts))
PERSON_PROMPT = (
    "Look at the person in this image. Answer on ONE line as key=value pairs "
    "separated by ';' with exactly these keys: gender (man/woman/child/unknown); "
    "age (an age range such as 20s or 30-40); top (colour and type of upper "
    "clothing); bottom (colour and type); headwear (none or what); glasses "
    "(yes/no); carrying (none or what); expression (one word). "
    "Use unknown when you cannot tell. No other text.")
PERSON_KEYS = ("gender", "age", "top", "bottom", "headwear", "glasses",
               "carrying", "expression", "posture")
PERSON_PROMPT = PERSON_PROMPT.replace(
    "expression (one word). ",
    "expression (one word); posture (standing/sitting/walking/waving/pointing). ")

# ── What the model may say about a person ────────────────────────────
# The same discipline as the search pipeline's descriptor_enrichment: a
# fixed vocabulary per attribute, synonyms folded, anything else dropped.
# A card that says less is better than a card that says something wrong
# to the person standing in front of it.
_COLOURS = ("white", "black", "silver", "grey", "red", "blue", "green", "yellow",
            "orange", "brown", "beige", "gold", "maroon", "purple", "pink", "denim")
_COLOUR_SYNONYMS = {"gray": "grey", "dark grey": "grey", "light grey": "grey", "charcoal": "grey",
                    "navy": "blue", "dark blue": "blue", "light blue": "blue", "sky blue": "blue",
                    "teal": "blue", "turquoise": "blue", "crimson": "red", "scarlet": "red",
                    "burgundy": "maroon", "wine": "maroon", "tan": "brown", "khaki": "beige",
                    "cream": "white", "off-white": "white", "off white": "white", "ivory": "white",
                    "golden": "gold", "violet": "purple", "lavender": "purple", "magenta": "pink",
                    "olive": "green", "lime": "green", "jeans": "denim", "mustard": "yellow"}
VOCAB: dict[str, tuple[str, ...]] = {
    "gender": ("man", "woman", "child"),
    "age": ("child", "teen", "20s", "30s", "40s", "50s", "60+"),
    "headwear": ("cap", "hat", "turban", "helmet", "hijab", "hood", "scarf", "headband", "beanie"),
    "glasses": ("yes",),
    "carrying": ("backpack", "bag", "suitcase", "box", "umbrella", "trolley", "phone",
                 "parcel", "basket", "laptop", "bottle", "cup", "camera", "folder", "badge"),
    "expression": ("smiling", "neutral", "serious", "laughing", "surprised", "focused"),
    "posture": ("standing", "sitting", "walking", "waving", "pointing"),
}
_SYNONYMS: dict[str, dict[str, str]] = {
    "gender": {"male": "man", "female": "woman", "boy": "child", "girl": "child", "lady": "woman",
               "gentleman": "man", "guy": "man", "kid": "child", "men": "man", "women": "woman",
               "young man": "man", "young woman": "woman", "adult male": "man", "adult female": "woman"},
    "headwear": {"baseball cap": "cap", "sun hat": "hat", "cowboy hat": "hat", "hard hat": "helmet",
                 "head scarf": "scarf", "headscarf": "scarf", "dupatta": "scarf", "hoodie": "hood"},
    "carrying": {"rucksack": "backpack", "handbag": "bag", "purse": "bag", "tote": "bag", "holdall": "bag",
                 "shopping bag": "bag", "sling bag": "bag", "messenger bag": "bag", "package": "parcel",
                 "luggage": "suitcase", "cart": "trolley", "mobile": "phone", "cell phone": "phone",
                 "smartphone": "phone", "mobile phone": "phone", "notebook": "laptop", "water bottle": "bottle",
                 "coffee": "cup", "coffee cup": "cup", "lanyard": "badge", "id card": "badge", "papers": "folder"},
    "expression": {"smile": "smiling", "happy": "smiling", "grin": "smiling", "grinning": "smiling",
                   "laugh": "laughing", "calm": "neutral", "relaxed": "neutral", "stern": "serious",
                   "frowning": "serious", "concentrating": "focused", "attentive": "focused"},
    "posture": {"stands": "standing", "stand": "standing", "sits": "sitting", "seated": "sitting",
                "walks": "walking", "walk": "walking", "waves": "waving", "wave": "waving",
                "points": "pointing", "point": "pointing"},
}
_NOTHING = ("none", "nothing", "no", "unknown", "n/a", "not visible", "unclear", "nil", "-", "")


def _colour_of(value: str) -> str | None:
    """The first colour word in a phrase, folded: 'dark navy jacket' → blue."""
    v = value.lower()
    for phrase, canon in sorted(_COLOUR_SYNONYMS.items(), key=lambda kv: -len(kv[0])):
        if phrase in v:
            return canon
    for c in _COLOURS:
        if re.search(r"\b" + c + r"\b", v):
            return c
    return None


_GARMENTS = ("t-shirt", "tshirt", "t shirt", "shirt", "polo", "kurta", "jacket", "hoodie", "sweater",
             "jumper", "blazer", "coat", "top", "dress", "saree", "sari", "suit", "vest", "blouse",
             "jersey", "cardigan", "trousers", "jeans", "shorts", "skirt", "pants", "chinos", "dhoti")


def _garment_of(value: str) -> str | None:
    v = value.lower()
    for g in _GARMENTS:
        if g in v:
            return {"tshirt": "t-shirt", "t shirt": "t-shirt", "sari": "saree", "pants": "trousers"}.get(g, g)
    return None


def normalise_fields(fields: dict[str, str]) -> dict[str, str]:
    """Fold a parsed answer into the vocabulary. Clothing keeps 'colour
    garment' with both parts folded ('dark navy jacket' → 'blue jacket');
    a colour alone is kept; a garment alone is kept. Everything the
    vocabulary does not know is dropped, as is nothing/unknown."""
    out: dict[str, str] = {}
    for key, raw in (fields or {}).items():
        v = (raw or "").strip().lower().strip(".")
        if key == "summary":
            continue
        if v in _NOTHING:
            continue
        if key in ("top", "bottom"):
            colour, garment = _colour_of(v), _garment_of(v)
            if colour == "denim" and garment in ("jeans", "denim"):
                garment = None                       # 'denim jeans' says it twice
            if colour and garment:
                out[key] = f"{colour} {garment}"
            elif colour:
                out[key] = colour
            elif garment:
                out[key] = garment
            continue
        if key == "age":
            m = re.search(r"(\d{2})\s*(?:s|'s|-|to|–)?\s*(\d{2})?", v)
            if "child" in v or "kid" in v:
                out[key] = "child"
            elif "teen" in v:
                out[key] = "teen"
            elif m:
                lo = int(m.group(1))
                out[key] = "60+" if lo >= 60 else ("teen" if lo < 20 else f"{lo // 10 * 10}s")
            continue
        if key == "glasses":
            if v.startswith("y") or "glasses" in v and not v.startswith("no"):
                out[key] = "yes"
            continue
        vocab = VOCAB.get(key)
        if not vocab:
            continue
        syn = _SYNONYMS.get(key, {})
        v = syn.get(v, v)
        if v in vocab:
            out[key] = v
            continue
        # a phrase that contains a vocabulary word or a synonym of one:
        # "wearing a cap" → cap, "stands near the desk" → standing
        for word in vocab:
            if re.search(r"\b" + re.escape(word) + r"\b", v):
                out[key] = word
                break
        else:
            for phrase, canon in sorted(syn.items(), key=lambda kv: -len(kv[0])):
                if re.search(r"\b" + re.escape(phrase) + r"\b", v):
                    out[key] = canon
                    break
    return out


def consensus(a: dict[str, str], b: dict[str, str]) -> dict[str, str]:
    """The fields two looks agree on. For clothing the colour must agree;
    the garment is kept when either look named one and they do not clash."""
    out: dict[str, str] = {}
    for key in set(a) & set(b):
        if key in ("top", "bottom"):
            ca, ga = (a[key].split(" ", 1) + [""])[:2]
            cb, gb = (b[key].split(" ", 1) + [""])[:2]
            ca_c, cb_c = (ca if ca in _COLOURS else ""), (cb if cb in _COLOURS else "")
            if ca_c and cb_c and ca_c != cb_c:
                continue
            colour = ca_c or cb_c
            garment = ga or gb if (not ga or not gb or ga == gb) else ""
            if colour or garment:
                out[key] = " ".join(x for x in (colour, garment) if x)
        elif a[key] == b[key]:
            out[key] = a[key]
    return out


def crop_person(jpeg: bytes, box: list[float], *, margin: float = 0.25,
                min_side: int = 96) -> bytes | None:
    """Cut the person's box (normalized x, y, w, h) out of a frame with a
    margin, as JPEG. None when the crop would be too small to describe
    or the image cannot be read."""
    try:
        import io
        from PIL import Image

        im = Image.open(io.BytesIO(jpeg))
        im.load()
        W, H = im.size
        x, y, w, h = box
        mx, my = w * margin, h * margin
        left, top = max(0, int((x - mx) * W)), max(0, int((y - my) * H))
        right, bottom = min(W, int((x + w + mx) * W)), min(H, int((y + h + my) * H))
        if right - left < min_side or bottom - top < min_side:
            return None
        out = io.BytesIO()
        im.crop((left, top, right, bottom)).convert("RGB").save(out, format="JPEG", quality=92)
        return out.getvalue()
    except Exception:  # noqa: BLE001 — a bad frame is "no crop", never a crash
        return None


#: Second look at a person this long after the first; fields that agree
#: are the card, and only those are spoken.
CONFIRM_GAP_S = 2.5


# ── parsing the person answer ───────────────────────────────────────

def parse_person_fields(text: str) -> dict[str, str]:
    """``gender=man; age=30s; top=red t-shirt; …`` → a dict of the known
    keys, values trimmed and lower-cased, ``unknown``/``none``/empty
    dropped. A free-text answer that carries no ``key=value`` pairs comes
    back as ``{"summary": <first 90 chars>}`` so the card still says
    something rather than nothing."""
    out: dict[str, str] = {}
    raw = (text or "").strip().strip("`").strip()
    for part in re.split(r"[;\n]+", raw):
        if "=" not in part and ":" not in part:
            continue
        sep = "=" if "=" in part else ":"
        key, _, value = part.partition(sep)
        key = key.strip().strip("*-• ").lower()
        value = value.strip().strip(".").strip().lower()
        if key not in PERSON_KEYS or not value:
            continue
        if value in ("unknown", "none", "n/a", "no", "not visible", "unclear"):
            if key == "glasses" and value == "no":
                continue
            continue
        out[key] = value[:40]
    if not out and raw:
        summary = re.sub(r"\s+", " ", raw)[:90].strip()
        # A real sentence, not "unknown" or a stray token.
        if len(summary.split()) >= 3 and summary.lower() not in ("i don't know", "not sure"):
            out["summary"] = summary
    return out


def person_summary(fields: dict[str, str]) -> str:
    """The one line that rides the box and is spoken for a newcomer:
    ``man, 30s, red t-shirt, backpack`` — the fields that matter most, in
    a fixed order, skipping what the model could not tell."""
    if "summary" in fields and len(fields) == 1:
        return fields["summary"]
    bits: list[str] = []
    for key in ("gender", "age", "top", "headwear", "glasses", "carrying", "posture"):
        v = fields.get(key)
        if not v:
            continue
        if key == "glasses":
            if v.startswith("y"):
                bits.append("glasses")
            continue
        if key == "headwear" and v in ("none", "no"):
            continue
        if key == "carrying" and v in ("none", "nothing"):
            continue
        bits.append(v)
    return ", ".join(bits)


_STOPWORDS = frozenset(
    "the a an and or of in on at to with for from by is are was were be been being "
    "this that these those there here it its his her their one two three some front "
    "before behind while as into onto near next who what".split())


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[:-len(suffix)]
    return word


def _words(text: str) -> set[str]:
    """Content words, crudely stemmed: 'stands' and 'standing' are one word,
    'the' and 'a' are none."""
    return {_stem(w) for w in re.findall(r"[a-z0-9']+", (text or "").lower())
            if len(w) > 2 and w not in _STOPWORDS}


def same_sentence(a: str, b: str, threshold: float = SPEAK_SAME_THRESHOLD) -> bool:
    """Word-set Jaccard similarity at or above ``threshold``: the model
    rephrasing the same view ("a man in a blue shirt stands…" / "one man
    wearing a blue shirt is standing…") is not news worth saying again."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return a.strip().lower() == b.strip().lower()
    return len(wa & wb) / len(wa | wb) >= threshold


# ── state ────────────────────────────────────────────────────────────

@dataclass
class PersonCard:
    track_id: Any
    first_seen: float
    last_seen: float
    box: list[float]
    score: float = 0.0
    described_at: float | None = None
    fields: dict[str, str] = field(default_factory=dict)
    summary: str = ""
    greeted: bool = False
    asking: bool = False
    looks: list[dict[str, str]] = field(default_factory=list)
    confirmed: bool = False

    def to_wire(self) -> dict[str, Any]:
        return {"track_id": self.track_id, "box": self.box, "score": round(self.score, 3),
                "fields": self.fields, "summary": self.summary,
                "seen_s": round(max(0.0, self.last_seen - self.first_seen), 1),
                "described": self.described_at is not None,
                "confirmed": self.confirmed, "looks": len(self.looks)}


@dataclass
class MirrorStats:
    captions: int = 0
    #: Scene lines answered by core's shared description (no call of our own).
    core_answers: int = 0
    cards: int = 0
    spoken: int = 0
    vlm_ms_last: float | None = None
    vlm_ms_avg: float | None = None
    started_at: float = field(default_factory=time.time)

    def note_vlm(self, ms: float) -> None:
        self.vlm_ms_last = round(ms)
        self.vlm_ms_avg = round(ms if self.vlm_ms_avg is None
                                else 0.8 * self.vlm_ms_avg + 0.2 * ms)

    def to_wire(self) -> dict[str, Any]:
        return {"captions": self.captions, "core_answers": self.core_answers,
                "cards": self.cards, "spoken": self.spoken,
                "vlm_ms_last": self.vlm_ms_last, "vlm_ms_avg": self.vlm_ms_avg,
                "uptime_s": round(time.time() - self.started_at)}


Frame = Callable[[str], Awaitable[bytes | None]]
Crop = Callable[[str, Any], Awaitable[bytes | None]]
Vision = Callable[[bytes, str | None], Awaitable[str | None]]
Speech = Callable[[str], Awaitable[bytes | None]]
Publish = Callable[[dict[str, Any]], None]
#: Core's shared description: async (max_age_s) -> dict | None (RFC-0004).
Description = Callable[[float], Awaitable[dict[str, Any] | None]]


class MirrorSession:
    """One camera's live narration. Start with :meth:`start`; feed it the
    relayed Tier-0 frames with :meth:`observe_tracks`; keep it alive with
    :meth:`heartbeat`; it stops itself when the page stops calling."""

    def __init__(self, camera_id: str, *, frame: Frame, crop: Crop | None,
                 vision: Vision, speech: Speech | None, publish: Publish,
                 voice: bool = True, clock: Callable[[], float] = time.monotonic,
                 scene_every_s: float = SCENE_EVERY_S, hires: Frame | None = None,
                 description: Description | None = None) -> None:
        self.camera_id = camera_id
        self._frame = frame
        # The scene line comes from core's one shared description when
        # wired (RFC-0004 step 4): the visit the enrichment tier just
        # described, or one gated look shared with every other asker.
        # Only the person confirmation look is still this loop's own.
        self._description = description
        self._crop = crop
        # The main stream's full-resolution frame (core's capture pool,
        # ~0.15 s warm). A person is cut out of THIS, with a margin: the
        # detector's sub-stream frame is 704×576 here, and the search
        # pipeline's captions come from full-resolution crops — that is
        # the difference in fineness the Mirror had.
        self._hires = hires
        self._vision = vision
        self._speech = speech
        self._publish = publish
        self.voice = voice
        self._clock = clock
        self._scene_every = max(1.0, float(scene_every_s))
        self.cards: dict[Any, PersonCard] = {}
        self.tracks: list[dict[str, Any]] = []
        self.tracks_at: float | None = None
        self.caption: str = ""
        self.caption_at: float | None = None
        self.caption_changed_at: float | None = None
        self.stats = MirrorStats()
        self.last_heartbeat = self._clock()
        self.last_spoken: str = ""
        self.speaking_until: float = 0.0
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ── lifecycle ────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.last_heartbeat = self._clock()
        self._task = asyncio.get_event_loop().create_task(self._run())
        self._publish({"mirror": {"state": "started", "camera": self.camera_id}})

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._publish({"mirror": {"state": "stopped", "camera": self.camera_id}})

    def heartbeat(self) -> None:
        self.last_heartbeat = self._clock()

    def to_wire(self) -> dict[str, Any]:
        return {"camera": self.camera_id, "running": self.running, "voice": self.voice,
                "caption": self.caption, "cards": [c.to_wire() for c in self.cards.values()],
                "people": sum(1 for t in self.tracks if t.get("label") == "person"),
                "stats": self.stats.to_wire()}

    # ── input: the relayed Tier-0 boxes ──────────────────────────

    def observe_tracks(self, frame: dict[str, Any]) -> None:
        """Called for every relayed frame of this camera. Keeps the cards in
        step with the tracks: a new person gets a card, a vanished one loses
        it after ``CARD_TTL_S``. Cheap: no model call happens here."""
        now = self._clock()
        tracks = [t for t in (frame.get("tracks") or []) if isinstance(t, dict)]
        self.tracks = tracks
        self.tracks_at = now
        seen: set[Any] = set()
        for t in tracks:
            if t.get("label") != "person" or t.get("id") is None:
                continue
            box = t.get("box") or []
            if len(box) != 4:
                continue
            seen.add(t["id"])
            card = self.cards.get(t["id"])
            if card is None:
                card = PersonCard(track_id=t["id"], first_seen=now, last_seen=now,
                                  box=[float(x) for x in box], score=float(t.get("score") or 0))
                self.cards[t["id"]] = card
            else:
                card.last_seen = now
                card.box = [float(x) for x in box]
                card.score = float(t.get("score") or 0)
        for tid in [k for k, c in self.cards.items() if now - c.last_seen > CARD_TTL_S]:
            del self.cards[tid]

    # ── policy: what to ask the model next ───────────────────────

    def next_person(self) -> PersonCard | None:
        """The person most worth a model call now, or None: never described
        first (oldest first), then the one whose description is stalest past
        ``PERSON_REFRESH_S``. A person must be in frame ``PERSON_MIN_AGE_S``
        and cover ``PERSON_MIN_AREA`` of it."""
        now = self._clock()
        eligible = [c for c in self.cards.values()
                    if not c.asking and now - c.first_seen >= PERSON_MIN_AGE_S
                    and now - c.last_seen <= 1.5
                    and c.box[2] * c.box[3] >= PERSON_MIN_AREA]
        fresh = [c for c in eligible if c.described_at is None]
        if fresh:
            return min(fresh, key=lambda c: c.first_seen)
        confirming = [c for c in eligible if not c.confirmed and len(c.looks) == 1
                      and now - (c.described_at or 0) >= CONFIRM_GAP_S]
        if confirming:
            return min(confirming, key=lambda c: c.described_at or 0)
        stale = [c for c in eligible if now - (c.described_at or 0) >= PERSON_REFRESH_S]
        if stale:
            return min(stale, key=lambda c: c.described_at or 0)
        return None

    def scene_due(self) -> bool:
        now = self._clock()
        if self.caption_at is None:
            return True
        busy = bool(self.tracks) and self.tracks_at is not None and now - self.tracks_at < 3.0
        return now - self.caption_at >= (self._scene_every if busy else SCENE_IDLE_EVERY_S)

    def should_speak(self, text: str) -> bool:
        if not self.voice or not text or self._speech is None:
            return False
        if self._clock() < self.speaking_until:
            return False
        return not same_sentence(text, self.last_spoken)

    # ── the loop ─────────────────────────────────────────────────

    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                if self._clock() - self.last_heartbeat > HEARTBEAT_TTL_S:
                    logger.info("mirror %s: no page for %.0fs; stopping",
                                self.camera_id, HEARTBEAT_TTL_S)
                    self._task = None
                    self._publish({"mirror": {"state": "stopped", "camera": self.camera_id,
                                              "reason": "no viewer"}})
                    return
                did = await self.step()
                if not did:
                    await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the loop must survive a bad frame
            logger.exception("mirror %s: loop failed; restarting in 2s", self.camera_id)
            await asyncio.sleep(2.0)
            if not self._stop.is_set():
                self._task = asyncio.get_event_loop().create_task(self._run())

    async def step(self) -> bool:
        """One decision: describe a person, or the scene, or nothing. Returns
        True when a model call was made."""
        card = self.next_person()
        if card is not None:
            await self._describe_person(card)
            return True
        if self.scene_due():
            await self._describe_scene()
            return True
        return False

    async def _person_image(self, card: PersonCard) -> bytes | None:
        """The sharpest picture of this person available now: the full-
        resolution frame cut to their box, else Tier-0's best crop of the
        track, else the detector's frame."""
        if self._hires is not None:
            try:
                full = await self._hires(self.camera_id)
            except Exception:  # noqa: BLE001 — the hi-res path is a nicety
                full = None
            if full:
                cut = crop_person(full, card.box)
                if cut:
                    return cut
        if self._crop is not None:
            try:
                cut = await self._crop(self.camera_id, card.track_id)
                if cut:
                    return cut
            except Exception:  # noqa: BLE001
                pass
        return await self._frame(self.camera_id)

    async def _describe_person(self, card: PersonCard) -> None:
        card.asking = True
        try:
            image = await self._person_image(card)
            if image is None:
                card.described_at = self._clock()   # do not spin on a dead camera
                return
            t0 = self._clock()
            answer = await self._vision(image, PERSON_PROMPT)
            ms = (self._clock() - t0) * 1000.0
            self.stats.note_vlm(ms)
            card.described_at = self._clock()
            if card.track_id not in self.cards:
                return                              # gone while we looked
            look = normalise_fields(parse_person_fields(answer or ""))
            card.looks.append(look)
            if len(card.looks) == 1:
                # First look: shown, marked unconfirmed, not spoken.
                card.fields = look
                card.summary = person_summary(look)
            else:
                agreed = consensus(card.looks[-2], card.looks[-1])
                card.fields = agreed
                card.summary = person_summary(agreed)
                card.confirmed = True
                if agreed:
                    self.stats.cards += 1
            payload: dict[str, Any] = {"card": card.to_wire(), "vlm_ms": round(ms),
                                       "stats": self.stats.to_wire()}
            # Spoken only once the two looks agree on at least two things:
            # a greeting that is wrong is worse than none.
            if card.confirmed and len(card.fields) >= 2 and card.summary and not card.greeted:
                card.greeted = True
                greeting = f"Hello! I see {_article(card.summary)}."
                payload["greeting"] = greeting
                await self._speak(greeting, payload)
            self._publish({"mirror": payload})
        finally:
            card.asking = False

    async def _describe_scene_via_core(self) -> bool:
        """The scene line from core. False when core did not answer, so
        the loop takes its own look and the stats say it did."""
        t0 = self._clock()
        try:
            body = await self._description(self._scene_every)
        except Exception:  # noqa: BLE001
            body = None
        if not body or not body.get("text"):
            return False
        text = str(body["text"]).strip().strip('"')
        ms = (self._clock() - t0) * 1000.0
        if body.get("model_called"):
            self.stats.note_vlm(ms)
        self.stats.core_answers += 1
        self.caption_at = self._clock()
        if not text:
            return True
        if self.caption and same_sentence(text, self.caption) \
                and self.caption_changed_at is not None \
                and self._clock() - self.caption_changed_at < CAPTION_HOLD_S:
            self._publish({"mirror": {"vlm_ms": round(ms), "stats": self.stats.to_wire(),
                                      "same": True, "source": body.get("source")}})
            return True
        self.caption = text
        self.caption_changed_at = self._clock()
        self.stats.captions += 1
        payload: dict[str, Any] = {"caption": text, "vlm_ms": round(ms),
                                   "people": sum(1 for t in self.tracks if t.get("label") == "person"),
                                   "stats": self.stats.to_wire(), "source": body.get("source")}
        if body.get("source") == "stale":
            payload["stale_s"] = body.get("age_s")
        if self.should_speak(text) and body.get("source") != "stale":
            await self._speak(text, payload)
        self._publish({"mirror": payload})
        return True

    async def _describe_scene(self) -> None:
        if self._description is not None:
            if await self._describe_scene_via_core():
                return
        image = None
        if self._hires is not None:
            try:
                image = await self._hires(self.camera_id)
            except Exception:  # noqa: BLE001
                image = None
        if image is None:
            image = await self._frame(self.camera_id)
        self.caption_at = self._clock()
        if image is None:
            self._publish({"mirror": {"caption": "", "no_frame": True}})
            return
        t0 = self._clock()
        live = self.tracks if (self.tracks_at is not None and self._clock() - self.tracks_at < 3.0) else []
        text = (await self._vision(image, scene_prompt(live)) or "").strip().strip('"')
        ms = (self._clock() - t0) * 1000.0
        self.stats.note_vlm(ms)
        self.caption_at = self._clock()
        if not text:
            return
        # A rephrase of what is on screen stays off it: the subtitle changes
        # when the view does, not every time the model finds new words.
        if self.caption and same_sentence(text, self.caption) \
                and self.caption_changed_at is not None \
                and self._clock() - self.caption_changed_at < CAPTION_HOLD_S:
            self._publish({"mirror": {"vlm_ms": round(ms), "stats": self.stats.to_wire(),
                                      "same": True}})
            return
        self.caption = text
        self.caption_changed_at = self._clock()
        self.stats.captions += 1
        payload: dict[str, Any] = {"caption": text, "vlm_ms": round(ms),
                                   "people": sum(1 for t in self.tracks if t.get("label") == "person"),
                                   "stats": self.stats.to_wire()}
        if self.should_speak(text):
            await self._speak(text, payload)
        self._publish({"mirror": payload})

    async def _speak(self, text: str, payload: dict[str, Any]) -> None:
        if self._speech is None or not self.voice:
            return
        try:
            audio = await self._speech(text)
        except Exception:  # noqa: BLE001 — speech is optional
            audio = None
        if not audio:
            return
        payload["audio_b64"] = base64.b64encode(audio).decode("ascii")
        payload["spoken"] = text
        self.last_spoken = text
        self.stats.spoken += 1
        # WAV at 22.05 kHz 16-bit mono: bytes / 44100 ≈ seconds. Hold the
        # floor until it has played plus a breath.
        self.speaking_until = self._clock() + len(audio) / 44100.0 + SPEAK_MIN_GAP_S


def _article(summary: str) -> str:
    s = summary.strip()
    if not s:
        return "someone"
    first = s.split(",")[0].split()[0] if s.split(",")[0].split() else ""
    if first in ("man", "woman", "child", "boy", "girl", "person"):
        return ("an " if first[0] in "aeiou" else "a ") + s
    return "someone: " + s
