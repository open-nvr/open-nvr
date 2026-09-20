# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Abandoned object — the unattended-item watch, on the ``opennvr-app-sdk``.

A bag on the concourse, a box against a fire door, a trolley left in the
aisle, a case at the platform edge. The question an operator actually
has is never "is there a bag?" — it is **"is anyone with it, how long
has it been alone, and who put it there?"** So this app follows each
item through a life:

    moving ──settles──▶ with owner ──owner walks away──▶ unattended
                            ▲                               │
                            │                        unattended_seconds
                       someone returns                      ▼
                            └───────── reclaimed ◀──── abandoned ──▶ escalated

* **Owner attribution.** When an item settles, the nearest person
  inside ``owner_radius`` is remembered as its owner — their track id,
  and the moment they left. The alert therefore says *who* left it and
  *when they walked off*, which is what turns an alarm into a
  description a guard can act on. No person at all when it settled is
  itself reported ("appeared with nobody near it").
* **Attendance, not presence.** An item is attended while ANY person is
  within ``owner_radius`` of it; ``owner_grace_seconds`` absorbs the
  detector losing that person for a frame. Only once nobody has been
  near it for the grace period does the unattended clock start — the
  single most effective false-alarm filter in this domain, because a
  bag beside its owner is the normal case.
* **Settling.** ``settle_seconds`` of near-stationary tracking before an
  item is a candidate at all, and ``move_tolerance`` (a fraction of the
  frame, so it means the same on any resolution) decides "stationary".
  Anything carried past the camera never enters the list.
* **Reclaimed is a first-class outcome.** If the item moves again, or
  someone comes back for it, the app says so and closes the item. An
  operator looking at the page can tell "still there" from "gone" —
  the thing every unattended-baggage panel leaves you guessing about.
* **Fixtures.** A bin, a planter, a parked pallet looks exactly like an
  abandoned box forever. **Mark as fixture** on the page records that
  spot for the camera and stops it alerting there, so the scene's
  furniture is silenced in one click instead of by lowering a threshold
  everywhere. Permanent fixtures can also be listed in the config.
* **Evidence.** Every alert carries a snapshot, the item's class, the
  track, how long it has been alone, the owner track and when they
  left, the zone and the model fingerprint.

Cameras and zones come from OpenNVR: with no ``cameras:`` listed the
app watches exactly the cameras picked for it in the App Catalog and
reads each camera's zone from the catalog's editor, live. A picked
camera with no zone watches its whole frame and says so.

It rides Tier-0's tracked detections (``consume_tier0: true``, subject
``opennvr.inference.tier0.>``) — no model of its own, no GPU cost, and
one detector feeds every app.

Run::

    python abandoned_object.py --config config.yml
    python abandoned_object.py --config config.yml --once
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import html as _html
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from opennvr_app_sdk import (
    Action,
    Alert,
    AlertType,
    AppManifest,
    Entity,
    Detector,
    Param,
    StateView,
    app,
)
from opennvr_app_sdk.cameras import UNIT_FRAME
from opennvr_app_sdk.config import load_yaml
from opennvr_app_sdk.geometry import Point, Zone, bbox_center, scale_vertices
from opennvr_app_sdk.state import keyed_state

logger = logging.getLogger("abandoned-object")

SEVERITIES: tuple[str, ...] = ("low", "medium", "high", "critical")
OBJECT_SUGGESTIONS = ["backpack", "handbag", "suitcase", "box", "bicycle", "bottle"]

#: The life of one item, in the words an operator would use.
MOVING, WITH_OWNER, UNATTENDED, ABANDONED, ESCALATED, RECLAIMED = (
    "moving", "with-owner", "unattended", "abandoned", "escalated", "reclaimed")

MANIFEST = AppManifest(
    id="abandoned-object",
    name="Abandoned Object",
    version="1.1.0",
    category="perimeter",
    summary=(
        "Follows every item left in a zone: who was with it, how long it has been "
        "alone, and whether anyone came back — alerting when one is unattended past "
        "the threshold, escalating if it stays, and closing itself when it is reclaimed."
    ),
    requires_tasks=["object_detection", "multi_object_tracking"],
    # Lights the first-class Left Items page (app/src/lib/appVerticals.ts).
    provides=["left_items"],
    subscribes="opennvr.inference.>",
    # Tier-0 tracks only person/vehicles/pets by default; the platform widens
    # it to these on the cameras picked for the app, or a stock install sees no bag.
    tier0_labels=["backpack", "handbag", "suitcase"],
    params=[
        Param("object_labels", list, default=["backpack", "handbag", "suitcase"],
              suggestions=OBJECT_SUGGESTIONS,
              description="Classes that can be left behind. Whatever your detector "
                          "emits — COCO gives backpack, handbag and suitcase."),
        Param("person_label", str, default="person",
              description="The class that counts as attendance. Anyone within "
                          "owner_radius of an item means the item is not alone."),
        Param("zones", "geometry.polygon", per_camera=True,
              description="Where items matter, drawn on the camera. Nothing drawn = the "
                          "whole frame, which on a busy concourse will find the furniture too."),
        Param("unattended_seconds", float, default=60.0,
              description="How long an item must be alone before it is abandoned. A "
                          "boarding gate or platform edge wants 45–60s; a seating area "
                          "3 minutes; long-stay parking 10."),
        Param("settle_seconds", float, default=5.0,
              description="How long an item must sit near-still before it is a candidate "
                          "at all. Keeps everything merely carried past the camera out."),
        Param("move_tolerance", float, default=0.02,
              description="How far the item's centre may drift and still count as still, "
                          "as a fraction of the frame width (0.02 = 2%). Beyond it the "
                          "item was carried, and its clock restarts."),
        Param("owner_radius", float, default=0.15,
              description="A person this close to the item (fraction of the frame width) "
                          "means the item is attended. Wider = fewer alerts, later."),
        Param("owner_grace_seconds", float, default=5.0,
              description="How long a person who has gone out of detection still counts "
                          "as being with the item. Absorbs detector gaps in a crowd."),
        Param("active_hours", "time_range",
              description="When alerts fire. Outside it items are still followed and "
                          "counted, quietly. Empty = around the clock."),
        Param("escalate_after_seconds", float, default=300.0,
              description="Still there, still unacknowledged this long after the alert: "
                          "raise a second, higher one. 0 = no escalation."),
        Param("alert_severity", str, default="high", choices=list(SEVERITIES),
              description="Severity of the alert; the escalation is one step higher."),
        Param("alert_cooldown_seconds", float, default=30.0,
              description="Per camera, the least time between two alerts — a pile of bags "
                          "is one alert, not six. Escalations ignore it."),
        Param("min_bbox_height", float, default=0.0,
              description="Ignore items shorter than this fraction of the frame. Drops "
                          "litter and far-away noise."),
        Param("max_bbox_height", float, default=0.0,
              description="Ignore items taller than this fraction of the frame (0 = no "
                          "limit). Stops a parked vehicle being read as a crate."),
        Param("fixture_radius", float, default=0.03,
              description="How close to a marked fixture an item must be to be treated as "
                          "that fixture, as a fraction of the frame width."),
        Param("attach_snapshot", bool, default=True,
              description="Fetch a still from the camera when the alert fires and attach it."),
    ],
    emits=[
        AlertType("abandoned-object", severity="high",
                  description="An item has been alone in the zone past the threshold."),
        AlertType("abandoned-object-escalated", severity="critical",
                  description="Still there, still unacknowledged, after the escalation delay."),
    ],
    state_schema=[
        StateView("unattended_now", "Unattended", kind="metric", path="unattended_now",
                  description="Items alone in a zone right now, alerted or counting down."),
        StateView("abandoned_now", "Abandoned", kind="metric", path="abandoned_now",
                  description="Items that have crossed the threshold and alerted."),
        StateView("alerts_today", "Alerts today", kind="metric", path="today.alerts"),
        StateView("reclaimed_today", "Reclaimed today", kind="metric", path="today.reclaimed",
                  description="Items someone came back for, or that were taken away."),
        StateView("items", "Items now", kind="table", path="items",
                  columns=["camera", "label", "state", "alone_s", "owner", "progress"],
                  description="Every item being followed, its state and how long it has "
                              "been alone."),
        StateView("per_camera", "Per camera", kind="table", path="per_camera",
                  columns=["camera", "zone", "items", "unattended", "alerts_today",
                           "reclaimed_today", "fixtures", "last"],
                  description="Each camera's live items and today's figures."),
        StateView("recent", "Recent", kind="log", path="recent", limit=12),
    ],
    actions=[
        Action(
            "acknowledge", "Acknowledge",
            params=[Param("camera", str, default=""), Param("track", str, default="")],
            description="Somebody is dealing with it: stops the escalation without "
                        "closing the item, so the page still shows it is there.",
        ),
        Action(
            "resolve", "Resolve",
            params=[Param("camera", str, required=True), Param("track", str, required=True)],
            description="Collected, removed or checked and harmless — close the item and "
                        "stop following it.",
        ),
        Action(
            "mark_fixture", "Mark as fixture",
            params=[Param("camera", str, required=True), Param("track", str, required=True)],
            confirm=True,
            description="This is the scene, not an incident — a bin, a planter, a pallet "
                        "that lives here. Remembers the spot and never alerts on it again.",
        ),
        Action(
            "clear_fixtures", "Clear fixtures",
            params=[Param("camera", str, default="")],
            confirm=True,
            description="Forget the spots marked as fixtures (leave the camera blank for "
                        "every camera).",
        ),
    ],
    # Home Assistant entities (HA-114): the counts per site and per camera,
    # and an "acknowledge" button per camera. Values come from /state below.
    entities=[
        Entity("unattended_now", "sensor", "Unattended items", state_path="unattended_now",
               state_class="measurement", icon="mdi:bag-personal-off"),
        Entity("abandoned_now", "sensor", "Abandoned items", state_path="abandoned_now",
               state_class="measurement", icon="mdi:bag-personal-off"),
        Entity("alerts_today", "sensor", "Abandoned-object alerts today",
               state_path="today.alerts", state_class="total_increasing"),
        Entity("unattended", "sensor", "Unattended items", per_camera=True,
               state_path="per_camera[camera={camera}].unattended",
               state_class="measurement", icon="mdi:bag-personal-off"),
        Entity("acknowledge", "button", "Acknowledge abandoned items", per_camera=True,
               action="acknowledge"),
    ],
    has_ui=True,   # GET /ui dashboard, proxied at /api/v1/apps/{id}/ui
)


# ── Config ──────────────────────────────────────────────────────────


@dataclass
class ActiveHours:
    """A daily window in local time; cross-midnight supported."""
    start: _dt.time
    end: _dt.time

    def contains(self, when: _dt.datetime) -> bool:
        t = when.time()
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end

    @classmethod
    def parse(cls, raw: Any) -> "ActiveHours | None":
        if not isinstance(raw, dict):
            return None
        s, e = str(raw.get("start") or "").strip(), str(raw.get("end") or "").strip()
        if not s or not e:
            return None
        try:
            return cls(_dt.time.fromisoformat(s), _dt.time.fromisoformat(e))
        except ValueError as exc:
            raise ValueError(f"active_hours must be HH:MM start/end: {exc}") from None


@dataclass
class CameraWatch:
    """One camera, its zone, and the pixel space the zone was drawn in.
    ``drawn`` is False when the zone is the whole-frame fallback.
    ``fixtures`` are anchors an operator (or the config) has marked as
    part of the scene."""
    camera_id: str
    zone: Zone
    frame_width: int
    frame_height: int
    drawn: bool = True
    fixtures: list[Point] = field(default_factory=list)


@dataclass
class AppConfig:
    nats_url: str
    nats_token: str | None
    subject_pattern: str
    object_labels: list[str]
    cameras: dict[str, CameraWatch]
    webhook_url: str | None
    nats_alerts_url: str | None = None
    nats_alerts_token: str | None = None
    nats_alerts_subject_prefix: str = "opennvr.alerts"
    contract_port: int | None = None
    contract_bind_host: str | None = None
    contract_host: str | None = None
    opennvr_url: str | None = None
    opennvr_token: str | None = None
    # ── the rule (all live-editable) ──
    person_label: str = "person"
    unattended_seconds: float = 60.0
    settle_seconds: float = 5.0
    move_tolerance: float = 0.02
    owner_radius: float = 0.15
    owner_grace_seconds: float = 5.0
    active_hours: ActiveHours | None = None
    escalate_after_seconds: float = 300.0
    alert_severity: str = "high"
    alert_cooldown_seconds: float = 30.0
    min_bbox_height: float = 0.0
    max_bbox_height: float = 0.0
    fixture_radius: float = 0.03
    attach_snapshot: bool = True
    track_ttl_seconds: float = 10.0
    consume_tier0: bool = False
    auto_cameras: bool = False


def _camera_key(raw_key: object, known: dict[str, Any]) -> str | None:
    """Resolve a per-camera config key to a camera id. The catalog's
    editor keys by the numeric core id (``"3"``); the app by the handle
    (``"cam3"``); hand-written config may use either."""
    key = str(raw_key).strip()
    if key in known:
        return key
    if key.isdigit() and f"cam{key}" in known:
        return f"cam{key}"
    return None


def _zone_from_drawn(drawn: Any, cam: CameraWatch) -> Zone | None:
    if not isinstance(drawn, (list, tuple)) or len(drawn) < 3:
        return None
    try:
        return Zone.from_config(name="zone",
                                vertices=scale_vertices(drawn, cam.frame_width,
                                                        cam.frame_height))
    except (TypeError, ValueError, IndexError):
        return None


def _whole_frame(cam_id: str, w: int, h: int) -> CameraWatch:
    return CameraWatch(cam_id, Zone.from_config("whole frame", [[0, 0], [w, 0], [w, h], [0, h]]),
                       w, h, drawn=False)


def _knobs_from(raw: dict[str, Any], base: AppConfig | None = None) -> dict[str, Any]:
    """The live-editable knobs, parsed and validated from a config dict."""
    d = base
    out: dict[str, Any] = {}

    def _get(key, default):
        if key in raw and raw[key] is not None:
            return raw[key]
        return getattr(d, key) if d is not None else default

    sev = str(_get("alert_severity", "high")).strip().lower() or "high"
    if sev not in SEVERITIES:
        raise ValueError(f"config: 'alert_severity' must be one of {', '.join(SEVERITIES)}")
    out["alert_severity"] = sev
    out["person_label"] = str(_get("person_label", "person")).strip().lower() or "person"
    try:
        out["unattended_seconds"] = max(1.0, float(_get("unattended_seconds", 60.0)))
        out["settle_seconds"] = max(0.0, float(_get("settle_seconds", 5.0)))
        out["move_tolerance"] = min(1.0, max(0.001, float(_get("move_tolerance", 0.02))))
        out["owner_radius"] = min(2.0, max(0.0, float(_get("owner_radius", 0.15))))
        out["owner_grace_seconds"] = max(0.0, float(_get("owner_grace_seconds", 5.0)))
        out["escalate_after_seconds"] = max(0.0, float(_get("escalate_after_seconds", 300.0)))
        out["alert_cooldown_seconds"] = max(0.0, float(_get("alert_cooldown_seconds", 30.0)))
        out["min_bbox_height"] = min(1.0, max(0.0, float(_get("min_bbox_height", 0.0))))
        out["max_bbox_height"] = min(1.0, max(0.0, float(_get("max_bbox_height", 0.0))))
        out["fixture_radius"] = min(1.0, max(0.0, float(_get("fixture_radius", 0.03))))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"config: numeric knob malformed: {exc}") from None
    out["attach_snapshot"] = bool(_get("attach_snapshot", True))
    if "active_hours" in raw:
        out["active_hours"] = ActiveHours.parse(raw.get("active_hours"))
    elif d is not None:
        out["active_hours"] = d.active_hours
    else:
        out["active_hours"] = None
    return out


def _fixtures_from(raw: Any, cameras: dict[str, CameraWatch]) -> None:
    """``fixtures:`` — permanent known-static spots, as a mapping of
    camera to points in the same unit space the zones are drawn in."""
    if not isinstance(raw, dict):
        return
    for raw_key, points in raw.items():
        cam_id = _camera_key(raw_key, cameras)
        if cam_id is None or not isinstance(points, (list, tuple)):
            continue
        cam = cameras[cam_id]
        marks: list[Point] = []
        for p in points:
            try:
                if isinstance(p, dict):
                    x, y = float(p["x"]), float(p["y"])
                else:
                    x, y = float(p[0]), float(p[1])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            scaled = scale_vertices([[x, y]], cam.frame_width, cam.frame_height)
            marks.append(Point(float(scaled[0][0]), float(scaled[0][1])))
        cam.fixtures = marks


def load_config(path: str) -> AppConfig:
    """Parse a YAML config file into a typed AppConfig."""
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

    labels_raw = raw.get("object_labels")
    if labels_raw is None:
        object_labels = ["backpack", "handbag", "suitcase"]
    else:
        object_labels = [str(s).lower() for s in labels_raw if str(s).strip()]
        if not object_labels:
            raise ValueError(
                "config: 'object_labels' must not be empty (omit the key for the "
                "default bag classes, or list at least one label)"
            )

    zones_override = raw.get("zones")
    zone_map = zones_override if isinstance(zones_override, dict) else {}

    cameras_raw = raw.get("cameras") or []
    auto_cameras = not cameras_raw
    if auto_cameras and not raw.get("opennvr_url"):
        raise ValueError(
            "config: at least one camera entry is required (or set "
            "opennvr_url and select cameras in the App Catalog)"
        )

    cameras: dict[str, CameraWatch] = {}
    for idx, c in enumerate(cameras_raw):
        try:
            camera_id = str(c["camera_id"])
            frame_width = int(c.get("frame_width", 1920))
            frame_height = int(c.get("frame_height", 1080))
            if frame_width <= 0 or frame_height <= 0:
                raise ValueError(
                    f"frame_width and frame_height must be > 0; got "
                    f"frame_width={frame_width}, frame_height={frame_height}"
                )
            cam = _whole_frame(camera_id, frame_width, frame_height)
            drawn = None
            for raw_key, val in zone_map.items():
                if _camera_key(raw_key, {camera_id: cam}) == camera_id:
                    drawn = val
                    break
            zone = _zone_from_drawn(drawn, cam)
            if zone is None and c.get("zone"):
                zone = Zone.from_config(name=str(c.get("zone_name", f"zone-{idx}")),
                                        vertices=scale_vertices(c["zone"], frame_width,
                                                                frame_height))
            elif zone is not None and c.get("zone_name"):
                zone = Zone(name=str(c["zone_name"]), polygon=zone.polygon)
            if zone is not None:
                cam.zone, cam.drawn = zone, True
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"config: camera entry {idx} malformed: {exc}") from exc
        if cam.camera_id in cameras:
            raise ValueError(f"config: duplicate camera_id {cam.camera_id!r} at entry {idx}")
        cameras[cam.camera_id] = cam

    _fixtures_from(raw.get("fixtures"), cameras)

    nats_alerts_url = str(raw["nats_alerts_url"]).strip() if raw.get("nats_alerts_url") else None
    nats_alerts_token = str(raw["nats_alerts_token"]) if raw.get("nats_alerts_token") else None
    if "nats_alerts_subject_prefix" in raw:
        nats_prefix = str(raw["nats_alerts_subject_prefix"]).strip()
        if not nats_prefix:
            raise ValueError("config: 'nats_alerts_subject_prefix' must not be empty")
    else:
        nats_prefix = "opennvr.alerts"

    # Back-compat: the 1.0 app called these dwell_seconds and the two
    # pixel radii. Same meaning, resolution-independent units now.
    if "unattended_seconds" not in raw and "dwell_seconds" in raw:
        raw["unattended_seconds"] = raw["dwell_seconds"]
    for old, new, span in (("move_tolerance_px", "move_tolerance", 1920.0),
                           ("person_radius_px", "owner_radius", 1920.0)):
        if new not in raw and old in raw:
            try:
                raw[new] = float(raw[old]) / span
            except (TypeError, ValueError):
                pass

    knobs = _knobs_from(raw)
    return AppConfig(
        nats_url=nats_url,
        nats_token=str(raw["nats_token"]) if raw.get("nats_token") else None,
        subject_pattern=subject,
        object_labels=object_labels,
        cameras=cameras,
        webhook_url=str(raw["webhook_url"]) if raw.get("webhook_url") else None,
        nats_alerts_url=nats_alerts_url,
        nats_alerts_token=nats_alerts_token,
        nats_alerts_subject_prefix=nats_prefix,
        contract_port=int(raw["contract_port"]) if raw.get("contract_port") is not None else None,
        contract_bind_host=(str(raw["contract_bind_host"])
                            if raw.get("contract_bind_host") else None),
        contract_host=str(raw["contract_host"]) if raw.get("contract_host") else None,
        opennvr_url=str(raw["opennvr_url"]) if raw.get("opennvr_url") else None,
        opennvr_token=str(raw["opennvr_token"]) if raw.get("opennvr_token") else None,
        track_ttl_seconds=float(raw.get("track_ttl_seconds", 10.0)),
        consume_tier0=bool(raw.get("consume_tier0", False)),
        auto_cameras=auto_cameras,
        **knobs,
    )


# ── Bookkeeping ─────────────────────────────────────────────────────


def _day_blank() -> dict[str, int]:
    return {"items": 0, "alerts": 0, "reclaimed": 0}


def _distance(a: Point, b: Point) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


class AbandonedObjectDetector(Detector):
    """Follows watched items through settle → attended → unattended →
    abandoned → reclaimed, per (camera, track), on the Tier-0 stream."""

    manifest = MANIFEST

    def setup(self) -> None:
        # One record per (camera, track) while the item is being followed.
        # ``data`` carries the item's whole life; see _fresh_item.
        self._items = keyed_state(ttl=self.cfg.track_ttl_seconds, auto_gc=False)
        # (camera) -> deque of (person_center, event_ts, track) recently seen.
        self._people: dict[str, deque[tuple[Point, float, str]]] = {}
        self._warned_missing_track = False
        self._today: dict[str, dict[str, int]] = {}
        self._today_key: str = self._now_local().date().isoformat()
        self._last: dict[str, float] = {}
        self._last_alert: dict[str, float] = {}
        self._recent: deque[dict[str, Any]] = deque(maxlen=50)
        self._started_at = time.time()
        self._nvr: Any = None
        self._nvr_tried = False
        self._unknown_cameras: set[str] = set()
        self._last_config: dict[str, Any] | None = None

    # ── time helpers ──

    def _now_local(self) -> _dt.datetime:
        return _dt.datetime.now()

    def _roll_day(self) -> None:
        key = self._now_local().date().isoformat()
        if key != self._today_key:
            self._today_key = key
            self._today = {}

    def _day(self, camera_id: str) -> dict[str, int]:
        return self._today.setdefault(camera_id, _day_blank())

    def _alerts_live(self) -> bool:
        hours = self.cfg.active_hours
        return True if hours is None else hours.contains(self._now_local())

    def _note(self, camera_id: str, message: str, level: str, now: float,
              **extra: Any) -> None:
        self._recent.append({"message": f"{camera_id}: {message}", "time": now,
                             "level": level, "camera": camera_id, **extra})

    # ── geometry in frame units ──

    def _px(self, cam: CameraWatch, fraction: float) -> float:
        """A fraction of the frame width, in the pixel space the zone
        was drawn in — so every distance knob means the same thing on a
        4K camera and on a D1 one."""
        return fraction * cam.frame_width

    def _is_fixture(self, cam: CameraWatch, point: Point) -> bool:
        r = self._px(cam, self.cfg.fixture_radius)
        return any(_distance(point, f) <= r for f in cam.fixtures)

    # ── the item's life ──

    @staticmethod
    def _fresh_item(label: str, centre: Point, ts: float, wall: float) -> dict[str, Any]:
        return {
            "label": label,
            "anchor": (centre.x, centre.y),
            "state": MOVING,
            "since": ts,             # when it settled at this anchor (event time)
            "wall": wall,            # last sighting on the wall clock
            "owner_track": "",       # the person who was nearest when it settled
            "owner_near_at": 0.0,    # last time ANY person was within owner_radius
            "alone_since": 0.0,      # when attendance lapsed
            "alerted_at": 0.0,       # wall clock of the alert
            "escalated": False,
            "acked": False,
            "fired": False,          # an alert for this item actually went out
            "counted": False,        # counted into today's items
        }

    def tick(self, now: float | None = None) -> list[Alert]:
        """Advance every followed item on the wall clock: unattended
        clocks turning into alerts, escalations, and items nobody has
        seen for ``track_ttl_seconds``. Tier-0 publishes only frames
        that contain detections, so an item alone in an empty scene
        produces no events at all — without this sweep it would sit at
        99% of its threshold forever."""
        now = time.time() if now is None else now
        self._roll_day()
        fired: list[Alert] = []
        cutoff = now - self.cfg.track_ttl_seconds
        for key, rec in list(self._items.items()):
            cam = self.cfg.cameras.get(key[0])
            if cam is None:
                self._items.pop(key)
                continue
            d = rec.data
            # Gone from view: an abandoned item that vanishes was taken
            # (or occluded) — say so rather than dropping it silently.
            if d.get("wall", 0.0) < cutoff:
                self._items.pop(key)
                if d["state"] in (ABANDONED, ESCALATED):
                    self._day(key[0])["reclaimed"] += 1
                    self._note(key[0], f"{d['label']} no longer in view — taken or hidden",
                               "info", now, label=d["label"], track=key[1])
                elif d["state"] == UNATTENDED:
                    self._note(key[0], f"{d['label']} left the zone before the threshold",
                               "info", now, label=d["label"], track=key[1])
                continue
            if d["state"] == UNATTENDED:
                alone = now - self._wall_of(d, d["alone_since"], now)
                if alone >= self.cfg.unattended_seconds:
                    alert = self._raise(cam, key[1], d, "abandoned-object", now, alone)
                    if alert is not None:
                        fired.append(alert)
            elif d["state"] == ABANDONED and not d["acked"]:
                # Only escalate an alert that actually went out: an item
                # merged into another camera alert by the cooldown, or one
                # that crossed the threshold outside alert hours, must not
                # arrive later as a critical with no first alert behind it.
                if (self.cfg.escalate_after_seconds > 0 and not d["escalated"]
                        and d.get("fired")
                        and now - d["alerted_at"] >= self.cfg.escalate_after_seconds):
                    alert = self._raise(cam, key[1], d, "abandoned-object-escalated", now,
                                        now - self._wall_of(d, d["alone_since"], now))
                    if alert is not None:
                        # Latch only on a real escalation: outside alert
                        # hours this retries, and goes out when the hours
                        # come back rather than being silently spent.
                        d["escalated"] = True
                        d["state"] = ESCALATED
                        fired.append(alert)
        return fired

    @staticmethod
    def _wall_of(d: dict[str, Any], event_ts: float, now: float) -> float:
        """Event timestamps and the wall clock are two timelines. An
        item's clocks are set from event time; the sweep runs on the
        wall clock. ``wall_offset`` is captured when the clock starts,
        so the two can be compared without assuming they agree."""
        offset = d.get("wall_offset", 0.0)
        if not event_ts:
            return now
        return event_ts + offset

    # ── the rule ──

    def on_detections(
        self,
        camera_id: str,
        detections: list[dict[str, Any]],
        event: dict[str, Any],
    ) -> list[Alert]:
        cam = self.cfg.cameras.get(camera_id)
        if cam is None:
            if camera_id not in self._unknown_cameras:
                self._unknown_cameras.add(camera_id)
                logger.info("events from %s ignored — not one of this app's cameras (%s)",
                            camera_id, sorted(self.cfg.cameras) or "none")
            return []
        event_ts = self.parse_event_ts(event.get("completed_at"))
        now = time.time()
        fired = self.tick(now)

        # ── who is about, and where the watched items are ──
        people: list[tuple[Point, str]] = []
        items: list[tuple[str, str, Point]] = []   # (track, label, centre)
        for det in detections:
            if not isinstance(det, dict):
                continue
            label = str(det.get("label", "")).lower()
            bbox = det.get("bbox")
            if not isinstance(bbox, dict):
                continue
            centre = bbox_center(bbox, cam.frame_width, cam.frame_height)
            if label == self.cfg.person_label:
                people.append((centre, str(det.get("track_id") or "")))
                continue
            if label not in self.cfg.object_labels:
                continue
            try:
                h = float(bbox.get("h", 0.0))
            except (TypeError, ValueError):
                h = 0.0
            if self.cfg.min_bbox_height > 0 and 0 < h < self.cfg.min_bbox_height:
                continue
            if self.cfg.max_bbox_height > 0 and h > self.cfg.max_bbox_height:
                continue
            if not cam.zone.contains(centre):
                continue
            if self._is_fixture(cam, centre):
                continue
            track_id = det.get("track_id")
            if track_id is None:
                if not self._warned_missing_track:
                    logger.warning(
                        "detections have no 'track_id' — an item cannot be followed "
                        "without one, so nothing will be reported. Consume Tier-0 or "
                        "chain a tracking adapter."
                    )
                    self._warned_missing_track = True
                continue
            items.append((str(track_id), label, centre))

        bucket = self._people.setdefault(camera_id, deque(maxlen=256))
        for centre, track in people:
            bucket.append((centre, event_ts, track))
        grace_cutoff = event_ts - self.cfg.owner_grace_seconds
        while bucket and bucket[0][1] < grace_cutoff:
            bucket.popleft()
        if items or people:
            self._last[camera_id] = now

        # ── follow each item ──
        radius = self._px(cam, self.cfg.owner_radius)
        tolerance = self._px(cam, self.cfg.move_tolerance)
        for track, label, centre in items:
            key = (camera_id, track)
            existing = self._items.get(key)
            if existing is not None and event_ts < existing.last_seen:
                continue          # out-of-order event
            rec = self._items.touch(key, at=event_ts)
            if not rec.data:
                rec.data.update(self._fresh_item(label, centre, event_ts, now))
                rec.data["wall_offset"] = now - event_ts
            d = rec.data
            d["wall"] = now
            d["wall_offset"] = now - event_ts
            anchor = Point(*d["anchor"])

            # Moved: it was carried. An item that had gone unattended and
            # then moves has been reclaimed — by its owner or by staff.
            if _distance(centre, anchor) > tolerance:
                if d["state"] in (UNATTENDED, ABANDONED, ESCALATED):
                    hit = self._nearest(bucket, centre, radius, event_ts)
                    who = hit[0] if hit else None
                    self._day(camera_id)["reclaimed"] += 1
                    self._note(camera_id,
                               f"{label} collected{' by ' + who if who else ''}"
                               f" after {int(now - self._wall_of(d, d['alone_since'], now))}s alone",
                               "info", now, label=label, track=track)
                    d["state"] = RECLAIMED
                    self._items.pop(key)
                    continue
                d["anchor"] = (centre.x, centre.y)
                d["since"] = event_ts
                d["state"] = MOVING
                continue

            # Stationary. Is anyone with it?
            attended = self._nearest(bucket, anchor, radius, event_ts)
            if attended is not None:
                near_track, seen_at = attended
                # The moment they were LAST ACTUALLY SEEN near it — not
                # now. The grace window exists so one dropped frame does
                # not flip the state; it must not also push the
                # "alone since" moment forward and shorten the clock.
                d["owner_near_at"] = max(d["owner_near_at"], seen_at)
                if not d["owner_track"]:
                    d["owner_track"] = near_track
                if d["state"] in (UNATTENDED,):
                    # Someone came back before the threshold.
                    d["state"] = WITH_OWNER
                    d["alone_since"] = 0.0
                    self._note(camera_id, f"{label} attended again", "info", now,
                               label=label, track=track)

            settled_for = event_ts - d["since"]
            if settled_for < self.cfg.settle_seconds:
                continue
            if not d["counted"]:
                d["counted"] = True
                self._day(camera_id)["items"] += 1
            if d["state"] == MOVING:
                d["state"] = WITH_OWNER if attended is not None else UNATTENDED
                if d["state"] == UNATTENDED:
                    d["alone_since"] = event_ts
                    self._note(camera_id,
                               f"{label} settled with nobody near it", "medium", now,
                               label=label, track=track)
            elif d["state"] == WITH_OWNER and attended is None:
                lapsed = event_ts - (d["owner_near_at"] or d["since"])
                if lapsed >= self.cfg.owner_grace_seconds:
                    d["state"] = UNATTENDED
                    d["alone_since"] = d["owner_near_at"] or event_ts
                    self._note(camera_id,
                               f"{label} left alone"
                               f"{' by ' + d['owner_track'] if d['owner_track'] else ''}",
                               "medium", now, label=label, track=track)

            if d["state"] == UNATTENDED:
                alone = now - self._wall_of(d, d["alone_since"], now)
                if alone >= self.cfg.unattended_seconds:
                    alert = self._raise(cam, track, d, "abandoned-object", now, alone)
                    if alert is not None:
                        fired.append(alert)
        return fired

    def _nearest(self, bucket, point: Point, radius: float,
                 now_ts: float) -> tuple[str, float] | None:
        """The closest person within ``radius`` of ``point`` inside the
        grace window, as (track id, when they were seen), or None when
        the item is alone. An unnamed track still counts as
        attendance — somebody is there either way."""
        cutoff = now_ts - self.cfg.owner_grace_seconds
        best: tuple[float, str, float] | None = None
        for centre, ts, track in bucket:
            if ts < cutoff:
                continue
            dist = _distance(centre, point)
            if dist <= radius and (best is None or dist < best[0]):
                best = (dist, track or "?", ts)
        return (best[1], best[2]) if best else None

    def _raise(self, cam: CameraWatch, track: str, d: dict[str, Any], kind: str,
               now: float, alone: float) -> Alert | None:
        """Fire, unless the hours or the per-camera cooldown say not to.
        The item still moves to ABANDONED either way — the page and the
        page's counters must reflect what is actually happening, alert or
        no alert."""
        cam_id = cam.camera_id
        escalation = kind.endswith("escalated")
        if not escalation:
            d["state"] = ABANDONED
            d["alerted_at"] = now
        if not self._alerts_live():
            if not escalation:
                self._note(cam_id, f"{d['label']} unattended {int(alone)}s "
                                   "(outside alert hours)", "info", now)
            return None
        if not escalation and self._cooldown_blocks(cam_id, now):
            self._note(cam_id, f"{d['label']} unattended {int(alone)}s "
                               "(merged into the last alert)", "info", now)
            return None
        self._last_alert[cam_id] = now
        self._day(cam_id)["alerts"] += 1
        d["fired"] = True
        owner = d.get("owner_track") or ""
        if escalation:
            severity = SEVERITIES[min(SEVERITIES.index(self.cfg.alert_severity) + 1,
                                      len(SEVERITIES) - 1)]
            title = f"{d['label'].capitalize()} still unattended at {cam_id}"
            description = (f"The {d['label']} is still in {cam.zone.name!r} on {cam_id}, "
                           f"{int(alone)}s alone and not acknowledged.")
        else:
            severity = self.cfg.alert_severity
            title = f"Unattended {d['label']} at {cam_id}"
            description = (
                f"A {d['label']} has been alone in {cam.zone.name!r} on {cam_id} for "
                f"{int(alone)}s"
                + (f"; the person who left it was track {owner}." if owner
                   else "; nobody was near it when it appeared.")
            )
        self._note(cam_id, title, severity, now, label=d["label"], track=track)
        return Alert(
            title=title,
            description=description,
            camera_id=cam_id,
            severity=severity,
            alert_type=kind,
            evidence={
                "label": d["label"],
                "track_id": track,
                "alone_seconds": round(alone, 1),
                "unattended_seconds": self.cfg.unattended_seconds,
                "owner_track": owner or None,
                "owner_left_seconds_ago": (round(now - self._wall_of(d, d["owner_near_at"], now), 1)
                                           if d.get("owner_near_at") else None),
                "zone_name": cam.zone.name,
                "anchor": [round(d["anchor"][0], 1), round(d["anchor"][1], 1)],
            },
            images=self._evidence(cam_id),
            tags=[kind, cam.zone.name, d["label"]],
        )

    def _cooldown_blocks(self, camera_id: str, now: float) -> bool:
        cd = self.cfg.alert_cooldown_seconds
        last = self._last_alert.get(camera_id, 0.0)
        return cd > 0 and last > 0 and (now - last) < cd

    # ── evidence ──

    def _platform(self):
        if self._nvr is None and not self._nvr_tried:
            self._nvr_tried = True
            try:
                from opennvr_app_sdk.client import OpenNVR
                self._nvr = OpenNVR(self.cfg.opennvr_url or None, timeout=3.0)
            except Exception as exc:
                logger.info("no platform client for snapshots: %s", exc)
        return self._nvr

    def _evidence(self, camera_id: str) -> dict[str, str]:
        if not self.cfg.attach_snapshot:
            return {}
        nvr = self._platform()
        if nvr is None:
            return {}
        try:
            jpeg = nvr.snapshot(camera_id)
            path = nvr.save_evidence(jpeg) if jpeg else None
        except Exception as exc:
            logger.warning("snapshot for %s failed: %s", camera_id, exc)
            return {}
        return {"snapshot": path} if path else {}

    # ── the sweep ──

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                for alert in self.tick():
                    self._dispatcher.fire(alert)
            except Exception:
                logger.warning("unattended sweep failed", exc_info=True)

    async def run(self, *, once: bool = False) -> None:
        tasks: list[asyncio.Task] = []
        if not once:
            tasks.append(asyncio.create_task(self._tick_loop()))
        try:
            await super().run(once=once)
        finally:
            for t in tasks:
                t.cancel()

    # ── camera discovery ──

    def on_cameras_update(self, camera_ids) -> None:
        super().on_cameras_update(camera_ids)
        self.refresh_cameras(camera_ids)

    def refresh_cameras(self, camera_ids) -> tuple[list[str], list[str]]:
        """Re-derive the camera set from the cameras picked for this app."""
        if not self.cfg.auto_cameras:
            return [], []
        ids = {f"cam{int(i)}" for i in camera_ids}
        current = set(self.cfg.cameras)
        added = sorted(ids - current)
        removed = sorted(current - ids)
        for cam_id in added:
            self.cfg.cameras[cam_id] = _whole_frame(cam_id, UNIT_FRAME, UNIT_FRAME)
        for cam_id in removed:
            self.cfg.cameras.pop(cam_id, None)
            self._people.pop(cam_id, None)
            self._unknown_cameras.discard(cam_id)
            for key, _ in [kv for kv in self._items.items() if kv[0][0] == cam_id]:
                self._items.pop(key)
        if added or removed:
            logger.info("camera set refreshed: +%s -%s (now %s)", added or "-", removed or "-",
                        sorted(self.cfg.cameras) or "(none)")
            if added and self._last_config is not None:
                self.on_config_update(self._last_config)
        return added, removed

    # ── live config ──

    def on_config_update(self, config: dict[str, Any]) -> None:
        """Catalog edits, applied live and idempotently."""
        self._last_config = dict(config)
        changed: list[str] = []
        if "object_labels" in config:
            labels = [str(s).lower() for s in (config.get("object_labels") or [])
                      if str(s).strip()]
            if labels and labels != self.cfg.object_labels:
                self.cfg.object_labels = labels
                changed.append("labels")
        try:
            knobs = _knobs_from(config, self.cfg)
        except ValueError as exc:
            logger.warning("config edit ignored: %s", exc)
            knobs = {}
        for key, value in knobs.items():
            if getattr(self.cfg, key) != value:
                setattr(self.cfg, key, value)
                changed.append(key)
        if "zones" in config:
            zones = config.get("zones")
            zones = zones if isinstance(zones, dict) else {}
            for cam_id, cam in self.cfg.cameras.items():
                drawn = None
                for raw_key, val in zones.items():
                    if _camera_key(raw_key, self.cfg.cameras) == cam_id:
                        drawn = val
                        break
                zone = _zone_from_drawn(drawn, cam)
                if zone is None:
                    zone, is_drawn = _whole_frame(cam_id, cam.frame_width,
                                                  cam.frame_height).zone, False
                else:
                    is_drawn = True
                if (is_drawn != cam.drawn
                        or [(p.x, p.y) for p in zone.polygon]
                        != [(p.x, p.y) for p in cam.zone.polygon]):
                    cam.zone, cam.drawn = zone, is_drawn
                    changed.append(f"zone:{cam_id}")
        if "fixtures" in config:
            _fixtures_from(config.get("fixtures"), self.cfg.cameras)
            changed.append("fixtures")
        if changed:
            logger.info("config applied live: %s", ", ".join(changed))

    # ── actions ──

    def _targets(self, raw: Any) -> list[str]:
        cam = str(raw or "").strip()
        if not cam:
            return list(self.cfg.cameras)
        if cam not in self.cfg.cameras:
            raise KeyError(f"unknown camera {cam!r}")
        return [cam]

    def _item(self, params: dict[str, Any]) -> tuple[str, str, Any]:
        cam_id = str(params.get("camera") or "").strip()
        track = str(params.get("track") or "").strip()
        if cam_id not in self.cfg.cameras:
            raise KeyError(f"unknown camera {cam_id!r}")
        rec = self._items.get((cam_id, track))
        if rec is None:
            raise KeyError(f"no item {track!r} on {cam_id!r}")
        return cam_id, track, rec

    def on_action(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        if name == "acknowledge":
            track = str(params.get("track") or "").strip()
            if track:
                cam_id, track, rec = self._item(params)
                rec.data["acked"] = True
                self._note(cam_id, f"{rec.data['label']} acknowledged", "info", now)
                return {"ok": True, "camera": cam_id, "track": track}
            targets = self._targets(params.get("camera"))
            touched = 0
            for key, rec in self._items.items():
                if key[0] in targets and rec.data.get("state") in (ABANDONED, ESCALATED):
                    rec.data["acked"] = True
                    touched += 1
            for cam_id in targets:
                self._note(cam_id, "alerts acknowledged", "info", now)
            return {"ok": True, "cameras": targets, "items": touched}
        if name == "resolve":
            cam_id, track, rec = self._item(params)
            label = rec.data.get("label", "item")
            self._items.pop((cam_id, track))
            self._day(cam_id)["reclaimed"] += 1
            self._note(cam_id, f"{label} resolved by operator", "info", now)
            return {"ok": True, "camera": cam_id, "track": track}
        if name == "mark_fixture":
            cam_id, track, rec = self._item(params)
            cam = self.cfg.cameras[cam_id]
            anchor = Point(*rec.data["anchor"])
            if not self._is_fixture(cam, anchor):
                cam.fixtures.append(anchor)
            self._items.pop((cam_id, track))
            self._note(cam_id, f"{rec.data.get('label', 'item')} marked as part of the scene",
                       "info", now)
            return {"ok": True, "camera": cam_id, "fixtures": len(cam.fixtures),
                    "at": [round(anchor.x, 1), round(anchor.y, 1)]}
        if name == "clear_fixtures":
            targets = self._targets(params.get("camera"))
            for cam_id in targets:
                self.cfg.cameras[cam_id].fixtures = []
                self._note(cam_id, "fixtures cleared", "info", now)
            return {"ok": True, "cameras": targets}
        raise KeyError(name)

    # ── surfaces ──

    def state_snapshot(self) -> dict[str, Any]:
        # /state advances the machine like the sweep does, so a poll can
        # be the call that crosses a threshold. Whatever that produces
        # has to go out here: the item is left in its new state, so no
        # later tick would fire it again, and the alert would be lost.
        for alert in self.tick():
            try:
                self._dispatcher.fire(alert)
            except Exception:
                logger.warning("alert from a /state poll failed to dispatch", exc_info=True)
        now = time.time()
        items: list[dict[str, Any]] = []
        per_cam_items: dict[str, int] = {}
        per_cam_unattended: dict[str, int] = {}
        longest = 0.0
        for (cam_id, track), rec in self._items.items():
            d = rec.data
            if not d:
                continue
            per_cam_items[cam_id] = per_cam_items.get(cam_id, 0) + 1
            alone = (now - self._wall_of(d, d["alone_since"], now)) if d["alone_since"] else 0.0
            if d["state"] in (UNATTENDED, ABANDONED, ESCALATED):
                per_cam_unattended[cam_id] = per_cam_unattended.get(cam_id, 0) + 1
                longest = max(longest, alone)
            items.append({
                "camera": cam_id,
                "track": track,
                "label": d["label"],
                "state": d["state"],
                "alone_s": round(alone, 1),
                "settled_s": round(now - self._wall_of(d, d["since"], now), 1),
                "owner": d.get("owner_track") or None,
                "owner_left_s": (round(now - self._wall_of(d, d["owner_near_at"], now), 1)
                                 if d.get("owner_near_at") else None),
                "acked": bool(d.get("acked")),
                "progress": round(min(1.0, alone / self.cfg.unattended_seconds), 3),
            })
        items.sort(key=lambda r: -r["alone_s"])
        per_camera = []
        for cam_id, cam in self.cfg.cameras.items():
            day = self._today.get(cam_id, _day_blank())
            per_camera.append({
                "camera": cam_id,
                "zone": cam.zone.name if cam.drawn else "— whole frame",
                "drawn": cam.drawn,
                "items": per_cam_items.get(cam_id, 0),
                "unattended": per_cam_unattended.get(cam_id, 0),
                "items_today": day["items"],
                "alerts_today": day["alerts"],
                "reclaimed_today": day["reclaimed"],
                "fixtures": len(cam.fixtures),
                "last": self._last.get(cam_id),
            })
        return {
            "unattended_now": sum(per_cam_unattended.values()),
            "abandoned_now": sum(1 for r in items if r["state"] in (ABANDONED, ESCALATED)),
            "attended_now": sum(1 for r in items if r["state"] == WITH_OWNER),
            "camera_count": len(per_camera),
            "longest_alone_s": round(longest, 1),
            "today": {
                "items": sum(d["items"] for d in self._today.values()),
                "alerts": sum(d["alerts"] for d in self._today.values()),
                "reclaimed": sum(d["reclaimed"] for d in self._today.values()),
                "since": self._today_key,
            },
            "unattended_seconds": self.cfg.unattended_seconds,
            "settle_seconds": self.cfg.settle_seconds,
            "owner_grace_seconds": self.cfg.owner_grace_seconds,
            "escalate_after_seconds": self.cfg.escalate_after_seconds,
            "alerts_active_now": self._alerts_live(),
            "active_hours": ({"start": self.cfg.active_hours.start.strftime("%H:%M"),
                              "end": self.cfg.active_hours.end.strftime("%H:%M")}
                             if self.cfg.active_hours else None),
            "fixtures": sum(len(c.fixtures) for c in self.cfg.cameras.values()),
            "per_camera": per_camera,
            "items": items,
            "needs_zone": [c for c, cam in self.cfg.cameras.items() if not cam.drawn],
            "recent": list(self._recent),
            "since": self._started_at,
        }

    def ui_html(self) -> str:
        """One static HTML page, no scripts: what is on the floor now,
        how long it has been alone, who left it, and today's figures."""
        snap = self.state_snapshot()
        esc = _html.escape
        now = time.time()

        def ago(ts):
            if not ts:
                return "—"
            m = max(0, int((now - ts) / 60))
            return "just now" if m == 0 else f"{m}m ago" if m < 60 else f"{m // 60}h ago"

        colour = {WITH_OWNER: "#46a758", MOVING: "#8b8d98", UNATTENDED: "#e5a000",
                  ABANDONED: "#e5484d", ESCALATED: "#c62a2f"}
        rows = "".join(
            f"<tr><td>{esc(i['camera'])}</td><td>{esc(i['label'])}</td>"
            f"<td><span class='pill' style='background:{colour.get(i['state'], '#8b8d98')}'>"
            f"{esc(i['state'])}</span></td><td>{int(i['alone_s'])}s</td>"
            f"<td>{esc(str(i['owner'] or '—'))}</td></tr>"
            for i in snap["items"]
        )
        table = ("<table><tr><th>Camera</th><th>Item</th><th>State</th><th>Alone</th>"
                 "<th>Left by</th></tr>" + rows + "</table>") if rows else (
            "<p class='dim'>Nothing on the floor.</p>")
        cards = "".join(
            f"<section class='card'><h2>{esc(r['camera'])}</h2>"
            f"<div class='dim small'>{esc(r['zone'])}"
            f"{' · ' + str(r['fixtures']) + ' fixtures' if r['fixtures'] else ''}</div>"
            f"<div class='stats'><div><b>{r['items']}</b><span class='dim'>items</span></div>"
            f"<div><b>{r['unattended']}</b><span class='dim'>unattended</span></div>"
            f"<div><b>{r['alerts_today']}</b><span class='dim'>alerts today</span></div>"
            f"<div><b>{r['reclaimed_today']}</b><span class='dim'>reclaimed</span></div></div>"
            f"<div class='dim small'>last seen {ago(r['last'])}</div></section>"
            for r in snap["per_camera"]
        )
        recent_rows = "".join(
            f"<tr><td style='color:{'#e5484d' if r.get('level') in ('high', 'critical') else '#1a1a1a'}'>"
            f"{esc(str(r.get('message', '')))}</td><td>{ago(r.get('time'))}</td></tr>"
            for r in reversed(snap["recent"][-12:])
        )
        recent = ("<table><tr><th>Event</th><th>When</th></tr>" + recent_rows + "</table>"
                  ) if recent_rows else "<p class='dim'>Nothing yet.</p>"
        hours = snap["active_hours"]
        policy = (f"alerts {hours['start']}–{hours['end']}" if hours else "alerts around the clock")
        return f"""<title>Abandoned Object</title>
<style>
 body {{ font: 14px system-ui, sans-serif; margin: 1.2rem; color: #1a1a1a; background: #fafafa; }}
 h1 {{ font-size: 1.1rem; margin: 0 0 .2rem }} h2 {{ font-size: .95rem; margin: 0 0 .4rem }}
 .dim {{ color: #6b6f76; font-weight: 400 }} .small {{ font-size: .8rem }}
 .pill {{ color: #fff; border-radius: 10px; padding: 1px 8px; font-size: .75rem }}
 .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: .8rem; margin: .8rem 0 }}
 .card {{ background: #fff; border: 1px solid #e0e0e0; border-radius: 6px; padding: .7rem .9rem }}
 .stats {{ display: flex; gap: 1rem; margin: .4rem 0; flex-wrap: wrap }}
 .stats b {{ font-size: 1.3rem; display: block }}
 table {{ border-collapse: collapse; width: 100% }}
 th, td {{ text-align: left; padding: .3rem .6rem; border-bottom: 1px solid #e0e0e0; font-size: .9rem }}
 th {{ color: #6b6f76; font-weight: 500 }}
</style>
<h1>Abandoned Object</h1>
<div class="dim"><b>{snap['unattended_now']}</b> unattended ·
 <b>{snap['abandoned_now']}</b> past the {int(snap['unattended_seconds'])}s threshold ·
 today <b>{snap['today']['items']}</b> items, <b>{snap['today']['alerts']}</b> alerts,
 <b>{snap['today']['reclaimed']}</b> reclaimed · {esc(policy)}</div>
<h2 style="margin-top:.9rem">On the floor now</h2>
{table}
<div class="grid">{cards or "<p class='dim'>No cameras selected.</p>"}</div>
<h2>Recent</h2>
{recent}
"""


# Spec-preferred short name; ``AbandonedObjectDetector`` is the
# historical one the tests (and README snippets) import.
AbandonedObject = AbandonedObjectDetector


# ── CLI ─────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point (``[project.scripts]``). The SDK
    runner owns argparse, logging, signals, and the dispatcher."""
    return app(AbandonedObjectDetector, load_config=load_config).run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
