# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Abandoned-object (unattended-item) example app — now on the
``opennvr-app-sdk``.

Fires an alert when a watched object (bag, suitcase, backpack, box, …)
stays roughly stationary inside an operator-defined zone for longer
than a dwell threshold AND no person has been near it for a
suppression window — the classic "unattended baggage" primitive for
transport hubs, lobbies, and secure perimeters.

What lives where after the migration
------------------------------------

The SDK's :class:`~opennvr_app_sdk.Detector` base owns the NATS
subscribe loop, per-message JSON decoding + exception isolation, the
``camera_id`` / ``result.detections`` payload walk, ``completed_at``
timestamp parsing with a clock fallback, alert dispatch, the CLI, and
signal handling. The zone geometry and the §11.5 alert stack live in
``opennvr_app_sdk.geometry`` / ``opennvr_app_sdk.alerts`` (thin shims
remain at ``zone.py`` / ``alerts.py`` for import compatibility).

What's left here is the rule — the stationary-anchor state machine and
the person-proximity suppression — plus this app's config parsing and
its declarative MANIFEST.

Architecture (unchanged)
------------------------

Subscribes to KAI-C's NATS inference broadcast surface like the other
monitoring apps (zero adapter cost on top of the detection stream
already running). It needs detections that carry a ``track_id`` so it
can tell that *the same* object has been sitting still — chain the
``bytetrack`` adapter after your detector, or use a detector that
tracks natively. Untracked detections are ignored with a one-time
warning.

How "abandoned" is decided
--------------------------

For each watched-object track in the zone we remember where it was
first seen and when (an SDK ``keyed_state`` record: the anchor point
rides ``data``, the settle time is the record's ``first_seen`` — reset
whenever the object moves). A track counts as *stationary* while its
center stays within ``move_tolerance_px`` of that anchor; if it moves
further the anchor resets (the object was carried, not abandoned).
When a track has been stationary in-zone for ``dwell_seconds`` AND no
``person`` detection has been seen within ``person_radius_px`` of it
during the last ``owner_grace_seconds``, we fire once (the record's
``alerted`` latch).

The person-proximity suppression is what stops every parked bag next
to its owner from alerting — an object is only "abandoned" once its
likely owner has left its vicinity. The recent-people buffer stays a
plain per-camera list (like package-delivery's person sightings): it
is a time-windowed ring buffer, not TTL-keyed presence state.

Run::

    python abandoned_object.py --config config.yml
    python abandoned_object.py --config config.yml --once
"""
from __future__ import annotations

import logging
import math
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
from opennvr_app_sdk.geometry import Point, Zone, bbox_center, scale_vertices
from opennvr_app_sdk.state import StateRecord, keyed_state

logger = logging.getLogger("abandoned-object")


MANIFEST = AppManifest(
    id="abandoned-object",
    name="Abandoned Object",
    version="1.0.0",
    category="perimeter",
    summary=(
        "Alerts when a watched object sits stationary in a zone past a "
        "dwell threshold with no person nearby."
    ),
    # Needs per-object identity on top of detection — chain a tracking
    # adapter (e.g. bytetrack) so detections carry ``track_id``.
    requires_tasks=["object_detection", "multi_object_tracking"],
    subscribes="opennvr.inference.>",
    params=[
        Param("object_labels", list, default=["backpack", "handbag", "suitcase"]),
        Param("person_label", str, default="person"),
        Param("dwell_seconds", float, default=30.0),
        Param("move_tolerance_px", float, default=40.0,
              description="Center drift allowed before the object counts as carried."),
        Param("person_radius_px", float, default=250.0,
              description="Person-proximity radius that suppresses the alert."),
        Param("owner_grace_seconds", float, default=10.0,
              description="How recently a nearby person still counts as the owner."),
        Param("track_ttl_seconds", float, default=60.0,
              description="Idle time after which an object track is forgotten."),
        Param("zones", "geometry.polygon", per_camera=True),  # drawn in the catalog UI
    ],
    emits=[AlertType("abandoned-object", severity="high")],
    # Declarative live view — the catalog renders the stationary-object
    # bookkeeping with no app-specific UI code.
    state_schema=[
        StateView(
            name="watched_objects",
            label="Watched objects",
            kind="metric",
            path="watched_objects",
            description="Stationary-candidate object tracks currently held in state.",
        ),
        StateView(
            name="abandoned",
            label="Abandoned",
            kind="metric",
            path="abandoned",
            description="Watched objects that have crossed the dwell threshold and alerted.",
        ),
        StateView(
            name="objects",
            label="Settling now",
            kind="table",
            path="objects",
            columns=["camera", "label", "dwell_s", "abandoned"],
            description="Each tracked object, its current dwell time, and whether it has fired.",
        ),
    ],
)


# ── Config ─────────────────────────────────────────────────────────


@dataclass
class CameraZone:
    camera_id: str
    zone: Zone
    frame_width: int
    frame_height: int


@dataclass
class AppConfig:
    nats_url: str
    nats_token: str | None
    subject_pattern: str
    object_labels: list[str]
    person_label: str
    dwell_seconds: float
    move_tolerance_px: float
    person_radius_px: float
    owner_grace_seconds: float
    track_ttl_seconds: float
    cameras: dict[str, CameraZone]
    webhook_url: str | None
    nats_alerts_url: str | None = None
    nats_alerts_token: str | None = None
    nats_alerts_subject_prefix: str = "opennvr.alerts"

    # App contract (spec §03) — all optional; see the SDK's contract
    # module. ``contract_port`` serves /health /manifest /state;
    # ``opennvr_url`` triggers registry self-registration on boot (the
    # agent's app door + the catalog status dot) and live config
    # delivery via on_config_update.
    contract_port: int | None = None
    contract_bind_host: str | None = None
    contract_host: str | None = None
    opennvr_url: str | None = None
    opennvr_token: str | None = None


def load_config(path: str) -> AppConfig:
    raw = load_yaml(path)

    nats_url = str(raw.get("nats_url") or "").strip()
    if not nats_url:
        raise ValueError("config: 'nats_url' is required")

    subject = str(raw.get("subject_pattern") or "opennvr.inference.>").strip()
    if not subject:
        raise ValueError("config: 'subject_pattern' must not be empty")

    def _pos_float(key: str, default: float) -> float:
        try:
            val = float(raw.get(key, default))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"config: {key!r} must be a number") from exc
        if val <= 0:
            raise ValueError(f"config: {key!r} must be > 0")
        return val

    dwell = _pos_float("dwell_seconds", 30.0)
    move_tol = _pos_float("move_tolerance_px", 40.0)
    person_radius = _pos_float("person_radius_px", 250.0)
    owner_grace = _pos_float("owner_grace_seconds", 10.0)
    track_ttl = _pos_float("track_ttl_seconds", 60.0)

    object_labels_raw = raw.get("object_labels")
    if object_labels_raw is None:
        object_labels = ["backpack", "handbag", "suitcase"]
    else:
        object_labels = [str(s).lower() for s in object_labels_raw]
        if not object_labels:
            raise ValueError("config: 'object_labels' must not be empty")
    person_label = str(raw.get("person_label", "person")).lower()

    cameras_raw = raw.get("cameras") or []
    if not cameras_raw and not raw.get("opennvr_url"):
        # Connected to OpenNVR, cameras are PICKED in the App Catalog and
        # geometry is drawn there — no YAML list needed. Standalone,
        # there is nowhere else to get them from.
        raise ValueError(
            "config: at least one camera entry is required (or set "
            "opennvr_url and pick cameras in the App Catalog)"
        )
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
                raise ValueError("frame_width and frame_height must be > 0")
            cam = CameraZone(
                camera_id=str(c["camera_id"]), zone=zone,
                frame_width=frame_width, frame_height=frame_height,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"config: camera entry {idx} malformed: {exc}") from exc
        if cam.camera_id in cameras:
            raise ValueError(f"config: duplicate camera_id {cam.camera_id!r} at entry {idx}")
        cameras[cam.camera_id] = cam

    nats_alerts_url = str(raw["nats_alerts_url"]).strip() if raw.get("nats_alerts_url") else None
    nats_alerts_token = str(raw["nats_alerts_token"]) if raw.get("nats_alerts_token") else None
    nats_prefix = str(raw.get("nats_alerts_subject_prefix", "opennvr.alerts")).strip()
    if not nats_prefix:
        raise ValueError("config: 'nats_alerts_subject_prefix' must not be empty")

    return AppConfig(
        nats_url=nats_url,
        nats_token=str(raw["nats_token"]) if raw.get("nats_token") else None,
        subject_pattern=subject,
        object_labels=object_labels,
        person_label=person_label,
        dwell_seconds=dwell,
        move_tolerance_px=move_tol,
        person_radius_px=person_radius,
        owner_grace_seconds=owner_grace,
        track_ttl_seconds=track_ttl,
        cameras=cameras,
        webhook_url=str(raw["webhook_url"]) if raw.get("webhook_url") else None,
        nats_alerts_url=nats_alerts_url,
        nats_alerts_token=nats_alerts_token,
        nats_alerts_subject_prefix=nats_prefix,
        contract_port=(
            int(raw["contract_port"]) if raw.get("contract_port") is not None else None
        ),
        contract_bind_host=(
            str(raw["contract_bind_host"]) if raw.get("contract_bind_host") else None
        ),
        contract_host=(
            str(raw["contract_host"]) if raw.get("contract_host") else None
        ),
        opennvr_url=str(raw["opennvr_url"]) if raw.get("opennvr_url") else None,
        opennvr_token=(
            str(raw["opennvr_token"]) if raw.get("opennvr_token") else None
        ),
    )


# ── Per-object state ───────────────────────────────────────────────


@dataclass
class _ObjectRecord(StateRecord):
    """The SDK ``StateRecord`` under this app's historical vocabulary:
    ``settled_since`` (when the object settled at its current anchor)
    is the record's ``first_seen`` — the app resets it whenever the
    object moves beyond tolerance, restarting the dwell clock. The
    anchor point and label ride as typed fields; the ``alerted`` latch
    comes straight from the base."""

    label: str = ""
    anchor: Point | None = None

    @property
    def settled_since(self) -> float:
        return self.first_seen


def _distance(a: Point, b: Point) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


# ── The rule ───────────────────────────────────────────────────────


class AbandonedObjectDetector(Detector):
    """Tracks stationary watched-objects in zones (via the SDK's
    Detector loop), suppresses those near a person, and fires when one
    is unattended past the dwell threshold.

    Stateful — one ``_ObjectRecord`` per (camera_id, track_id) key in
    an SDK ``keyed_state``, plus a plain per-camera ring buffer of
    recent person sightings for the proximity suppression. Both are
    bounded: object tracks are GC'd after ``track_ttl_seconds`` idle,
    the people buffer is trimmed to the owner-grace window.
    """

    manifest = MANIFEST

    def setup(self) -> None:
        # auto_gc off: the historical GC is scoped to the camera the
        # current event belongs to (other cameras' tracks age on their
        # own event streams) — driven explicitly in _gc instead of
        # inside touch().
        self._objects = keyed_state(
            ttl=self.cfg.track_ttl_seconds,
            auto_gc=False,
            record_factory=_ObjectRecord,
        )
        # (camera_id) -> list of (person_center, ts) seen recently.
        self._recent_people: dict[str, list[tuple[Point, float]]] = {}
        self._warned_missing_track = False

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
        self._catalog_cameras: dict[str, CameraZone] = {}

    def _camera_for(self, camera_id: str) -> "CameraZone | None":
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

    def _build_catalog_camera(self, camera_id: str) -> "CameraZone | None":
        w, h = self.CATALOG_FRAME
        drawn = per_camera_value(getattr(self, "_drawn", {}), camera_id)
        # Nothing drawn yet: watch the whole frame rather than nothing.
        vertices = drawn if isinstance(drawn, (list, tuple)) and len(drawn) >= 3 \
            else full_frame_polygon(1)
        return CameraZone(camera_id=camera_id,
                    zone=Zone.from_config(name="drawn",
                                          vertices=scale_vertices(vertices, w, h)),
                    frame_width=w, frame_height=h)

    def on_detections(
        self,
        camera_id: str,
        detections: list[dict[str, Any]],
        event: dict[str, Any],
    ) -> list[Alert]:
        """The abandon rule for one event. Returns the alerts to fire
        (the SDK base dispatches them). The existing tests drive it
        through ``handle_event`` without spinning up NATS."""
        camera = self._camera_for(camera_id)
        if camera is None:
            return []
        event_ts = self.parse_event_ts(event.get("completed_at"))

        self._gc(camera_id, event_ts)

        # First pass: record person positions for proximity suppression.
        people_now: list[Point] = []
        for det in detections:
            if not isinstance(det, dict):
                continue
            if str(det.get("label", "")).lower() != self.cfg.person_label:
                continue
            bbox = det.get("bbox")
            if isinstance(bbox, dict):
                people_now.append(bbox_center(bbox, camera.frame_width, camera.frame_height))
        bucket = self._recent_people.setdefault(camera_id, [])
        for p in people_now:
            bucket.append((p, event_ts))

        # Second pass: update object tracks + test the abandon predicate.
        fired: list[Alert] = []
        for det in detections:
            if not isinstance(det, dict):
                continue
            label = str(det.get("label", "")).lower()
            if label not in self.cfg.object_labels:
                continue
            track_id = det.get("track_id")
            if track_id is None:
                if not self._warned_missing_track:
                    logger.warning(
                        "object detections have no 'track_id' — abandoned-object "
                        "needs a tracking adapter (e.g. bytetrack) upstream. "
                        "Ignoring untracked objects."
                    )
                    self._warned_missing_track = True
                continue
            bbox = det.get("bbox")
            if not isinstance(bbox, dict):
                continue
            center = bbox_center(bbox, camera.frame_width, camera.frame_height)
            # Only objects inside the watched zone are candidates.
            if not camera.zone.contains(center):
                continue
            key = (camera_id, str(track_id))
            is_new = key not in self._objects
            track: _ObjectRecord = self._objects.touch(key, at=event_ts)
            if is_new:
                track.label = label
                track.anchor = center
                continue
            assert track.anchor is not None
            # Moved beyond tolerance → it was carried; reset the anchor
            # and the dwell clock, clear any prior alert latch.
            if _distance(center, track.anchor) > self.cfg.move_tolerance_px:
                track.anchor = center
                track.first_seen = event_ts  # settled_since reset
                track.alerted = False
                continue
            dwell = event_ts - track.settled_since
            if dwell < self.cfg.dwell_seconds or track.alerted:
                continue
            # Suppress if a person was near the object recently.
            if self._person_near(camera_id, track.anchor, event_ts):
                continue
            fired.append(self._build_alert(
                camera=camera, label=track.label, track_id=str(track_id),
                dwell_seconds=dwell, anchor=track.anchor, event=event,
            ))
            track.alerted = True
        return fired

    def state_snapshot(self) -> dict[str, Any]:
        """``GET /state`` — the live stationary-object bookkeeping. Dwell
        is measured against wall time (POSIX seconds), the same timeline
        the records' ``settled_since`` rides on (event ``completed_at``
        with a clock fallback)."""
        now = time.time()
        objects: list[dict[str, Any]] = []
        abandoned = 0
        for (camera_id, _track_id), track in self._objects.items():
            if track.alerted:
                abandoned += 1
            objects.append({
                "camera": camera_id,
                "label": track.label,
                # Clamp: out-of-order / future event timestamps must not
                # surface a negative dwell.
                "dwell_s": round(max(0.0, now - track.settled_since), 1),
                "abandoned": track.alerted,
            })
        return {
            "watched_objects": len(self._objects),
            "abandoned": abandoned,
            "objects": objects,
        }

    def _person_near(self, camera_id: str, point: Point, now_ts: float) -> bool:
        cutoff = now_ts - self.cfg.owner_grace_seconds
        for p, ts in self._recent_people.get(camera_id, []):
            if ts >= cutoff and _distance(p, point) <= self.cfg.person_radius_px:
                return True
        return False

    def _gc(self, camera_id: str, now_ts: float) -> None:
        # Forget idle object tracks — scoped to this camera only.
        obj_cutoff = now_ts - self.cfg.track_ttl_seconds
        for key, track in self._objects.items():
            if key[0] == camera_id and track.last_seen < obj_cutoff:
                self._objects.pop(key)
        # Trim the recent-people buffer to the owner-grace window.
        ppl_cutoff = now_ts - self.cfg.owner_grace_seconds
        bucket = self._recent_people.get(camera_id)
        if bucket:
            self._recent_people[camera_id] = [
                (p, ts) for p, ts in bucket if ts >= ppl_cutoff
            ]

    def _build_alert(
        self, *, camera: CameraZone, label: str, track_id: str,
        dwell_seconds: float, anchor: Point, event: dict[str, Any],
    ) -> Alert:
        correlation_id = str(event.get("correlation_id") or "")
        return Alert(
            title=f"Unattended {label} in zone {camera.zone.name!r}",
            description=(
                f"A {label} (track {track_id}) has been stationary in zone "
                f"{camera.zone.name!r} on camera {camera.camera_id} for "
                f"{dwell_seconds:.0f}s with no person nearby."
            ),
            camera_id=camera.camera_id,
            severity="high",
            correlation_id=correlation_id,
            evidence={
                "label": label,
                "track_id": track_id,
                "dwell_seconds": round(dwell_seconds, 1),
                "anchor_px": [round(anchor.x, 1), round(anchor.y, 1)],
                "zone_name": camera.zone.name,
                "adapter": event.get("adapter"),
                "adapter_version": event.get("adapter_version"),
                "model_fingerprint": event.get("model_fingerprint"),
            },
            tags=["abandoned-object", camera.zone.name, label],
        )


# Spec-preferred short name; ``AbandonedObjectDetector`` is the
# historical one the tests (and README snippets) import.
AbandonedObject = AbandonedObjectDetector


# ── CLI ────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point (``[project.scripts]``). The SDK
    runner owns argparse, logging, signals, and the dispatcher."""
    return app(AbandonedObjectDetector, load_config=load_config).run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
