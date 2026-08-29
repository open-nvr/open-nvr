# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""RFC-0002 Phase 5: per-skill Tier-1 budgets, enforced at KAI-C.

One skill's runaway consumer must degrade ITSELF, not starve the
stack. KAI-C is the enforcement point because it is where every
governed inference call already meets (the same argument that placed
the Phase 0 domain-event normaliser here): Tier-1 dispatch, core's
enrichment, and any app all pass through ``/api/v1/infer/{adapter}``,
so a budget here needs no cooperation from initiators.

The budget is **calls per minute per (adapter, camera)** — the RFC's
"per camera per skill", with the adapter as the skill's provider-side
name. Sliding one-minute window, in memory (KAI-C is a single
process; restart forgiveness is fine for a rate limit).

**Shed-and-report, never silent** (Tier-0's region-shedding
discipline): an over-budget call is refused with HTTP 429 and a §3.5-
shaped error body, audited as ``inference.refused_budget``, counted in
``kaic_budget_shed_total{adapter,camera}``, and logged at WARNING
(rate-limited to once per window per bucket so a storm reports itself
without becoming its own log flood).

Configuration (env):

* ``KAIC_BUDGET_PER_CAMERA_PER_MIN`` — the global default (int).
  ``0`` disables budgeting entirely. Default 120: generous enough
  that a normal install never sees it, tight enough that a hot loop
  (an app re-inferring every frame) degrades predictably.
* ``KAIC_BUDGET_OVERRIDES`` — per-adapter overrides, e.g.
  ``"fast_plate_ocr=30,caption=60"``. ``0`` disables for that adapter.

Calls WITHOUT a ``camera_id`` are exempt: they are conformance probes,
health checks, and ad-hoc operator calls — not per-camera skill
traffic, and starving them would break the platform's own plumbing.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque

logger = logging.getLogger("kai-c.budgets")

DEFAULT_PER_CAMERA_PER_MIN = 120
WINDOW_SECONDS = 60.0

#: Bound on distinct (adapter, camera) buckets — a producer minting
#: fresh camera ids per call must not grow memory unbounded. At the
#: cap, the stalest bucket is evicted (its window restarts — the cheap
#: failure direction, one extra minute of grace).
MAX_BUCKETS = 4096


def parse_overrides(raw: str | None) -> dict[str, int]:
    """``"a=30,b=0"`` → ``{"a": 30, "b": 0}``. Malformed pairs are
    logged and skipped — a typo must not take budgeting down."""
    out: dict[str, int] = {}
    for pair in (raw or "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        name, sep, value = pair.partition("=")
        try:
            if not sep:
                raise ValueError("missing '='")
            out[name.strip()] = max(0, int(value.strip()))
        except ValueError as exc:
            logger.warning("KAIC_BUDGET_OVERRIDES: skipping %r (%s)", pair, exc)
    return out


class SkillBudgets:
    """Sliding-window rate limits per (adapter, camera). Thread-safe."""

    def __init__(
        self,
        *,
        default_per_min: int = DEFAULT_PER_CAMERA_PER_MIN,
        overrides: dict[str, int] | None = None,
        clock=time.monotonic,
    ) -> None:
        self.default_per_min = max(0, int(default_per_min))
        self.overrides = dict(overrides or {})
        self._clock = clock
        self._lock = threading.Lock()
        self._windows: dict[tuple[str, str], deque[float]] = {}
        # (adapter, camera) → shed count, for /metrics. Never reset.
        self._shed: dict[tuple[str, str], int] = {}
        # bucket → last WARNING timestamp (log flood control).
        self._warned_at: dict[tuple[str, str], float] = {}

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "SkillBudgets":
        env = os.environ if env is None else env
        try:
            default = int(env.get("KAIC_BUDGET_PER_CAMERA_PER_MIN",
                                  DEFAULT_PER_CAMERA_PER_MIN))
        except ValueError:
            logger.warning(
                "KAIC_BUDGET_PER_CAMERA_PER_MIN is not an int; using %d",
                DEFAULT_PER_CAMERA_PER_MIN)
            default = DEFAULT_PER_CAMERA_PER_MIN
        return cls(default_per_min=default,
                   overrides=parse_overrides(env.get("KAIC_BUDGET_OVERRIDES")))

    def limit_for(self, adapter: str) -> int:
        """The per-minute limit for one adapter (0 = unlimited)."""
        return self.overrides.get(adapter, self.default_per_min)

    def admit(self, adapter: str, camera_id: str | None) -> bool:
        """True = within budget (and the call is counted); False = shed.

        Exempt: no camera_id (probes/operator calls), or a limit of 0.
        """
        if not camera_id:
            return True
        limit = self.limit_for(adapter)
        if limit <= 0:
            return True
        now = self._clock()
        key = (adapter, str(camera_id))
        with self._lock:
            window = self._windows.get(key)
            if window is None:
                if len(self._windows) >= MAX_BUCKETS:
                    stalest = min(
                        self._windows,
                        key=lambda k: self._windows[k][-1]
                        if self._windows[k] else 0.0,
                    )
                    del self._windows[stalest]
                window = self._windows[key] = deque()
            cutoff = now - WINDOW_SECONDS
            while window and window[0] < cutoff:
                window.popleft()
            if len(window) >= limit:
                self._shed[key] = self._shed.get(key, 0) + 1
                last = self._warned_at.get(key, -WINDOW_SECONDS)
                if now - last >= WINDOW_SECONDS:
                    self._warned_at[key] = now
                    logger.warning(
                        "budget shed: %s on camera %s is over %d calls/min "
                        "(%d shed so far) — the caller receives 429s until "
                        "the window drains",
                        adapter, camera_id, limit, self._shed[key],
                    )
                return False
            window.append(now)
            return True

    def shed_total(self) -> dict[tuple[str, str], int]:
        with self._lock:
            return dict(self._shed)

    def render_metrics(self) -> str:
        """Prometheus lines for the shed counter (appended to /metrics)."""
        shed = self.shed_total()
        if not shed:
            return ""
        lines = [
            "# HELP kaic_budget_shed_total Inference calls refused over "
            "the per-(adapter, camera) budget (RFC-0002 Phase 5).",
            "# TYPE kaic_budget_shed_total counter",
        ]
        for (adapter, camera), count in sorted(shed.items()):
            lines.append(
                f'kaic_budget_shed_total{{adapter="{adapter}",'
                f'camera="{camera}"}} {count}'
            )
        return "\n".join(lines) + "\n"
