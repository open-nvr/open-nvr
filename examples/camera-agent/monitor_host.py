# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
MonitorHost — the camera-agent's front door onto the App SDK rule
library (app-sdk-spec.html §07, "One rule library, two front doors").

The catalog apps (``examples/occupancy-counting``,
``examples/line-crossing``) and the agent's conversational monitors
used to implement the SAME rules twice: the examples on the SDK's
:class:`~opennvr_app_sdk.Detector`, the agent in bespoke poll loops
inside ``MonitorManager``. This module removes the duplication for the
converged kinds: a conversational monitor request now instantiates the
example app's own Detector class in-process and drives it with the
agent's existing frame source + detection client.

Converged monitor kinds (SDK-backed via this host)
--------------------------------------------------
* ``kind="count"``     → ``occupancy_counting.OccupancyCounter``
  (whole-frame zone, no threshold → pure live/peak counting; pass
  ``max_count`` / ``min_count`` params to get the example's
  edge-triggered over/under/cleared alerts, routed into the agent's
  notify machinery).
* ``kind="crossing"``  → ``line_crossing.LineCrossingDetector``
  (the SDK ``Tripwire`` decides each crossing; the host tallies
  in/out/net exactly like the legacy ``LineCounter`` did:
  ``b_to_a`` = ends on the positive side = "in").

Legacy monitor kinds (still on ``MonitorManager``'s bespoke loop)
-----------------------------------------------------------------
* ``kind="notify"`` — presence with a re-fire cooldown ("Heads up — I
  see a person on cam1" every ≥30 s while present). The SDK library
  has no cooldown-refire archetype (occupancy is edge-triggered, so a
  person who stays on camera would notify once, not every cooldown);
  converging it would change user-visible behavior, so it stays legacy
  until the SDK grows that shape.
* Alarms (``AlarmManager``) — sticky, acknowledgeable, time-windowed;
  same reasoning.

Rule classes are imported from the example packages themselves (one
rule library — no re-implementation here). The examples are flat
single-module projects that aren't importable as installed packages
from the agent's venv (both ship a top-level ``alerts.py`` shim, so
installing the two side by side would collide), so the host loads
``occupancy_counting.py`` / ``line_crossing.py`` by file path from the
sibling example directories via ``importlib`` — the canonical classes,
not copies.

Event source
------------
Two front doors into each hosted detector, matching the SDK's §02
InferenceSubscriber shape:

* **Frame polling (active, default)** — identical driving model to the
  legacy loops: every ``interval_s`` the host fetches a frame through
  the agent's ``CameraContext`` and runs the agent's detection client,
  then wraps the result as a §12 ``InferenceCompletedEvent`` dict and
  feeds ``Detector.handle_event``. Works with no NATS configured, and
  with the synthetic demo client.
* **``feed_event(event)`` (passive)** — hand a decoded
  ``opennvr.inference.*`` event straight to every hosted detector.
  This is the in-process NATS bridge point for events off the bus to
  drive the same rule instances with zero extra inference. NOT YET
  WIRED: the agent's event subscriber doesn't call it in this release
  — hosted monitors are driven by the active poll path only. Note for
  whoever wires it: unlike the poll path, raw bus events bypass
  ``_adapt_detections``/``_adapt_tracks`` normalisation, so the two
  paths can disagree on labels/tracks until that's unified.

Async-loop hygiene
------------------
Hosted detectors share the agent's event loop with the Pipecat
pipeline. ``Detector.handle_event`` is synchronous but cheap (pure
geometry/state-machine math over ≤64 detections), and the alert bridge
below never blocks: it appends to in-memory lists and calls
``Notifier.fire`` (which schedules a task). The only awaits in the
poll loop are the frame fetch and the adapter call — the same awaits
the legacy loop did.

Identity
--------
Each hosted detector emits alerts AS its own monitor: the SDK's
ContextVar-scoped default source (see ``opennvr_app_sdk.alerts``) is
installed around every ``on_detections`` call, and the host gives each
detector instance a per-monitor source block
(``"<rule-app-id>.monitor-<id>"``), so several monitors in this one
process never clobber each other's ``source`` even when their handler
calls interleave on the loop.
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import math
import os
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from context import spawn_unscoped
from opennvr_app_sdk.alerts import Alert, AlertDispatcher
from opennvr_app_sdk.geometry import Tripwire, Zone

logger = logging.getLogger(__name__)


# ── Rule library loading (the example modules ARE the library) ──────

# Where the rule-library example dirs live. In the dev/examples-tree layout
# this file sits in examples/camera-agent/, so its parent is examples/. In a
# packaged container the agent modules are flattened under /app and the rule
# dirs are copied to a separate location, so OPENNVR_RULES_DIR overrides this
# (the Dockerfile sets it). Keeping the default = parent means dev + tests are
# unchanged.
_EXAMPLES_DIR = Path(os.environ["OPENNVR_RULES_DIR"]).resolve() \
    if os.environ.get("OPENNVR_RULES_DIR") \
    else Path(__file__).resolve().parent.parent

# rule name → (example dir, module file, module name)
_RULE_MODULES: dict[str, tuple[str, str, str]] = {
    "occupancy": ("occupancy-counting", "occupancy_counting.py", "occupancy_counting"),
    "line_crossing": ("line-crossing", "line_crossing.py", "line_crossing"),
}

RULES = tuple(_RULE_MODULES)


def _load_rule_module(rule: str) -> Any:
    """Import the example app's module by file path (cached in
    ``sys.modules`` under its canonical name)."""
    dirname, filename, modname = _RULE_MODULES[rule]
    cached = sys.modules.get(modname)
    if cached is not None:
        return cached
    path = _EXAMPLES_DIR / dirname / filename
    if not path.exists():
        raise RuntimeError(
            f"rule library module for {rule!r} not found at {path} — "
            f"the camera-agent expects to live in the examples/ tree "
            f"next to {dirname}/"
        )
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"could not build import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(modname, None)
        raise
    return module


# ── Alert bridge (custom AlertChannel) ──────────────────────────────


class AgentAlertChannel:
    """SDK :class:`~opennvr_app_sdk.alerts.AlertChannel` that routes
    alerts into the agent instead of (or alongside) webhooks/NATS.

    ``send`` is called synchronously from ``Detector.handle_event`` on
    the agent's event loop, so the sink MUST NOT block — the host's
    sink only updates in-memory tallies and (when the monitor wants
    notifications) calls the agent's fire-and-forget notify path.
    """

    name = "camera-agent"

    def __init__(self, sink: Callable[[Alert], None]) -> None:
        self._sink = sink

    def send(self, alert: Alert) -> bool:
        # AlertDispatcher.fire already isolates exceptions per channel;
        # returning True marks in-process delivery as succeeded.
        self._sink(alert)
        return True


# ── Hosted monitor record ───────────────────────────────────────────

# "No ceiling" sentinel for counting-only occupancy monitors: the
# occupancy example requires max_occupancy, but a plain "count people"
# watch must never fire OVER.
_NO_THRESHOLD = 10**9

# Legacy LineCounter direction mapping. For the tripwire A→B (same
# signed-side formula as camera_agent._line_side), a track that ends on
# the POSITIVE side was counted "in" by the legacy counter; the SDK
# names that crossing "b_to_a" (started right of A→B). See
# opennvr_app_sdk.geometry.Tripwire.crossing.
_DIRECTION_TO_FLOW = {"b_to_a": "in", "a_to_b": "out"}


@dataclass
class HostedMonitor:
    """One conversational monitor backed by an SDK Detector instance."""

    id: int
    rule: str                       # "occupancy" | "line_crossing"
    camera_ids: list[str]
    target: str
    interval_s: float
    notify_on_alert: bool
    detector: Any = None            # the SDK Detector instance
    task: asyncio.Task | None = None
    active: bool = True
    # Set synchronously by MonitorHost.stop() BEFORE the task is
    # cancelled — the alert bridge drops anything a stopped monitor's
    # still-unwinding poll task fires (see MonitorHost._on_alert).
    stopped: bool = False
    # Set when the poll task dies to an unexpected exception; surfaced
    # as ``status: "error: …"`` in to_dict()/list()/snapshot().
    error: str | None = None
    alerts_fired: int = 0
    # Optional ``(camera_id, current, peak_candidate)`` callback — the
    # MonitorManager wires this to its Monitor.current/peak dicts.
    counts_sink: Callable[[str, int, int], None] | None = None
    # Per-camera crossing tallies ({"in": n, "out": n}); occupancy
    # counts are read from the detector's own state_snapshot().
    tallies: dict[str, dict[str, int]] = field(default_factory=dict)
    recent_alerts: deque = field(default_factory=lambda: deque(maxlen=20))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "rule": self.rule,
            "camera_ids": list(self.camera_ids),
            "target": self.target,
            "interval_s": self.interval_s,
            "active": self.active,
            "status": (
                f"error: {self.error}" if self.error
                else ("active" if self.active else "stopped")
            ),
            "alerts_fired": self.alerts_fired,
        }


# ── The host ────────────────────────────────────────────────────────


class MonitorHost:
    """Maps conversational monitor requests onto SDK Detector instances
    running in the agent's process.

    Parameters
    ----------
    get_frame:
        ``async (camera_id) -> jpeg bytes`` — the agent's existing
        frame source (``CameraContext.get_frame``).
    infer:
        ``async (frame_jpeg=..., extra=...) -> response dict`` — the
        agent's detection client (real KAI-C adapter or the synthetic
        demo client).
    notify:
        ``(monitor_id, alert) -> None`` — the agent's notify machinery
        (notification list + webhook fan-out). Called only for
        monitors created with alerting enabled; must not block.
    dedup:
        Optional ``(detections) -> detections`` de-duplicator applied
        before occupancy counting — the agent passes its per-label IoU
        NMS so converged counts match the legacy ``_count_target``.
    stop_check:
        Optional ``() -> bool``; when true the poll loops exit (wired
        to the runtime's stop event, like the legacy loops).
    """

    def __init__(
        self,
        *,
        get_frame: Callable[[str], Awaitable[bytes]],
        infer: Callable[..., Awaitable[dict[str, Any]]],
        notify: Callable[[int, Alert], None] | None = None,
        dedup: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
        stop_check: Callable[[], bool] | None = None,
        busy_check: Callable[[], bool] | None = None,
        default_interval_s: float = 8.0,
    ) -> None:
        self._get_frame = get_frame
        self._infer = infer
        self._notify = notify
        self._dedup = dedup
        self._stop_check = stop_check or (lambda: False)
        # True while an interactive turn is in flight — the poll loop yields
        # its inference for that cycle so the user's turn isn't starved.
        self._busy_check = busy_check or (lambda: False)
        self._default_interval = float(default_interval_s)
        self._monitors: dict[int, HostedMonitor] = {}
        self._next_id = 1

    # ── Public API ─────────────────────────────────────────────────

    def create(
        self,
        rule: str,
        camera_ids: list[str] | str,
        params: dict[str, Any] | None = None,
        *,
        monitor_id: int | None = None,
        counts_sink: Callable[[str, int, int], None] | None = None,
    ) -> int:
        """Build + start one SDK-backed monitor. Returns its id.

        Raises ``ValueError`` with an operator/LLM-relayable message
        when the params don't satisfy the rule's expectations — no
        monitor is registered and no task is spawned in that case.
        """
        if rule not in _RULE_MODULES:
            raise ValueError(
                f"unknown rule {rule!r} — converged rules are: "
                f"{', '.join(sorted(_RULE_MODULES))}"
            )
        cams = [camera_ids] if isinstance(camera_ids, str) else list(camera_ids)
        cams = [str(c).strip() for c in cams if str(c).strip()]
        if not cams:
            raise ValueError("at least one camera_id is required")
        params = dict(params or {})

        target = str(params.get("target") or "").strip().lower()
        if not target:
            raise ValueError(
                "'target' is required — what should the monitor watch "
                "for (e.g. 'person', 'car')?"
            )
        interval_s = self._parse_interval(params)

        mid = self._alloc_id(monitor_id)
        mon = HostedMonitor(
            id=mid,
            rule=rule,
            camera_ids=cams,
            target=target,
            interval_s=interval_s,
            notify_on_alert=self._wants_alerts(rule, params),
            tallies={cam: {"in": 0, "out": 0} for cam in cams},
            counts_sink=counts_sink,
        )

        # Every alert this detector fires lands here (the dispatcher's
        # only channel) — SDK rules never learn about the agent.
        dispatcher = AlertDispatcher(
            [AgentAlertChannel(lambda alert, _mon=mon: self._on_alert(_mon, alert))]
        )
        if rule == "occupancy":
            detector = self._build_occupancy(cams, target, params, dispatcher)
        else:
            detector = self._build_line_crossing(cams, target, params, dispatcher)

        # Per-monitor alert identity: the Detector base scopes its
        # ``_source_block`` around each handler call via the SDK's
        # ContextVar (built exactly for multiple detectors sharing one
        # process); qualify the name so two monitors on the same rule
        # are distinguishable in their alerts' §11.5 source block.
        base = detector._source_block or {
            "kind": "app", "name": rule, "version": "1.0.0",
        }
        detector._source_block = {
            **base, "name": f"{base['name']}.monitor-{mid}",
        }

        mon.detector = detector
        self._monitors[mid] = mon
        mon.task = spawn_unscoped(self._loop(mon), name=f"sdk-monitor-{mid}")
        logger.info(
            "hosted monitor #%d started: %s %r on %s (interval %.1fs, alerts %s)",
            mid, rule, target, cams, interval_s,
            "on" if mon.notify_on_alert else "off",
        )
        return mid

    def stop(self, monitor_id: int) -> bool:
        """Stop + forget one hosted monitor. The ``stopped`` flag is set
        synchronously BEFORE the task is cancelled: a poll task resumed
        from its awaits after this returns can still run one last sync
        ``handle_event`` before the CancelledError lands, and the alert
        bridge drops anything a stopped monitor fires (``_on_alert``)."""
        mon = self._monitors.pop(monitor_id, None)
        if mon is None:
            return False
        mon.stopped = True
        mon.active = False
        if mon.task is not None:
            mon.task.cancel()
            mon.task = None
        logger.info("hosted monitor #%d stopped", monitor_id)
        return True

    def stop_all(self) -> None:
        for mid in list(self._monitors):
            self.stop(mid)

    def list(self) -> list[dict[str, Any]]:
        return [m.to_dict() for m in self._monitors.values()]

    def get(self, monitor_id: int) -> HostedMonitor | None:
        return self._monitors.get(monitor_id)

    def snapshot(self, monitor_id: int | None = None) -> dict[str, Any]:
        """Live rule state per hosted monitor: the detector's own
        ``state_snapshot()`` (when it has one) plus the host's
        crossing tallies and recent alerts."""
        monitors = (
            [self._monitors[monitor_id]]
            if monitor_id is not None and monitor_id in self._monitors
            else list(self._monitors.values())
        )
        out: dict[str, Any] = {}
        for mon in monitors:
            snap = getattr(mon.detector, "state_snapshot", None)
            out[str(mon.id)] = {
                **mon.to_dict(),
                "detector_state": snap() if callable(snap) else {},
                "tallies": {c: dict(t) for c, t in mon.tallies.items()},
                "recent_alerts": [a.to_wire() for a in mon.recent_alerts],
            }
        return out

    def feed_event(self, event: dict[str, Any]) -> list[Alert]:
        """NATS front door: hand one decoded ``opennvr.inference.*``
        event to every active hosted detector (each drops cameras it
        doesn't watch). Returns every alert fired."""
        fired: list[Alert] = []
        camera_id = event.get("camera_id") if isinstance(event, dict) else None
        for mon in list(self._monitors.values()):
            if not mon.active:
                continue
            # Isolate per monitor: Detector.handle_event does NOT swallow
            # on_detections exceptions (only the NATS _handle_raw path
            # does), so one raising rule must not kill the caller — or
            # starve every other hosted monitor of the event.
            try:
                fired.extend(mon.detector.handle_event(event))
            except Exception:
                logger.exception(
                    "monitor #%s: detector raised on fed event; skipping",
                    mon.id,
                )
                continue
            if isinstance(camera_id, str) and camera_id in mon.camera_ids:
                self._push_counts(mon, camera_id)
        return fired

    # ── Rule config builders (programmatic, validated) ─────────────

    def _build_occupancy(
        self,
        cams: list[str],
        target: str,
        params: dict[str, Any],
        dispatcher: AlertDispatcher,
    ) -> Any:
        occ = _load_rule_module("occupancy")
        max_count = self._parse_count(params, "max_count", alias="max_occupancy")
        min_count = self._parse_count(params, "min_count", alias="min_occupancy")
        if max_count is not None and min_count is not None and min_count > max_count:
            raise ValueError(
                f"'min_count' ({min_count}) must be <= 'max_count' ({max_count})"
            )
        try:
            debounce = int(params.get("debounce_frames", 1))
        except (TypeError, ValueError):
            raise ValueError("'debounce_frames' must be a whole number >= 1") from None
        if debounce < 1:
            raise ValueError("'debounce_frames' must be a whole number >= 1")

        # A plain "count" watch has no zone — the whole frame counts.
        # Generously oversized so any bbox center (normalized corner- or
        # center-form) is inside; the geometry is irrelevant in count
        # mode, only the label filter matters (legacy parity).
        zone = Zone.from_config(
            name="whole-frame",
            vertices=[(-100.0, -100.0), (100.0, -100.0), (100.0, 100.0), (-100.0, 100.0)],
        )
        cameras = {
            cam: occ.CameraZone(
                camera_id=cam,
                zone=zone,
                frame_width=1,   # detections stay in normalized coords
                frame_height=1,
                max_occupancy=max_count if max_count is not None else _NO_THRESHOLD,
                min_occupancy=min_count,
            )
            for cam in cams
        }
        cfg = occ.AppConfig(
            nats_url="in-process",  # never used: the host drives handle_event
            nats_token=None,
            subject_pattern="opennvr.inference.>",
            watch_labels=[target],
            debounce_frames=debounce,
            clear_alerts=bool(params.get("clear_alerts", False)),
            cameras=cameras,
            webhook_url=None,
        )
        return occ.OccupancyCounter(cfg, dispatcher)

    def _build_line_crossing(
        self,
        cams: list[str],
        target: str,
        params: dict[str, Any],
        dispatcher: AlertDispatcher,
    ) -> Any:
        lc = _load_rule_module("line_crossing")
        line = params.get("line")
        if not (isinstance(line, (list, tuple)) and len(line) == 4):
            raise ValueError(
                "'line' is required as [x1, y1, x2, y2] in 0-1 frame "
                "coordinates — where should the counting line go?"
            )
        try:
            x1, y1, x2, y2 = (float(v) for v in line)
        except (TypeError, ValueError):
            raise ValueError("'line' values must all be numbers") from None
        if not all(math.isfinite(v) for v in (x1, y1, x2, y2)):
            # json.loads accepts NaN/Infinity; a NaN endpoint passes
            # Tripwire's degenerate-line check (nan != 0 comparisons) but
            # every crossing() comparison involving NaN is False — the watch
            # would be created "successfully" and then silently never fire.
            raise ValueError("'line' values must all be finite numbers")
        direction = str(params.get("direction") or "both").strip()
        # Tripwire itself rejects a degenerate line (A == B) and a bad
        # count_direction with operator-readable ValueErrors.
        wire = Tripwire.from_config(
            name=str(params.get("line_name") or "monitor-line"),
            a=(x1, y1), b=(x2, y2), count_direction=direction,
        )
        try:
            track_ttl = float(params.get("track_ttl_seconds", 30.0))
        except (TypeError, ValueError):
            raise ValueError("'track_ttl_seconds' must be a number > 0") from None
        if not math.isfinite(track_ttl) or track_ttl <= 0:
            raise ValueError("'track_ttl_seconds' must be a finite number > 0")

        cameras = {
            cam: lc.CameraWire(
                camera_id=cam,
                wire=wire,
                frame_width=1,   # the agent's lines/tracks are normalized
                frame_height=1,
            )
            for cam in cams
        }
        cfg = lc.AppConfig(
            nats_url="in-process",  # never used: the host drives handle_event
            nats_token=None,
            subject_pattern="opennvr.inference.>",
            watch_labels=[target],
            track_ttl_seconds=track_ttl,
            cameras=cameras,
            webhook_url=None,
        )
        return lc.LineCrossingDetector(cfg, dispatcher)

    # ── Param helpers ──────────────────────────────────────────────

    def _parse_interval(self, params: dict[str, Any]) -> float:
        raw = params.get("interval_s")
        if raw is None:
            return self._default_interval
        try:
            interval = float(raw)
        except (TypeError, ValueError):
            raise ValueError("'interval_s' must be a number > 0") from None
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("'interval_s' must be a finite number > 0")
        return interval

    @staticmethod
    def _parse_count(
        params: dict[str, Any], key: str, *, alias: str,
    ) -> int | None:
        raw = params.get(key, params.get(alias))
        if raw is None:
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"'{key}' must be a whole number >= 0") from None
        if value < 0:
            raise ValueError(f"'{key}' must be a whole number >= 0")
        return value

    @staticmethod
    def _wants_alerts(rule: str, params: dict[str, Any]) -> bool:
        """Alerting is opt-in: explicit ``notify_on_alert`` or, for
        occupancy, the presence of a threshold ("alert if >3 cars").
        The create_monitor tool path passes neither, so converged
        count/crossing watches stay silent tallies — exactly the
        legacy behavior."""
        if params.get("notify_on_alert") is not None:
            return bool(params["notify_on_alert"])
        if rule == "occupancy":
            return (
                params.get("max_count", params.get("max_occupancy")) is not None
                or params.get("min_count", params.get("min_occupancy")) is not None
            )
        return False

    def _alloc_id(self, monitor_id: int | None) -> int:
        if monitor_id is None:
            monitor_id = self._next_id
        if monitor_id in self._monitors:
            raise ValueError(f"monitor id {monitor_id} is already in use")
        self._next_id = max(self._next_id, int(monitor_id)) + 1
        return int(monitor_id)

    # ── Alert sink ─────────────────────────────────────────────────

    def _on_alert(self, mon: HostedMonitor, alert: Alert) -> None:
        if mon.stopped:
            # Post-stop straggler: cancellation only lands at the poll
            # task's next await, so a poll resumed from its frame/infer
            # awaits after stop() can run one more sync handle_event.
            # The monitor is deleted — drop everything it fires.
            return
        mon.alerts_fired += 1
        mon.recent_alerts.append(alert)
        if mon.rule == "line_crossing":
            flow = _DIRECTION_TO_FLOW.get(str(alert.evidence.get("direction")))
            tally = mon.tallies.setdefault(alert.camera_id, {"in": 0, "out": 0})
            if flow:
                tally[flow] += 1
        if mon.notify_on_alert and self._notify is not None:
            self._notify(mon.id, alert)

    # ── Poll loop (frame front door) ───────────────────────────────

    async def _loop(self, mon: HostedMonitor) -> None:
        try:
            while mon.active and not self._stop_check():
                # Yield this cycle to an in-flight interactive turn.
                if not self._busy_check():
                    for cam in mon.camera_ids:
                        if not mon.active:
                            break
                        await self._poll(mon, cam)
                await asyncio.sleep(mon.interval_s)
        except asyncio.CancelledError:  # pragma: no cover
            pass
        except Exception as exc:
            # Task death must not leave a silent zombie: mark the
            # monitor so list()/snapshot() (and the /monitors endpoint)
            # show it errored instead of still "active" with no task.
            mon.active = False
            mon.error = f"{type(exc).__name__}: {exc}"
            logger.exception("hosted monitor #%d loop crashed", mon.id)

    async def _poll(self, mon: HostedMonitor, cam: str) -> None:
        try:
            frame = await self._get_frame(cam)
            extra = {"task": "track"} if mon.rule == "line_crossing" else None
            resp = await self._infer(frame_jpeg=frame, extra=extra)
        except Exception as exc:
            logger.info("hosted monitor #%d: poll of %s failed (%s)", mon.id, cam, exc)
            return
        result = resp.get("result") or {} if isinstance(resp, dict) else {}
        event = self._as_inference_event(mon, cam, result)
        try:
            # handle_event installs this monitor's ContextVar-scoped
            # source, runs the rule, and dispatches alerts through the
            # bridge channel. Sync + cheap: it never blocks the loop.
            mon.detector.handle_event(event)
        except Exception:  # pragma: no cover - Detector already isolates
            logger.exception("hosted monitor #%d handle_event failed", mon.id)
            return
        self._push_counts(mon, cam)

    def _push_counts(self, mon: HostedMonitor, cam: str) -> None:
        """Derive (current, peak-candidate) for one camera and hand it
        to the sink — the legacy Monitor.current/peak semantics."""
        if mon.counts_sink is None:
            return
        if mon.rule == "line_crossing":
            tally = mon.tallies.setdefault(cam, {"in": 0, "out": 0})
            current = tally["in"] - tally["out"]   # net, like LineCounter
            peak_candidate = tally["in"]           # legacy: peak = max in
        else:
            snap = mon.detector.state_snapshot().get("cameras", {})
            current = int(snap.get(cam, {}).get("last_count", 0))
            peak_candidate = current
        mon.counts_sink(cam, current, peak_candidate)

    # ── InferenceCompletedEvent adaptation ─────────────────────────

    def _as_inference_event(
        self, mon: HostedMonitor, cam: str, result: dict[str, Any],
    ) -> dict[str, Any]:
        """Wrap one adapter response as the §12 event dict the SDK
        Detector consumes, normalizing the shapes the agent's legacy
        loops tolerated (label under ``class``, tracks with ``center``
        instead of ``bbox``, list-form bboxes, missing labels on
        tracked objects)."""
        if mon.rule == "line_crossing":
            detections = self._adapt_tracks(mon, result)
        else:
            detections = self._adapt_detections(result)
        return {"camera_id": cam, "result": {"detections": detections}}

    def _adapt_detections(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        raw = result.get("detections") or []
        if not isinstance(raw, list):
            return []
        raw = [d for d in raw[:64] if isinstance(d, dict)]
        if self._dedup is not None:
            # Legacy parity: the agent's per-label IoU NMS ran before
            # counting (the YOLOv8 adapter doesn't always NMS).
            raw = self._dedup(raw)
        out: list[dict[str, Any]] = []
        for det in raw:
            label = str(det.get("label") or det.get("class") or "").strip()
            bbox = det.get("bbox")
            if not isinstance(bbox, dict):
                # Occupancy needs a bbox center; the legacy counter
                # counted label matches regardless. Synthesize an
                # origin box (inside the whole-frame zone).
                bbox = {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}
            out.append({**det, "label": label, "bbox": bbox})
        return out

    def _adapt_tracks(
        self, mon: HostedMonitor, result: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Mirror the legacy ``MonitorManager._extract_tracks`` walk:
        tracks may carry ``center`` or dict/list bboxes, ids under
        ``track_id`` or ``id``, and unlabeled tracks count as the
        target (a tracker that drops labels shouldn't zero the tally)."""
        raw = result.get("tracks") or result.get("detections") or []
        if not isinstance(raw, list):
            return []
        out: list[dict[str, Any]] = []
        for tr in raw:
            if not isinstance(tr, dict):
                continue
            label = str(tr.get("label") or tr.get("class") or "").strip().lower()
            if label and label != mon.target:
                continue
            tid = tr.get("track_id", tr.get("id"))
            if tid is None:
                continue
            center = tr.get("center")
            if isinstance(center, (list, tuple)) and len(center) >= 2:
                x, y = float(center[0]), float(center[1])
            else:
                bb = tr.get("bbox") or {}
                if isinstance(bb, dict):
                    x = float(bb.get("x", 0)) + float(bb.get("w", 0)) / 2
                    y = float(bb.get("y", 0)) + float(bb.get("h", 0)) / 2
                elif isinstance(bb, (list, tuple)) and len(bb) >= 4:
                    x = float(bb[0]) + float(bb[2]) / 2
                    y = float(bb[1]) + float(bb[3]) / 2
                else:
                    continue
            out.append({
                "label": label or mon.target,
                "track_id": tid,
                # Zero-size bbox whose corner is the center: with the
                # 1×1 frame, geometry.bbox_center returns (x, y) back.
                "bbox": {"x": x, "y": y, "w": 0.0, "h": 0.0},
            })
        return out


__all__ = ["AgentAlertChannel", "HostedMonitor", "MonitorHost", "RULES"]
