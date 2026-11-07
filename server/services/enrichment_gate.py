# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Admission control for the background enrichers (#583).

Never wait, always drop
-----------------------
Enrichment — a caption, two VQA questions, an embedding per finished
visit — is a luxury the recorder and the detector must never pay for.
The field report that made this module: a CPU-only box, Ollama's
moondream at 30 s+ a frame, core timing out at 15 s and *releasing its
semaphore on the timeout*, so a fresh request went out while the model
was still computing the last one. 478 timeouts an hour, two cores
burnt, zero captions saved. The semaphore was the right idea with the
wrong release condition: it bounded how many requests core was
*waiting on*, not how many the adapter was *working on*.

So every adapter gets one :class:`AdapterGate` with three rules:

1. **Slots come from the adapter's own declaration.** ``scheduling.
   max_inflight`` in ``server/config/adapters_index.yml`` is what the
   adapter says it can take; a gate never opens more than that. A slot
   is held until the adapter *answers* — or until core gives up on it,
   at which point rule 3 applies.
2. **The wait line is short, and a full line drops.** Visits arriving
   while every slot is busy wait — this is background work — but only
   ``EVENTS_ENRICHMENT_QUEUE_DEPTH`` of them. The next one is dropped on
   the spot with a counter, not queued: a queue that grows is a box that
   is losing, and the visit is not lost — it has no ``event_text`` row,
   which is exactly what ``enrichment_backfill`` looks for.
3. **Timeouts trip a breaker.** ``EVENTS_ENRICHMENT_BREAKER_TIMEOUTS``
   in a row and the gate opens: nothing is sent for a cooldown (60 s,
   doubling to 10 min), one probe goes through when it expires, and the
   first success closes it again. Each trip records a system event
   (``enrichment_adapter_too_slow``) so the UI says *why* captions
   stopped, instead of the store silently staying empty.

Caption and VQA resolve to the same adapter on most boxes (moondream
answers both), so they share a gate: one slow model gets one budget,
not two. Plate OCR is deliberately NOT behind this — it is fast, once
per track, and losing a plate read is a worse outcome than losing a
sentence.

What the bound covers, honestly: what the ENRICHERS send. The agent's
scene descriptions, the plate sweep and the KAI-C service post to the
same adapters outside this module, so an operator asking "what do you
see?" while a caption is in flight puts two requests on a one-slot
model. The gate keeps enrichment from being the thing that piles on;
it does not make the adapter's total load observable from here.

The backfill asks the governor (``enrichment_governor.
wait_for_backfill_slot``) before every item; that wait includes
:func:`all_idle` here, so the back catalogue is only swept when nothing
live is in hand and no breaker is cooling. Live always wins; history
has waited this long.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Literal

logger = logging.getLogger("enrichment_gate")

Outcome = Literal["ok", "timeout", "error"]

#: The system-event type each breaker trip records (edge-triggered:
#: active on trip, inactive on the first success after it).
ALERT_TYPE = "enrichment_adapter_too_slow"

#: First cooldown after a trip, and the ceiling the doubling stops at.
COOLDOWN_BASE_S = 60.0
COOLDOWN_MAX_S = 600.0

#: TCP connect ceiling, separate from the per-call limit. The 90 s limit
#: is for a model computing; a host that never answers the SYN should
#: cost 5 s, not hold the adapter's only slot for a minute and a half.
CONNECT_TIMEOUT_S = 5.0

#: KAI-C registration names that differ from their registry id.
_REGISTRY_ALIASES = {"clip": "clip-embeddings"}


class _Dropped:
    """The value :func:`infer_through_gate` returns when the call was
    never made — refused at the gate. Distinct from None (made, and
    failed) so a caller can tell "nobody looked" from "looked and found
    nothing": the backfill holds its cursor on the first and advances
    past the second."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "DROPPED"

    def __bool__(self) -> bool:
        return False


DROPPED = _Dropped()

#: Weight of the newest call in the latency average. ~0.2 means the
#: figure settles in a handful of calls and forgets one outlier quickly.
LATENCY_ALPHA = 0.2


def _now() -> float:
    """The gate's clock. A function, not ``time.monotonic`` bound at
    import, so a test can freeze the BREAKER's sense of time without
    freezing asyncio's — which shares ``time.monotonic``."""
    return time.monotonic()


def _settings() -> Any:
    from core.config import settings

    return settings


def queue_depth() -> int:
    return max(0, int(getattr(_settings(), "events_enrichment_queue_depth", 8)))


def timeout_s() -> float:
    return max(1.0, float(getattr(_settings(), "events_enrichment_timeout_s", 90.0)))


def breaker_timeouts() -> int:
    return max(1, int(getattr(_settings(), "events_enrichment_breaker_timeouts", 5)))


def max_inflight_for(adapter: str) -> int:
    """The adapter's own ``scheduling.max_inflight`` from the registry.

    Matched on the registry id exactly (moondream-vlm, blip-scene-caption
    register under their id) or through one named alias (the clip
    adapter registers as ``clip``; its entry is ``clip-embeddings``).
    Nothing fuzzier: a prefix match handed an empty or one-letter name
    the FIRST entry's budget, which is a detector's, not a captioner's.
    Unknown adapters get 1: the registry's own default, and the only
    safe guess for a model nobody declared a budget for.
    """
    name = (adapter or "").lower().strip()
    if not name:
        return 1
    name = _REGISTRY_ALIASES.get(name, name)
    try:
        from routers.adapters_catalog import load_adapters_index

        entries = load_adapters_index()
    except Exception as exc:                      # noqa: BLE001
        logger.debug("enrichment gate: registry unreadable (%s)", exc)
        return 1
    for entry in entries:
        if entry.id.lower() == name:
            return max(1, int(entry.scheduling.max_inflight))
    return 1


class AdapterGate:
    """One adapter's admission state. See the module docstring."""

    def __init__(self, adapter: str, *, max_inflight: int, queue_depth: int,
                 breaker_timeouts: int) -> None:
        self.adapter = adapter
        self.max_inflight = max(1, int(max_inflight))
        self.queue_depth = max(0, int(queue_depth))
        self.breaker_timeouts = max(1, int(breaker_timeouts))
        self._slots = asyncio.Semaphore(self.max_inflight)
        self.inflight = 0
        self.waiting = 0
        # Counters, for the log lines and a future /metrics row.
        self.admitted = 0
        self.dropped_full = 0
        self.dropped_open = 0
        self.completed = 0
        self.timeouts = 0
        self.errors = 0
        # Breaker.
        self.timeouts_in_row = 0
        self.open_until: float | None = None
        self.cooldown_s = COOLDOWN_BASE_S
        self.trips = 0
        self._probe_out = False
        # Smoothed seconds per call; None until the first answer, and
        # stamped so a stale figure can be ignored.
        self.latency_ewma_s: float | None = None
        self.latency_updated_at: float | None = None

    # ── state ─────────────────────────────────────────────────────

    @property
    def is_open(self) -> bool:
        return self.open_until is not None

    @property
    def cooling(self) -> bool:
        """Open AND still refusing: the cooldown has not expired, or the
        one probe is out. An open breaker whose cooldown has passed is
        waiting for a caller to be its probe — the next admit() — and
        with no live traffic that caller may be the backfill, so it
        must not read as busy: that is how a breaker that tripped at
        night stayed "paused" until morning with nothing ever probing."""
        if not self.is_open:
            return False
        return self._probe_out or _now() < (self.open_until or 0.0)

    def idle(self) -> bool:
        return self.inflight == 0 and self.waiting == 0 and not self.cooling

    def snapshot(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "max_inflight": self.max_inflight,
            "inflight": self.inflight,
            "waiting": self.waiting,
            "admitted": self.admitted,
            "dropped_full": self.dropped_full,
            "dropped_open": self.dropped_open,
            "completed": self.completed,
            "timeouts": self.timeouts,
            "errors": self.errors,
            "breaker_open": self.is_open,
            "breaker_cooling": self.cooling,
            "breaker_trips": self.trips,
            "cooldown_s": self.cooldown_s if self.is_open else 0.0,
            "latency_ewma_s": self.latency_ewma_s,
            "latency_age_s": (None if self.latency_updated_at is None
                              else max(0.0, _now() - self.latency_updated_at)),
        }

    # ── admission ─────────────────────────────────────────────────

    async def admit(self, *, max_waiting: int | None = None) -> bool:
        """Take a slot, or say no. ``True`` means the caller HOLDS a slot
        and must call :meth:`release` exactly once, whatever happens.

        ``max_waiting`` narrows the wait line for this call only — the
        governor uses it to keep a slow box's line short for callers
        that matter less (a caption sentence) than others (the colour
        the filters match on)."""
        depth = self.queue_depth if max_waiting is None else min(self.queue_depth, max(0, int(max_waiting)))
        probe = False
        if self.is_open:
            now = _now()
            if now < (self.open_until or 0.0) or self._probe_out:
                self.dropped_open += 1
                self._note_drop("breaker open")
                return False
            # Cooldown over: exactly one probe goes through — but the
            # token is only spent once it HOLDS a slot. Spending it here
            # and then losing on the wait-line check left the gate cooling
            # with no probe in flight until a straggler released.
            probe = True
        if not self._slots.locked():
            # A slot is free: take it without joining the line. (No await
            # between the check and the acquire, so nothing can steal it.)
            await self._slots.acquire()
        else:
            if self.waiting >= depth:
                self.dropped_full += 1
                self._note_drop("wait line full")
                return False
            self.waiting += 1
            try:
                await self._slots.acquire()
            finally:
                self.waiting -= 1
        if self.is_open and not probe:
            # The breaker tripped while this one was waiting. Its wait
            # bought nothing; give the slot back and drop.
            self._slots.release()
            self.dropped_open += 1
            return False
        if probe:
            self._probe_out = True
        self.inflight += 1
        self.admitted += 1
        return True

    def release(self, outcome: Outcome, *, elapsed_s: float | None = None) -> None:
        self.inflight = max(0, self.inflight - 1)
        self._slots.release()
        if elapsed_s is not None and outcome != "error":
            # How long this adapter really takes, smoothed — the governor's
            # second signal beside CPU%. A timeout counts at the full limit,
            # which is the honest number: that is how long the caller waited.
            # Stamped, because a number nobody has refreshed in an hour says
            # nothing about now — the governor lets it expire.
            e = max(0.0, float(elapsed_s))
            self.latency_ewma_s = e if self.latency_ewma_s is None \
                else LATENCY_ALPHA * e + (1.0 - LATENCY_ALPHA) * self.latency_ewma_s
            self.latency_updated_at = _now()
        # While open, only the PROBE's outcome moves the breaker. A call
        # admitted before the trip (max_inflight > 1) finishing late is
        # old news: it must not extend the cooldown or double the backoff.
        probe_result = self.is_open and self._probe_out
        if outcome == "ok":
            self.completed += 1
            self.timeouts_in_row = 0
            if probe_result:
                self._close()
            return
        if outcome == "timeout":
            self.timeouts += 1
            self.timeouts_in_row += 1
            if probe_result:
                # The probe failed: stay open, back off further.
                self._reopen()
            elif not self.is_open and self.timeouts_in_row >= self.breaker_timeouts:
                self._trip()
            return
        self.errors += 1
        if probe_result:
            # A probe that errored is not a success; stay open.
            self._reopen()

    # ── breaker ───────────────────────────────────────────────────

    def _trip(self) -> None:
        self.trips += 1
        self.open_until = _now() + self.cooldown_s
        self._probe_out = False
        logger.warning(
            "enrichment gate: %s timed out %d times in a row — pausing "
            "enrichment for %.0fs (trip #%d). The adapter is slower than "
            "the visit rate; a GPU, a lighter captioner (CAPTION_ADAPTER="
            "moondream on CPU), or fewer assigned cameras fixes it. See #583",
            self.adapter, self.timeouts_in_row, self.cooldown_s, self.trips)
        _notify(self, active=True)
        self.cooldown_s = min(self.cooldown_s * 2, COOLDOWN_MAX_S)

    def _reopen(self) -> None:
        self.open_until = _now() + self.cooldown_s
        self._probe_out = False
        logger.warning("enrichment gate: %s still too slow — pausing another %.0fs",
                       self.adapter, self.cooldown_s)
        self.cooldown_s = min(self.cooldown_s * 2, COOLDOWN_MAX_S)

    def _close(self) -> None:
        logger.info("enrichment gate: %s answered again — resuming enrichment "
                    "after %d trip(s)", self.adapter, self.trips)
        self.open_until = None
        self._probe_out = False
        self.cooldown_s = COOLDOWN_BASE_S
        _notify(self, active=False)

    # ── logging ───────────────────────────────────────────────────

    def _note_drop(self, why: str) -> None:
        total = self.dropped_full + self.dropped_open
        # The first, then one per hundred: findable, never the whole log.
        if total == 1 or total % 100 == 0:
            logger.warning(
                "enrichment gate: %s dropped a visit (%s) — %d dropped so far; "
                "backfill (EVENTS_ENRICHMENT_BACKFILL=true) describes them "
                "later, when the box is idle",
                self.adapter, why, total)


# ── registry ──────────────────────────────────────────────────────

_gates: dict[str, AdapterGate] = {}


def gate_for(adapter: str) -> AdapterGate:
    """The one gate for this adapter, created on first use from the
    registry's budget and the operator's knobs."""
    key = (adapter or "").lower()
    gate = _gates.get(key)
    if gate is None:
        gate = AdapterGate(
            adapter or "?",
            max_inflight=max_inflight_for(adapter),
            queue_depth=queue_depth(),
            breaker_timeouts=breaker_timeouts(),
        )
        _gates[key] = gate
    return gate


def snapshot_all() -> list[dict[str, Any]]:
    return [g.snapshot() for _, g in sorted(_gates.items())]


def all_idle() -> bool:
    return all(g.idle() for g in _gates.values())


def _reset_for_tests() -> None:
    _gates.clear()
    _notify_tasks.clear()


# ── the one call the enrichers make ───────────────────────────────

async def infer_through_gate(adapter: str, body: dict[str, Any], *,
                             kind: str, log: logging.Logger, caller: str,
                             what: str = "",
                             interactive: bool = False,
                             priority: str = "live") -> dict[str, Any] | None:
    """POST one infer request to KAI-C through the governor and the
    adapter's gate, and return the decoded JSON body, or None.

    :data:`DROPPED` means the call was never made — refused by the
    governor or dropped at the gate. None covers every way a call that
    WAS made failed, each logged in the caller's voice (``caller`` is the
    log prefix, ``log`` the caller's logger so the line lands where an
    operator grepping for that enricher looks): timed out, unreachable,
    a non-200, or a body that is not JSON. The caller decides what the
    body means. ``kind`` ("caption" / "vqa" / "embed") is what the
    governor ranks when there is budget for one call and not three.

    ``interactive`` is for a person waiting on the answer — the search
    box embedding its query. It still respects the adapter's slots (the
    model cannot do more) but never asks the governor and never drops
    for load: it waits its turn. Background enrichment yields to the
    box; a person typing "white van" does not.

    Only a 200 is "ok" to the breaker. A 503 from KAI-C or a crashed
    adapter is an answer in the HTTP sense and nothing else: it must not
    reset the timeout streak, and it must not close an open breaker —
    that is how a probe answered "busy" in 50 ms once resumed full
    traffic against a model still saturated.

    Only ReadTimeout/WriteTimeout count as "timed out": the connection
    was made and the adapter went quiet computing — the #583 shape. A
    ConnectTimeout is a host that never answered the SYN, a PoolTimeout
    is our own client; both are "unreachable" and count as an error,
    not a timeout, for the breaker.

    The slot is held until the adapter ANSWERED or core gave up — never
    released early to let the next request pile onto a model still busy
    with this one. That early release was the original defect.
    """
    import httpx

    from core.config import settings
    from services.enrichment_governor import admit_live

    gate = gate_for(adapter)
    admitted = (await gate.admit() if interactive
                else await admit_live(gate, kind, priority))
    if not admitted:
        return DROPPED
    limit = timeout_s()
    suffix = f" for {what}" if what else ""
    started = time.monotonic()
    outcome: Outcome = "error"
    try:
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(limit, connect=CONNECT_TIMEOUT_S),
                trust_env=False) as client:
            resp = await client.post(
                f"{settings.kai_c_url}/api/v1/infer/{adapter}",
                json=body,
                headers={"X-Internal-Api-Key": settings.internal_api_key},
            )
        if resp.status_code == 200:
            outcome = "ok"
        elif resp.status_code == 504:
            # KAI-C's proxy gave up waiting for the adapter: the model is
            # slow, not down. Counted as a timeout so the breaker trips;
            # read as a plain failure it never did, and the Search lane
            # asked the same visit again every time (Ventuno Q, 2026-11-05).
            outcome = "timeout"
    except (httpx.ReadTimeout, httpx.WriteTimeout):
        outcome = "timeout"
        log.warning(
            "%s: %s timed out after %.1fs (limit %.0fs)%s — the adapter is "
            "slower than the visit rate; see #583",
            caller, adapter, time.monotonic() - started, limit, suffix)
        return None
    except Exception as exc:                      # noqa: BLE001
        log.warning("%s: %s unreachable (%s: %s)", caller, adapter,
                    type(exc).__name__, exc)
        return None
    finally:
        gate.release(outcome, elapsed_s=time.monotonic() - started)
    if resp.status_code != 200:
        log.warning("%s: %s returned %s%s", caller, adapter, resp.status_code, suffix)
        return None
    try:
        return resp.json()
    except Exception:                             # noqa: BLE001
        return None


# ── the operator-visible side ─────────────────────────────────────

def _notify(gate: AdapterGate, *, active: bool) -> None:
    """The breaker's edge, as the operator sees it."""
    description = (
        f"{gate.adapter} is too slow for the visit rate — enrichment paused "
        f"for {gate.cooldown_s:.0f}s after {gate.timeouts_in_row} timeouts in a row"
        if active else
        f"{gate.adapter} is answering again — enrichment resumed"
    )
    data = {"adapter": gate.adapter, "trips": gate.trips,
            "cooldown_s": gate.cooldown_s if active else 0,
            "timeouts_in_row": gate.timeouts_in_row}
    # One edge PER ADAPTER: system events dedupe on the type alone, so a
    # shared type made clip's trip vanish behind moondream's and
    # moondream's recovery announce "resumed" while clip stayed paused.
    notify_edge(f"{ALERT_TYPE}:{gate.adapter}", active=active,
                description=description, data=data)


def notify_edge(event_type: str, *, active: bool, description: str,
                data: dict[str, Any]) -> None:
    """Record an edge as a system event and push it on the bus.

    Fire-and-forget from the caller's point of view: the DB write runs
    in a worker thread (``system_events`` is deliberately sync), the bus
    publish is scheduled, and neither can fail the enrichment call that
    raised it. Shared by the breaker and the governor.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        return

    async def _go() -> None:
        try:
            from services.system_events import record_system_event_edge

            await asyncio.to_thread(
                record_system_event_edge,
                event_type=event_type, active=active, severity="warning",
                description=description, data=data)
        except Exception:                          # noqa: BLE001
            logger.debug("enrichment gate: system event write failed", exc_info=True)
        try:
            from services.event_bus_service import publish_system_alert

            await publish_system_alert(
                alert_type=event_type,
                state="active" if active else "inactive",
                severity="warning" if active else "info",
                payload={"description": description, **data})
        except Exception:                          # noqa: BLE001
            logger.debug("enrichment gate: bus publish failed", exc_info=True)

    # Hold the reference: asyncio keeps only weak refs to tasks, and a
    # notification collected mid-flight is one that never lands.
    task = loop.create_task(_go())
    _notify_tasks.add(task)
    task.add_done_callback(_notify_tasks.discard)


_notify_tasks: set[asyncio.Task] = set()
