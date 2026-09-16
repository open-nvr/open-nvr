# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Loitering-detection example app — the ``opennvr-app-sdk`` reference
migration (App SDK spec §08 step 2).

Watches one or more cameras for watched-label entities (person, car,
etc.) lingering in operator-defined zones beyond a dwell threshold,
and fires alerts when the threshold is exceeded.

What lives where after the migration
------------------------------------

The SDK's :class:`~opennvr_app_sdk.Detector` base now owns everything
that used to be boilerplate here: the NATS subscribe loop, per-message
JSON decoding + exception isolation, the ``camera_id`` /
``result.detections`` payload walk, ``completed_at`` timestamp parsing
with a clock fallback, alert dispatch, the CLI, and signal handling.
The §11.5 alert stack and the zone geometry moved to
``opennvr_app_sdk.alerts`` / ``opennvr_app_sdk.geometry`` (thin shims
remain at ``alerts.py`` / ``zone.py`` for import compatibility).

What's left here is the rule — the per-(camera, label) dwell state
machine — plus this app's config parsing and its declarative MANIFEST.

Architecture (unchanged)
------------------------

Unlike ``intrusion-detection`` (a FrameApp: it DRIVES inference by
polling KAI-C per camera), this app SUBSCRIBES to KAI-C's NATS
broadcast surface (``opennvr.inference.*``) — it consumes inference
results another app is already driving, so adapter GPU is paid once
and N subscribers fan out from one inference stream.

State machine (per camera × watched-label)
------------------------------------------

Expressed with the SDK's ``keyed_state`` (TTL + latch + GC):

* First presence-frame → ``touch`` creates the record
  (``present_since = first_seen = event_ts``, ``alerted = False``)
* Continuous presence → ``touch`` refreshes ``last_seen``
* ``record.age >= threshold_seconds`` AND ``not record.alerted``
    → fire alert, latch ``alerted = True``
* Absence beyond ``grace_period_seconds`` → the record is GC'd, so a
  fresh dwell episode starts cleanly the next time someone enters.
  The GC is driven manually (``auto_gc=False``) because the reset
  rule is scoped: only keys of the camera the current event belongs
  to, and never a label that IS present in the current frame — that
  exception is what keeps sparse inference streams (0.1 fps with
  grace=5s) accruing one continuous episode.

Per-camera × per-label: a person and a car loitering simultaneously
fire separate alerts. Per-track tracking (one person leaves, a
different one arrives) is NOT modeled in v1 — the upstream adapter
would need to emit ``track_id`` on each detection AND we'd need to
swap the state key to (camera, label, track_id), which is a follow-up.

Run::

    python loitering_detection.py --config config.yml
    python loitering_detection.py --config config.yml --once  # one event then exit
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from opennvr_app_sdk import (
    Alert,
    AlertType,
    AppManifest,
    Detector,
    Param,
    StateView,
    app,
)
from opennvr_app_sdk.config import load_yaml
from opennvr_app_sdk.cameras import full_frame_polygon, per_camera_value
from opennvr_app_sdk.geometry import Zone, bbox_center, scale_vertices
from opennvr_app_sdk.state import StateRecord, keyed_state

logger = logging.getLogger("loitering-detection")


MANIFEST = AppManifest(
    id="loitering-detection",
    name="Loitering Detection",
    version="1.0.0",
    category="perimeter",
    summary="Alerts when a watched object dwells in a zone beyond a threshold.",
    requires_tasks=["object_detection"],  # checked vs GET /api/v1/adapters
    subscribes="opennvr.inference.>",
    params=[
        Param("watch_labels", list, default=["person"]),
        Param("threshold_seconds", float, default=60.0),
        Param("grace_period_seconds", float, default=5.0,
              description="Absence gap absorbed without splitting a dwell."),
        Param("zones", "geometry.polygon", per_camera=True),  # drawn in the catalog UI
    ],
    emits=[AlertType("loitering", severity="medium")],
    # Declarative live-state views — the catalog renders the dwell
    # bookkeeping (see state_snapshot) with zero app-specific UI code.
    state_schema=[
        StateView(name="active_dwells", label="Active dwells",
                  kind="metric", path="active_dwells",
                  description="Watched objects currently accruing dwell time "
                              "in a zone (one per live (camera, label) key)."),
        StateView(name="alerted", label="Alerts fired",
                  kind="metric", path="alerted",
                  description="Active dwells that have crossed the loiter "
                              "threshold and latched an alert."),
        StateView(name="dwelling", label="Dwelling now",
                  kind="table", path="dwelling",
                  columns=["camera", "label", "dwell_s", "alerted"],
                  description="Each object currently dwelling in a zone, with "
                              "its accrued dwell in seconds and whether it has "
                              "already fired a loitering alert."),
    ],
)


# ── Config ─────────────────────────────────────────────────────────


@dataclass
class CameraWatch:
    """One camera + its zone + its pixel dimensions. We don't subscribe
    per-camera — we subscribe to the wildcard ``opennvr.inference.>``
    and match by ``camera_id`` from the event payload — but we still
    need this struct so the zone math + alert payload can look up the
    right zone polygon when an event arrives."""

    camera_id: str
    zone: Zone
    frame_width: int
    frame_height: int


@dataclass
class AppConfig:
    """Top-level config loaded from YAML."""

    nats_url: str
    nats_token: str | None
    subject_pattern: str
    watch_labels: list[str]
    threshold_seconds: float
    grace_period_seconds: float
    cameras: dict[str, CameraWatch]  # keyed by camera_id for O(1) lookup
    webhook_url: str | None
    # Optional NATS alert fan-out. Distinct from ``nats_url``: that one
    # is where we SUBSCRIBE for inference events, this one is where we
    # PUBLISH our alerts. In a single-host deployment both will point
    # at the same broker; in a federated setup they may differ.
    nats_alerts_url: str | None = None
    nats_alerts_token: str | None = None
    nats_alerts_subject_prefix: str = "opennvr.alerts"

    # App contract (spec §03) — all optional; see the SDK's contract
    # module. ``contract_port`` serves /health /manifest /state;
    # ``opennvr_url`` triggers registry self-registration on boot.
    contract_port: int | None = None
    contract_bind_host: str | None = None
    contract_host: str | None = None
    opennvr_url: str | None = None
    opennvr_token: str | None = None


def load_config(path: str) -> AppConfig:
    """Parse a YAML config file into a typed AppConfig.

    Raises ``ValueError`` on malformed config — caller's job to
    surface a useful operator message and exit non-zero."""
    raw = load_yaml(path)

    nats_url = str(raw.get("nats_url") or "").strip()
    if not nats_url:
        raise ValueError("config: 'nats_url' is required")

    # Subject pattern — default to all inference results, operator
    # can narrow to a single adapter (e.g. opennvr.inference.yolov8.>)
    if "subject_pattern" in raw:
        subject = str(raw.get("subject_pattern") or "").strip()
        if not subject:
            raise ValueError("config: 'subject_pattern' must not be empty")
    else:
        subject = "opennvr.inference.>"

    try:
        threshold = float(raw.get("threshold_seconds", 60.0))
    except (TypeError, ValueError) as exc:
        raise ValueError("config: 'threshold_seconds' must be a number") from exc
    if threshold <= 0:
        raise ValueError("config: 'threshold_seconds' must be > 0")

    try:
        grace = float(raw.get("grace_period_seconds", 5.0))
    except (TypeError, ValueError) as exc:
        raise ValueError("config: 'grace_period_seconds' must be a number") from exc
    if grace <= 0:
        raise ValueError("config: 'grace_period_seconds' must be > 0")

    cameras_raw = raw.get("cameras") or []
    if not cameras_raw and not raw.get("opennvr_url"):
        # Connected to OpenNVR, cameras are PICKED in the App Catalog and
        # geometry is drawn there — no YAML list needed. Standalone,
        # there is nowhere else to get them from.
        raise ValueError(
            "config: at least one camera entry is required (or set "
            "opennvr_url and pick cameras in the App Catalog)"
        )
    cameras: dict[str, CameraWatch] = {}
    for idx, c in enumerate(cameras_raw):
        try:
            zone = Zone.from_config(
                name=str(c.get("zone_name", f"zone-{idx}")),
                vertices=c["zone"],
            )
            frame_width = int(c.get("frame_width", 1920))
            frame_height = int(c.get("frame_height", 1080))
            # Frame dimensions are used to scale normalized bboxes back
            # to pixels for zone math. Non-positive values silently
            # bucket every detection at the origin and miss every zone
            # check; refuse the config rather than swallow this.
            # (Peer review H3.)
            if frame_width <= 0 or frame_height <= 0:
                raise ValueError(
                    f"frame_width and frame_height must be > 0; got "
                    f"frame_width={frame_width}, frame_height={frame_height}"
                )
            cam = CameraWatch(
                camera_id=str(c["camera_id"]),
                zone=zone,
                frame_width=frame_width,
                frame_height=frame_height,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"config: camera entry {idx} malformed: {exc}"
            ) from exc
        # Two camera entries with the same id would silently overwrite —
        # the second wins and the operator's intent for the first is
        # lost without warning. Refuse at validate time.
        # (Peer review H1.)
        if cam.camera_id in cameras:
            raise ValueError(
                f"config: duplicate camera_id {cam.camera_id!r} at entry {idx}"
            )
        cameras[cam.camera_id] = cam

    # ``watch_labels`` defaults to ``["person"]`` when absent — but an
    # explicit empty list should be rejected rather than silently
    # producing a detector that never matches anything. (Peer review H2.)
    watch_labels_raw = raw.get("watch_labels")
    if watch_labels_raw is None:
        watch_labels = ["person"]
    else:
        watch_labels = [str(s).lower() for s in watch_labels_raw]
        if not watch_labels:
            raise ValueError(
                "config: 'watch_labels' must not be empty (omit the key to "
                "use the default ['person'], or list at least one label)"
            )

    nats_alerts_url = str(raw["nats_alerts_url"]).strip() if raw.get("nats_alerts_url") else None
    nats_alerts_token = str(raw["nats_alerts_token"]) if raw.get("nats_alerts_token") else None
    if "nats_alerts_subject_prefix" in raw:
        nats_prefix = str(raw["nats_alerts_subject_prefix"]).strip()
        if not nats_prefix:
            raise ValueError(
                "config: 'nats_alerts_subject_prefix' must not be empty "
                "(omit the key to use the default 'opennvr.alerts')"
            )
    else:
        nats_prefix = "opennvr.alerts"

    return AppConfig(
        nats_url=nats_url,
        nats_token=str(raw["nats_token"]) if raw.get("nats_token") else None,
        subject_pattern=subject,
        watch_labels=watch_labels,
        threshold_seconds=threshold,
        grace_period_seconds=grace,
        cameras=cameras,
        webhook_url=str(raw["webhook_url"]) if raw.get("webhook_url") else None,
        nats_alerts_url=nats_alerts_url,
        nats_alerts_token=nats_alerts_token,
        nats_alerts_subject_prefix=nats_prefix,
        contract_port=(
            int(raw["contract_port"]) if raw.get("contract_port") is not None else None
        ),
        contract_bind_host=raw.get("contract_bind_host"),
        contract_host=raw.get("contract_host"),
        opennvr_url=raw.get("opennvr_url"),
        opennvr_token=raw.get("opennvr_token"),
    )


# ── Per-(camera, label) dwell state ────────────────────────────────


class _DwellState(StateRecord):
    """The SDK ``StateRecord`` under this app's historical vocabulary:
    ``present_since`` is the timestamp of the first uninterrupted
    presence frame (the record's ``first_seen``). ``last_seen`` and the
    ``alerted`` latch come straight from the base."""

    @property
    def present_since(self) -> float:
        return self.first_seen


# ── The rule ───────────────────────────────────────────────────────


class LoiteringDetector(Detector):
    """Consumes ``opennvr.inference.{adapter}.{camera_id}.completed``
    events (via the SDK's Detector loop) and tracks per-camera ×
    per-label dwell time.

    Stateful — one ``_DwellState`` per (camera_id, label) key, held in
    a ``keyed_state``. The state survives across events but is GC'd
    when ``grace_period_seconds`` elapse without a presence frame.
    Bounded by configured cameras × watch_labels — events from unknown
    camera_ids short-circuit before touching state.

    Override ``_build_alert`` in a subclass to enrich the alert payload
    (snapshot URL, evidence bundle, etc.). The default fires a
    §11.5-shaped Alert via the SDK ``AlertDispatcher``.
    """

    manifest = MANIFEST

    def setup(self) -> None:
        # auto_gc off: the grace-period reset is scoped per camera and
        # spares in-frame labels (see module docstring) — driven
        # explicitly in _gc_absent_labels instead of inside touch().
        self._states = keyed_state(
            ttl=self.cfg.grace_period_seconds,
            auto_gc=False,
            record_factory=_DwellState,
        )

    # ── Cameras picked in the catalog ──────────────────────────────
    #
    # Connected to OpenNVR, this app works on the cameras picked for it in
    # its configuration — the SDK drops every other camera's events before
    # they reach on_detections — using the zone drawn there. A YAML
    # camera entry still wins for its own camera, so a standalone config
    # keeps working unchanged.

    #: The virtual frame catalog geometry is scaled into. Detection boxes
    #: arrive normalised, so any fixed size is exact; 1920x1080 keeps the
    #: pixel-denominated tuning parameters meaning what their defaults
    #: were chosen for.
    CATALOG_FRAME = (1920, 1080)

    def on_config_update(self, config: dict[str, Any]) -> None:
        """Pick up zone drawn in the catalog, live."""
        drawn = (config or {}).get("zones")
        self._drawn = drawn if isinstance(drawn, dict) else {}
        self._catalog_cameras: dict[str, CameraWatch] = {}

    def _camera_for(self, camera_id: str) -> "CameraWatch | None":
        camera = self.cfg.cameras.get(camera_id)
        if camera is not None:
            return camera
        if self._config_poll_thread is None:
            return None   # standalone: only the YAML cameras exist
        cache = getattr(self, "_catalog_cameras", {})
        if camera_id not in cache:
            # Rebound, not mutated: the config poll thread swaps the dict.
            self._catalog_cameras = {**cache, camera_id: self._build_catalog_camera(camera_id)}
        return self._catalog_cameras[camera_id]

    def _build_catalog_camera(self, camera_id: str) -> "CameraWatch | None":
        w, h = self.CATALOG_FRAME
        drawn = per_camera_value(getattr(self, "_drawn", {}), camera_id)
        # Nothing drawn yet: watch the whole frame rather than nothing.
        vertices = drawn if isinstance(drawn, (list, tuple)) and len(drawn) >= 3 \
            else full_frame_polygon(1)
        return CameraWatch(camera_id=camera_id,
                    zone=Zone.from_config(name="drawn",
                                          vertices=scale_vertices(vertices, w, h)),
                    frame_width=w, frame_height=h)

    def on_detections(
        self,
        camera_id: str,
        detections: list[dict[str, Any]],
        event: dict[str, Any],
    ) -> list[Alert]:
        """The dwell rule for one event. Returns the alerts to fire
        (the SDK base dispatches them). Pure w.r.t. ``self._states`` —
        the existing tests drive it through ``handle_event`` without
        spinning up NATS."""
        camera = self._camera_for(camera_id)
        if camera is None:
            # Another monitoring app may be watching this camera; we're not.
            return []

        # Use the event's completed_at timestamp if present (so the
        # state machine tracks wall-clock dwell on the adapter side,
        # not network latency to us). Falls back to "now" if missing.
        event_ts = self.parse_event_ts(event.get("completed_at"))

        # For each watched-label that has at least one in-zone
        # detection in THIS event, update its dwell state. We use a
        # set so multiple detections of the same label in one frame
        # only count once.
        labels_seen_in_zone: set[str] = set()
        for det in detections:
            if not isinstance(det, dict):
                continue
            label = str(det.get("label", "")).lower()
            if label not in self.cfg.watch_labels:
                continue
            bbox = det.get("bbox")
            if not isinstance(bbox, dict):
                continue
            center = bbox_center(bbox, camera.frame_width, camera.frame_height)
            if camera.zone.contains(center):
                labels_seen_in_zone.add(label)

        # Reset stale state — ONLY for (camera, label) pairs whose
        # entity is NOT present in this frame. A label that IS in
        # this frame's labels_seen_in_zone counts as a fresh
        # presence ping; we refresh its ``last_seen`` rather than
        # GC the state. This is what makes the grace-period
        # semantics correct under sparse inference frequencies
        # (1 fps with grace=5s tolerates up-to-5s detection gaps;
        # at 0.1 fps with grace=5s a continuous presence frame at
        # t=10 would otherwise look stale vs. t=0).
        self._gc_absent_labels(camera_id, labels_seen_in_zone, event_ts)

        fired_now: list[Alert] = []
        for label in labels_seen_in_zone:
            key = (camera_id, label)
            # Defensive against out-of-order events: NATS doesn't
            # strictly order across publishers, and a misbehaving
            # publisher can emit events with non-monotonic
            # ``completed_at`` timestamps. If an event is older
            # than the most recent one we've already processed
            # for this (camera, label), treat it as a duplicate /
            # stale message and skip — dwell math relies on
            # monotonic time and a backward jump would silently
            # corrupt the state. (Peer review M2.)
            existing = self._states.get(key)
            if existing is not None and event_ts < existing.last_seen:
                logger.debug(
                    "skipping out-of-order event for %s/%s "
                    "(event_ts=%.3f < last_seen=%.3f)",
                    camera_id, label, event_ts, existing.last_seen,
                )
                continue
            state = self._states.touch(key, at=event_ts)
            dwell = state.age  # event_ts - present_since
            if dwell >= self.cfg.threshold_seconds and not state.alerted:
                fired_now.append(self._build_alert(
                    camera=camera, label=label, dwell_seconds=dwell, event=event,
                ))
                state.alerted = True
        return fired_now

    def _gc_absent_labels(
        self,
        camera_id: str,
        labels_present_now: set[str],
        now_ts: float,
    ) -> None:
        """Reset state for any (camera, label) whose label is NOT
        present in the current frame AND whose ``last_seen`` is older
        than ``grace_period_seconds``. This is how a brief detection
        gap (occlusion, false negative) gets absorbed without
        prematurely ending a dwell episode."""
        cutoff = now_ts - self.cfg.grace_period_seconds
        for key, state in self._states.items():
            if (
                key[0] == camera_id
                and key[1] not in labels_present_now
                and state.last_seen < cutoff
            ):
                self._states.pop(key)

    def _build_alert(
        self,
        *,
        camera: CameraWatch,
        label: str,
        dwell_seconds: float,
        event: dict[str, Any],
    ) -> Alert:
        correlation_id = str(event.get("correlation_id") or "")
        return Alert(
            title=f"{label.capitalize()} loitering in zone {camera.zone.name!r}",
            description=(
                f"Detected {label} dwelling in zone {camera.zone.name!r} on "
                f"camera {camera.camera_id} for {dwell_seconds:.1f}s "
                f"(threshold {self.cfg.threshold_seconds:.1f}s)."
            ),
            camera_id=camera.camera_id,
            severity="medium",
            correlation_id=correlation_id,
            evidence={
                "label": label,
                "dwell_seconds": round(dwell_seconds, 2),
                "threshold_seconds": self.cfg.threshold_seconds,
                "zone_name": camera.zone.name,
                "adapter": event.get("adapter"),
                "adapter_version": event.get("adapter_version"),
                "model_fingerprint": event.get("model_fingerprint"),
            },
            tags=["loitering", camera.zone.name, label],
        )

    def state_snapshot(self) -> dict[str, Any]:
        """``GET /state`` — live dwell bookkeeping the catalog renders
        via ``state_schema``. Each live ``_DwellState`` is one object
        currently accruing dwell in a zone.

        ``present_since`` / ``last_seen`` are POSIX event-time seconds
        (``parse_event_ts`` → ``time.time`` fallback, matching the
        ``keyed_state`` default clock), so "now" is ``time.time()``.
        A backdated ``completed_at`` could put ``present_since`` in the
        future relative to our wall clock; clamp at 0 so ``dwell_s`` is
        never negative."""
        now = time.time()
        dwelling: list[dict[str, Any]] = []
        for (camera_id, label), record in self._states.items():
            dwelling.append({
                "camera": camera_id,
                "label": label,
                "dwell_s": round(max(0.0, now - record.present_since), 1),
                "alerted": bool(record.alerted),
            })
        return {
            "active_dwells": len(dwelling),
            "alerted": sum(1 for row in dwelling if row["alerted"]),
            "dwelling": dwelling,
        }


# Spec-preferred short name; ``LoiteringDetector`` is the historical
# one the tests (and README snippets) import.
Loitering = LoiteringDetector


# ── CLI ────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point (``[project.scripts]``). The SDK
    runner owns argparse, logging, signals, and the dispatcher."""
    return app(LoiteringDetector, load_config=load_config).run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
