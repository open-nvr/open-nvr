# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the agent says while it works — "let me check the gate camera".

A person who has to look something up says what they are about to do,
then goes quiet and does it. The agent knows *what it is about to do* the
moment the first LLM pass returns a tool call — the tool and its
arguments (camera, time window, label, plate) — and that is the only
moment worth filling: what follows (the tool, the second LLM pass, TTS) is
the slow part.

So the sentence is not a paraphrase of the question; it states the
action, from the tool call's own arguments — which are the question's
context, already extracted. One sentence shape per tool, slots filled.
No LLM is involved unless ``filler_source: model`` asks the model to
write the line in the SAME first pass (a few extra output tokens, no
extra call); even then a template is the fallback.

Cost per turn: one short Piper synthesis (cached by text), run in
parallel with the tool, and one message on the page's /updates socket.
Nothing is added to the answer's own path, and the answer cuts the
filler if it arrives first.

Guardrails: only when the expected wait (the agent's own recent stage
timings for that tool + the second LLM pass + TTS) is long enough to
need it; at most one per turn; never the same opener twice running.
"""
from __future__ import annotations

import logging
import random
import re
from collections import deque
from datetime import datetime
from typing import Any, Iterable

logger = logging.getLogger("camera_agent.fillers")

# Expected cost of a tool before any measurement exists (ms). Vision and
# footage are slow; registry and ring lookups are not worth a sentence.
DEFAULT_TOOL_MS: dict[str, int] = {
    "describe_camera": 2500, "describe_window": 4000, "describe_event": 3000,
    "detect_objects": 1200, "recognize_faces": 1500, "camera_snapshot": 300,
    "search_footage": 1500, "search_history": 800,
    "recent_events": 150, "recent_plates": 150,
    "list_apps": 200, "app_status": 400, "recent_app_alerts": 100,
    "list_people": 100,
}
DEFAULT_LLM_MS = 1500
DEFAULT_TTS_MS = 600

_OPENERS = ("Let me", "One moment, I'll", "Sure — I'll", "Okay, I'll")


def humanize_camera(camera_id: str | None, cameras: Iterable[Any] = ()) -> str:
    """"cam1" → "camera one"; a roster entry's role wins when it is short
    ("front door", "gate")."""
    if not camera_id:
        return "the cameras"
    cid = str(camera_id).strip()
    if cid.lower() in ("all", "*", ""):
        return "all cameras"
    for cam in cameras:
        if str(getattr(cam, "camera_id", "")) == cid:
            role = str(getattr(cam, "role", "") or "").strip()
            if role and len(role.split()) <= 3:
                return f"the {role} camera" if "camera" not in role.lower() else f"the {role}"
    m = re.fullmatch(r"(?i)cam(?:era)?[\s_-]*(\d+)", cid)
    if m:
        return f"camera {_number_words(int(m.group(1)))}"
    return f"the {cid.replace('_', ' ').replace('-', ' ')} camera"


def _number_words(n: int) -> str:
    words = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
             "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
             "seventeen", "eighteen", "nineteen", "twenty"]
    return words[n] if 0 <= n < len(words) else str(n)


def spell_plate(plate: str) -> str:
    """"66HH07" → "6 6 H H 0 7" — spoken letter by letter, as people read plates."""
    return " ".join(ch.upper() for ch in str(plate) if ch.isalnum())


def _clock(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    hour12 = dt.hour % 12 or 12
    suffix = "am" if dt.hour < 12 else "pm"
    return f"{hour12} {suffix}" if dt.minute == 0 else f"{hour12}:{dt.minute:02d} {suffix}"


def time_window(args: dict[str, Any]) -> str:
    start, end = _clock(args.get("start_time")), _clock(args.get("end_time"))
    if start and end:
        return f" between {start} and {end}"
    if start:
        return f" since {start}"
    secs = args.get("window_seconds")
    try:
        secs = float(secs) if secs not in (None, "") else None
    except (TypeError, ValueError):
        secs = None
    if secs:
        if secs < 90:
            return " in the last minute"
        if secs < 3600:
            return f" in the last {int(round(secs / 60))} minutes"
        hours = secs / 3600
        return " in the last hour" if hours < 1.5 else f" in the last {int(round(hours))} hours"
    mins = args.get("within_minutes")
    try:
        mins = int(mins) if mins not in (None, "") else None
    except (TypeError, ValueError):
        mins = None
    if mins:
        return " in the last hour" if 45 <= mins <= 75 else f" in the last {mins} minutes"
    return ""


def template_line(tool: str, args: dict[str, Any], cameras: Iterable[Any] = (), opener: str = "Let me") -> str | None:
    """The sentence for one tool call, or None when the tool is not worth
    announcing (instant lookups, control verbs)."""
    args = args or {}
    cam = humanize_camera(args.get("camera_id") or args.get("camera"), cameras)
    ids = args.get("camera_ids")
    if isinstance(ids, list) and len(ids) > 1:
        cam = "all cameras" if len(ids) >= 3 else " and ".join(humanize_camera(c, cameras) for c in ids[:2])
    label = str(args.get("label") or "").strip().lower()
    what = f" for {label}s" if label and not label.endswith("s") else (f" for {label}" if label else "")
    plate = str(args.get("plate") or "").strip()
    q = str(args.get("question") or "").strip().rstrip("?.!")
    q_part = f" — {q[0].lower() + q[1:]}" if 0 < len(q.split()) <= 8 else ""
    if tool == "describe_camera":
        return f"{opener} take a look at {cam}{q_part}."
    if tool == "detect_objects":
        return f"{opener} check what's on {cam} right now."
    if tool == "recognize_faces":
        return f"{opener} see who is on {cam}."
    if tool == "search_history":
        if plate:
            return f"{opener} check {cam} for plate {spell_plate(plate)}{time_window(args)}."
        return f"{opener} check {cam}{what}{time_window(args)}."
    if tool == "describe_window":
        return f"{opener} look at what happened on {cam}{time_window(args)}{q_part}."
    if tool == "describe_event":
        return f"{opener} look at that event more closely{q_part}."
    if tool == "search_footage":
        kw = args.get("keywords")
        kw = ", ".join(str(k) for k in kw[:3]) if isinstance(kw, list) and kw else ""
        return f"{opener} search the footage{(' for ' + kw) if kw else ''}{time_window(args)}."
    if tool == "recent_plates" and plate:
        return f"{opener} check the plate reads for {spell_plate(plate)}{time_window(args)}."
    if tool == "app_status":
        app = str(args.get("app_id") or "").replace("-", " ")
        return f"{opener} ask the {app} app." if app else None
    if tool == "create_background_task":
        return f"{opener} start that in the background and report back."
    return None


class ThinkingAloud:
    """Per-runtime state: the opener rotation, the one-per-turn latch,
    stage timings from recent turns, and a small audio cache."""

    def __init__(self, *, min_ms: float = 1500.0, source: str = "template", enabled: bool = True):
        self.enabled = enabled
        self.min_ms = float(min_ms)
        self.source = source if source in ("template", "model") else "template"
        self._last_opener: str | None = None
        self._said_this_turn = False
        self.stage_ms: dict[str, deque] = {}
        self.audio: dict[str, str] = {}          # text → base64 wav
        self.recent: deque = deque(maxlen=30)     # decisions, for /interruptions-style tuning

    # ── turn lifecycle ────────────────────────────────────────────────
    def new_turn(self) -> None:
        self._said_this_turn = False

    def record_stages(self, trace: Iterable[dict[str, Any]], timings: dict[str, Any] | None = None) -> None:
        """Feed the turn's trace (step/detail/ms) and /converse timings so
        the next decision uses this site's real numbers."""
        for step in trace or []:
            ms = step.get("ms")
            if isinstance(ms, (int, float)):
                key = str(step.get("step") or "")
                self.stage_ms.setdefault(key, deque(maxlen=20)).append(float(ms))
        for key in ("tts", "stt"):
            ms = (timings or {}).get(key)
            if isinstance(ms, (int, float)):
                self.stage_ms.setdefault(key, deque(maxlen=20)).append(float(ms))

    def _median(self, key: str, default: float) -> float:
        xs = sorted(self.stage_ms.get(key, ()))
        if not xs:
            return float(default)
        return xs[len(xs) // 2]

    def expected_wait_ms(self, tool: str) -> float:
        return (self._median(tool, DEFAULT_TOOL_MS.get(tool, 500))
                + self._median("llm", DEFAULT_LLM_MS)
                + self._median("tts", DEFAULT_TTS_MS))

    # ── the decision ──────────────────────────────────────────────────
    def line_for(self, tool: str, args: dict[str, Any], *, cameras: Iterable[Any] = (),
                 model_line: str | None = None) -> str | None:
        """The sentence to speak before running ``tool`` — or None."""
        if not self.enabled or self._said_this_turn:
            return None
        expected = self.expected_wait_ms(tool)
        if expected < self.min_ms:
            self.recent.append({"tool": tool, "said": None, "why": f"expected {int(expected)} ms < {int(self.min_ms)}"})
            return None
        text = None
        why = "template"
        if self.source == "model" and model_line:
            candidate = " ".join(model_line.split()).strip()
            n = len(candidate.split())
            # A usable model line is short, a single sentence, and not the
            # answer itself (no digits-heavy content, no "I see").
            if 3 <= n <= 14 and candidate.count(".") <= 1 and not re.search(r"\d{2,}", candidate) \
                    and not re.match(r"(?i)^(i see|there (is|are)|yes|no)\b", candidate):
                text = candidate if candidate.endswith((".", "!")) else candidate + "."
                why = "model"
        if text is None:
            opener = random.choice([o for o in _OPENERS if o != self._last_opener] or list(_OPENERS))
            text = template_line(tool, args, cameras, opener)
            if text:
                self._last_opener = opener
        if not text:
            self.recent.append({"tool": tool, "said": None, "why": "no template for this tool"})
            return None
        self._said_this_turn = True
        self.recent.append({"tool": tool, "said": text, "why": f"{why}; expected {int(expected)} ms"})
        return text
