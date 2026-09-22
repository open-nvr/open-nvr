# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""When the door should actually ring, and with what.

An alert and a chime are not the same event, and treating them as one
is what makes doorbells annoying. Every face at the door is worth
RECORDING; only some are worth INTERRUPTING somebody for. A household
whose bell rings each time a resident walks in stops hearing the bell,
and then it does not work for the stranger either.

So the alert still fires for every visit — the feed, the history and
the operator inbox are unaffected by anything in this module. This
decides one narrower question: does something ring, and what does it
play.

Three rules, and each one is a thing real doorbells get wrong
-------------------------------------------------------------
1. **Who it is decides the sound.** A recognised resident is announced
   silently. A visitor rings. A watchlist match is not a doorbell at
   all, it is an alarm. The default table below is opinionated on
   purpose; it is a starting point an operator edits, not a law.
2. **Quiet hours silence the bell, not the alarm.** A delivery at
   03:00 goes in the log and does not wake the house. A stranger at
   03:00 does — suppressing that would be a burglar alarm that
   observes bedtime. Only the ``alarm`` tone overrides quiet hours,
   which is exactly the line between "someone is here" and "something
   is wrong".
3. **A bell that rings twelve times is noise.** The alert dedup window
   stops repeat ALERTS for the same person; this has its own, longer
   window, because being told again in a feed costs nothing and being
   rung at again costs attention.

Ringing is somebody else's job, deliberately
--------------------------------------------
This module decides; it does not make a noise. The decision rides in
the alert envelope, so whatever the household actually rings — the
alerts-subscriber relay into ntfy or Telegram, a Home Assistant
automation over the MQTT bridge, a webhook to a smart speaker — plays
the right tone at the right time. An app that tried to own the speaker
would work on exactly one deployment.

And when it decides NOT to ring, it says why, in words, in the same
envelope. A doorbell that silently chose to stay quiet is
indistinguishable from a broken one, and the operator debugging it at
09:00 has nothing to go on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time as _time
from typing import Any

logger = logging.getLogger("smart_doorbell.chime")

#: What can be asked for. ``none`` is a real choice, not a disabled
#: state: "announce this person without ringing" is the setting a
#: household wants for the people who live there.
CHIME_TONES: tuple[str, ...] = ("none", "chime", "ding_dong", "alarm")

#: The starting point, by person category (see ``PERSON_CATEGORIES``),
#: plus ``unknown`` for a face nobody has enrolled.
#:
#: Residents are silent because a bell that rings when the family comes
#: home is the bell people stop hearing. People with business at the
#: door ring. A watchlist match alarms — it is the one recognised face
#: that must be louder than a stranger.
DEFAULT_TONES: dict[str, str] = {
    "family": "none",
    "resident": "none",
    "friend": "chime",
    "staff": "chime",
    "contractor": "ding_dong",
    "visitor": "ding_dong",
    "watchlist": "alarm",
    "unknown": "ding_dong",
}

#: Tones that quiet hours do not silence. Everything else waits until
#: morning; this is what still wakes the house.
OVERRIDE_QUIET: frozenset[str] = frozenset({"alarm"})


@dataclass(frozen=True)
class ChimeDecision:
    """What to ring, or why nothing rang."""

    tone: str
    ring: bool
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"tone": self.tone, "ring": self.ring, "reason": self.reason}


def parse_quiet_hours(window: str | None) -> tuple[_time, _time] | None:
    """``"22:00-07:00"`` → the two clock times, or None.

    A window that does not parse is treated as no quiet hours at all
    and logged. Silently ignoring it would be worse: the operator set
    it expecting silence, and would find out at 03:00.
    """
    text = (window or "").strip()
    if not text:
        return None
    try:
        start_s, end_s = text.split("-", 1)
        start = _time.fromisoformat(start_s.strip())
        end = _time.fromisoformat(end_s.strip())
    except ValueError:
        logger.warning("quiet_hours %r is not HH:MM-HH:MM; ignoring it", window)
        return None
    return start, end


def in_quiet_hours(now: datetime, window: tuple[_time, _time] | None) -> bool:
    """Is *now* inside the window, including one that crosses midnight?

    22:00-07:00 is the normal shape of a quiet window and the one an
    interval check gets wrong: start > end means the window wraps, and
    03:00 is inside it.
    """
    if window is None:
        return False
    start, end = window
    at = now.time()
    if start == end:
        return False              # a zero-width window silences nothing
    if start < end:
        return start <= at < end
    return at >= start or at < end


class ChimePolicy:
    """Decides, and remembers when it last rang.

    Pure except for the per-key last-rang clock, which is what makes
    the re-ring window work. Kept here rather than in the app so the
    whole decision — tone, quiet hours, cooldown — is testable in one
    place without building a doorbell.
    """

    def __init__(self, *, tones: dict[str, str] | None = None,
                 quiet_hours: str | None = None,
                 rechime_seconds: float = 300.0,
                 enabled: bool = True) -> None:
        self.enabled = bool(enabled)
        self.rechime_seconds = max(0.0, float(rechime_seconds))
        self._quiet = parse_quiet_hours(quiet_hours)
        self._quiet_text = (quiet_hours or "").strip()
        self.tones = dict(DEFAULT_TONES)
        for key, tone in (tones or {}).items():
            name = str(tone).strip().lower()
            if name not in CHIME_TONES:
                logger.warning("chime tone %r for %r is not one of %s; ignoring",
                               tone, key, ", ".join(CHIME_TONES))
                continue
            self.tones[str(key).strip().lower()] = name
        self._last_rang: dict[str, float] = {}

    def tone_for(self, category: str | None, recognized: bool) -> str:
        if not recognized:
            return self.tones.get("unknown", "ding_dong")
        return self.tones.get((category or "").strip().lower(), "chime")

    def decide(self, *, key: str, category: str | None, recognized: bool,
               now: float, wall: datetime | None = None) -> ChimeDecision:
        """Ring or not, for this visit.

        ``key`` buckets the cooldown — the app passes (camera, person),
        so the same person at two doors may ring twice and two people at
        one door are not folded into each other.
        """
        tone = self.tone_for(category, recognized)
        if not self.enabled:
            return ChimeDecision(tone, False, "the chime is switched off")
        if tone == "none":
            who = (category or "").strip().lower() or "unknown"
            return ChimeDecision(tone, False,
                                 f"{who} is announced without ringing")

        if tone not in OVERRIDE_QUIET and in_quiet_hours(
                wall or datetime.now(), self._quiet):
            # Recorded and alerted, just not rung. The alarm tone does
            # not reach here, which is the whole point of the exception.
            return ChimeDecision(tone, False,
                                 f"quiet hours ({self._quiet_text})")

        last = self._last_rang.get(key)
        if last is not None and self.rechime_seconds > 0:
            waited = now - last
            if waited < self.rechime_seconds:
                return ChimeDecision(
                    tone, False,
                    f"rang {int(waited)}s ago, waiting "
                    f"{int(self.rechime_seconds)}s between rings")

        # Only a ring updates the clock. Counting a suppressed one would
        # push the next real ring further away every time somebody
        # walked past during quiet hours.
        self._last_rang[key] = now
        return ChimeDecision(tone, True, "")
