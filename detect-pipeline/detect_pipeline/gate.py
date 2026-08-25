# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
The gate (Tier-1 dispatch decision) — PR B.

Tier-0 (PR A) produces tracks cheaply, every frame. The **gate** decides which of
those tracks are worth the *expensive* Tier-1 model (VLM caption, face, plate) —
so the costly model runs **once per meaningful event on the best frame**, not on
every frame. The gate never sits in front of catching an event (Tier-0 already
detected and, elsewhere, recorded it); it sits only in front of the expensive
*interpretation*.

Design invariants enforced here:

* **Shadow mode is the default.** In shadow mode the gate computes the same
  escalate/suppress decisions but marks them non-enforcing, so nothing about the
  running system changes — you *measure* what it would suppress (and whether that
  mattered) before ever trusting it. Flip ``shadow=False`` to enforce.
* **Recording is never gated** — that is MediaMTX's job and is not touched here.
* **Safety rails**: per-camera ``always_analyze`` (disable the gate for a camera),
  ``critical_classes`` (force-escalate regardless of the gate), a ``heartbeat``
  pass (bound worst-case interpretation latency), and stationary suppression.

The gate is **pluggable by trigger**: this module implements the default object
``TriggerPolicy`` (gate on object tracks). Other trigger signals — scene_change,
interval, field_statistic — plug in on the same ``evaluate`` seam (see
``docs/design/trigger-policies.md``); the gate core never assumes a domain.

Pure + injectable: ``evaluate(tracks, now)`` takes the wall/monotonic clock as an
argument, so every decision path is unit-tested with no I/O, no models, no NATS.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field

from .regions import Box
from .tracking import Track


class TriggerPolicy(str, enum.Enum):
    """What cheap signal wakes the expensive tier. Default = object motion.

    Only ``MOTION`` is implemented in PR B; the others are declared so the gate is
    authored against the interface, not hardcoded (see trigger-policies.md).
    """

    MOTION = "motion"
    SCENE_CHANGE = "scene_change"
    INTERVAL = "interval"
    FIELD_STATISTIC = "field_statistic"
    CHAINED = "chained"
    ALWAYS = "always"
    NONE = "none"


@dataclass
class GateConfig:
    """Per-camera gate policy. All rails default to the safe/measure-only posture."""

    # Measure-only by default: decisions computed + audited, never enforced.
    shadow: bool = True
    trigger: TriggerPolicy = TriggerPolicy.MOTION
    # None = every class is interesting; else only these (critical always are).
    interesting_classes: frozenset[str] | None = None
    # Classes that ALWAYS escalate (person-in-zone, weapon, ...): bypass stationary
    # and "uninteresting" suppression (still rate-limited by cooldown).
    critical_classes: frozenset[str] = frozenset()
    # Per-camera escape hatch: disable the gate here (escalate every confirmed
    # track, no suppression) — for gate/cash-room/perimeter cameras.
    always_analyze: bool = False
    # Escalate when a track sits inside any of these full-frame-pixel boxes.
    zones: tuple[Box, ...] = ()
    # Re-escalate the same track at most once per this many seconds.
    escalate_cooldown_s: float = 30.0
    # >0: force an escalate pass on confirmed tracks every N seconds even if the
    # scene is static (bounds worst-case interpretation latency). 0 = off.
    heartbeat_s: float = 0.0
    # Suppress the expensive tier on settled (stationary) objects.
    stationary_suppress: bool = True
    # A track must have at least this many hits (confirmed) before it can escalate.
    min_hits: int = 1


@dataclass(frozen=True)
class GateDecision:
    """One track's gate outcome, with the reason (audited — incl. suppressions)."""

    track_id: int
    label: str
    escalate: bool
    reason: str      # new_track | reactivated | critical_class | in_zone |
                     # always_analyze | heartbeat | refresh  /  stationary |
                     # cooldown | uninteresting_class | not_confirmed
    shadow: bool     # True = advisory (measure-only), not enforced


@dataclass
class GateResult:
    decisions: list[GateDecision] = field(default_factory=list)
    shadow: bool = True

    @property
    def enforced(self) -> bool:
        """False in shadow mode — callers must NOT actually gate on the result."""
        return not self.shadow

    def to_dispatch(self) -> list[GateDecision]:
        """Tracks to send to the expensive tier: escalations, but only when enforced.

        In shadow mode this is empty — the system behaves exactly as before while
        the decisions are still recorded for miss-rate analysis.
        """
        if self.shadow:
            return []
        return [d for d in self.decisions if d.escalate]


def _boxes_overlap(a: Box, b: Box) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


class Gate:
    """Stateful per-camera gate. Feed it each frame's tracks; get decisions back."""

    def __init__(self, config: GateConfig | None = None) -> None:
        self.cfg = config or GateConfig()
        self._last_escalate: dict[int, float] = {}   # track_id -> last escalate time
        self._seen: set[int] = set()                  # track_ids ever observed
        self._was_stationary: dict[int, bool] = {}    # track_id -> stationary last frame
        # None until the first frame anchors it — a 0.0 seed would make the first
        # real-clock frame (now = frame.ts, a large value) fire the heartbeat.
        self._last_heartbeat: float | None = None

    def _decide(self, t: Track, now: float, heartbeat_due: bool) -> GateDecision:
        def out(escalate: bool, reason: str) -> GateDecision:
            return GateDecision(t.id, t.label, escalate, reason, self.cfg.shadow)

        if t.hits < self.cfg.min_hits:
            return out(False, "not_confirmed")

        # ── unconditional floors: these deliberately BYPASS the per-track cooldown ──
        #  * always_analyze = the gate is *disabled* for this camera (escalate every
        #    frame); letting cooldown gate it would silently re-enable gating.
        #  * heartbeat = a hard worst-case-latency bound; if cooldown could gate it,
        #    the bound would not hold whenever heartbeat_s < cooldown_s.
        if self.cfg.always_analyze:
            return out(True, "always_analyze")
        if heartbeat_due:
            return out(True, "heartbeat")

        in_cooldown = (
            t.id in self._last_escalate
            and (now - self._last_escalate[t.id]) < self.cfg.escalate_cooldown_s
        )
        is_critical = t.label in self.cfg.critical_classes
        in_zone = bool(self.cfg.zones) and any(_boxes_overlap(t.box, z) for z in self.cfg.zones)
        interesting = (
            self.cfg.interesting_classes is None
            or t.label in self.cfg.interesting_classes
            or is_critical
        )
        is_new = t.id not in self._seen
        reactivated = self._was_stationary.get(t.id, False) and not t.stationary

        # ── forced escalations that MAY be rate-limited: once per cooldown is enough
        #    for a re-look at the same critical object / zone intrusion ──
        if not in_cooldown:
            if is_critical:
                return out(True, "critical_class")
            if in_zone:
                return out(True, "in_zone")
        elif is_critical or in_zone:
            # It WOULD have been forced through; the only thing holding it back
            # is the rate limit. Falling into the normal path below labelled a
            # stationary critical object "stationary", which reads as "we
            # suppress these" — the opposite of the documented contract, and it
            # inflated gate_suppressions_total{reason="stationary"}.
            return out(False, "cooldown")

        # ── normal path ──
        if not interesting:
            return out(False, "uninteresting_class")
        if self.cfg.stationary_suppress and t.stationary:
            return out(False, "stationary")
        if in_cooldown:
            return out(False, "cooldown")
        if is_new:
            return out(True, "new_track")
        if reactivated:
            return out(True, "reactivated")
        return out(True, "refresh")   # moving, interesting, past cooldown -> refresh

    def evaluate(self, tracks: list[Track], now: float) -> GateResult:
        """Decide escalate/suppress for each track at time ``now`` (monotonic secs)."""
        if self._last_heartbeat is None:
            self._last_heartbeat = now          # anchor the clock to the first frame
        heartbeat_due = (
            self.cfg.heartbeat_s > 0 and (now - self._last_heartbeat) >= self.cfg.heartbeat_s
        )
        decisions: list[GateDecision] = []
        for t in tracks:
            d = self._decide(t, now, heartbeat_due)
            decisions.append(d)
            # Track cadence realistically even in shadow mode, so measured
            # escalation counts match what enforcement would produce.
            if d.escalate:
                self._last_escalate[t.id] = now
            self._seen.add(t.id)
            self._was_stationary[t.id] = t.stationary
        if heartbeat_due:
            self._last_heartbeat = now
        # Forget tracks that have disappeared, so IDs reused later look "new".
        present = {t.id for t in tracks}
        gone = self._seen - present
        for tid in gone:
            self._last_escalate.pop(tid, None)
            self._was_stationary.pop(tid, None)
        self._seen &= present
        return GateResult(decisions=decisions, shadow=self.cfg.shadow)
