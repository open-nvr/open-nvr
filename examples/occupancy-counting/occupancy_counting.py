# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Occupancy-counting example app — on the ``opennvr-app-sdk``
(App SDK spec §08 step 5, swept after the loitering reference
migration).

Counts how many watched-label entities (people, vehicles, …) are
inside each operator-defined zone on every inference frame, and fires
an alert when a zone crosses an occupancy threshold — too many
(over-occupancy: a crowded exit, an over-capacity room) or, optionally,
too few (under-occupancy: a post that should always be staffed).

What lives where after the migration
------------------------------------

The SDK's :class:`~opennvr_app_sdk.Detector` base owns the NATS
subscribe loop, per-message JSON decoding + exception isolation, the
``camera_id`` / ``result.detections`` payload walk, alert dispatch,
the CLI, signal handling, and the §03 contract endpoints. The §11.5
alert stack and the zone geometry moved to ``opennvr_app_sdk.alerts``
/ ``opennvr_app_sdk.geometry`` (thin shims remain at ``alerts.py`` /
``zone.py`` for import compatibility).

What's left here is the rule — the edge-triggered occupancy state
machine — plus this app's config parsing and its declarative MANIFEST.

Architecture (unchanged)
------------------------

Like ``loitering-detection`` and unlike ``intrusion-detection``, this
app SUBSCRIBES to KAI-C's NATS inference broadcast surface
(``opennvr.inference.>``) rather than driving its own inference. It
rides whatever detection stream another app (e.g. intrusion-detection)
is already producing, so it pays zero adapter/GPU cost on top — one
inference fans out to N counting consumers.

State machine (per camera × zone)
---------------------------------

Occupancy is a level, not an event, so we alert on *transitions* of an
edge-triggered state machine rather than on every frame:

* ``normal``  → ``over``   when count > ``max_occupancy``   → fire OVER
* ``normal``  → ``under``  when count < ``min_occupancy``   → fire UNDER
* ``over`` / ``under`` → ``normal`` when count returns to the
  acceptable band → fire CLEARED (only if ``clear_alerts: true``)

Edge-triggering is what stops a crowded room from emitting one alert
per inference frame. A short ``debounce_frames`` requirement (default
1) can be raised so a single noisy frame doesn't flip the state.

The state is deliberately a plain ``dict`` rather than the SDK's
``keyed_state``: occupancy is a *level* keyed by a bounded, config-known
camera set — there is no TTL/absence semantics to garbage-collect, and
the debounce latch is a frame counter, not a time latch.

Per-track identity is NOT used: occupancy is a count of in-zone
detections per frame, so a detector that emits ``track_id`` and one
that doesn't both work. Double-counting from duplicate boxes on the
same object is mitigated by the detector's own NMS upstream.

Run::

    python occupancy_counting.py --config config.yml
    python occupancy_counting.py --config config.yml --once
"""
from __future__ import annotations

import logging
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
from opennvr_app_sdk.geometry import Zone, bbox_center

logger = logging.getLogger("occupancy-counting")


MANIFEST = AppManifest(
    id="occupancy-counting",
    name="Occupancy Counting",
    version="1.0.0",
    category="analytics",
    summary="Alerts on zone occupancy threshold crossings (over / under / cleared).",
    requires_tasks=["object_detection"],  # checked vs GET /api/v1/adapters
    subscribes="opennvr.inference.>",
    params=[
        Param("watch_labels", list, default=["person"]),
        Param("max_occupancy", int, required=True,
              description="Fire OVER when the in-zone count exceeds this."),
        Param("min_occupancy", int,
              description="Fire UNDER when the in-zone count drops below this."),
        Param("debounce_frames", int, default=1,
              description="Consecutive frames a new band must persist before firing."),
        Param("clear_alerts", bool, default=False,
              description="Also fire a low-severity alert when a zone returns to normal."),
        Param("zones", "geometry.polygon", per_camera=True),  # drawn in the catalog UI
    ],
    emits=[
        AlertType("occupancy_over", severity="high"),
        AlertType("occupancy_under", severity="medium"),
        AlertType("occupancy_cleared", severity="low"),
    ],
    # Declarative live-state views — the catalog renders GET /state
    # (state_snapshot below) with zero app-specific UI code. The
    # cameras dict renders as a table with the camera id as the
    # leading column.
    state_schema=[
        StateView(
            name="people",
            label="People counted",
            kind="metric",
            path="total_people",
            description="Sum of the last head-count across all watched zones.",
        ),
        StateView(
            name="over",
            label="Zones over limit",
            kind="metric",
            path="zones_over",
            description="How many zones are currently above their max_occupancy.",
        ),
        StateView(
            name="cameras",
            label="Live occupancy",
            kind="table",
            path="cameras",
            columns=["id", "level", "last_count", "pending"],
            description="Current occupancy band + last count per camera.",
        ),
    ],
)


# ── Config ─────────────────────────────────────────────────────────


@dataclass
class CameraZone:
    """One camera + one counted zone + its pixel dimensions.

    ``max_occupancy`` / ``min_occupancy`` may be set per-camera to
    override the app-level defaults — a doorway and a stadium concourse
    on the same deployment want very different thresholds.
    """

    camera_id: str
    zone: Zone
    frame_width: int
    frame_height: int
    max_occupancy: int
    min_occupancy: int | None


@dataclass
class AppConfig:
    nats_url: str
    nats_token: str | None
    subject_pattern: str
    watch_labels: list[str]
    debounce_frames: int
    clear_alerts: bool
    cameras: dict[str, CameraZone]  # keyed by camera_id for O(1) lookup
    webhook_url: str | None
    nats_alerts_url: str | None = None
    nats_alerts_token: str | None = None
    nats_alerts_subject_prefix: str = "opennvr.alerts"
    # Consume the always-on Tier-0 detector (docs/tier0-consumption.md).
    # ON by default: Tier-0 ships enabled in the standard stack and is the
    # only detection stream a default install produces, so an app that
    # ignored it would sit silent forever. The SDK's own default is off
    # (contract compatibility); an app that also subscribes to a heavy
    # adapter should narrow ``subject_pattern`` to
    # ``opennvr.inference.tier0.>`` — as the shipped config does — or turn
    # this off, otherwise the same object is counted from both streams.
    consume_tier0: bool = True

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

    Raises ``ValueError`` on malformed config so the CLI can surface a
    useful operator message and exit non-zero."""
    raw = load_yaml(path)

    nats_url = str(raw.get("nats_url") or "").strip()
    if not nats_url:
        raise ValueError("config: 'nats_url' is required")

    if "subject_pattern" in raw:
        subject = str(raw.get("subject_pattern") or "").strip()
        if not subject:
            raise ValueError("config: 'subject_pattern' must not be empty")
    else:
        subject = "opennvr.inference.>"

    try:
        debounce = int(raw.get("debounce_frames", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError("config: 'debounce_frames' must be an integer") from exc
    if debounce < 1:
        raise ValueError("config: 'debounce_frames' must be >= 1")

    clear_alerts = bool(raw.get("clear_alerts", False))

    # App-level default thresholds; per-camera entries may override.
    default_max = raw.get("max_occupancy")
    default_min = raw.get("min_occupancy")

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

    cameras_raw = raw.get("cameras") or []
    if not cameras_raw:
        raise ValueError("config: at least one camera entry is required")
    cameras: dict[str, CameraZone] = {}
    for idx, c in enumerate(cameras_raw):
        try:
            zone = Zone.from_config(
                name=str(c.get("zone_name", f"zone-{idx}")),
                vertices=c["zone"],
            )
            frame_width = int(c.get("frame_width", 1920))
            frame_height = int(c.get("frame_height", 1080))
            if frame_width <= 0 or frame_height <= 0:
                raise ValueError(
                    f"frame_width and frame_height must be > 0; got "
                    f"frame_width={frame_width}, frame_height={frame_height}"
                )
            # Threshold resolution: per-camera value wins, else the
            # app-level default. max_occupancy is mandatory (an
            # occupancy counter with no ceiling never fires OVER);
            # min_occupancy is optional (most deployments only care
            # about over-occupancy).
            raw_max = c.get("max_occupancy", default_max)
            if raw_max is None:
                raise ValueError(
                    f"camera entry {idx}: 'max_occupancy' is required "
                    "(set it per-camera or as an app-level default)"
                )
            max_occ = int(raw_max)
            if max_occ < 0:
                raise ValueError("'max_occupancy' must be >= 0")
            raw_min = c.get("min_occupancy", default_min)
            min_occ = int(raw_min) if raw_min is not None else None
            if min_occ is not None and min_occ < 0:
                raise ValueError("'min_occupancy' must be >= 0")
            if min_occ is not None and min_occ > max_occ:
                raise ValueError(
                    f"'min_occupancy' ({min_occ}) must be <= "
                    f"'max_occupancy' ({max_occ})"
                )
            cam = CameraZone(
                camera_id=str(c["camera_id"]),
                zone=zone,
                frame_width=frame_width,
                frame_height=frame_height,
                max_occupancy=max_occ,
                min_occupancy=min_occ,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"config: camera entry {idx} malformed: {exc}"
            ) from exc
        if cam.camera_id in cameras:
            raise ValueError(
                f"config: duplicate camera_id {cam.camera_id!r} at entry {idx}"
            )
        cameras[cam.camera_id] = cam

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
        debounce_frames=debounce,
        clear_alerts=clear_alerts,
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
        consume_tier0=bool(raw.get("consume_tier0", True)),
        opennvr_url=raw.get("opennvr_url"),
        opennvr_token=raw.get("opennvr_token"),
    )


# ── Per-camera occupancy state ─────────────────────────────────────


@dataclass
class _ZoneState:
    """Edge-triggered occupancy state for one camera × zone.

    ``level`` is the current alerting band: ``"normal"`` / ``"over"`` /
    ``"under"``. ``pending`` + ``pending_count`` implement the debounce:
    a candidate new level must persist for ``debounce_frames`` frames
    before it becomes the committed ``level`` and fires an alert.
    """

    level: str = "normal"
    pending: str | None = None
    pending_count: int = 0
    last_count: int = 0


# ── The rule ───────────────────────────────────────────────────────


class OccupancyCounter(Detector):
    """Consumes inference events (via the SDK's Detector loop) and
    counts in-zone entities per camera, firing edge-triggered
    occupancy alerts.

    Stateful — one ``_ZoneState`` per camera_id. State is bounded by
    the configured camera set (events from unknown cameras are dropped
    before any state is created)."""

    manifest = MANIFEST

    def setup(self) -> None:
        self._states: dict[str, _ZoneState] = {}
        # Camera ids seen on the bus that this app has no config for —
        # tracked so the "not my camera" warning fires once each, not per
        # frame (Tier-0 publishes continuously).
        self._unknown_cameras: set[str] = set()

    # ── Pure helpers (testable without NATS) ──────────────────────

    def count_in_zone(self, camera: CameraZone, detections: list[Any]) -> int:
        """Count detections whose label is watched and whose bbox
        center falls inside the camera's zone."""
        count = 0
        for det in detections:
            if not isinstance(det, dict):
                continue
            label = str(det.get("label", "")).lower()
            if label not in self._config.watch_labels:
                continue
            bbox = det.get("bbox")
            if not isinstance(bbox, dict):
                continue
            center = bbox_center(bbox, camera.frame_width, camera.frame_height)
            if camera.zone.contains(center):
                count += 1
        return count

    def _classify(self, camera: CameraZone, count: int) -> str:
        """Map a raw count to an alerting band."""
        if count > camera.max_occupancy:
            return "over"
        if camera.min_occupancy is not None and count < camera.min_occupancy:
            return "under"
        return "normal"

    def on_detections(
        self,
        camera_id: str,
        detections: list[dict[str, Any]],
        event: dict[str, Any],
    ) -> list[Alert]:
        """The occupancy rule for one event. Returns the alerts to fire
        (the SDK base dispatches them and ``handle_event`` returns
        them, which is what the tests assert on)."""
        camera = self._config.cameras.get(camera_id)
        if camera is None:
            # Another monitoring app may be watching this camera; we're not.
            # But an id that matches NOTHING configured is the difference
            # between "not my camera" and "this app counts nothing, forever"
            # — and the two look identical from outside. Say it once per
            # unknown id so a config/id mismatch (the classic ``cam-1`` vs
            # ``cam1``) is one log line instead of a silent no-op.
            if camera_id not in self._unknown_cameras:
                self._unknown_cameras.add(camera_id)
                logger.warning(
                    "receiving events for camera %r, which is not in my config "
                    "— nothing will be counted for it. Configured cameras: %s",
                    camera_id, sorted(self._config.cameras) or "(none)",
                )
            return []

        count = self.count_in_zone(camera, detections)
        candidate = self._classify(camera, count)
        state = self._states.setdefault(camera_id, _ZoneState())
        state.last_count = count

        # Already in the candidate band → nothing to commit; clear any
        # half-formed pending transition (the level is stable).
        if candidate == state.level:
            state.pending = None
            state.pending_count = 0
            return []

        # Debounce: the candidate must persist for N consecutive frames
        # before we commit the transition and fire.
        if state.pending == candidate:
            state.pending_count += 1
        else:
            state.pending = candidate
            state.pending_count = 1

        if state.pending_count < self._config.debounce_frames:
            return []

        previous = state.level
        state.level = candidate
        state.pending = None
        state.pending_count = 0

        # Returning to normal → only fire if clear_alerts is on.
        if candidate == "normal" and not self._config.clear_alerts:
            return []

        return [self._build_alert(
            camera=camera, count=count, level=candidate,
            previous=previous, event=event,
        )]

    def state_snapshot(self) -> dict[str, Any]:
        """``GET /state`` — live occupancy per configured camera plus
        two roll-ups (total people, zones over limit) for the app's
        dashboard summary chips."""
        return {
            "total_people": sum(s.last_count for s in self._states.values()),
            "zones_over": sum(1 for s in self._states.values() if s.level == "over"),
            "cameras": {
                camera_id: {
                    "level": state.level,
                    "last_count": state.last_count,
                    "pending": state.pending,
                }
                for camera_id, state in self._states.items()
            },
        }

    def _build_alert(
        self,
        *,
        camera: CameraZone,
        count: int,
        level: str,
        previous: str,
        event: dict[str, Any],
    ) -> Alert:
        correlation_id = str(event.get("correlation_id") or "")
        if level == "over":
            title = f"Over-occupancy in zone {camera.zone.name!r}"
            description = (
                f"{count} entities in zone {camera.zone.name!r} on camera "
                f"{camera.camera_id} (limit {camera.max_occupancy})."
            )
            severity = "high"
        elif level == "under":
            title = f"Under-occupancy in zone {camera.zone.name!r}"
            description = (
                f"Only {count} entities in zone {camera.zone.name!r} on "
                f"camera {camera.camera_id} (minimum {camera.min_occupancy})."
            )
            severity = "medium"
        else:  # cleared
            title = f"Occupancy back to normal in zone {camera.zone.name!r}"
            description = (
                f"Zone {camera.zone.name!r} on camera {camera.camera_id} "
                f"returned to normal occupancy ({count}) from {previous!r}."
            )
            severity = "low"
        return Alert(
            title=title,
            description=description,
            camera_id=camera.camera_id,
            severity=severity,
            correlation_id=correlation_id,
            evidence={
                "count": count,
                "level": level,
                "previous_level": previous,
                "max_occupancy": camera.max_occupancy,
                "min_occupancy": camera.min_occupancy,
                "zone_name": camera.zone.name,
                "watch_labels": self._config.watch_labels,
                "adapter": event.get("adapter"),
                "adapter_version": event.get("adapter_version"),
                "model_fingerprint": event.get("model_fingerprint"),
            },
            tags=["occupancy", level, camera.zone.name],
        )


# ── CLI ────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point (``[project.scripts]``). The SDK
    runner owns argparse, logging, signals, and the dispatcher."""
    return app(OccupancyCounter, load_config=load_config).run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
