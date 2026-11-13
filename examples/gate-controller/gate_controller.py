# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
gate-controller — the hardware half of gate automation.

The decision and the barrier are deliberately two apps. The License
Plate Recognition app knows WHO may enter — its register, allowlist,
monitors — and publishes every judgement as a contracted
``access.decided.v1`` fact (docs/EVENT_CONTRACTS.md). This app knows
WHICH wiring opens WHICH gate, and judges nothing.

That split is not tidiness, it is how the industry builds this, for
three reasons worth holding on to while reading the code:

**Safety is not ours.** Under UL 325 and EN 12453 entrapment
protection lives in the gate operator, which must monitor its own
safety devices on every cycle. This app is an accessory input in that
model — the same class as the push button by the gate. So it asks for
a momentary open, never claims to be a safety device, never holds a
barrier by re-pulsing a momentary input, and caps how long a hold can
last without a person knowing.

**A plate is a weak credential.** Anyone can print one. Because the
decision arrives as an event rather than a function call, a second
factor fits between deciding and opening without touching either side.
The local guards here — a confidence floor, an allowed-reason list, a
repeat-plate rate limit — are that seam being used, not the plate
reader's policy being duplicated.

**The audit outlives the hardware.** Every decision is recorded
whether the relay answered, was in dry run, or was never wired.

Zero inference and zero model access: a NATS subscription, a state
machine, and a contact closure.

Known limitation: actuation is synchronous on the event loop, because
``handle_event`` returns its alerts. A transport whose device has gone
unreachable therefore blocks the loop for up to its timeout (3 s) while
it fails — delaying the tick and other gates' decisions. Bounded and
rare, but real; reading positions back was moved to a thread for the
same reason and actuation should follow.

Run:
    python gate_controller.py --config config.yml
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from opennvr_app_sdk import (
    Action,
    Alert,
    AlertSource,
    AlertType,
    AppManifest,
    Detector,
    Entity,
    Param,
    StateView,
    app,
    set_default_source,
)

from transports import PROFILES, Opener, OpenerError, StuckClosed, build_opener

logger = logging.getLogger("gate-controller")

#: The contracted decision event this app consumes.
DECISION_SUBJECT_PATTERN = "opennvr.events.access.decided.v1.>"
DECISION_SCHEMA = "access.decided.v1"

# ── Gate states ─────────────────────────────────────────────────────
CLOSED, OPEN, OPENING, CLOSING = "closed", "open", "opening", "closing"
HELD, FAULTED, UNKNOWN = "held", "faulted", "unknown"

# ── What happened, for the timeline ─────────────────────────────────
OPENED, REFUSED, HELD_ACT, RELEASED = "opened", "refused", "held", "released"
FAULT, TEST, NO_RELAY, COOLDOWN = "fault", "test", "no_relay", "cooldown"

#: A hold nobody ends is a gate standing open all night. Even an
#: explicit "until released" is capped; an operator can always hold again.
ABSOLUTE_MAX_HOLD_MINUTES = 12 * 60

#: Ceiling on the repeat-plate memory per gate, so a busy gate cannot
#: grow it without bound inside a single window.
_MAX_TRACKED_PLATES = 2000

SAFETY_NOTE = (
    "This app requests an open; your gate operator owns safety "
    "(UL 325 / EN 12453). Keep its photo-eyes and edges wired and tested."
)


def _handle(key: Any) -> str:
    """Gate keys may be numeric core ids (``"3"``) or handles
    (``"cam3"``) — one normalisation, same as the LPR app."""
    k = str(key).strip()
    if not k:
        return ""
    return k if k.startswith("cam") else f"cam{k}"


# ── Hold-open schedules ─────────────────────────────────────────────

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_GROUPS = {"weekdays": [0, 1, 2, 3, 4], "weekends": [5, 6],
           "daily": [0, 1, 2, 3, 4, 5, 6], "all": [0, 1, 2, 3, 4, 5, 6]}


@dataclass
class HoldWindow:
    """"Stand open from 08:00 to 09:00 on weekdays."

    The one scheduling feature every barrier controller has, because it
    is the one sites all want: the morning rush, the delivery window,
    the shift change. Outside it the gate goes back to deciding per
    vehicle.
    """

    start: int          # minutes since midnight
    end: int
    days: list[int]
    label: str = ""

    def active_at(self, when: datetime) -> bool:
        minute = when.hour * 60 + when.minute
        if self.start <= self.end:
            return when.weekday() in self.days and self.start <= minute < self.end
        # Crosses midnight: the tail belongs to the previous day's window.
        if when.weekday() in self.days and minute >= self.start:
            return True
        return ((when.weekday() - 1) % 7) in self.days and minute < self.end

    def minutes_left(self, when: datetime) -> float:
        """Minutes from ``when`` until this window closes. Only
        meaningful while the window is active."""
        minute = when.hour * 60 + when.minute + when.second / 60.0
        end = float(self.end)
        if self.start <= self.end:
            return max(1.0, end - minute)
        # Crosses midnight: the end is tomorrow if we are past the start.
        return max(1.0, (end + 24 * 60 - minute) if minute >= self.start
                   else end - minute)

    def describe(self) -> str:
        if self.label:
            return self.label
        days = ("every day" if len(self.days) == 7
                else "Mon–Fri" if self.days == [0, 1, 2, 3, 4]
                else "Sat–Sun" if self.days == [5, 6]
                else ", ".join(_DAY_NAMES[d] for d in self.days))
        return f"Held open {_hhmm(self.start)}–{_hhmm(self.end)} ({days})"


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _parse_time(raw: Any) -> int:
    parts = str(raw or "").strip().split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError) as exc:
        raise ValueError(f"time must be HH:MM, got {raw!r}") from exc
    if not (0 <= hour <= 24 and 0 <= minute < 60):
        raise ValueError(f"time out of range: {raw!r}")
    return min(hour * 60 + minute, 24 * 60)


def parse_schedule(raw: Any) -> list[HoldWindow]:
    """``[{start, end, days}]`` → windows. A malformed entry is skipped
    with a warning: a typo in one window must not take a gate's whole
    schedule — or the app — down with it."""
    windows: list[HoldWindow] = []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return windows
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            start = _parse_time(entry.get("start"))
            end = _parse_time(entry.get("end"))
        except ValueError as exc:
            logger.warning("skipping hold window: %s", exc)
            continue
        days_raw = entry.get("days", "daily")
        days: list[int] = []
        if isinstance(days_raw, str):
            days = list(_GROUPS.get(days_raw.strip().lower(), []))
            if not days:
                for part in days_raw.split(","):
                    index = _DAYS.get(part.strip().lower()[:3])
                    if index is not None:
                        days.append(index)
        elif isinstance(days_raw, list):
            for part in days_raw:
                index = (int(part) if isinstance(part, int)
                         else _DAYS.get(str(part).strip().lower()[:3]))
                if index is not None and 0 <= index <= 6:
                    days.append(index)
        if not days:
            days = list(range(7))
        windows.append(HoldWindow(start, end, sorted(set(days)),
                                  str(entry.get("label") or "")))
    return windows


# ── One gate ────────────────────────────────────────────────────────


@dataclass
class Gate:
    """A barrier, its wiring, and what we believe it is doing."""

    camera_id: str
    name: str
    opener: Opener | None
    schedule: list[HoldWindow] = field(default_factory=list)
    #: True only when the wiring can report the barrier's real
    #: position. When False the page says so rather than inventing one.
    monitored: bool = False

    state: str = CLOSED
    since: float = field(default_factory=time.time)
    #: Epoch when a hold ends; None when not held.
    held_until: float | None = None
    hold_reason: str = ""
    held_by_schedule: bool = False

    #: The schedule window this gate has already tried to apply. A
    #: window that cannot be honoured (momentary-only wiring, a latch
    #: that refused) must be reported ONCE, not on every one-second
    #: tick — 3,600 rows an hour would evict every real event from the
    #: log and drown the page.
    schedule_tried: str = ""
    last_pulse: float = 0.0            # monotonic
    opened_today: int = 0
    faults_today: int = 0
    last_event: dict[str, Any] | None = None
    #: Epoch of the last successful open. Kept on the gate rather than
    #: searched out of the event deque, which holds only 200 rows — a
    #: quiet gate's Home Assistant timestamp went unavailable as soon as
    #: busier gates pushed its last open off the end.
    last_open: float | None = None
    fault_note: str = ""
    #: Plate → monotonic timestamps, for the repeat-plate guard.
    recent_plates: dict[str, list[float]] = field(default_factory=dict)

    def set_state(self, state: str, now: float | None = None,
                  refresh: bool = False) -> None:
        """Move to ``state``. ``refresh`` re-stamps ``since`` even when
        the state is unchanged — a second car opening an already-open
        unmonitored gate restarts its settle timer, instead of the
        display snapping to "closed" seconds after a real pulse."""
        if state != self.state:
            self.state = state
            self.since = now or time.time()
        elif refresh:
            self.since = now or time.time()


# ── Config ──────────────────────────────────────────────────────────


@dataclass
class AppConfig:
    """Operator-tunable settings. Validated in ``load_config``."""

    nats_url: str
    nats_token: str | None = None
    subject_pattern: str = DECISION_SUBJECT_PATTERN

    gates: dict[str, Any] = field(default_factory=dict)
    pulse_cooldown_seconds: float = 5.0
    dry_run: bool = False

    #: How long a gate that cannot report its position is *shown* as
    #: open after a pulse. Display only — nothing is actuated on it.
    assumed_open_seconds: float = 20.0
    #: How often to read back the position of gates that can report one.
    poll_seconds: float = 5.0
    #: Longest a hold may last. 0 disables holds entirely.
    max_hold_minutes: float = 120.0

    # ── Local guards (defence in depth; the LPR app still decides) ──
    min_confidence: float = 0.0
    allowed_reasons: list[str] = field(default_factory=list)
    max_opens_per_plate: int = 0
    max_opens_window_minutes: float = 10.0

    # Alert delivery (SDK stack: stdout always; webhook/NATS opt-in).
    webhook_url: str | None = None
    nats_alerts_url: str | None = None
    nats_alerts_token: str | None = None

    # App contract (spec §03) — /health /manifest /state + registry
    # self-registration, all owned by the SDK.
    contract_port: int | None = None
    contract_bind_host: str | None = None
    contract_host: str | None = None
    opennvr_url: str | None = None
    opennvr_token: str | None = None


def load_config(path: str | Path) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"config file {path} did not parse to a dict")
    nats_url = raw.get("nats_url")
    if not nats_url:
        raise ValueError(
            "config: nats_url is required — this app consumes the "
            "platform's access.decided.v1 events from the bus")
    # 1.0 called it `relays` and every value was a bare URL. Still loads.
    gates = raw.get("gates")
    if gates is None:
        gates = raw.get("relays")
    return AppConfig(
        nats_url=str(nats_url),
        nats_token=raw.get("nats_token") or None,
        subject_pattern=str(raw.get("subject_pattern") or DECISION_SUBJECT_PATTERN),
        gates=dict(gates or {}),
        pulse_cooldown_seconds=float(raw.get("pulse_cooldown_seconds", 5.0)),
        dry_run=bool(raw.get("dry_run", False)),
        assumed_open_seconds=float(raw.get("assumed_open_seconds", 20.0)),
        poll_seconds=float(raw.get("poll_seconds", 5.0)),
        max_hold_minutes=float(raw.get("max_hold_minutes", 120.0)),
        min_confidence=float(raw.get("min_confidence", 0.0)),
        allowed_reasons=[str(r).strip().lower()
                         for r in (raw.get("allowed_reasons") or [])],
        max_opens_per_plate=int(raw.get("max_opens_per_plate", 0)),
        max_opens_window_minutes=float(raw.get("max_opens_window_minutes", 10.0)),
        webhook_url=raw.get("webhook_url"),
        nats_alerts_url=raw.get("nats_alerts_url"),
        nats_alerts_token=raw.get("nats_alerts_token"),
        contract_port=(int(raw["contract_port"])
                       if raw.get("contract_port") is not None else None),
        contract_bind_host=raw.get("contract_bind_host"),
        contract_host=raw.get("contract_host"),
        opennvr_url=raw.get("opennvr_url"),
        opennvr_token=raw.get("opennvr_token"),
    )


MANIFEST = AppManifest(
    id="gate-controller",
    # No camera picker: this app acts on decisions other apps publish,
    # which those apps have already limited to the cameras they picked.
    camera_picker=False,
    name="Gate Controller",
    version="1.1.0",
    category="automation",
    summary=(
        "Opens the barrier for allowed vehicles. Consumes the platform's "
        "access.decided.v1 events and drives dry contact, an IP relay, "
        "Modbus or an ONVIF door controller; deny and anything unknown "
        "actuate nothing. Live gate state, hold-open schedules, manual "
        "control, and an audit line for every decision."
    ),
    requires_tasks=[],
    requires_scopes=["events:access.decided"],
    subscribes=DECISION_SUBJECT_PATTERN,
    provides=["gates"],
    params=[
        Param("gates", dict, default={},
              description=(
                  "Which wiring opens which gate: {camera: {profile, host, …}}. "
                  "Cameras by core id ('3') or handle ('cam3'). Transports: "
                  "dry_contact (GPIO — every barrier accepts one), http "
                  "(Shelly, Tasmota, ESPHome), modbus, onvif (Profile C), "
                  "mqtt. A bare URL string still works.")),
        Param("pulse_cooldown_seconds", float, default=5.0,
              description=(
                  "Per-gate re-trigger suppression — one car, one pulse, even "
                  "when several allow decisions land while the boom is up.")),
        Param("dry_run", bool, default=False,
              description=(
                  "Record as if opening, without touching hardware — for "
                  "commissioning a site safely.")),
        Param("assumed_open_seconds", float, default=20.0,
              description=(
                  "How long a gate that cannot report its position is shown "
                  "as open after a pulse. Display only.")),
        Param("poll_seconds", float, default=5.0,
              description="How often to read back the position of gates that "
                          "can report one."),
        Param("max_hold_minutes", float, default=120.0,
              description=(
                  "Longest a hold-open may last before the gate returns to "
                  "deciding per vehicle. 0 disables holds.")),
        Param("min_confidence", float, default=0.0,
              description=(
                  "Refuse to open below this OCR confidence, even on an "
                  "allow. A last-line guard: the plate reader decides, but a "
                  "barrier is worth a second look.")),
        Param("allowed_reasons", list, default=[],
              description=(
                  "If set, open only for these decision reasons (for example "
                  "['registered']) — so a wider allowlist can inform the log "
                  "without lifting the boom.")),
        Param("max_opens_per_plate", int, default=0,
              description=(
                  "Refuse and alert when one plate opens a gate more than "
                  "this many times in the window — the signature of a copied "
                  "plate. 0 is off.")),
        Param("max_opens_window_minutes", float, default=10.0,
              description="The window for max_opens_per_plate."),
    ],
    emits=[
        AlertType("barrier_opened", severity="low",
                  description="The barrier was asked to open for an allowed vehicle."),
        AlertType("barrier_fault", severity="high",
                  description="The wiring did not accept the open — a vehicle is "
                              "waiting at a gate that did not open."),
        AlertType("barrier_held_open", severity="low",
                  description="A gate is standing open (schedule or operator)."),
        AlertType("barrier_refused", severity="low",
                  description="An allow this app's local guards did not act on "
                              "(confidence, reason, repeat plate)."),
    ],
    actions=[
        Action("open_now", "Open now",
               params=[Param("camera", str, required=True)],
               description="Ask this gate to open now, whatever the last "
                           "decision was.", confirm=True),
        Action("hold_open", "Hold open",
               params=[Param("camera", str, required=True),
                       Param("minutes", float, default=15.0)],
               description="Stand this gate open for a while. 0 means until "
                           "released (still capped by max_hold_minutes).",
               confirm=True),
        Action("release_hold", "Release hold",
               params=[Param("camera", str, required=True)],
               description="End the hold and go back to deciding per vehicle."),
        Action("test", "Test pulse",
               params=[Param("camera", str, required=True)],
               description="Prove the wiring without waiting for a car. "
                           "Recorded as a test.", confirm=True),
    ],
    # Home Assistant: the questions a person asks about a gate — is it
    # open, did anything fail, and let this one in.
    entities=[
        Entity("gates_open", "sensor", "Gates open", state_path="today.open_now",
               state_class="measurement", icon="mdi:boom-gate-up"),
        Entity("opened_today", "sensor", "Gate opens today",
               state_path="today.opened", state_class="total_increasing",
               icon="mdi:boom-gate-up-outline"),
        Entity("faults_today", "sensor", "Gate faults today",
               state_path="today.faults", state_class="total_increasing",
               icon="mdi:boom-gate-alert"),
        Entity("gate_open", "binary_sensor", "Gate open", per_camera=True,
               state_path="per_camera[camera={camera}].is_open",
               device_class="garage", icon="mdi:boom-gate"),
        Entity("gate_state", "sensor", "Gate state", per_camera=True,
               state_path="per_camera[camera={camera}].state",
               icon="mdi:boom-gate"),
        Entity("gate_fault", "binary_sensor", "Gate fault", per_camera=True,
               state_path="per_camera[camera={camera}].is_faulted",
               device_class="problem", icon="mdi:boom-gate-alert"),
        Entity("last_open", "sensor", "Last opened", per_camera=True,
               state_path="per_camera[camera={camera}].last_open_iso",
               device_class="timestamp", icon="mdi:clock-outline"),
        Entity("open_gate", "button", "Open gate", per_camera=True,
               action="open_now", icon="mdi:boom-gate-up"),
        Entity("release", "button", "Release hold", per_camera=True,
               action="release_hold", icon="mdi:boom-gate-down"),
    ],
    state_schema=[
        StateView(name="open_now", label="Gates open now",
                  kind="metric", path="today.open_now"),
        StateView(name="opened", label="Opened today",
                  kind="metric", path="today.opened"),
        StateView(name="denied", label="Refused today",
                  kind="metric", path="today.denied"),
        StateView(name="faults", label="Faults today",
                  kind="metric", path="today.faults"),
        StateView(name="recent", label="Recent gate activity",
                  kind="log", path="recent", limit=12),
    ],
    description=(
        "The hardware half of gate automation, and only that half. The "
        "License Plate Recognition app decides who may enter and publishes "
        "every judgement as a contracted access.decided.v1 event; this app "
        "wires those decisions to your site's barriers.\n\n"
        "It speaks what barriers actually speak: a dry contact from a GPIO "
        "pin — which every barrier ever made accepts — an HTTP relay "
        "(Shelly, Tasmota, ESPHome), a Modbus TCP coil, an ONVIF Profile C "
        "door controller, or an MQTT topic. Name your product as a profile "
        "and give it a host; the URLs and the momentary timing are filled "
        "in for you.\n\n"
        "The Gates page shows what each barrier is doing right now, with "
        "Open and Hold controls, hold-open schedules for the morning rush, "
        "and a line for every decision — who was let in, who was refused, "
        "and what the gate actually did. Faults are loud, because a fault "
        "means a car is waiting at a gate that did not open.\n\n"
        "Deny decisions, unknown decision values, and anything the local "
        "guards do not like actuate nothing. Fail closed is the contract.\n\n"
        + SAFETY_NOTE
    ),
    author="OpenNVR",
    website="https://github.com/open-nvr/open-nvr",
    license="AGPL-3.0",
    contact="https://github.com/open-nvr/open-nvr/discussions",
    use_cases=[
        "Barrier lift for registered vehicles at society and campus gates",
        "Factory truck gates: open only for the logistics register",
        "Hold the gate open for the morning rush, close it the rest of the day",
        "Dry-run commissioning before a single wire is connected",
        "Audit trail: every decision recorded, wired or not",
    ],
    has_ui=True,
)


class GateController(Detector):
    """access.decided.v1 → a contact closure, with state and controls."""

    manifest = MANIFEST
    load_config = staticmethod(load_config)

    def setup(self) -> None:
        cfg = self.cfg
        self._gates: dict[str, Gate] = {}
        self._wiring_errors: dict[str, str] = {}
        self._build_gates(cfg.gates)
        self._dry_run = bool(cfg.dry_run)
        self._cooldown = max(0.0, float(cfg.pulse_cooldown_seconds))
        self._day = _today()
        self._today = {"opened": 0, "denied": 0, "faults": 0, "manual": 0}
        self._events: deque[dict[str, Any]] = deque(maxlen=200)
        self._recent: deque[dict[str, Any]] = deque(maxlen=25)
        self._needs_wiring: set[str] = set()
        self._event_seq = 0
        self._started = time.time()

    def _build_gates(self, raw: Any) -> None:
        """Config → gates. A gate whose wiring will not build is kept as
        a gate with no opener and a visible error rather than vanishing:
        a silently missing gate is how a site finds out at 3am that
        nothing was ever wired."""
        if not isinstance(raw, dict):
            return
        for key, spec in raw.items():
            handle = _handle(key)
            if not handle:
                continue
            name, schedule = handle, []
            if isinstance(spec, dict):
                name = str(spec.get("name") or handle)
                schedule = parse_schedule(spec.get("schedule"))
            opener: Opener | None = None
            try:
                opener = build_opener(spec)
            except OpenerError as exc:
                self._wiring_errors[handle] = str(exc)
                logger.error("gate %s is not wired: %s", handle, exc)
            gate = Gate(camera_id=handle, name=name, opener=opener,
                        schedule=schedule)
            if opener is not None:
                try:
                    gate.monitored = opener.read_state() is not None
                except Exception:  # noqa: BLE001 — probing must not break boot
                    gate.monitored = False
            self._gates[handle] = gate

    # ── One decision ───────────────────────────────────────────────

    def handle_event(self, event: Any) -> list[Alert]:
        if not isinstance(event, dict) or event.get("schema") != DECISION_SCHEMA:
            return []
        self._contract_note_event()
        camera_id = str(event.get("camera_id") or "")
        payload = event.get("payload")
        if not camera_id or not isinstance(payload, dict):
            return []
        self._roll_day()

        decision = payload.get("decision")
        plate = str(payload.get("plate_text") or "?")
        reason = str(payload.get("reason") or "")
        owner, unit = payload.get("owner"), payload.get("unit")
        confidence = payload.get("confidence")
        correlation = event.get("correlation_id")

        # THE contract rule: anything but a literal "allow" actuates
        # nothing — deny today, and decision values invented by future
        # producers, all fail closed.
        if decision != "allow":
            self._today["denied"] += 1
            self._record(camera_id, REFUSED, plate=plate, owner=owner, unit=unit,
                         decision=str(decision), reason=reason,
                         confidence=confidence)
            return []

        gate = self._gates.get(camera_id)
        if gate is None or gate.opener is None:
            # An allow at a camera with no barrier is normal — not every
            # gate-in camera has one. Recorded, never alerted.
            self._needs_wiring.add(camera_id)
            self._record(camera_id, NO_RELAY, plate=plate, owner=owner, unit=unit,
                         decision="allow", reason=reason, confidence=confidence,
                         note=self._wiring_errors.get(camera_id)
                         or "No relay configured for this camera.")
            return []

        # ── Local guards: the seam a plate-only credential needs ────
        refusal = self._local_refusal(gate, plate, reason, confidence)
        if refusal:
            self._today["denied"] += 1
            self._record(camera_id, REFUSED, plate=plate, owner=owner, unit=unit,
                         decision="allow", reason=reason, confidence=confidence,
                         note=refusal)
            return [self._alert(
                "barrier_refused", "low",
                f"Gate held closed for {plate} at {gate.name}",
                f"The plate reader allowed {plate}, but this app did not act: "
                f"{refusal}.", gate, correlation,
                {"plate_text": plate, "reason": reason,
                 "refused_because": refusal})]

        if gate.held_until is not None:
            self._record(camera_id, HELD_ACT, plate=plate, owner=owner, unit=unit,
                         decision="allow", reason=reason, confidence=confidence,
                         note="Gate is already held open.")
            return []

        now = time.monotonic()
        if (self._cooldown > 0 and gate.last_pulse
                and (now - gate.last_pulse) < self._cooldown):
            self._record(camera_id, COOLDOWN, plate=plate, owner=owner, unit=unit,
                         decision="allow", reason=reason, confidence=confidence,
                         note=f"Same gate opened {int(now - gate.last_pulse)} s ago.")
            return []

        return self._do_open(gate, plate=plate, owner=owner, unit=unit,
                             reason=reason, confidence=confidence,
                             correlation=correlation, by=None)

    def _local_refusal(self, gate: Gate, plate: str, reason: str,
                       confidence: Any) -> str:
        """Why this app will not act on an allow, or ``""``."""
        cfg = self.cfg
        if cfg.min_confidence > 0 and isinstance(confidence, (int, float)):
            if float(confidence) < cfg.min_confidence:
                return (f"read confidence {float(confidence):.0%} is below the "
                        f"{cfg.min_confidence:.0%} this gate requires")
        if cfg.allowed_reasons and reason.lower() not in cfg.allowed_reasons:
            return (f"'{reason or 'unspecified'}' is not one of the reasons "
                    f"this gate opens for ({', '.join(cfg.allowed_reasons)})")
        limit = int(cfg.max_opens_per_plate or 0)
        if limit > 0 and plate and plate != "?":
            window = max(1.0, float(cfg.max_opens_window_minutes)) * 60.0
            now = time.monotonic()
            seen = [t for t in gate.recent_plates.get(plate, []) if now - t < window]
            gate.recent_plates[plate] = seen
            if len(seen) >= limit:
                return (f"{plate} has opened this gate {len(seen)} times in "
                        f"{int(window / 60)} min — check the plate is not copied")
        return ""

    # ── Actuation ──────────────────────────────────────────────────

    def _do_open(self, gate: Gate, *, plate: str, owner: Any, unit: Any,
                 reason: str, confidence: Any, correlation: Any,
                 by: str | None, test: bool = False) -> list[Alert]:
        """Pulse, record, alert. The one path to a moving barrier."""
        if gate.opener is None:
            return []
        ok, detail, stuck = True, "", False
        if not self._dry_run:
            try:
                gate.opener.pulse()
            except StuckClosed as exc:
                ok, detail, stuck = False, str(exc), True
            except OpenerError as exc:
                ok, detail = False, str(exc)
            except Exception as exc:  # noqa: BLE001 — never crash on wiring
                ok, detail = False, f"unexpected wiring error: {exc}"

        if not ok:
            gate.faults_today += 1
            self._today["faults"] += 1
            gate.fault_note = detail
            gate.set_state(FAULTED)
            who = plate + (f" ({owner})" if owner else "")
            self._record(gate.camera_id, FAULT, plate=plate, owner=owner,
                         unit=unit, decision="allow", reason=reason,
                         confidence=confidence, by=by, note=detail)
            # A contact that closed and never released is the OPPOSITE
            # failure: the barrier is probably up and staying up, not
            # down with a car in front of it. Same severity, different
            # words, because they need different things done about them.
            if stuck:
                title = f"Gate may be STUCK OPEN at {gate.name}"
                body = (f"{gate.opener.kind} {gate.opener.address}: {detail}. "
                        f"Check the barrier — it may be standing open.")
            else:
                title = (f"Gate did NOT open at {gate.name}"
                         + (" (test)" if test else f" — {who} waiting"))
                body = (f"{gate.opener.kind} {gate.opener.address} did not "
                        f"accept the open: {detail}. "
                        + ("This was a wiring test." if test
                           else "A vehicle is very likely waiting at the "
                                "barrier."))
            return [self._alert(
                "barrier_fault", "high", title, body, gate, correlation,
                {"plate_text": plate, "transport": gate.opener.kind,
                 "address": gate.opener.address, "error": detail,
                 "stuck_open": stuck})]

        # The cooldown starts on an ACTUAL pulse: a failed open must be
        # retriable by the very next decision, not blocked by the
        # attempt that failed.
        gate.last_pulse = time.monotonic()
        if plate and plate not in ("?", "—"):
            gate.recent_plates.setdefault(plate, []).append(gate.last_pulse)
            self._prune_plates(gate)
        gate.fault_note = ""
        if gate.held_until is None:
            gate.set_state(OPENING if gate.monitored else OPEN, refresh=True)
        note = " [dry run]" if self._dry_run else ""

        if test:
            self._today["manual"] += 1
            self._record(gate.camera_id, TEST, by=by, note=f"Wiring test{note}.")
            return []

        gate.opened_today += 1
        gate.last_open = time.time()
        self._today["opened"] += 1
        if by:
            self._today["manual"] += 1
        who = plate + (f" ({owner})" if owner else "")
        self._record(gate.camera_id, OPENED, plate=plate, owner=owner, unit=unit,
                     decision="allow", reason=reason, confidence=confidence, by=by)
        return [self._alert(
            "barrier_opened", "low",
            f"Gate opened for {who}{note}",
            (f"{gate.name} opened for {who}"
             + (f" ({reason})" if reason else "")
             + (f", by {by}" if by else "")
             + f" — {gate.opener.kind} {gate.opener.address}{note}."),
            gate, correlation,
            {"plate_text": plate, "transport": gate.opener.kind,
             "address": gate.opener.address, "dry_run": self._dry_run, "by": by})]

    def _prune_plates(self, gate: Gate) -> None:
        """Forget plates outside the repeat-plate window.

        Only ``_local_refusal`` pruned before, and only the one plate it
        was asked about, and only when the guard was enabled — so with
        the guard off (the default) every plate ever admitted stayed in
        memory for the life of the process. A gate doing 800 vehicles a
        day is supposed to run for years.
        """
        window = max(1.0, float(self.cfg.max_opens_window_minutes)) * 60.0
        cutoff = time.monotonic() - window
        for key in [k for k, v in gate.recent_plates.items()
                    if not v or v[-1] < cutoff]:
            gate.recent_plates.pop(key, None)
        # Backstop for a busy gate inside one window: keep the newest.
        if len(gate.recent_plates) > _MAX_TRACKED_PLATES:
            for key in sorted(gate.recent_plates,
                              key=lambda k: gate.recent_plates[k][-1]
                              )[:len(gate.recent_plates) - _MAX_TRACKED_PLATES]:
                gate.recent_plates.pop(key, None)

    def _alert(self, kind: str, severity: str, title: str, description: str,
               gate: Gate, correlation: Any, evidence: dict[str, Any]) -> Alert:
        alert = Alert(
            severity=severity, title=title, description=description,
            camera_id=gate.camera_id, source=AlertSource(),
            correlation_id=correlation,
            evidence={"gate": gate.name, "alert_type": kind, **evidence},
            tags=[kind],
        )
        self._dispatcher.fire(alert)
        self._contract_note_alerts(1)
        return alert

    # ── The clock: schedules, holds, settling the display ──────────

    def tick(self, now: float | None = None) -> list[Alert]:
        now = now or time.time()
        self._roll_day(now)
        when = datetime.fromtimestamp(now)
        alerts: list[Alert] = []
        for gate in self._gates.values():
            alerts += self._tick_gate(gate, now, when)
        return alerts

    def _tick_gate(self, gate: Gate, now: float, when: datetime) -> list[Alert]:
        alerts: list[Alert] = []
        cap = float(self.cfg.max_hold_minutes or 0)

        # Schedule windows own the gate unless an operator hold is running.
        window = next((w for w in gate.schedule if w.active_at(when)), None)
        if window is None:
            gate.schedule_tried = ""
        elif (gate.held_by_schedule and gate.hold_reason
              and gate.hold_reason != window.describe()):
            # Back-to-back windows (08:00-09:00 then 09:00-12:00): the
            # gate stays held, but under the NEW window, so its end time
            # and its label follow. Without this the page keeps showing
            # the first window and an end time already in the past.
            gate.hold_reason = window.describe()
            gate.schedule_tried = window.describe()
            gate.held_until = min(now + window.minutes_left(when) * 60.0,
                                  now + ABSOLUTE_MAX_HOLD_MINUTES * 60.0)
        if (cap > 0 and window and gate.held_until is None
                and gate.schedule_tried != window.describe()):
            gate.schedule_tried = window.describe()
            alerts += self._start_hold(
                gate, minutes=window.minutes_left(when),
                reason=window.describe(), by=None, by_schedule=True, now=now)
        elif gate.held_by_schedule and window is None:
            alerts += self._release_hold(gate, now=now, note="schedule ended")

        # Expire a hold at its own end. Scheduled holds run to the end
        # of their window (the operator configured it) but are still
        # bounded by held_until, which _start_hold caps at the absolute
        # ceiling — so "12 hours maximum" is enforced, not decorative.
        if gate.held_until is not None and now >= gate.held_until:
            alerts += self._release_hold(
                gate, now=now,
                note="window ended" if gate.held_by_schedule else "hold ended")

        # Settle a display state that nothing else will move on.
        # Unmonitored gates go OPEN on a pulse and only this brings them
        # back. A monitored gate goes OPENING and normally leaves via
        # poll_states — but a status endpoint that starts answering None
        # (a 500, a reboot) leaves it OPENING forever, counted as open in
        # today.open_now and in the Home Assistant sensor, so it is given
        # the same escape.
        settle = max(1.0, float(self.cfg.assumed_open_seconds))
        if (gate.held_until is None and now - gate.since >= settle
                and ((not gate.monitored and gate.state == OPEN)
                     or (gate.monitored and gate.state == OPENING))):
            gate.set_state(CLOSED, now)
        return alerts

    def poll_states(self) -> None:
        """Read the real position of monitored gates. Blocking — the run
        loop calls it in a thread."""
        for gate in self._gates.values():
            if gate.opener is None or not gate.monitored:
                continue
            try:
                observed = gate.opener.read_state()
            except Exception:  # noqa: BLE001
                observed = None
            if observed is None or gate.held_until is not None:
                continue
            if gate.state == FAULTED and observed == "closed":
                continue      # a fault stands until something opens again
            gate.set_state(OPEN if observed == "open" else CLOSED)

    # ── Holds ──────────────────────────────────────────────────────

    def _start_hold(self, gate: Gate, *, minutes: float, reason: str,
                    by: str | None, by_schedule: bool,
                    now: float | None = None) -> list[Alert]:
        now = now or time.time()
        cap = float(self.cfg.max_hold_minutes or 0)
        if gate.opener is None or cap <= 0:
            return []
        if not gate.opener.can_hold:
            # Honest refusal: re-pulsing a momentary input to fake a hold
            # is how a barrier ends up closing on a car.
            self._record(gate.camera_id, REFUSED, by=by,
                         note="This wiring is momentary-only, so the app "
                              "cannot hold it open.")
            return []
        # A manual hold is clamped to the cap (and "until released",
        # which arrives as 0, becomes the cap). A SCHEDULED hold is the
        # operator's explicit intent — a configured four-hour delivery
        # window must not be silently cut to two — so it runs to the end
        # of its window, with the absolute ceiling as the only backstop.
        # Either way held_until is the time the page shows, so the
        # promise on screen is the one the gate keeps.
        minutes = (min(float(minutes or 0), ABSOLUTE_MAX_HOLD_MINUTES)
                   if by_schedule
                   else min(float(minutes or 0) or cap, cap,
                            ABSOLUTE_MAX_HOLD_MINUTES))
        try:
            if not self._dry_run:
                gate.opener.hold(True)
        except OpenerError as exc:
            gate.fault_note = str(exc)
            gate.set_state(FAULTED, now)
            gate.faults_today += 1
            self._today["faults"] += 1
            self._record(gate.camera_id, FAULT, by=by, note=str(exc))
            return [self._alert("barrier_fault", "high",
                                f"Gate {gate.name} could not be held open",
                                str(exc), gate, None, {"error": str(exc)})]
        gate.held_until = now + minutes * 60.0
        gate.hold_reason = reason
        gate.held_by_schedule = by_schedule
        gate.set_state(HELD, now)
        if by:
            self._today["manual"] += 1
        self._record(gate.camera_id, HELD_ACT, by=by, note=reason)
        return [self._alert(
            "barrier_held_open", "low",
            f"{gate.name} is standing open",
            f"{reason}. It returns to deciding per vehicle at "
            f"{datetime.fromtimestamp(gate.held_until).strftime('%H:%M')}"
            + (f", held by {by}" if by else "") + ".",
            gate, None,
            {"until": gate.held_until, "by": by, "reason": reason})]

    def _release_hold(self, gate: Gate, *, now: float | None = None,
                      by: str | None = None, note: str = "") -> list[Alert]:
        now = now or time.time()
        if gate.held_until is None or gate.opener is None:
            return []
        try:
            if not self._dry_run:
                gate.opener.hold(False)
        except OpenerError as exc:
            # The latch did not let go. The hold state is cleared ANYWAY:
            # keeping it would make the tick retry every second (a fault
            # alert per second, forever) while handle_event treats the
            # gate as held and stops pulsing for arriving cars — a gate
            # nobody can open until the process restarts. Clearing it
            # leaves a faulted gate, which is the truth: we no longer
            # know where the barrier is, and the operator can see that.
            gate.fault_note = str(exc)
            gate.held_until = None
            gate.hold_reason = ""
            gate.held_by_schedule = False
            gate.set_state(FAULTED, now)
            gate.faults_today += 1
            self._today["faults"] += 1
            self._record(gate.camera_id, FAULT, by=by, note=str(exc))
            return [self._alert(
                "barrier_fault", "high",
                f"{gate.name} did not come out of hold",
                f"{exc}. The barrier's position is now unknown — check it.",
                gate, None, {"error": str(exc)})]
        gate.held_until = None
        gate.hold_reason = ""
        gate.held_by_schedule = False
        gate.set_state(CLOSED, now)
        if by:
            self._today["manual"] += 1
        self._record(gate.camera_id, RELEASED, by=by, note=note)
        return []

    # ── Operator actions ───────────────────────────────────────────

    def on_action(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        camera = _handle(params.get("camera"))
        gate = self._gates.get(camera)
        if gate is None:
            return {"ok": False, "error": f"no gate configured for {camera or '?'}"}
        if gate.opener is None:
            return {"ok": False,
                    "error": self._wiring_errors.get(camera,
                                                     "this gate has no wiring")}
        by = str(params.get("_actor") or params.get("by") or "operator")

        if name in ("open_now", "test"):
            self._do_open(gate, plate="—", owner=None, unit=None,
                          reason="manual" if name == "open_now" else "test",
                          confidence=None, correlation=None, by=by,
                          test=(name == "test"))
            return {"ok": gate.state != FAULTED, "state": gate.state}
        if name == "hold_open":
            if not gate.opener.can_hold:
                return {"ok": False,
                        "error": ("This wiring is momentary-only: it can ask "
                                  "the operator to open, but it cannot hold "
                                  "the gate. Wire a latching output to hold.")}
            minutes = _minutes_param(params.get("minutes", 15.0))
            self._start_hold(gate, minutes=minutes,
                             reason=f"Held open by {by}", by=by,
                             by_schedule=False)
            return {"ok": gate.held_until is not None,
                    "held_until": gate.held_until, "state": gate.state}
        if name == "release_hold":
            self._release_hold(gate, by=by, note=f"released by {by}")
            return {"ok": gate.held_until is None, "state": gate.state}
        return {"ok": False, "error": f"unknown action {name}"}

    # ── Recording ──────────────────────────────────────────────────

    def _record(self, camera_id: str, action: str, *, plate: str = "",
                owner: Any = None, unit: Any = None, decision: str = "",
                reason: str = "", confidence: Any = None, by: str | None = None,
                note: str | None = None) -> None:
        gate = self._gates.get(camera_id)
        self._event_seq += 1
        row = {
            "id": f"e{self._event_seq}",
            "time": time.time(),
            "gate": camera_id,
            "gate_name": gate.name if gate else camera_id,
            "plate": plate or None,
            "owner": owner,
            "unit": unit,
            "decision": decision or None,
            "reason": reason or None,
            "action": action,
            "by": by,
            "note": note,
            "confidence": (float(confidence)
                           if isinstance(confidence, (int, float)) else None),
        }
        self._events.appendleft(row)
        if gate is not None:
            gate.last_event = row
        self._recent.appendleft({
            "message": _summarise(row),
            "time": row["time"],
            "level": ("high" if action == FAULT
                      else "low" if action in (OPENED, HELD_ACT) else "info"),
        })

    def _roll_day(self, now: float | None = None) -> None:
        today = _today(now)
        if today == self._day:
            return
        self._day = today
        self._today = {"opened": 0, "denied": 0, "faults": 0, "manual": 0}
        for gate in self._gates.values():
            gate.opened_today = 0
            gate.faults_today = 0

    # ── Contract surface ───────────────────────────────────────────

    def state_snapshot(self) -> dict[str, Any]:
        now = time.time()
        when = datetime.fromtimestamp(now)
        gates: list[dict[str, Any]] = []
        per_camera: dict[str, Any] = {}
        open_now = 0
        for gate in sorted(self._gates.values(), key=lambda g: g.name.lower()):
            opener = gate.opener
            is_open = gate.state in (OPEN, HELD, OPENING)
            open_now += 1 if is_open else 0
            window = (next((w for w in gate.schedule if w.active_at(when)), None)
                      or (gate.schedule[0] if gate.schedule else None))
            last_open = gate.last_open
            gates.append({
                "id": gate.camera_id,
                "name": gate.name,
                "state": gate.state,
                "since": gate.since,
                "transport": opener.kind if opener else "none",
                "address": (opener.address if opener
                            else self._wiring_errors.get(gate.camera_id,
                                                         "not wired")),
                "vendor": (opener.vendor if opener else "") or None,
                "monitored": gate.monitored,
                "can_hold": bool(opener.can_hold) if opener else False,
                "held_until": gate.held_until,
                "hold_reason": gate.hold_reason or None,
                "schedule_note": window.describe() if window else None,
                "dry_run": self._dry_run,
                "opened_today": gate.opened_today,
                "faults_today": gate.faults_today,
                "last_event": gate.last_event,
                "fault_note": gate.fault_note or None,
            })
            per_camera[gate.camera_id] = {
                "state": gate.state,
                "is_open": is_open,
                "is_faulted": gate.state == FAULTED,
                "opened_today": gate.opened_today,
                "last_open_iso": (
                    datetime.fromtimestamp(last_open).astimezone().isoformat()
                    if last_open else None),
            }
        wired = {handle for handle, gate in self._gates.items()
                 if gate.opener is not None}
        return {
            "gates": gates,
            "per_camera": per_camera,
            "today": {**self._today, "open_now": open_now},
            "needs_wiring": sorted(self._needs_wiring - wired),
            "events": list(self._events)[:60],
            "recent": list(self._recent),
            "dry_run": self._dry_run,
            "profiles": sorted(PROFILES),
            "safety_note": SAFETY_NOTE,
            "since": self._started,
        }

    def on_config_update(self, config: dict[str, Any]) -> None:
        """Wiring, guards and dry run all apply live."""
        if "gates" in config or "relays" in config:
            raw = config.get("gates", config.get("relays"))
            previous = dict(self._gates)
            old_openers = [g.opener for g in self._gates.values()
                           if g.opener is not None]
            self._gates, self._wiring_errors = {}, {}
            self._build_gates(raw)
            for handle, was in previous.items():
                gate = self._gates.get(handle)
                if gate is None:
                    continue
                # Carry the WHOLE live state across, not a subset. Losing
                # held_until left a gate stuck in "held" that no tick
                # could expire and no Release could clear — the button
                # reported success while the latch stayed engaged and the
                # barrier stayed up.
                gate.state, gate.since = was.state, was.since
                gate.opened_today, gate.faults_today = (was.opened_today,
                                                        was.faults_today)
                gate.held_until = was.held_until
                gate.hold_reason = was.hold_reason
                gate.held_by_schedule = was.held_by_schedule
                gate.schedule_tried = was.schedule_tried
                gate.last_pulse = was.last_pulse
                gate.last_event = was.last_event
                gate.last_open = was.last_open
                gate.fault_note = was.fault_note
                gate.recent_plates = was.recent_plates
            for opener in old_openers:
                try:
                    opener.close()          # sockets, GPIO lines, clients
                except Exception:  # noqa: BLE001 - teardown is best effort
                    logger.debug("closing a replaced opener failed",
                                 exc_info=True)
            logger.info("wiring updated live: %d gate(s)", len(self._gates))
        for key in ("pulse_cooldown_seconds", "assumed_open_seconds",
                    "poll_seconds", "max_hold_minutes", "min_confidence",
                    "max_opens_window_minutes"):
            if key in config:
                try:
                    setattr(self.cfg, key, float(config[key]))
                except (TypeError, ValueError):
                    pass
        if "max_opens_per_plate" in config:
            try:
                self.cfg.max_opens_per_plate = int(config["max_opens_per_plate"])
            except (TypeError, ValueError):
                pass
        if "allowed_reasons" in config:
            self.cfg.allowed_reasons = [
                str(r).strip().lower() for r in (config["allowed_reasons"] or [])]
        self._cooldown = max(0.0, float(self.cfg.pulse_cooldown_seconds))
        if "dry_run" in config:
            self._dry_run = bool(config["dry_run"])

    def ui_html(self) -> str:
        state = self.state_snapshot()
        rows = "".join(
            f"<tr><td>{_esc(g['name'])}</td><td><b>{_esc(g['state'])}</b></td>"
            f"<td>{_esc(g['transport'])}</td><td>{_esc(g['address'])}</td>"
            f"<td>{g['opened_today']}</td><td>{g['faults_today']}</td></tr>"
            for g in state["gates"]) or "<tr><td colspan=6>No gates wired.</td></tr>"
        log = "".join(f"<li>{_esc(_summarise(e))}</li>" for e in state["events"][:15])
        return (
            "<h2>Gates</h2>"
            + ("<p><b>Commissioning mode</b> — nothing is actually opening.</p>"
               if state["dry_run"] else "")
            + f"<p>Today: {state['today']['opened']} opened, "
              f"{state['today']['denied']} refused, "
              f"{state['today']['faults']} faults.</p>"
              "<table border=1 cellpadding=4><tr><th>Gate</th><th>State</th>"
              "<th>Wiring</th><th>Address</th><th>Opens</th><th>Faults</th></tr>"
            + rows + "</table><h3>Recent</h3><ul>" + log + "</ul>"
            + f"<p><small>{_esc(SAFETY_NOTE)}</small></p>")

    # ── The loop ───────────────────────────────────────────────────

    async def run(self, *, once: bool = False) -> None:
        tasks: list[asyncio.Task] = []
        if not once:
            tasks.append(asyncio.create_task(self._tick_loop()))
        try:
            await super().run(once=once)
        finally:
            for task in tasks:
                task.cancel()
            for gate in self._gates.values():
                if gate.opener is not None:
                    gate.opener.close()

    async def _tick_loop(self) -> None:
        """Schedules and hold expiry on the loop every second; reading
        positions back — a socket round trip per monitored gate — in a
        thread, on its own slower cadence."""
        last_poll = 0.0
        while True:
            await asyncio.sleep(1.0)
            try:
                self.tick()
                interval = max(1.0, float(self.cfg.poll_seconds or 0))
                now = time.monotonic()
                if now - last_poll >= interval:
                    last_poll = now
                    await asyncio.to_thread(self.poll_states)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a bad tick must not end the app
                logger.exception("gate tick failed")


# ── Helpers ─────────────────────────────────────────────────────────


def _minutes_param(raw: Any) -> float:
    """A hold length, or the default.

    NaN and infinity are the dangerous inputs: ``min(nan, cap)`` is
    ``nan``, so ``held_until`` became ``nan``, ``now >= nan`` was never
    true, and the gate latched open with an expiry that could not be
    reached — then crashed formatting it. 0 stays meaningful ("until
    released"); a negative is a typo, not a request to release
    immediately, so it falls back to the default rather than cycling
    the barrier.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 15.0
    if value != value or value in (float("inf"), float("-inf")):
        return 15.0
    return value if value >= 0 else 15.0


def _today(now: float | None = None) -> str:
    return datetime.fromtimestamp(now or time.time()).strftime("%Y-%m-%d")


def _summarise(row: dict[str, Any]) -> str:
    what = {
        OPENED: "opened", REFUSED: "not opened", HELD_ACT: "held open",
        RELEASED: "hold released", FAULT: "DID NOT OPEN", TEST: "test pulse",
        NO_RELAY: "allowed, no relay", COOLDOWN: "ignored, too soon",
    }.get(row["action"], row["action"])
    plate = row.get("plate")
    who = f" for {plate}" if plate and plate not in ("—", "?") else ""
    owner = f" ({row['owner']})" if row.get("owner") else ""
    by = f" by {row['by']}" if row.get("by") else ""
    note = f" — {row['note']}" if row.get("note") else ""
    return f"{row['gate_name']}: {what}{who}{owner}{by}{note}"


def _esc(text: Any) -> str:
    return (str(text if text is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# This process is the gate-controller app.
set_default_source(kind="app", name="gate-controller", version="1.1.0")


def main(argv: list[str] | None = None) -> int:
    return app(GateController, load_config=load_config).run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
