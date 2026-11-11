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

SCENE_PROMPT = (
    "You are OpenNVR Mirror at a technology show, describing a live camera to "
    "the people in front of it. ONE concrete sentence, at most 25 words: how "
    "many people, the colour and kind of their clothes, what each holds or does "
    "(waving, phone, bag, laptop), notable objects. Name colours. Do not use "
    "'casual attire', 'setting', 'suggesting', 'appears', 'seemingly'. No "
    "preamble, no quotes.")
PERSON_PROMPT = (
    "Look at the person in this image. Answer on ONE line as key=value pairs "
    "separated by ';' with exactly these keys: gender (man/woman/child/unknown); "
    "age (an age range such as 20s or 30-40); top (colour and type of upper "
    "clothing); bottom (colour and type); headwear (none or what); glasses "
    "(yes/no); carrying (none or what); expression (one word). "
    "Use unknown when you cannot tell. No other text.")
PERSON_KEYS = ("gender", "age", "top", "bottom", "headwear", "glasses",
               "carrying", "expression")


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
    for key in ("gender", "age", "top", "headwear", "glasses", "carrying"):
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

    def to_wire(self) -> dict[str, Any]:
        return {"track_id": self.track_id, "box": self.box, "score": round(self.score, 3),
                "fields": self.fields, "summary": self.summary,
                "seen_s": round(max(0.0, self.last_seen - self.first_seen), 1),
                "described": self.described_at is not None}


@dataclass
class MirrorStats:
    captions: int = 0
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
        return {"captions": self.captions, "cards": self.cards, "spoken": self.spoken,
                "vlm_ms_last": self.vlm_ms_last, "vlm_ms_avg": self.vlm_ms_avg,
                "uptime_s": round(time.time() - self.started_at)}


Frame = Callable[[str], Awaitable[bytes | None]]
Crop = Callable[[str, Any], Awaitable[bytes | None]]
Vision = Callable[[bytes, str | None], Awaitable[str | None]]
Speech = Callable[[str], Awaitable[bytes | None]]
Publish = Callable[[dict[str, Any]], None]


class MirrorSession:
    """One camera's live narration. Start with :meth:`start`; feed it the
    relayed Tier-0 frames with :meth:`observe_tracks`; keep it alive with
    :meth:`heartbeat`; it stops itself when the page stops calling."""

    def __init__(self, camera_id: str, *, frame: Frame, crop: Crop | None,
                 vision: Vision, speech: Speech | None, publish: Publish,
                 voice: bool = True, clock: Callable[[], float] = time.monotonic,
                 scene_every_s: float = SCENE_EVERY_S) -> None:
        self.camera_id = camera_id
        self._frame = frame
        self._crop = crop
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

    async def _describe_person(self, card: PersonCard) -> None:
        card.asking = True
        try:
            image = None
            if self._crop is not None:
                try:
                    image = await self._crop(self.camera_id, card.track_id)
                except Exception:  # noqa: BLE001 — the crop is a nicety
                    image = None
            if image is None:
                image = await self._frame(self.camera_id)
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
            fields = parse_person_fields(answer or "")
            if fields:
                card.fields = fields
                card.summary = person_summary(fields)
                self.stats.cards += 1
            payload: dict[str, Any] = {"card": card.to_wire(), "vlm_ms": round(ms),
                                       "stats": self.stats.to_wire()}
            if card.summary and not card.greeted:
                card.greeted = True
                greeting = f"Hello! I see {_article(card.summary)}."
                payload["greeting"] = greeting
                await self._speak(greeting, payload)
            self._publish({"mirror": payload})
        finally:
            card.asking = False

    async def _describe_scene(self) -> None:
        image = await self._frame(self.camera_id)
        self.caption_at = self._clock()
        if image is None:
            self._publish({"mirror": {"caption": "", "no_frame": True}})
            return
        t0 = self._clock()
        text = (await self._vision(image, SCENE_PROMPT) or "").strip().strip('"')
        ms = (self._clock() - t0) * 1000.0
        self.stats.note_vlm(ms)
        self.caption_at = self._clock()
        if not text:
            return
        self.caption = text
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
