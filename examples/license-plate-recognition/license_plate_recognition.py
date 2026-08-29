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
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from alerts import (
    Alert,
    AlertSource,
    DEFAULT_ALERT_SUBJECT_PREFIX,
)
from opennvr_app_sdk import AlertType, AppManifest, Detector, Param, StateView, app
from opennvr_app_sdk.cameras import cameras_for_skill

logger = logging.getLogger("license-plate-recognition")

SKILL = "license_plate_recognition"

#: The contracted subject this app consumes (EVENT_CONTRACTS.md).
PLATE_SUBJECT_PATTERN = "opennvr.events.plate.recognized.v1.>"

#: Re-resolve the assignment-table camera scope this often. Cheap (one
#: internal GET) and keeps "assign a camera in one place" true for a
#: long-running app without a restart.
SCOPE_REFRESH_SECONDS = 60.0


MANIFEST = AppManifest(
    id="license-plate-recognition",
    name="License Plate Recognition",
    version="2.0.0",
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
    ],
    emits=[
        AlertType("plate_read", severity="low",
                  description="Info-severity read for unlisted plates."),
        AlertType("plate_expected", severity="low"),
        AlertType("plate_watchlist", severity="high"),
    ],
    state_schema=[
        StateView(name="allowlist_size", label="Allowlist",
                  kind="metric", path="allowlist_size"),
        StateView(name="denylist_size", label="Denylist",
                  kind="metric", path="denylist_size"),
        StateView(name="deduped", label="Plates deduped",
                  kind="metric", path="deduped_plates_tracked",
                  description="Distinct (camera, plate) pairs in the dedup window."),
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
        # Camera scope: explicit config wins and never refreshes; else
        # the assignment table (refreshed lazily per SCOPE_REFRESH_
        # SECONDS); None = no restriction declared.
        self._explicit_scope: frozenset[str] | None = (
            frozenset(cfg.cameras) if cfg.cameras else None
        )
        self._assigned_scope: frozenset[str] | None = None
        self._scope_fetched_at: float | None = None

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
            # helper's contract. Keep the previous answer on failure.
            if assigned is not None:
                self._assigned_scope = frozenset(assigned)
            elif self._scope_fetched_at == now and self._assigned_scope is None:
                self._assigned_scope = None
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
        plate = "".join(plate.split()).upper()

        scope = self._scope()
        if scope is not None and camera_id not in scope:
            return []

        confidence = payload.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            confidence = None
        if (self.cfg.min_confidence > 0 and confidence is not None
                and confidence < self.cfg.min_confidence):
            return []

        now = time.monotonic()
        key = (camera_id, plate)
        if self.cfg.dedup_window_seconds > 0:
            last = self._last_fired.get(key)
            if last is not None and (now - last) < self.cfg.dedup_window_seconds:
                return []
            self._last_fired[key] = now

        alert = self._build_alert(
            camera_id, plate,
            confidence=confidence,
            vehicle_label=payload.get("vehicle_label"),
            correlation_id=event.get("correlation_id"),
        )
        self._dispatcher.fire(alert)
        self._recent.append({
            "message": f"{plate} on {camera_id}",
            "time": time.time(),
            "level": alert.severity,
        })
        self._contract_note_alerts(1)
        return [alert]

    def _build_alert(
        self, camera_id: str, plate: str, *,
        confidence: float | None,
        vehicle_label: str | None,
        correlation_id: str | None,
    ) -> Alert:
        allowlist, denylist = self._watchlists   # one read = one generation
        if plate in denylist:
            severity, title = "high", f"Watchlist plate {plate} seen"
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
                "in_denylist": plate in denylist,
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
            "recent": list(self._recent),
        }

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
        if (allow, deny) == self._watchlists:
            return
        self._watchlists = (allow, deny)
        logger.info(
            "watchlists updated live from the registry: allowlist=%d denylist=%d",
            len(allow), len(deny),
        )


# ── CLI ─────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Console entry point — the SDK runner owns argparse, logging,
    signals, and the dispatcher."""
    return app(PlateAlerter, load_config=load_config).run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
