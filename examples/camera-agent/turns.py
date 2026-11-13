# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Interruptions that behave like a person's.

Smart Turn decides *when the user has finished*. This module decides the
other thing: while the agent is speaking, is a sound in the room a person
trying to take the floor from it — and should it yield?

A human answers that from several cues at once: the sound is speech, it
lasts more than a syllable, it isn't a backchannel ("mm-hm", "okay" —
which means *keep going*), and it is addressed to them. The gate below
recovers each cue from what the pipeline already has:

* **speech** — Silero's VAD start/stop frames (a cough or a chair never
  makes a VAD start/stop pair of any length);
* **duration** — the VAD pair must span ``min_ms``;
* **words** — the transcript must hold ``min_words`` real words once
  backchannels are stripped;
* **addressee** — :func:`addressed_to_agent`: does the phrase read as
  something said *to* the agent (its name, second person, an imperative
  or question, a correction, the site's own vocabulary) rather than a
  remark to someone else in the room?

Only a phrase that passes every gate interrupts; anything else is dropped
from the aggregation and the agent keeps talking. When the agent is
silent none of this applies — the first VAD start opens the turn exactly
as before, so end-of-turn detection (Smart Turn) is untouched.

The strategy is a :class:`BaseUserTurnStartStrategy`, so it plugs into the
1.8 user aggregator like the built-ins; it replaces
``VADUserTurnStartStrategy`` + ``MinWordsUserTurnStartStrategy`` because
those two cannot be combined (the first start wins and a later one can't
interrupt) and the min-words one alone starts every turn late.
"""
from __future__ import annotations

import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

logger = logging.getLogger("camera_agent.turns")

# Utterances that mean "I'm listening, go on" — never an interruption on
# their own, and never counted as words.
DEFAULT_BACKCHANNELS: frozenset[str] = frozenset({
    "yeah", "yes", "yep", "yup", "ya", "ok", "okay", "kay", "mhm", "mm", "mmm",
    "mm-hm", "mm-hmm", "uh-huh", "uhhuh", "uh", "um", "hmm", "hm", "aha",
    "right", "sure", "true", "i see", "got it", "go on", "alright", "fine",
    "cool", "nice", "wow", "oh", "ah", "huh", "really", "interesting",
})

# Words that mark speech as directed at the listener: second person,
# attention-getters, corrections, requests and questions.
_ADDRESSED_CUES: frozenset[str] = frozenset({
    "you", "your", "yours", "please", "stop", "wait", "hold", "hang", "no",
    "nope", "actually", "sorry", "excuse", "never", "listen", "hey", "hi",
    "hello", "look", "check", "show", "tell", "open", "play", "pull", "go",
    "give", "read", "find", "search", "describe", "what", "which", "where",
    "when", "who", "how", "why", "can", "could", "would", "will", "is",
    "are", "do", "does", "did", "other", "wrong", "not", "instead", "again",
    "repeat", "louder", "quiet", "mute", "cancel", "enough", "thanks", "thank",
})

# Third-person / narrative openers: the speaker is telling someone else
# about something, not talking to the agent.
_ASIDE_OPENERS: tuple[str, ...] = (
    "he ", "she ", "they ", "we ", "i think we", "i'll ", "i will ", "let's ",
    "lets ", "did you see", "have you seen", "remember when", "yesterday ",
    "last night", "my ", "our ", "the guy", "that guy", "this guy",
)

_WORD_RE = re.compile(r"[a-z0-9']+")


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower().replace("’", "'"))


def strip_backchannels(words: Iterable[str], backchannels: frozenset[str] = DEFAULT_BACKCHANNELS) -> list[str]:
    """Words with the listening noises removed ("yeah okay so which gate"
    → ["so", "which", "gate"])."""
    out = []
    ws = list(words)
    i = 0
    while i < len(ws):
        two = " ".join(ws[i:i + 2])
        if two in backchannels:
            i += 2
            continue
        if ws[i] in backchannels:
            i += 1
            continue
        out.append(ws[i])
        i += 1
    return out


def addressed_to_agent(text: str, *, agent_name: str = "", site_words: Iterable[str] = ()) -> tuple[bool, str]:
    """Does ``text`` read as something said TO the agent?

    Returns ``(verdict, reason)``. Deliberately generous: the cost of a
    missed interruption (the agent finishes a sentence it should not
    have) is smaller than the cost of a false one (the agent stops
    mid-answer for a remark to someone else), but the gate before this one
    already removed noise and backchannels, so what reaches here is real
    speech of a few words — the question is only *who it is for*.
    """
    raw = text.strip()
    low = raw.lower()
    words = _words(low)
    if not words:
        return False, "no words"
    name_words = [w for w in _words(agent_name) if len(w) > 2]
    if name_words and any(w in words for w in name_words):
        return True, "named the agent"
    if "?" in raw:
        return True, "a question"
    site = {w for s in site_words for w in _words(str(s)) if len(w) > 2}
    if site and any(w in site for w in words):
        return True, "site vocabulary"
    if any(low.startswith(o) for o in _ASIDE_OPENERS):
        return False, "an aside (third-person opener)"
    if words[0] in _ADDRESSED_CUES or any(w in _ADDRESSED_CUES for w in words[:3]):
        return True, "second person / request / correction"
    if len(words) >= 8:
        # A long, fluent sentence with none of the cues above is far more
        # likely a conversation with someone else than a command.
        return False, "long narrative without a cue"
    return False, "no cue that it was for the agent"


@dataclass
class InterruptionGate:
    """The rule set, and a small ring of recent decisions for diagnostics."""
    min_ms: float = 300.0
    min_words: int = 2
    addressee: bool = True
    agent_name: str = ""
    site_words: tuple[str, ...] = ()
    backchannels: frozenset[str] = DEFAULT_BACKCHANNELS
    recent: deque = field(default_factory=lambda: deque(maxlen=50))

    def judge(self, text: str, speech_ms: float | None, *, require_addressee: bool | None = None) -> tuple[bool, str]:
        """``require_addressee`` overrides the configured addressee check
        for one decision — off when the agent has already fallen silent by
        the time the transcript lands (there is nobody else being
        interrupted, so a real phrase is for the agent)."""
        words = _words(text)
        real = strip_backchannels(words, self.backchannels)
        addressee = self.addressee if require_addressee is None else (self.addressee and require_addressee)
        if speech_ms is not None and speech_ms < self.min_ms:
            verdict, why = False, f"too short ({int(speech_ms)} ms < {int(self.min_ms)} ms)"
        elif not real:
            verdict, why = False, "backchannel only"
        elif len(real) < self.min_words:
            verdict, why = False, f"{len(real)} word(s) < {self.min_words}"
        elif addressee:
            verdict, why = addressed_to_agent(text, agent_name=self.agent_name, site_words=self.site_words)
            why = ("addressed: " if verdict else "not addressed: ") + why
        else:
            verdict, why = True, "words and duration"
        self.recent.append({"ts": time.time(), "text": text, "speech_ms": speech_ms,
                            "interrupt": verdict, "reason": why})
        return verdict, why


def make_start_strategy(gate: InterruptionGate, *, on_decision: Callable[[dict[str, Any]], None] | None = None) -> Any:
    """Build the Pipecat start strategy (imported lazily so this module
    stays importable without Pipecat, like the rest of the agent)."""
    from pipecat.frames.frames import (
        BotStartedSpeakingFrame,
        BotStoppedSpeakingFrame,
        InterimTranscriptionFrame,
        TranscriptionFrame,
        VADUserStartedSpeakingFrame,
        VADUserStoppedSpeakingFrame,
    )
    from pipecat.turns.types import ProcessFrameResult
    from pipecat.turns.user_start import BaseUserTurnStartStrategy

    class GatedInterruptionStartStrategy(BaseUserTurnStartStrategy):
        """VAD opens a turn while the agent is silent; while it speaks, a
        turn (and the interruption) opens only for a phrase that passes
        the gate — see the module docstring."""

        def __init__(self, gate: InterruptionGate, **kwargs):
            super().__init__(enable_interruptions=True, **kwargs)
            self._gate = gate
            self._bot_speaking = False
            self._turn_active = False
            self._speech_start: float | None = None
            self._speech_ms: float | None = None
            self._spoke_over_bot = False

        async def handle_user_turn_started(self):
            self._turn_active = True
            self._speech_start = None
            self._speech_ms = None
            self._spoke_over_bot = False

        async def handle_user_turn_stopped(self):
            self._turn_active = False

        async def process_frame(self, frame):
            if isinstance(frame, BotStartedSpeakingFrame):
                self._bot_speaking = True
            elif isinstance(frame, BotStoppedSpeakingFrame):
                self._bot_speaking = False
            elif isinstance(frame, VADUserStartedSpeakingFrame):
                if self._turn_active:
                    return ProcessFrameResult.CONTINUE
                if not self._bot_speaking:
                    # The agent is silent: the first sound of speech opens
                    # the turn, exactly as VADUserTurnStartStrategy does, so
                    # Smart Turn sees the whole utterance.
                    await self.trigger_user_turn_started(enable_interruptions=False)
                    return ProcessFrameResult.STOP
                self._speech_start = getattr(frame, "timestamp", None) or time.time()
                self._speech_ms = None
                self._spoke_over_bot = True
            elif isinstance(frame, VADUserStoppedSpeakingFrame):
                if self._speech_start is not None:
                    end = getattr(frame, "timestamp", None) or time.time()
                    stop_secs = float(getattr(frame, "stop_secs", 0.0) or 0.0)
                    self._speech_ms = max(0.0, (end - self._speech_start - stop_secs) * 1000.0)
            elif isinstance(frame, TranscriptionFrame):
                return await self._on_transcript(frame)
            elif isinstance(frame, InterimTranscriptionFrame):
                # Interim text is not judged: the gate wants the whole phrase.
                return ProcessFrameResult.CONTINUE
            return ProcessFrameResult.CONTINUE

        async def _on_transcript(self, frame):
            if self._turn_active:
                return ProcessFrameResult.CONTINUE
            text = (frame.text or "").strip()
            if not text:
                return ProcessFrameResult.CONTINUE
            if not self._bot_speaking and not self._spoke_over_bot:
                # A transcript with no VAD-started turn (very soft speech,
                # or the turn ended before the STT answered): open the turn
                # on the text alone, like the built-in fallback does.
                await self.trigger_user_turn_started(enable_interruptions=False)
                return ProcessFrameResult.STOP
            verdict, why = self._gate.judge(text, self._speech_ms, require_addressee=self._bot_speaking)
            event = {"text": text, "speech_ms": self._speech_ms, "interrupt": verdict,
                     "reason": why, "bot_speaking": self._bot_speaking}
            if on_decision:
                try:
                    on_decision(event)
                except Exception:  # noqa: BLE001 — diagnostics never break the turn
                    pass
            self._speech_start = None
            self._spoke_over_bot = False
            if verdict:
                logger.info("interruption accepted (%s): %r", why, text)
                await self.trigger_user_turn_started(enable_interruptions=self._bot_speaking)
                return ProcessFrameResult.STOP
            logger.info("interruption ignored (%s): %r", why, text)
            self._speech_ms = None
            await self.trigger_reset_aggregation()
            return ProcessFrameResult.CONTINUE

    return GatedInterruptionStartStrategy(gate)
