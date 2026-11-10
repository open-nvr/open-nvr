# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Whether the box can afford enrichment right now (#583).

``enrichment_gate`` is the mechanism: per-adapter slots, a short wait
line, a breaker. This is the policy on top of it — a three-state answer
to "should any of that be sent at all", read from signals the box
already computes:

* the host monitor's last CPU sample and its ``cpu_high`` alert
  (``system_monitor_service``, sampled every 15 s for the UI anyway);
* each gate's smoothed latency, against ``EVENTS_ENRICHMENT_SLOW_CALL_S``
  — a model taking longer than that per call is behind the camera
  whatever the CPU figure says (a slow GPU model looks idle to psutil);
* any breaker open.

States, and what each one buys::

    NORMAL     live visits enriched through the gate; backfill allowed
               once things have been quiet for a while
    LIVE_ONLY  CPU above the soft threshold or an adapter running slow:
               captions and embeddings get NO wait line (a free slot or
               nothing), VQA keeps half of it — the colour and type the
               filters match on are worth more than a sentence when
               there is budget for one call, not three; backfill held
    PAUSED     the monitor's own cpu_high alert, or a breaker open:
               nothing is sent, nothing waits; backfill held

Steps UP are immediate — a box that is losing should stop losing now.
Steps DOWN wait ``STABLE_S`` of the calmer condition, so a CPU figure
bouncing around the threshold does not flap the state every 15 s.
Each change records one ``enrichment_throttled`` system event with the
reason, so the operator sees "captions paused: CPU 84%" rather than a
store that quietly stopped filling.

Busy is load, not clock. "Busy hours" happen on their own because the
CPU is busy then. ``EVENTS_ENRICHMENT_BACKFILL_WINDOW`` is the one
clock-shaped knob, for the back catalogue only: an operator who would
rather history were swept at 01:00–06:00 than whenever the box looks
idle at 14:00 can say so, and the governor honours both conditions.

Nothing here reads the DB or calls an adapter. It is evaluated lazily,
on each admission and each backfill step, from numbers already in
memory — cheap enough to ask every time and never worth a loop of its
own.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from services import enrichment_gate as gate_mod

logger = logging.getLogger("enrichment_governor")

#: The system-event type each state change records.
ALERT_TYPE = "enrichment_throttled"

#: How long the calmer condition must hold before the state steps down.
STABLE_S = 30.0

#: A latency figure older than this says nothing about now. The EWMA
#: only moves on calls; after a breaker episode it sits at ~90 s until
#: the next call, and the next call was the thing it was blocking.
SLOW_SIGNAL_TTL_S = 300.0

Kind = Literal["caption", "vqa", "embed"]


class State(str, Enum):
    NORMAL = "normal"
    LIVE_ONLY = "live_only"
    PAUSED = "paused"


_RANK = {State.NORMAL: 0, State.LIVE_ONLY: 1, State.PAUSED: 2}


def _now() -> float:
    return time.monotonic()


def _settings() -> Any:
    from core.config import settings

    return settings


def soft_cpu_percent() -> float:
    return float(getattr(_settings(), "events_enrichment_governor_cpu_percent", 65))


def slow_call_s() -> float:
    return max(1.0, float(getattr(_settings(), "events_enrichment_slow_call_s", 20.0)))


def backfill_idle_s() -> float:
    return max(0.0, float(getattr(_settings(), "events_enrichment_backfill_idle_s", 120)))


def backfill_share() -> float:
    """The share of wall time the backfill may spend in the adapters,
    (0, 1]; see events_enrichment_backfill_share."""
    try:
        v = float(getattr(_settings(), "events_enrichment_backfill_share", 0.5))
    except (TypeError, ValueError):
        v = 0.5
    return min(1.0, max(0.05, v))


def interactive_hold_s() -> float:
    return max(0.0, float(getattr(_settings(), "events_enrichment_backfill_interactive_hold_s", 90)))


def pace_sleep(spent_s: float, *, share: float | None = None, floor_s: float = 0.0) -> float:
    """How long the backfill rests after a visit that spent ``spent_s``
    in the adapters, so its share of the wall clock stays at
    ``share``: at 0.5 it rests as long as it worked; at 1 it rests only
    the fixed ``floor_s``."""
    share = backfill_share() if share is None else min(1.0, max(0.05, float(share)))
    rest = float(spent_s) * (1.0 - share) / share if spent_s > 0 else 0.0
    return max(float(floor_s), rest)


#: When a person last used the box (a search, a camera-agent turn),
#: monotonic. The backfill stands aside for interactive_hold_s after it.
_last_interactive: float = 0.0
_last_interactive_source: str = ""


def note_interactive(source: str = "") -> None:
    """A person is using the box: a search ran, the camera agent took a
    turn. The backfill holds for interactive_hold_s from now."""
    global _last_interactive, _last_interactive_source
    _last_interactive = _now()
    _last_interactive_source = source or ""


def interactive_recently() -> bool:
    hold = interactive_hold_s()
    return bool(hold) and _last_interactive > 0 and (_now() - _last_interactive) < hold


# ── the signals ───────────────────────────────────────────────────

def host_cpu_percent() -> float | None:
    """The monitor's last CPU sample; None when the monitor has not
    ticked yet or psutil is missing. None is treated as "unknown", not
    as zero — a box whose load nobody can read is not thereby idle."""
    try:
        from services.system_monitor_service import get_system_monitor

        sample = get_system_monitor().latest_sample() or {}
        cpu = sample.get("cpu_percent")
        return float(cpu) if cpu is not None else None
    except Exception:                              # noqa: BLE001
        return None


def host_cpu_high() -> bool:
    try:
        from services.system_monitor_service import ALERT_CPU_HIGH, get_system_monitor

        return get_system_monitor().alert_active(ALERT_CPU_HIGH)
    except Exception:                              # noqa: BLE001
        return False


def slowest_adapter() -> tuple[str, float] | None:
    """(adapter, latency) of the slowest gate over the slow-call budget,
    or None when every adapter is keeping up."""
    worst: tuple[str, float] | None = None
    budget = slow_call_s()
    for snap in gate_mod.snapshot_all():
        lat = snap.get("latency_ewma_s")
        age = snap.get("latency_age_s")
        if lat is None or lat < budget:
            continue
        if age is not None and age > SLOW_SIGNAL_TTL_S:
            continue                  # stale: nobody has asked it lately
        if worst is None or lat > worst[1]:
            worst = (snap["adapter"], float(lat))
    return worst


def open_breaker() -> str | None:
    """An adapter whose breaker is still REFUSING. An open breaker whose
    cooldown has expired is waiting for its probe, and the backfill may
    be that probe — reading it as PAUSED would keep everyone away from
    an adapter that may have been fine for hours."""
    for snap in gate_mod.snapshot_all():
        if snap.get("breaker_cooling"):
            return str(snap["adapter"])
    return None


# ── the state machine ─────────────────────────────────────────────

class Governor:
    def __init__(self) -> None:
        self.state = State.NORMAL
        self.reason = ""
        self._since = _now()          # when the current state began
        self._calmer_since: float | None = None
        self._last_wanted = State.NORMAL
        self._last_eval = _now()

    def _wanted(self) -> tuple[State, str]:
        """What the signals say right now, before hysteresis."""
        breaker = open_breaker()
        if breaker:
            return State.PAUSED, f"{breaker} breaker open"
        if host_cpu_high():
            return State.PAUSED, "host cpu_high alert active"
        cpu = host_cpu_percent()
        soft = soft_cpu_percent()
        if cpu is not None and cpu >= soft:
            return State.LIVE_ONLY, f"CPU {cpu:.0f}% ≥ {soft:.0f}%"
        slow = slowest_adapter()
        if slow:
            return State.LIVE_ONLY, f"{slow[0]} averaging {slow[1]:.0f}s a call"
        return State.NORMAL, ""

    def evaluate(self) -> State:
        wanted, reason = self._wanted()
        now = _now()
        gap = now - self._last_eval
        self._last_eval = now
        if _RANK[wanted] > _RANK[self.state]:
            self._set(wanted, reason, now)
        elif _RANK[wanted] < _RANK[self.state]:
            # Calmer. Step down only once it has stayed calmer a while —
            # measured from when calm was first SEEN, since the signals
            # are only read here. The exception is a long silence: this
            # runs on admissions, and a box with no visits for three
            # hours after a pause has been calm for three hours, not for
            # zero. Its first visit must not be the one that is dropped.
            if gap >= STABLE_S:
                self._set(wanted, reason, now)
            elif self._calmer_since is None or wanted != self._last_wanted:
                self._calmer_since = now
            elif now - self._calmer_since >= STABLE_S:
                self._set(wanted, reason, now)
        else:
            self._calmer_since = None
            if reason and reason != self.reason:
                self.reason = reason
        self._last_wanted = wanted
        return self.state

    def _set(self, state: State, reason: str, now: float) -> None:
        prev = self.state
        self.state, self.reason = state, reason
        self._since = now
        self._calmer_since = None
        if state is State.NORMAL:
            logger.info("enrichment governor: %s → normal (%s)", prev.value,
                        "load back under the line")
            gate_mod.notify_edge(
                ALERT_TYPE, active=False,
                description="enrichment resumed — load back under the line",
                data={"state": state.value, "previous": prev.value})
            return
        logger.warning("enrichment governor: %s → %s (%s) — %s", prev.value,
                       state.value, reason,
                       "nothing is sent to the enrichers" if state is State.PAUSED
                       else "captions and embeddings only when a slot is free; "
                            "VQA keeps a short line; backfill held")
        gate_mod.notify_edge(
            ALERT_TYPE, active=True,
            description=f"enrichment {'paused' if state is State.PAUSED else 'throttled'}: {reason}",
            data={"state": state.value, "previous": prev.value, "reason": reason})

    def stable_for(self) -> float:
        return _now() - self._since

    def snapshot(self) -> dict[str, Any]:
        """Read-only: reports the state as last evaluated and never
        advances it. A status endpoint polling this must not be what
        moves the governor or records system events."""
        return {"state": self.state.value, "reason": self.reason,
                "stable_for_s": round(self.stable_for(), 1),
                "cpu_percent": host_cpu_percent(),
                "soft_cpu_percent": soft_cpu_percent(),
                "slow_call_s": slow_call_s(),
                "backfill_window": window_text(),
                "backfill_allowed": (
                    self.state is State.NORMAL
                    and self.stable_for() >= backfill_idle_s()
                    and gate_mod.all_idle()
                    and in_window(parse_window(window_text())))}


_governor: Governor | None = None


def governor() -> Governor:
    global _governor
    if _governor is None:
        _governor = Governor()
    return _governor


def _reset_for_tests() -> None:
    global _governor, _last_interactive, _last_interactive_source
    _governor = None
    _last_interactive, _last_interactive_source = 0.0, ""


# ── what the enrichers ask ────────────────────────────────────────

Priority = Literal["live", "requested"]


async def admit_live(gate: gate_mod.AdapterGate, kind: Kind,
                     priority: Priority = "live") -> bool:
    """The enrichers' one question: may this call go to the adapter?

    ``True`` means a slot is held (release it). ``False`` is a drop —
    counted on the gate like any other, and the visit stays for the
    backfill.

    ``priority="requested"`` is a visit somebody ASKED about (the
    requested lane, ``enrichment_requests``): in LIVE-ONLY it keeps the
    full wait line rather than the live path's short one, because a
    person is waiting on it and the catch-up will not reach it sooner.
    PAUSED still refuses it — the box is losing, and a question does not
    change that; the lane's worker waits and retries."""
    state = governor().evaluate()
    if state is State.PAUSED:
        if priority == "requested":
            # Not a drop: the lane's worker holds and asks again. Counting
            # it on the gate would let a flapping PAUSE bury the real
            # breaker drops under retries of work that was never lost.
            return False
        gate.dropped_open += 1
        gate._note_drop(f"governor paused: {governor().reason}")
        return False
    if state is State.LIVE_ONLY and priority == "live":
        # Under budget, the filters' claims outrank the sentence.
        return await gate.admit(max_waiting=(gate.queue_depth // 2) if kind == "vqa" else 0)
    return await gate.admit()


# ── what the backfill asks ────────────────────────────────────────

_WINDOW_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def window_text() -> str:
    return str(getattr(_settings(), "events_enrichment_backfill_window", "") or "").strip()


def parse_window(text: str) -> tuple[int, int] | None:
    """``"HH:MM-HH:MM"`` → (start_minute, end_minute) of the local day,
    or None for empty / malformed (malformed is logged once and treated
    as "no window" — a typo must not silently switch the backfill off)."""
    text = (text or "").strip()
    if not text:
        return None
    m = _WINDOW_RE.match(text)
    if not m:
        _warn_window_once(text)
        return None
    h1, m1, h2, m2 = (int(g) for g in m.groups())
    if not (0 <= h1 <= 23 and 0 <= h2 <= 23 and 0 <= m1 <= 59 and 0 <= m2 <= 59):
        _warn_window_once(text)
        return None
    return h1 * 60 + m1, h2 * 60 + m2


_warned_windows: set[str] = set()


def _warn_window_once(text: str) -> None:
    if text in _warned_windows:
        return
    _warned_windows.add(text)
    logger.warning("EVENTS_ENRICHMENT_BACKFILL_WINDOW=%r is not HH:MM-HH:MM — "
                   "ignored, the backfill runs whenever the box is idle", text)


def in_window(window: tuple[int, int] | None, now: datetime | None = None) -> bool:
    """Is the local wall clock inside the window? An empty window is
    always open; one that wraps midnight (22:00-06:00) works."""
    if window is None:
        return True
    start, end = window
    t = now or datetime.now().astimezone()
    minute = t.hour * 60 + t.minute
    if start == end:
        return True
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def backfill_allowed(g: Governor | None = None, *, starting: bool = False) -> bool:
    """May the back catalogue be touched right now?

    ``starting`` is the stricter question asked before a PASS begins: the
    box must have been NORMAL for the idle period — "when it finds
    resources". Between items of a pass already under way only the live
    conditions matter (NORMAL, nothing live in hand, inside the window);
    a pass that has begun is not restarted from scratch on every item.
    """
    g = g or governor()
    if g.evaluate() is not State.NORMAL:
        return False
    if starting and g.stable_for() < backfill_idle_s():
        return False
    if not gate_mod.all_idle():
        return False
    if interactive_recently():
        return False
    return in_window(parse_window(window_text()))


async def wait_for_backfill_slot(*, starting: bool = False, poll_s: float = 1.0) -> None:
    """Block until the back catalogue may be swept: NORMAL (for the idle
    period when ``starting``), nothing live in hand, and inside the
    window if one is set. For the backfill only — live callers never
    wait on this."""
    import asyncio

    said = False
    while not backfill_allowed(starting=starting):
        if not said:
            g = governor()
            why = (g.reason or g.state.value) if g.state is not State.NORMAL else (
                "outside the backfill window" if not in_window(parse_window(window_text()))
                else (f"someone is using the box ({_last_interactive_source or 'interactive'})"
                      if interactive_recently() else "waiting for the box to stay idle"))
            logger.info("enrichment backfill: holding — %s", why)
            said = True
        await asyncio.sleep(poll_s)
