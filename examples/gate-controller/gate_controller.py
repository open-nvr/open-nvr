# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
gate-controller — barrier actuation for ``access.decided.v1``.

The License Plate Recognition app (or any decision-making app) puts
the ADMISSION DECISION on the bus as a contracted fact
(docs/EVENT_CONTRACTS.md ``access.decided.v1``). This app is the
hardware side of that split: it subscribes to decisions and pulses a
relay when the decision is ``allow`` — barrier booms, sliding gates,
door strikes, anything reachable as an HTTP relay (Shelly, Tasmota,
ESPHome, most commercial gate relays expose one).

Policy and wiring evolve independently on purpose: the LPR app knows
WHO may enter (register, allowlist, monitors) and never touches
hardware; this app knows WHICH relay opens WHICH gate and never makes
admission judgements. Any decision other than ``allow`` — including
decision values invented by future producers — actuates NOTHING
(fail closed, per the contract).

Zero inference, zero core access: the whole app is a NATS
subscription and an HTTP call.

Run:
    python gate_controller.py --config config.yml
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx  # module attribute on purpose: tests monkeypatch gate_controller.httpx
import yaml

from opennvr_app_sdk import (
    Alert,
    AlertSource,
    AlertType,
    AppManifest,
    Detector,
    Param,
    StateView,
    app,
    set_default_source,
)

logger = logging.getLogger("gate-controller")

#: The contracted decision event this app consumes.
DECISION_SUBJECT_PATTERN = "opennvr.events.access.decided.v1.>"
DECISION_SCHEMA = "access.decided.v1"

#: HTTP budget for one relay pulse. Barriers are near-field devices —
#: if the relay hasn't answered in 3s it isn't going to.
RELAY_TIMEOUT_SECONDS = 3.0


def _camera_handle(key: Any) -> str:
    """Relay-map keys may be numeric core ids (``"3"``) or platform
    handles (``"cam3"``) — one normalisation, same as the LPR app."""
    k = str(key).strip()
    if not k:
        return ""
    return k if k.startswith("cam") else f"cam{k}"


def parse_relays(raw: Any) -> dict[str, dict[str, str]]:
    """``relays`` config → ``{camN: {url, method}}``.

    Values may be a bare relay URL string or ``{url, method}``
    (method GET default; POST for relays that want it). Entries
    without a URL are skipped — a partly bad map must not take the
    working gates down with it.
    """
    relays: dict[str, dict[str, str]] = {}
    if not isinstance(raw, dict):
        return relays
    for key, value in raw.items():
        handle = _camera_handle(key)
        if not handle:
            continue
        if isinstance(value, str):
            url, method = value.strip(), "GET"
        elif isinstance(value, dict):
            url = str(value.get("url") or "").strip()
            method = str(value.get("method") or "GET").upper().strip()
        else:
            continue
        if not url:
            continue
        if method not in ("GET", "POST"):
            method = "GET"
        relays[handle] = {"url": url, "method": method}
    return relays


MANIFEST = AppManifest(
    id="gate-controller",
    name="Gate Controller",
    version="1.0.0",
    category="automation",
    summary=(
        "Opens the barrier for allowed vehicles: consumes the "
        "platform's access.decided.v1 events and pulses an HTTP relay "
        "per gate camera. Deny (and anything unknown) actuates "
        "nothing — fail closed."
    ),
    # No inference, no adapters — this app is bus + relay only.
    requires_tasks=[],
    # Consuming gate decisions is a declared, granted, audited scope.
    requires_scopes=["events:access.decided"],
    subscribes=DECISION_SUBJECT_PATTERN,
    params=[
        Param("relays", dict, default={},
              description=(
                  "Which relay opens which gate: {camera: url} or "
                  "{camera: {url, method}}. Cameras by core id ('3') "
                  "or handle ('cam3'); any HTTP relay works (Shelly, "
                  "Tasmota, ESPHome, commercial gate relays).")),
        Param("pulse_cooldown_seconds", float, default=5.0,
              description=(
                  "Per-gate re-trigger suppression — one car, one "
                  "pulse, even when multiple allow decisions arrive "
                  "while the boom is already up.")),
        Param("dry_run", bool, default=False,
              description=(
                  "Log and alert as if opening, without calling the "
                  "relay — for commissioning a site safely.")),
    ],
    emits=[
        AlertType("barrier_opened", severity="low",
                  description="Relay pulsed for an allowed vehicle."),
        AlertType("barrier_fault", severity="high",
                  description=(
                      "The relay call failed — an allowed vehicle is "
                      "waiting at a gate that did not open.")),
    ],
    state_schema=[
        StateView(name="gates", label="Gates wired",
                  kind="metric", path="gates_wired"),
        StateView(name="opened", label="Barrier pulses",
                  kind="metric", path="opened_total"),
        StateView(name="denied", label="Denies seen",
                  kind="metric", path="denied_total"),
        StateView(name="faults", label="Relay faults",
                  kind="metric", path="fault_total"),
        StateView(name="recent", label="Recent gate activity",
                  kind="log", path="recent", limit=12),
    ],
    description=(
        "The hardware half of gate automation. The License Plate "
        "Recognition app decides WHO may enter (its register, "
        "allowlist and monitors) and publishes every decision as a "
        "contracted access.decided.v1 event; this app decides nothing "
        "— it wires those decisions to your site's relays and pulses "
        "the right one when a vehicle is allowed.\n\n"
        "Works with any HTTP-reachable relay: Shelly, Tasmota, "
        "ESPHome, and most commercial barrier controllers. Per-gate "
        "cooldown keeps one car to one pulse; dry-run mode lets you "
        "commission a site before touching hardware; every pulse and "
        "every fault raises an alert.\n\n"
        "Deny decisions — and any decision value this app does not "
        "recognise — actuate nothing. Fail closed is the contract."
    ),
    author="OpenNVR",
    website="https://github.com/open-nvr/open-nvr",
    license="AGPL-3.0",
    contact="https://github.com/open-nvr/open-nvr/discussions",
    use_cases=[
        "Barrier lift for registered vehicles at society/campus gates",
        "Factory truck gates: open only for the logistics register",
        "Dry-run commissioning before wiring the relay",
        "Audit trail: an alert for every pulse and every fault",
    ],
)


# ── Config ──────────────────────────────────────────────────────────


@dataclass
class AppConfig:
    """Operator-tunable settings. Validated in ``load_config``."""

    nats_url: str
    nats_token: str | None = None
    subject_pattern: str = DECISION_SUBJECT_PATTERN

    relays: dict[str, Any] = field(default_factory=dict)
    pulse_cooldown_seconds: float = 5.0
    dry_run: bool = False

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
    return AppConfig(
        nats_url=str(nats_url),
        nats_token=raw.get("nats_token") or None,
        subject_pattern=str(
            raw.get("subject_pattern") or DECISION_SUBJECT_PATTERN),
        relays=dict(raw.get("relays") or {}),
        pulse_cooldown_seconds=float(raw.get("pulse_cooldown_seconds", 5.0)),
        dry_run=bool(raw.get("dry_run", False)),
        webhook_url=raw.get("webhook_url"),
        nats_alerts_url=raw.get("nats_alerts_url"),
        nats_alerts_token=raw.get("nats_alerts_token"),
        contract_port=(
            int(raw["contract_port"]) if raw.get("contract_port") is not None else None
        ),
        contract_bind_host=raw.get("contract_bind_host"),
        contract_host=raw.get("contract_host"),
        opennvr_url=raw.get("opennvr_url"),
        opennvr_token=raw.get("opennvr_token"),
    )


# ── The app ─────────────────────────────────────────────────────────


class GateController(Detector):
    """access.decided.v1 → relay pulse (allow only, cooldown, alerts)."""

    manifest = MANIFEST
    load_config = staticmethod(load_config)

    def setup(self) -> None:
        cfg = self.cfg
        self._relays: dict[str, dict[str, str]] = parse_relays(cfg.relays)
        self._cooldown: float = max(0.0, float(cfg.pulse_cooldown_seconds))
        self._dry_run: bool = bool(cfg.dry_run)
        self._last_pulse: dict[str, float] = {}   # camera → monotonic ts
        self._opened = 0
        self._denied = 0
        self._faults = 0
        self._recent: deque[dict[str, Any]] = deque(maxlen=25)

    # ── One decision envelope ──────────────────────────────────────

    def handle_event(self, event: Any) -> list[Alert]:
        if not isinstance(event, dict):
            return []
        if event.get("schema") != DECISION_SCHEMA:
            return []
        self._contract_note_event()
        camera_id = str(event.get("camera_id") or "")
        payload = event.get("payload")
        if not camera_id or not isinstance(payload, dict):
            return []
        decision = payload.get("decision")
        plate = str(payload.get("plate_text") or "?")

        # THE contract rule: anything but a literal "allow" actuates
        # nothing — deny today, and decision values invented by future
        # producers, all fail closed.
        if decision != "allow":
            self._denied += 1
            self._note(f"{plate} at {camera_id}: "
                       f"{payload.get('reason') or decision} — gate stays closed",
                       "info")
            return []

        relay = self._relays.get(camera_id)
        if relay is None:
            # An allow at a camera with no wired relay is normal (not
            # every gate-in camera has a barrier) — log, don't alert.
            self._note(f"{plate} allowed at {camera_id} (no relay wired)",
                       "info")
            return []

        now = time.monotonic()
        last = self._last_pulse.get(camera_id)
        if self._cooldown > 0 and last is not None and (now - last) < self._cooldown:
            self._note(f"{plate} allowed at {camera_id} — boom already up "
                       f"(cooldown)", "info")
            return []

        ok = True
        if not self._dry_run:
            ok = self._pulse(relay)
        # Cooldown starts only on an ACTUAL pulse (or dry-run pretend):
        # a failed relay must be retriable by the very next decision.
        if ok:
            self._last_pulse[camera_id] = now

        owner = payload.get("owner")
        who = f"{plate}" + (f" ({owner})" if owner else "")
        if ok:
            self._opened += 1
            note = " [dry run]" if self._dry_run else ""
            alert = Alert(
                severity="low",
                title=f"Barrier opened for {who}{note}",
                description=(
                    f"Allowed vehicle {who} at {camera_id} "
                    f"({payload.get('reason') or 'allow'}); relay "
                    f"{relay['method']} {relay['url']}{note}."),
                camera_id=camera_id,
                source=AlertSource(),
                correlation_id=event.get("correlation_id"),
                evidence={"plate_text": plate, "relay_url": relay["url"],
                          "dry_run": self._dry_run},
            )
            self._note(f"OPENED for {who} at {camera_id}{note}", "low")
        else:
            self._faults += 1
            alert = Alert(
                severity="high",
                title=f"Barrier FAULT at {camera_id} — {who} waiting",
                description=(
                    f"Relay call failed for allowed vehicle {who}: "
                    f"{relay['method']} {relay['url']}. The gate did "
                    f"not open."),
                camera_id=camera_id,
                source=AlertSource(),
                correlation_id=event.get("correlation_id"),
                evidence={"plate_text": plate, "relay_url": relay["url"]},
            )
            self._note(f"FAULT at {camera_id} for {who}", "high")
        self._dispatcher.fire(alert)
        self._contract_note_alerts(1)
        return [alert]

    def _pulse(self, relay: dict[str, str]) -> bool:
        """One relay call. True = the relay answered 2xx."""
        try:
            if relay["method"] == "POST":
                resp = httpx.post(relay["url"], timeout=RELAY_TIMEOUT_SECONDS)
            else:
                resp = httpx.get(relay["url"], timeout=RELAY_TIMEOUT_SECONDS)
            return 200 <= resp.status_code < 300
        except Exception as exc:  # noqa: BLE001 — relay trouble is a fault, not a crash
            logger.warning("relay pulse failed (%s %s): %s",
                           relay["method"], relay["url"], exc)
            return False

    def _note(self, message: str, level: str) -> None:
        self._recent.append(
            {"message": message, "time": time.time(), "level": level})

    # ── Contract surface ───────────────────────────────────────────

    def state_snapshot(self) -> dict[str, Any]:
        return {
            "gates_wired": len(self._relays),
            "opened_total": self._opened,
            "denied_total": self._denied,
            "fault_total": self._faults,
            "dry_run": self._dry_run,
            "recent": list(self._recent),
        }

    def on_config_update(self, config: dict[str, Any]) -> None:
        """Relay map, cooldown and dry-run all apply live."""
        if "relays" in config:
            relays = parse_relays(config.get("relays"))
            if relays != self._relays:
                self._relays = relays
                logger.info("relay map updated live: %d gates", len(relays))
        if "pulse_cooldown_seconds" in config:
            try:
                self._cooldown = max(
                    0.0, float(config.get("pulse_cooldown_seconds", 5.0)))
            except (TypeError, ValueError):
                pass
        if "dry_run" in config:
            self._dry_run = bool(config.get("dry_run"))


# This process is the gate-controller app.
set_default_source(kind="app", name="gate-controller", version="1.0.0")


def main(argv: list[str] | None = None) -> int:
    return app(GateController, load_config=load_config).run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
