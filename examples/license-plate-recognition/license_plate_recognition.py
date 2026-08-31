# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
license-plate-recognition — a PURE CONSUMER of the platform's plate
events (RFC-0002 Phase 4; the reference implementation).

The app subscribes to ``opennvr.events.plate.recognized.v1.>`` — the
contracted domain event (docs/EVENT_CONTRACTS.md) that KAI-C's
normaliser publishes for every accepted OCR read, regardless of which
platform path initiated it (Tier-1 dispatch on assigned cameras, or
core's per-visit enrichment). What remains here is exactly the app's
JOB and nothing else: watchlist severity routing, the per-(camera,
plate) dedup ledger, and alert delivery.

**Zero inference code.** Earlier versions polled camera frames and
drove their own YOLOv8 → crop → fast-plate-ocr chain through KAI-C —
duplicating the detection Tier-0 already runs on the same vehicles
(RFC-0002 gap 2). That chain now runs inside the platform, once per
vehicle visit on the best frame; this app consumes the result. Adding
this app to an install adds **no** inference cost.

Camera scope: the assignment table (Phase 2). Cameras assigned the
``license_plate_recognition`` skill are the app's scope, fetched via
the SDK's ``cameras_for_skill`` and refreshed periodically; an explicit
``cameras:`` list in config overrides, and neither declared = alert on
every camera's plate events (no restriction declared).

Run:
    python license_plate_recognition.py --config config.yml

Foreground daemon; SIGINT/SIGTERM stops cleanly.
"""
from __future__ import annotations

import logging
import re
import time
from collections import deque
from datetime import date
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from alerts import (
    Alert,
    AlertSource,
    DEFAULT_ALERT_SUBJECT_PREFIX,
)
from opennvr_app_sdk import (
    AlertType, AppManifest, Detector, DomainEventPublisher, Param,
    StateView, app,
)
from opennvr_app_sdk.cameras import cameras_for_skill

logger = logging.getLogger("license-plate-recognition")

SKILL = "license_plate_recognition"

#: The contracted subject this app consumes (EVENT_CONTRACTS.md).
PLATE_SUBJECT_PATTERN = "opennvr.events.plate.recognized.v1.>"

#: The decision event this app publishes on gate-in reads while
#: barrier mode is on (docs/EVENT_CONTRACTS.md `access.decided.v1`).
ACCESS_DECISION_SCHEMA = "access.decided.v1"

#: Re-resolve the assignment-table camera scope this often. Cheap (one
#: internal GET) and keeps "assign a camera in one place" true for a
#: long-running app without a restart.
SCOPE_REFRESH_SECONDS = 60.0


def _normalize_plate(value: Any) -> str:
    """The platform's plate normalisation (KAI-C normaliser / core's
    extract_plate): upper-case, no separators — one rule everywhere so
    registry and watchlist entries match regardless of producer."""
    return "".join(str(value).split()).upper()


#: Metadata keys a register entry may carry. ``model`` is the vehicle
#: model (Honda City); ``expires`` (YYYY-MM-DD) makes an entry a
#: visitor pass — past the date it no longer counts as registered.
REGISTRY_FIELDS = ("owner", "unit", "type", "model", "note", "expires")


def parse_registry(raw: Any) -> dict[str, dict[str, str]]:
    """Parse the ``registry`` config into ``{PLATE: metadata}``.

    Entries may be bare plate strings or dicts with a ``plate`` key
    plus optional metadata (:data:`REGISTRY_FIELDS` — a society's
    vehicle register: whose car, which flat, which model, and an
    optional expiry for visitor passes). Unknown keys are dropped;
    entries without a plate are skipped — a partly bad import must
    not break the register.
    """
    registry: dict[str, dict[str, str]] = {}
    for entry in raw or []:
        if isinstance(entry, str):
            plate = _normalize_plate(entry)
            if plate:
                registry[plate] = {}
            continue
        if not isinstance(entry, dict):
            continue
        plate = _normalize_plate(entry.get("plate", ""))
        if not plate:
            continue
        registry[plate] = {
            k: str(entry[k]).strip()
            for k in REGISTRY_FIELDS
            if str(entry.get(k) or "").strip()
        }
    return registry


#: Alert severities a monitor may configure (the SDK alert levels).
MONITOR_SEVERITIES = ("info", "low", "medium", "high", "critical")


def parse_monitors(raw: Any) -> dict[str, dict[str, Any]]:
    """Parse the ``monitors`` config into ``{PLATE: rule}``.

    A monitor is one plate under surveillance with ITS OWN alert
    configuration: ``{plate, note, severity, active, cameras}``.
    ``severity`` defaults to high and falls back to high when not one
    of :data:`MONITOR_SEVERITIES`; ``active: false`` keeps the rule but
    silences it; ``cameras`` (platform handles, e.g. ``["cam3"]``)
    restricts where the alert fires — empty = every camera. Bare
    strings are shorthand for an active high-severity monitor.
    """
    monitors: dict[str, dict[str, Any]] = {}
    for entry in raw or []:
        if isinstance(entry, str):
            plate = _normalize_plate(entry)
            if plate:
                monitors[plate] = {"note": "", "severity": "high",
                                   "active": True, "cameras": frozenset()}
            continue
        if not isinstance(entry, dict):
            continue
        plate = _normalize_plate(entry.get("plate", ""))
        if not plate:
            continue
        severity = str(entry.get("severity") or "high").lower().strip()
        if severity not in MONITOR_SEVERITIES:
            severity = "high"
        monitors[plate] = {
            "note": str(entry.get("note") or "").strip(),
            "severity": severity,
            "active": bool(entry.get("active", True)),
            "cameras": frozenset(
                str(c).strip() for c in (entry.get("cameras") or [])
                if str(c).strip()
            ),
        }
    return monitors


def gate_in_cameras(camera_roles: Any) -> frozenset[str]:
    """Platform handles of gate-IN cameras from the ``camera_roles``
    config map. Keys may be numeric core ids (``"3"`` — what the
    Vehicles page writes) or platform handles (``"cam3"``); values are
    ``{role, label?}`` records or bare role strings."""
    out: set[str] = set()
    if isinstance(camera_roles, dict):
        for key, value in camera_roles.items():
            role = value.get("role") if isinstance(value, dict) else value
            if role != "gate_in":
                continue
            k = str(key).strip()
            if k:
                out.add(k if k.startswith("cam") else f"cam{k}")
    return frozenset(out)


def gate_out_cameras(camera_roles: Any) -> frozenset[str]:
    """Platform handles of gate-OUT cameras (same parsing rules as
    :func:`gate_in_cameras`)."""
    out: set[str] = set()
    if isinstance(camera_roles, dict):
        for key, value in camera_roles.items():
            role = value.get("role") if isinstance(value, dict) else value
            if role != "gate_out":
                continue
            k = str(key).strip()
            if k:
                out.add(k if k.startswith("cam") else f"cam{k}")
    return frozenset(out)


def bounded_levenshtein(a: str, b: str, *, cap: int = 3) -> int:
    """Edit distance with an early exit past ``cap`` — fuzzy matching
    never needs to know HOW far apart two plates are beyond the
    configured tolerance, only whether they are within it."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        best = cur[0]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (ca != cb)))
            if cur[-1] < best:
                best = cur[-1]
        if best > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def compile_plate_formats(raw: Any) -> list[re.Pattern[str]]:
    """Compile the ``plate_formats`` config (a list of regexes a VALID
    plate must fully match, e.g. Indian ``[A-Z]{2}\\d{1,2}[A-Z]{1,3}\\d{4}``).
    Invalid patterns are logged and skipped — one typo must not turn
    format validation off for the working patterns."""
    out: list[re.Pattern[str]] = []
    for item in raw or []:
        text = str(item).strip()
        if not text:
            continue
        try:
            out.append(re.compile(text))
        except re.error as exc:
            logger.warning("plate_formats: skipping invalid regex %r: %s", text, exc)
    return out


def registry_entry_active(entry: dict[str, str] | None, *, today: date | None = None) -> bool:
    """True when a register entry currently counts as registered.

    No ``expires`` = permanent. An unparseable expiry keeps the entry
    ACTIVE (a typo in a date must not turn a resident into a stranger
    at 2am); the register UI is where bad dates get surfaced.
    """
    if entry is None:
        return False
    expires = entry.get("expires")
    if not expires:
        return True
    try:
        return (today or date.today()) <= date.fromisoformat(expires.strip())
    except (TypeError, ValueError):
        return True


MANIFEST = AppManifest(
    id="license-plate-recognition",
    name="License Plate Recognition",
    version="2.5.0",
    category="vehicle",
    summary=(
        "Consumes the platform's plate.recognized.v1 events and routes "
        "severity through allow/deny watchlists. Zero inference in the "
        "app — the detect→crop→OCR chain runs in the platform, once per "
        "vehicle visit."
    ),
    # The chain the PLATFORM must be able to run for plates to flow —
    # the catalog uses this to warn when the OCR adapter is missing.
    requires_tasks=["object_detection", "license_plate_recognition"],
    # Decision 7: the OCR adapter installs WITH the app (the overlay
    # ships + registers it); yolov8 is the standard stack's, reused.
    requires_adapters=["fast_plate_ocr"],
    # Plates are PII (decision 6): consuming plate.recognized.v1 is a
    # declared scope — granted at registration, audited, catalog-visible.
    requires_scopes=["events:plate.recognized"],
    # This app powers the first-class Vehicles page.
    provides=["vehicles"],
    subscribes=PLATE_SUBJECT_PATTERN,
    params=[
        Param("dedup_window_seconds", float, default=60.0,
              description="Per-(camera, plate) re-fire suppression; 0 fires every read."),
        Param("min_confidence", float, default=0.0,
              description="Drop reads below this OCR confidence (0 keeps all)."),
        Param("allowlist", list, default=[],
              description="Plates that fire a low-severity 'expected vehicle' alert."),
        Param("denylist", list, default=[],
              description="Plates that fire a high-severity 'watchlist plate' alert."),
        Param("monitors", list, default=[],
              description=(
                  "Plates under surveillance, each with its own alert: "
                  "{plate, note, severity(info..critical), active, "
                  "cameras}. The denylist is shorthand for active "
                  "high-severity monitors.")),
        Param("registry", list, default=[],
              description=(
                  "The vehicle register: entries are plates or {plate, "
                  "owner, unit, type, model, note, expires} records "
                  "(expires YYYY-MM-DD = visitor pass). Registered "
                  "vehicles pass as expected; with alarm_on_unknown they "
                  "define who is known.")),
        Param("alarm_on_unknown", bool, default=False,
              description=(
                  "Society mode: raise a high-severity alarm for any "
                  "plate NOT in the registry/allowlist (denylist still "
                  "wins). Off = unknown plates log as info reads.")),
        Param("fuzzy_max_distance", int, default=1,
              description=(
                  "OCR tolerance: an unlisted read within this edit "
                  "distance of a registered/allowlisted/monitored plate "
                  "matches it for ALARMS (O→0 never makes a resident a "
                  "stranger). Barrier decisions stay exact-match. "
                  "0 = exact only.")),
        Param("plate_formats", list, default=[],
              description=(
                  "Regexes a valid plate must fully match, e.g. Indian "
                  "'[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{3,4}'. Reads that "
                  "match none go to the review queue instead of raising "
                  "alarms. Empty = no validation.")),
        Param("overstay_hours", float, default=0.0,
              description=(
                  "Alert when a VISITOR (not registered/allowlisted) "
                  "has been inside longer than this many hours — needs "
                  "gate IN (and ideally OUT) camera roles. 0 = off. "
                  "Checked as reads arrive.")),
        Param("barrier_mode", str, default="off",
              description=(
                  "Gate automation: 'off' publishes nothing; "
                  "'registered' publishes an access.decided.v1 event "
                  "for every plate read on a gate-IN camera — allow "
                  "for active registry/allowlist plates, deny "
                  "otherwise. The gate-controller app consumes these "
                  "and drives the barrier relay.")),
        Param("camera_roles", dict, default={},
              description=(
                  "The site layout: {camera_id: {role, label?}} with "
                  "role gate_in | gate_out | parking | other. Consumed "
                  "by the Vehicles page (gate history, inside-now); "
                  "declared here so config saves validate.")),
        Param("unknown_cooldown_seconds", float, default=300.0,
              description=(
                  "Per-plate re-alarm suppression for unknown vehicles, "
                  "across cameras — one stranger, one alarm, not one "
                  "per gate camera.")),
    ],
    emits=[
        AlertType("plate_read", severity="low",
                  description="Info-severity read for unlisted plates."),
        AlertType("plate_expected", severity="low"),
        AlertType("plate_watchlist", severity="high"),
        AlertType("vehicle_overstay", severity="medium",
                  description=(
                      "A visitor vehicle has been inside longer than "
                      "overstay_hours.")),
        AlertType("plate_unknown", severity="high",
                  description=(
                      "Unregistered vehicle seen while alarm_on_unknown "
                      "is enabled.")),
    ],
    has_ui=True,   # GET /ui dashboard, proxied at /api/v1/apps/{id}/ui
    ui_mode="internal",
    # Store listing — what the catalog's Details section renders. This
    # example is the reference for community manifests: fill these in
    # and your app has a storefront, no frontend work needed.
    description=(
        "Turns any camera into a license plate reader. The platform runs "
        "the detect → crop → OCR chain once per vehicle visit and "
        "publishes plate.recognized.v1 events; this app consumes them "
        "and applies your watchlists live.\n\n"
        "Plates on the denylist raise a high-severity alert the moment "
        "they are read; allowlisted plates log as expected vehicles; "
        "everything else is recorded with its evidence photo. Every read "
        "is kept in the timeline with the best frame, searchable from "
        "the Vehicles page.\n\n"
        "Assign cameras the License Plate Recognition skill "
        "(Cameras → edit → Assignments) to choose where OCR runs — "
        "inference is budgeted and runs in the platform, not in this app.\n\n"
        "Built for housing societies AND industrial or company sites — "
        "factory gates, company campuses, warehouses, logistics yards. "
        "Need more? Phone-number and SMS alert delivery, WhatsApp "
        "notifications, complete gate and process automation (barrier "
        "lift on registered vehicles, truck-bay logging), scheduled "
        "reports, or any custom feature — contact us and we'll build "
        "it for your deployment."
    ),
    author="OpenNVR",
    website="https://github.com/open-nvr/open-nvr",
    license="AGPL-3.0",
    # Custom work — SMS/phone alert channels, barrier automation,
    # bespoke features per site. Rendered as "Contact us" in the
    # catalog and on the Vehicles page.
    contact="https://github.com/open-nvr/open-nvr/discussions",
    use_cases=[
        "Alert the moment a watchlisted plate passes any camera",
        "Society mode: register every resident vehicle, alarm on any stranger",
        "Automatic barrier: publish allow/deny gate decisions (gate-controller opens the boom)",
        "Industrial & company automation: factory gates, truck logging, campus access",
        "Log every vehicle with plate, time and evidence photo",
        "Expected-vehicle handling for known cars (allowlist / registry)",
        "Searchable per-plate history from the Vehicles page",
    ],
    state_schema=[
        StateView(name="allowlist_size", label="Allowlist",
                  kind="metric", path="allowlist_size"),
        StateView(name="monitored", label="Monitored plates",
                  kind="metric", path="monitored_plates",
                  description="Plates under surveillance (monitors + denylist)."),
        StateView(name="registry_size", label="Registered vehicles",
                  kind="metric", path="registry_size"),
        StateView(name="unknown_alarms", label="Unknown-vehicle alarms",
                  kind="metric", path="unknown_alarms",
                  description="Alarms fired for unregistered plates since start."),
        StateView(name="deduped", label="Plates deduped",
                  kind="metric", path="deduped_plates_tracked",
                  description="Distinct (camera, plate) pairs in the dedup window."),
        StateView(name="review", label="Needs review",
                  kind="metric", path="reviews_total",
                  description="Reads diverted for a human (bad format / low confidence)."),
        StateView(name="inside", label="Visitors inside",
                  kind="metric", path="inside_visitors"),
        StateView(name="recent", label="Recent plate reads",
                  kind="log", path="recent", limit=12,
                  description="Latest reads; denylist hits show red."),
    ],
)


# ── Config ──────────────────────────────────────────────────────────


@dataclass
class AppConfig:
    """Operator-tunable settings. Validated in ``load_config``."""

    # The event bus (the SDK Detector loop reads these three).
    nats_url: str
    nats_token: str | None = None
    subject_pattern: str = PLATE_SUBJECT_PATTERN

    # Camera scope. Explicit list wins; else the assignment table via
    # ``opennvr_url`` (cameras assigned the LPR skill); else no
    # restriction — every camera's plate events alert.
    cameras: list[str] = field(default_factory=list)

    # The app's job.
    dedup_window_seconds: float = 60.0
    min_confidence: float = 0.0
    allowlist: list[str] = field(default_factory=list)
    denylist: list[str] = field(default_factory=list)

    # The society register: plates (or {plate, owner, unit, type, note}
    # records) of every known vehicle. With ``alarm_on_unknown`` on, any
    # plate outside registry+allowlist raises a high-severity alarm,
    # rate-limited per plate by ``unknown_cooldown_seconds`` across
    # cameras (one stranger = one alarm, not one per gate camera).
    registry: list[Any] = field(default_factory=list)
    # Plates under surveillance, each with its own alert config
    # ({plate, note, severity, active, cameras}); ``denylist`` remains
    # as shorthand for active high-severity monitors.
    monitors: list[Any] = field(default_factory=list)
    alarm_on_unknown: bool = False
    # Gate automation: publish access.decided.v1 on gate-IN reads.
    barrier_mode: str = "off"
    camera_roles: dict[str, Any] = field(default_factory=dict)
    # Trust round: OCR-tolerant matching, plate-format validation,
    # visitor overstay.
    fuzzy_max_distance: int = 1
    plate_formats: list[str] = field(default_factory=list)
    overstay_hours: float = 0.0
    unknown_cooldown_seconds: float = 300.0

    # Alert delivery channels (see alerts.py / the SDK alert stack).
    webhook_url: str | None = None
    nats_alerts_url: str | None = None
    nats_alerts_token: str | None = None
    nats_alerts_subject_prefix: str = DEFAULT_ALERT_SUBJECT_PREFIX

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
            "platform's plate.recognized.v1 events from the bus")
    return AppConfig(
        nats_url=str(nats_url),
        nats_token=raw.get("nats_token") or None,
        subject_pattern=str(
            raw.get("subject_pattern") or PLATE_SUBJECT_PATTERN),
        cameras=[str(c).strip() for c in (raw.get("cameras") or [])
                 if str(c).strip()],
        dedup_window_seconds=float(raw.get("dedup_window_seconds", 60.0)),
        min_confidence=float(raw.get("min_confidence", 0.0)),
        allowlist=[str(p).upper().strip() for p in (raw.get("allowlist") or [])],
        denylist=[str(p).upper().strip() for p in (raw.get("denylist") or [])],
        registry=list(raw.get("registry") or []),
        monitors=list(raw.get("monitors") or []),
        barrier_mode=str(raw.get("barrier_mode") or "off"),
        camera_roles=dict(raw.get("camera_roles") or {}),
        fuzzy_max_distance=int(raw.get("fuzzy_max_distance", 1)),
        plate_formats=[str(f) for f in (raw.get("plate_formats") or [])],
        overstay_hours=float(raw.get("overstay_hours", 0.0)),
        alarm_on_unknown=bool(raw.get("alarm_on_unknown", False)),
        unknown_cooldown_seconds=float(raw.get("unknown_cooldown_seconds", 300.0)),
        webhook_url=raw.get("webhook_url"),
        nats_alerts_url=raw.get("nats_alerts_url"),
        nats_alerts_token=raw.get("nats_alerts_token"),
        nats_alerts_subject_prefix=str(
            raw.get("nats_alerts_subject_prefix", DEFAULT_ALERT_SUBJECT_PREFIX)
        ).strip() or DEFAULT_ALERT_SUBJECT_PREFIX,
        contract_port=(
            int(raw["contract_port"]) if raw.get("contract_port") is not None else None
        ),
        contract_bind_host=raw.get("contract_bind_host"),
        contract_host=raw.get("contract_host"),
        opennvr_url=raw.get("opennvr_url"),
        opennvr_token=raw.get("opennvr_token"),
    )


# ── The app ─────────────────────────────────────────────────────────


class PlateAlerter(Detector):
    """Watchlist + dedup + alert delivery over ``plate.recognized.v1``.

    Extends the SDK :class:`Detector` for its NATS loop, §03 contract
    surface, and dispatcher plumbing, overriding :meth:`handle_event`
    because the input is a DOMAIN envelope (schema / camera_id /
    payload), not an ``InferenceCompletedEvent``.
    """

    manifest = MANIFEST
    load_config = staticmethod(load_config)

    def setup(self) -> None:
        cfg = self.cfg
        # Per-(camera_id, plate) timestamp for dedup. A plain dict on
        # purpose: dedup reads "last actually-fired", never refreshes on
        # suppression, and its shape is pinned by this app's tests.
        self._last_fired: dict[tuple[str, str], float] = {}
        self._recent: deque[dict[str, Any]] = deque(maxlen=25)
        # BOTH watchlists in ONE tuple so a live config swap is a single
        # rebind (a reader never sees new-allow with old-deny).
        self._watchlists: tuple[set[str], set[str]] = (
            {p for p in cfg.allowlist if p},
            {p for p in cfg.denylist if p},
        )
        # The society register + unknown-vehicle alarm state. The
        # cooldown ledger is per PLATE (not per camera): one stranger
        # driving past three gate cameras is one alarm.
        self._registry: dict[str, dict[str, str]] = parse_registry(cfg.registry)
        self._monitors: dict[str, dict[str, Any]] = self._merged_monitors(
            cfg.monitors, cfg.denylist)
        self._alarm_on_unknown: bool = bool(cfg.alarm_on_unknown)
        self._unknown_cooldown: float = max(0.0, float(cfg.unknown_cooldown_seconds))
        self._unknown_last: dict[str, float] = {}
        self._unknown_alarms: int = 0
        # Gate automation (access.decided.v1). The publisher's channel
        # connects lazily on first publish and never raises on bus
        # trouble; mode values other than 'registered' mean off.
        self._barrier_mode: str = (
            "registered" if cfg.barrier_mode == "registered" else "off")
        self._gate_in: frozenset[str] = gate_in_cameras(cfg.camera_roles)
        self._gate_out: frozenset[str] = gate_out_cameras(cfg.camera_roles)
        # Trust round state. Review queue: reads we will NOT act on
        # (bad format / low confidence) surfaced for a human instead of
        # silently dropped. Overstay ledger: visitor plates currently
        # inside (entered at a gate-IN camera, not yet seen at gate-OUT).
        self._fuzzy_max: int = max(0, int(cfg.fuzzy_max_distance))
        self._formats: list[re.Pattern[str]] = compile_plate_formats(cfg.plate_formats)
        self._review: deque[dict[str, Any]] = deque(maxlen=50)
        self._review_last: dict[str, float] = {}
        self._reviews_total: int = 0
        self._overstay_hours: float = max(0.0, float(cfg.overstay_hours))
        self._inside_visitors: dict[str, dict[str, Any]] = {}
        self._overstay_alerts: int = 0
        self._decision_publisher = DomainEventPublisher(
            cfg.nats_url, token=cfg.nats_token,
            producer="app:license-plate-recognition")
        self._decisions_published: int = 0
        # Camera scope: explicit config wins and never refreshes; else
        # the assignment table (refreshed lazily per SCOPE_REFRESH_
        # SECONDS); None = no restriction declared.
        self._explicit_scope: frozenset[str] | None = (
            frozenset(cfg.cameras) if cfg.cameras else None
        )
        self._assigned_scope: frozenset[str] | None = None
        self._scope_fetched_at: float | None = None

    @staticmethod
    def _merged_monitors(monitors_raw: Any, denylist: Any) -> dict[str, dict[str, Any]]:
        """Monitors + denylist shorthand in ONE lookup: a denylist
        plate without its own monitor becomes an active high-severity
        rule (never overriding an explicit monitor for the same plate)."""
        monitors = parse_monitors(monitors_raw)
        for p in denylist or []:
            plate = _normalize_plate(p)
            if plate and plate not in monitors:
                monitors[plate] = {"note": "", "severity": "high",
                                   "active": True, "cameras": frozenset()}
        return monitors

    def _active_monitor(self, plate: str, camera_id: str) -> dict[str, Any] | None:
        """The monitor rule that should FIRE for this read, if any:
        the rule exists, is active, and this camera is in its scope."""
        rule = self._monitors.get(plate)
        if rule is None or not rule.get("active", True):
            return None
        cameras = rule.get("cameras") or frozenset()
        if cameras and camera_id not in cameras:
            return None
        return rule

    def _add_review(self, camera_id: str, plate: str,
                    confidence: Any, reason: str) -> None:
        """Queue a read for human review instead of acting on it.
        Per-plate 5-minute cooldown so one badly-lit plate doesn't
        flood the queue."""
        now = time.monotonic()
        last = self._review_last.get(plate)
        if last is not None and (now - last) < 300.0:
            return
        self._review_last[plate] = now
        if len(self._review_last) > 2048:
            for stale, _ts in sorted(self._review_last.items(),
                                     key=lambda kv: kv[1])[:1024]:
                self._review_last.pop(stale, None)
        self._reviews_total += 1
        self._review.append({
            "plate": plate,
            "camera_id": camera_id,
            "confidence": confidence,
            "reason": reason,
            "time": time.time(),
        })

    def _fuzzy_lookup(self, plate: str) -> tuple[str, str, int] | None:
        """Nearest known plate within the fuzzy tolerance, or None.

        Search order encodes priority: monitors first (surveillance
        must over-alert, never under-alert), then ACTIVE registry
        entries, then the allowlist. Returns (kind, canonical, distance).
        """
        best: tuple[str, str, int] | None = None
        candidates: list[tuple[str, str]] = []
        candidates.extend(("monitor", p) for p in self._monitors)
        candidates.extend(
            ("registry", p) for p, e in self._registry.items()
            if registry_entry_active(e))
        allowlist, _ = self._watchlists
        candidates.extend(("allowlist", p) for p in allowlist)
        for kind, canonical in candidates:
            d = bounded_levenshtein(plate, canonical, cap=self._fuzzy_max)
            if d == 0 or d > self._fuzzy_max:
                continue
            if best is None or d < best[2]:
                best = (kind, canonical, d)
                if d == 1 and kind == "monitor":
                    break  # can't do better than distance-1 monitor
        return best

    def _track_gates(self, camera_id: str, plate: str, *,
                     visitor: bool) -> None:
        """Maintain the inside-visitors ledger from gate reads."""
        if camera_id in self._gate_out:
            self._inside_visitors.pop(plate, None)
            return
        if camera_id not in self._gate_in or not visitor:
            return
        if plate not in self._inside_visitors:
            if len(self._inside_visitors) > 4096:
                for stale, rec in sorted(self._inside_visitors.items(),
                                         key=lambda kv: kv[1]["entered"])[:2048]:
                    self._inside_visitors.pop(stale, None)
            self._inside_visitors[plate] = {
                "entered": time.time(),
                "camera_id": camera_id,
                "alerted": False,
            }

    def _sweep_overstays(self) -> list[Alert]:
        """Fire ONE overstay alert per visit for visitors inside longer
        than the threshold. Event-driven by design (checked as reads
        arrive — no timer thread); week-old entries age out as missed
        exits."""
        if self._overstay_hours <= 0:
            return []
        fired: list[Alert] = []
        now = time.time()
        limit = self._overstay_hours * 3600.0
        for plate, rec in list(self._inside_visitors.items()):
            age = now - rec["entered"]
            if age > 7 * 24 * 3600.0:
                self._inside_visitors.pop(plate, None)  # missed exit
                continue
            if rec["alerted"] or age < limit:
                continue
            rec["alerted"] = True
            hours = age / 3600.0
            alert = Alert(
                severity="medium",
                title=f"Overstay: visitor {plate} inside {hours:.1f}h",
                description=(
                    f"Visitor vehicle {plate} entered at "
                    f"{rec['camera_id']} and has been inside "
                    f"{hours:.1f} hours (threshold "
                    f"{self._overstay_hours:g}h) with no gate-out read."),
                camera_id=rec["camera_id"],
                source=AlertSource(),
                evidence={"plate_text": plate,
                          "entered_at": rec["entered"],
                          "hours_inside": round(hours, 2),
                          "overstay": True},
            )
            self._dispatcher.fire(alert)
            self._overstay_alerts += 1
            self._contract_note_alerts(1)
            fired.append(alert)
        return fired

    # ── Camera scope (Phase 2 integration) ─────────────────────────

    def _scope(self) -> frozenset[str] | None:
        """The camera-id set to alert on; ``None`` = every camera."""
        if self._explicit_scope is not None:
            return self._explicit_scope
        if not self.cfg.opennvr_url:
            return None
        now = time.monotonic()
        if (self._scope_fetched_at is None
                or now - self._scope_fetched_at >= SCOPE_REFRESH_SECONDS):
            self._scope_fetched_at = now
            try:
                assigned = cameras_for_skill(
                    self.cfg.opennvr_url, SKILL,
                    api_key=self.cfg.opennvr_token,
                )
            except Exception:  # noqa: BLE001 — scope is advisory, never fatal
                assigned = None
            # None = "no restriction declared / couldn't tell" — the SDK
            # helper's contract. On failure keep the previous answer
            # (advisory scope must never turn a hiccup into a policy).
            if assigned is not None:
                self._assigned_scope = frozenset(assigned)
        return self._assigned_scope

    # ── The rule (one domain envelope) ─────────────────────────────

    def handle_event(self, event: Any) -> list[Alert]:
        if not isinstance(event, dict):
            return []
        if event.get("schema") != "plate.recognized.v1":
            # Not ours (a widened subject_pattern, a future v2 running in
            # parallel) — ignore without counting it as seen.
            return []
        self._contract_note_event()
        camera_id = str(event.get("camera_id") or "")
        payload = event.get("payload")
        if not camera_id or not isinstance(payload, dict):
            return []
        plate = payload.get("plate_text")
        if not isinstance(plate, str) or not plate.strip():
            return []
        # Same normalisation as the platform's producer (KAI-C normaliser /
        # core's extract_plate): upper, no separators — so watchlist entries
        # match regardless of which producer fired the event.
        plate = _normalize_plate(plate)

        scope = self._scope()
        if scope is not None and camera_id not in scope:
            return []

        confidence = payload.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            confidence = None
        # Trust round: reads we won't act on go to the REVIEW QUEUE,
        # never silently to the floor — a human decides.
        if self._formats and not any(rx.fullmatch(plate) for rx in self._formats):
            self._add_review(camera_id, plate, confidence, "bad_format")
            return self._sweep_overstays()
        if (self.cfg.min_confidence > 0 and confidence is not None
                and confidence < self.cfg.min_confidence):
            self._add_review(camera_id, plate, confidence, "low_confidence")
            return self._sweep_overstays()

        now = time.monotonic()
        key = (camera_id, plate)
        if self.cfg.dedup_window_seconds > 0:
            last = self._last_fired.get(key)
            if last is not None and (now - last) < self.cfg.dedup_window_seconds:
                return []
            self._last_fired[key] = now

        # Society mode: is this plate known to the install at all?
        # Monitored (incl. denylist) and allowlisted plates count as
        # "known" — a monitored plate fires ITS alert, never the
        # unknown-vehicle one.
        allowlist, _denylist = self._watchlists
        monitor = self._active_monitor(plate, camera_id)
        registry_entry = self._registry.get(plate)
        # A visitor pass past its expiry no longer counts as registered
        # — the plate becomes a stranger again (the point of a pass).
        registry_active = registry_entry_active(registry_entry)

        # OCR-tolerant matching, asymmetric by design: a near-miss read
        # is enough to SUPPRESS the unknown-vehicle alarm (O→0 must not
        # wake the guard for a resident) and enough to FIRE a monitor
        # alert (surveillance over-alerts, never under-alerts) — but
        # never enough to open a barrier (actuation stays exact-match).
        fuzzy_match: dict[str, Any] | None = None
        exact_known = (monitor is not None or registry_active
                       or plate in allowlist or plate in self._monitors)
        if not exact_known and self._fuzzy_max > 0:
            hit = self._fuzzy_lookup(plate)
            if hit is not None:
                kind, canonical, distance = hit
                fuzzy_match = {"kind": kind, "plate": canonical,
                               "distance": distance}
                if kind == "monitor":
                    rule = self._monitors.get(canonical)
                    if rule is not None and rule.get("active", True) and (
                            not rule.get("cameras")
                            or camera_id in rule["cameras"]):
                        monitor = rule

        unknown_alarm = False
        if (self._alarm_on_unknown
                and not registry_active
                and fuzzy_match is None
                and plate not in allowlist
                and plate not in self._monitors):
            last_alarm = self._unknown_last.get(plate)
            if last_alarm is None or (now - last_alarm) >= self._unknown_cooldown:
                self._unknown_last[plate] = now
                self._unknown_alarms += 1
                unknown_alarm = True
                if len(self._unknown_last) > 4096:
                    # Bound the cooldown ledger: evict the stalest half.
                    for stale, _ts in sorted(
                            self._unknown_last.items(), key=lambda kv: kv[1]
                    )[:2048]:
                        self._unknown_last.pop(stale, None)

        alert = self._build_alert(
            camera_id, plate,
            confidence=confidence,
            vehicle_label=payload.get("vehicle_label"),
            correlation_id=event.get("correlation_id"),
            monitor=monitor,
            registry_entry=registry_entry if registry_active else None,
            registry_expired=(registry_entry is not None and not registry_active),
            unknown_alarm=unknown_alarm,
            fuzzy_match=fuzzy_match,
        )
        self._dispatcher.fire(alert)

        # Gate automation: on a gate-IN read, put the admission decision
        # on the bus as a FACT (access.decided.v1). Actuation is the
        # gate-controller app's job — policy here, hardware there.
        if self._barrier_mode == "registered" and camera_id in self._gate_in:
            if monitor is not None or plate in self._monitors:
                decision, reason = "deny", "monitored"
            elif registry_active:
                decision, reason = "allow", "registered"
            elif plate in allowlist:
                decision, reason = "allow", "allowlisted"
            elif registry_entry is not None:
                decision, reason = "deny", "expired_pass"
            elif fuzzy_match is not None:
                # A near-miss read NEVER opens the barrier — the next
                # (clean) read will. Actuation is exact-match only.
                decision, reason = "deny", "uncertain_read"
            else:
                decision, reason = "deny", "unknown"
            entry_meta = registry_entry or {}
            published = self._decision_publisher.publish(
                ACCESS_DECISION_SCHEMA,
                camera_id=camera_id,
                payload={
                    "plate_text": plate,
                    "decision": decision,
                    "reason": reason,
                    "owner": entry_meta.get("owner"),
                    "unit": entry_meta.get("unit"),
                    "confidence": confidence,
                },
                correlation_id=event.get("correlation_id"),
            )
            if published:
                self._decisions_published += 1

        self._recent.append({
            "message": f"{plate} on {camera_id}",
            "time": time.time(),
            "level": alert.severity,
        })
        self._contract_note_alerts(1)

        # Overstay tracking: visitors (nothing that matched a list,
        # exactly or fuzzily) enter the ledger at gate-IN and leave it
        # at gate-OUT; the sweep fires due alerts as reads arrive.
        is_visitor = (monitor is None and not registry_active
                      and plate not in allowlist
                      and plate not in self._monitors
                      and fuzzy_match is None)
        self._track_gates(camera_id, plate, visitor=is_visitor)
        return [alert] + self._sweep_overstays()

    def _build_alert(
        self, camera_id: str, plate: str, *,
        confidence: float | None,
        vehicle_label: str | None,
        correlation_id: str | None,
        monitor: dict[str, Any] | None = None,
        registry_entry: dict[str, str] | None = None,
        registry_expired: bool = False,
        unknown_alarm: bool = False,
        fuzzy_match: dict[str, Any] | None = None,
    ) -> Alert:
        allowlist, _denylist = self._watchlists   # one read = one generation
        if monitor is not None:
            severity = str(monitor.get("severity") or "high")
            note = str(monitor.get("note") or "")
            title = (f"Monitored plate {plate} seen — {note}" if note
                     else f"Monitored plate {plate} seen")
            if fuzzy_match is not None and fuzzy_match.get("kind") == "monitor":
                title += f" (read ≈ {fuzzy_match['plate']})"
        elif fuzzy_match is not None and fuzzy_match.get("kind") in ("registry", "allowlist"):
            canonical = str(fuzzy_match["plate"])
            entry = self._registry.get(canonical) or {}
            owner = entry.get("owner", "")
            tag = f" ({owner})" if owner else ""
            severity = "low"
            title = (f"Probable registered vehicle {canonical}{tag} — "
                     f"read {plate}")
        elif unknown_alarm:
            severity, title = "high", f"Unknown vehicle {plate}"
        elif registry_entry is not None:
            owner = registry_entry.get("owner", "")
            unit = registry_entry.get("unit", "")
            tag = (
                f" ({owner}, {unit})" if owner and unit
                else f" ({owner})" if owner
                else f" (unit {unit})" if unit
                else ""
            )
            severity, title = "low", f"Registered vehicle {plate} seen{tag}"
        elif plate in allowlist:
            severity, title = "low", f"Expected plate {plate} seen"
        else:
            severity, title = "info", f"Plate {plate} read"
        conf_note = (
            f", confidence={confidence:.2f}" if confidence is not None else ""
        )
        return Alert(
            severity=severity,
            title=title,
            description=(
                f"License plate '{plate}' read on camera {camera_id} "
                f"({vehicle_label or 'vehicle'}{conf_note})."
            ),
            camera_id=camera_id,
            source=AlertSource(),
            correlation_id=correlation_id,
            evidence={
                "plate_text": plate,
                "confidence": confidence,
                "vehicle_label": vehicle_label,
                "in_allowlist": plate in allowlist,
                # Kept name for consumers: "on the bad list" now means
                # "has a monitor rule" (denylist is monitor shorthand).
                "in_denylist": plate in self._monitors,
                "monitor": (
                    {"severity": monitor["severity"], "note": monitor["note"]}
                    if monitor is not None else None
                ),
                "in_registry": registry_entry is not None,
                "registry": registry_entry,
                "registry_expired": registry_expired,
                "unknown_alarm": unknown_alarm,
                "fuzzy_match": fuzzy_match,
            },
        )

    # ── Contract surface ───────────────────────────────────────────

    def state_snapshot(self) -> dict[str, Any]:
        scope = self._explicit_scope or self._assigned_scope
        return {
            "cameras": sorted(scope) if scope is not None else [],
            "deduped_plates_tracked": len(self._last_fired),
            "allowlist_size": len(self._watchlists[0]),
            "denylist_size": len(self._watchlists[1]),
            "registry_size": len(self._registry),
            "monitored_plates": len(self._monitors),
            "alarm_on_unknown": self._alarm_on_unknown,
            "barrier_mode": self._barrier_mode,
            "review": list(self._review),
            "reviews_total": self._reviews_total,
            "inside_visitors": len(self._inside_visitors),
            "overstay_hours": self._overstay_hours,
            "overstay_alerts": self._overstay_alerts,
            "fuzzy_max_distance": self._fuzzy_max,
            "gate_in_cameras": sorted(self._gate_in),
            "decisions_published": self._decisions_published,
            "unknown_alarms": self._unknown_alarms,
            "recent": list(self._recent),
        }

    def ui_html(self) -> str:
        """The app dashboard (RFC-0002 Phase 4 app-surface convention):
        ONE self-contained, static HTML document — no scripts (core's
        catalog renders it in a sandboxed iframe and refetches on an
        interval, so the page is a snapshot, not an app)."""
        import html as _html

        snap = self.state_snapshot()
        allow, deny = self._watchlists
        rows = []
        for item in reversed(list(self._recent)[-12:]):
            level = str(item.get("level", "info"))
            color = {"high": "#e5484d", "low": "#46a758"}.get(level, "#8b8d98")
            age_min = max(0, int((time.time() - item.get("time", 0)) / 60))
            rows.append(
                f"<tr><td style='color:{color};font-weight:600'>"
                f"{_html.escape(str(item.get('message', '')))}</td>"
                f"<td>{_html.escape(level)}</td><td>{age_min}m ago</td></tr>"
            )
        table = (
            "<table><tr><th>Read</th><th>Severity</th><th>When</th></tr>"
            + "".join(rows) + "</table>" if rows
            else "<p class='dim'>No plate reads yet.</p>"
        )
        scope = snap.get("cameras") or []
        scope_line = (
            ", ".join(_html.escape(c) for c in scope)
            if scope else "all cameras (no assignment restriction)"
        )
        return f"""<title>License Plate Recognition</title>
<style>
 body {{ font: 14px system-ui, sans-serif; margin: 1.2rem; color: #1a1a1a;
        background: #fafafa; }}
 h1 {{ font-size: 1.1rem; margin: 0 0 .2rem }}
 .dim {{ color: #6b6f76 }}
 .stats {{ display: flex; gap: 1.5rem; margin: .8rem 0 }}
 .stats b {{ font-size: 1.3rem; display: block }}
 table {{ border-collapse: collapse; width: 100% }}
 th, td {{ text-align: left; padding: .3rem .6rem;
          border-bottom: 1px solid #e0e0e0; font-size: .9rem }}
 th {{ color: #6b6f76; font-weight: 500 }}
 .note {{ margin-top: 1rem; font-size: .85rem; color: #6b6f76 }}
</style>
<h1>License Plate Recognition</h1>
<div class="dim">Watching: {scope_line}</div>
<div class="stats">
 <div><b>{len(allow)}</b><span class="dim">allowlist</span></div>
 <div><b>{len(self._monitors)}</b><span class="dim">monitored</span></div>
 <div><b>{len(self._registry)}</b><span class="dim">registered</span></div>
 <div><b>{self._unknown_alarms}</b><span class="dim">unknown alarms</span></div>
 <div><b>{snap.get("deduped_plates_tracked", 0)}</b><span class="dim">plates deduped</span></div>
</div>
<div class="dim">Unknown-vehicle alarm: {"ON" if self._alarm_on_unknown else "off"}
 · Barrier: {"AUTO (registered vehicles)" if self._barrier_mode == "registered" else "off"}
 · Decisions published: {self._decisions_published}</div>
{table}
<div class="note">Watchlists are edited in the App Catalog's config
form (applied live). Full history: the timeline's plate search.</div>
"""

    def on_config_update(self, config: dict[str, Any]) -> None:
        """Live watchlist edits from the catalog's config form — one
        tuple rebind, idempotent, exactly as the poller version did."""
        allow = {
            str(p).upper().strip()
            for p in (config.get("allowlist") or [])
            if str(p).strip()
        }
        deny = {
            str(p).upper().strip()
            for p in (config.get("denylist") or [])
            if str(p).strip()
        }
        if (allow, deny) != self._watchlists:
            self._watchlists = (allow, deny)
            logger.info(
                "watchlists updated live: allowlist=%d denylist=%d",
                len(allow), len(deny),
            )
        # The society register + alarm mode update live too — the whole
        # point of the Vehicles page's registry editor.
        if "registry" in config:
            registry = parse_registry(config.get("registry"))
            if registry != self._registry:
                self._registry = registry
                logger.info("vehicle register updated live: %d plates", len(registry))
        if "monitors" in config or "denylist" in config:
            monitors = self._merged_monitors(
                config.get("monitors"), config.get("denylist"))
            if monitors != self._monitors:
                self._monitors = monitors
                logger.info("monitors updated live: %d plates", len(monitors))
        if "alarm_on_unknown" in config:
            self._alarm_on_unknown = bool(config.get("alarm_on_unknown"))
        if "fuzzy_max_distance" in config:
            try:
                self._fuzzy_max = max(0, int(config.get("fuzzy_max_distance", 1)))
            except (TypeError, ValueError):
                pass
        if "plate_formats" in config:
            self._formats = compile_plate_formats(config.get("plate_formats"))
        if "overstay_hours" in config:
            try:
                self._overstay_hours = max(
                    0.0, float(config.get("overstay_hours", 0.0)))
            except (TypeError, ValueError):
                pass
        if "camera_roles" in config:
            self._gate_out = gate_out_cameras(config.get("camera_roles"))
        if "barrier_mode" in config:
            self._barrier_mode = (
                "registered" if config.get("barrier_mode") == "registered"
                else "off")
        if "camera_roles" in config:
            self._gate_in = gate_in_cameras(config.get("camera_roles"))
        if "unknown_cooldown_seconds" in config:
            try:
                self._unknown_cooldown = max(
                    0.0, float(config.get("unknown_cooldown_seconds", 300.0)))
            except (TypeError, ValueError):
                pass


# ── CLI ─────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Console entry point — the SDK runner owns argparse, logging,
    signals, and the dispatcher."""
    return app(PlateAlerter, load_config=load_config).run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
