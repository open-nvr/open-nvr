# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Loitering detection on the ``opennvr-app-sdk``.

Alerts when a *tracked* person or vehicle stays inside an operator-drawn
zone longer than a dwell threshold — the ATM vestibule after hours, the
fire exit, the loading bay, the forecourt, the stairwell. The rule every
product in this segment ships, done the way the good ones do it:

* **Per track, not per label.** A stay belongs to one tracked object.
  Two people taking turns at a door are two stays; one person leaving
  and coming back after ``grace_period_seconds`` is a new stay. The
  stock stack's Tier-0 detector already tracks (``consume_tier0: true``
  and subscribe to ``opennvr.inference.tier0.>`` — what the compose
  config does). Without a ``track_id`` the app degrades to one stay per
  (camera, label) with a one-time warning, so it still works upstream
  of a plain detector.
* **Staged alerts.** ``threshold_seconds`` raises the loitering alert;
  ``escalate_after_seconds`` later, if they are still there, raises a
  second, higher-severity one — "notify, then escalate", which is what a
  monitoring desk actually wants. A stay alerts once per stage.
* **Time of day reframes everything.** ``active_hours`` is when alerts
  fire; outside it the app either stays quiet or, with
  ``after_hours_threshold_seconds`` set, alerts sooner (a person at a
  back door at 02:00 is a stronger signal than at 14:00). Stays are
  counted all day either way.
* **Gatherings.** ``group_size`` raises a ``gathering`` alert when that
  many watched objects dwell in the same zone together for
  ``group_seconds`` — the crowd at the shutter, not the single smoker.
* **Noise controls.** ``grace_period_seconds`` absorbs detector gaps
  inside one stay; ``min_bbox_height`` ignores objects too small to be
  what you watch; ``alert_cooldown_seconds`` turns a burst into one
  alert per camera; an operator can *dismiss* a current dweller (a
  known contractor) from the Loitering page so their stay raises
  nothing more.
* **Evidence and history.** Alerts carry a snapshot from the camera.
  Finished stays are published as dwell on the platform's footfall
  history (``occupancy.footfall.v1``, dwell fields only), so the
  Loitering page can chart last week without this app remembering it.

Cameras come from OpenNVR, not from YAML: with no ``cameras:`` listed the
app watches exactly the cameras picked for it in the App Catalog
(Configure → Cameras), follows pick changes live, and reads each camera's
zone from the catalog's per-camera zone editor — no restart. A picked
camera with no zone drawn watches its whole frame and says so on the
dashboard.

Run::

    python loitering_detection.py --config config.yml
    python loitering_detection.py --config config.yml --once
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import html as _html
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from opennvr_app_sdk import (
    Action,
    Alert,
    AlertType,
    AppManifest,
    Detector,
    Param,
    StateView,
    app,
)
from opennvr_app_sdk.cameras import UNIT_FRAME
from opennvr_app_sdk.config import load_yaml
from opennvr_app_sdk.domain_events import DomainEventPublisher
from opennvr_app_sdk.geometry import Zone, bbox_center, scale_vertices
from opennvr_app_sdk.state import keyed_state

logger = logging.getLogger("loitering-detection")

FOOTFALL_SCHEMA = "occupancy.footfall.v1"
SEVERITIES: tuple[str, ...] = ("low", "medium", "high", "critical")
LABEL_SUGGESTIONS = ["person", "car", "truck", "motorcycle", "bicycle"]
#: Dwell-length buckets for the day's histogram, in seconds (upper bounds).
HISTOGRAM_EDGES: tuple[tuple[str, float], ...] = (
    ("<30s", 30), ("30s–1m", 60), ("1–2m", 120), ("2–5m", 300), ("5m+", float("inf")),
)

MANIFEST = AppManifest(
    id="loitering-detection",
    name="Loitering Detection",
    version="1.1.0",
    category="perimeter",
    summary=(
        "Alerts when a tracked person or vehicle stays in a drawn zone beyond a "
        "dwell threshold, escalates if they remain, alerts sooner after hours, "
        "flags gatherings, and keeps dwell history per camera."
    ),
    requires_tasks=["object_detection", "multi_object_tracking"],
    # Lights the first-class Loitering page (app/src/lib/appVerticals.ts).
    provides=["loitering"],
    subscribes="opennvr.inference.>",
    params=[
        Param("watch_labels", list, default=["person"], suggestions=LABEL_SUGGESTIONS,
              description="Object classes whose dwell is measured."),
        Param("zones", "geometry.polygon", per_camera=True,
              description="The zone, drawn on the camera. Nothing drawn = the whole frame."),
        Param("threshold_seconds", float, default=60.0,
              description="Dwell inside the zone that counts as loitering. Forecourts and fire "
                          "exits: 30–60 s. Lobbies and waiting areas: 2–5 min."),
        Param("escalate_after_seconds", float, default=0.0,
              description="If still there this long after the first alert, raise a second, "
                          "higher-severity one. 0 = no escalation."),
        Param("alert_severity", str, default="medium", choices=list(SEVERITIES),
              description="Severity of the first alert; the escalation is one step higher."),
        Param("active_hours", "time_range",
              description="Alerts fire inside this daily window (cross-midnight allowed). "
                          "Empty = always. Stays are counted all day regardless."),
        Param("after_hours_threshold_seconds", float, default=0.0,
              description="Outside active_hours: 0 = stay quiet; otherwise alert after THIS "
                          "many seconds instead (usually shorter — after hours is more suspicious)."),
        Param("group_size", int, default=0,
              description="Raise a 'gathering' alert when this many watched objects dwell in the "
                          "zone together. 0 = off."),
        Param("group_seconds", float, default=30.0,
              description="How long the group must be together before the gathering alert."),
        Param("alert_cooldown_seconds", float, default=0.0,
              description="Per camera, the least time between two first-stage alerts. "
                          "Escalations are not held back."),
        Param("grace_period_seconds", float, default=5.0,
              description="A detection gap shorter than this does not end a stay (occlusion, "
                          "a missed frame). Longer, and the next sighting is a new stay."),
        Param("min_bbox_height", float, default=0.0,
              description="Ignore objects whose box is shorter than this fraction of the frame "
                          "(0.1 = a tenth). Filters far traffic and birds."),
        Param("daily_reset_hour", int, default=0,
              description="Local hour at which 'today' starts over (0 = midnight, 6 = 06:00)."),
        Param("attach_snapshot", bool, default=True,
              description="Fetch a still from the camera when an alert fires and attach it."),
        Param("publish_dwell", bool, default=True,
              description="Publish finished stays as dwell history so the Loitering page can "
                          "chart past days."),
        Param("dwell_period_seconds", int, default=60),
    ],
    emits=[
        AlertType("loitering", severity="medium",
                  description="A watched object stayed in the zone beyond the threshold."),
        AlertType("loitering-escalated", severity="high",
                  description="Still there after the escalation delay."),
        AlertType("gathering", severity="high",
                  description="group_size or more watched objects dwelling together."),
    ],
    state_schema=[
        StateView("dwelling_now", "Dwelling now", kind="metric", path="dwelling_now",
                  description="Tracked objects currently inside a zone."),
        StateView("stays_today", "Stays today", kind="metric", path="today.stays"),
        StateView("alerts_today", "Alerts today", kind="metric", path="today.alerts"),
        StateView("longest_today", "Longest stay today (s)", kind="metric", path="today.longest_s"),
        StateView("per_camera", "Per camera", kind="table", path="per_camera",
                  columns=["camera", "zone", "dwelling", "stays_today", "alerts_today",
                           "longest_s", "last"],
                  description="Each watched camera's zone and today's dwell figures."),
        StateView("dwelling", "Dwelling now", kind="table", path="dwelling",
                  columns=["camera", "label", "track", "dwell_s", "stage"],
                  description="Every object inside a zone right now, with its accrued dwell "
                              "and alert stage (watching / alerted / escalated / dismissed)."),
        StateView("recent", "Recent stays", kind="log", path="recent", limit=12),
    ],
    actions=[
        Action(
            "dismiss", "Dismiss a dweller",
            params=[
                Param("camera", str, required=True),
                Param("track", str, required=True,
                      description="The track id shown on the Loitering page."),
            ],
            description="Mark the current stay as known (a contractor, staff) so it raises "
                        "no further alert. A new stay by the same object starts fresh.",
        ),
        Action(
            "reset_today", "Reset today's figures", params=[], confirm=True,
            description="Zero today's stays, alerts and longest-stay counters on every camera.",
        ),
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
    """One camera + its zone + the pixel space the zone was drawn in.
    ``drawn`` is False when the zone is the whole-frame fallback."""
    camera_id: str
    zone: Zone
    frame_width: int
    frame_height: int
    drawn: bool = True


@dataclass
class AppConfig:
    nats_url: str
    nats_token: str | None
    subject_pattern: str
    watch_labels: list[str]
    threshold_seconds: float
    grace_period_seconds: float
    cameras: dict[str, CameraWatch]  # keyed by camera_id
    webhook_url: str | None
    nats_alerts_url: str | None = None
    nats_alerts_token: str | None = None
    nats_alerts_subject_prefix: str = "opennvr.alerts"
    contract_port: int | None = None
    contract_bind_host: str | None = None
    contract_host: str | None = None
    opennvr_url: str | None = None
    opennvr_token: str | None = None
    # ── alerting knobs (all live-editable) ──
    escalate_after_seconds: float = 0.0
    alert_severity: str = "medium"
    active_hours: ActiveHours | None = None
    after_hours_threshold_seconds: float = 0.0
    group_size: int = 0
    group_seconds: float = 30.0
    alert_cooldown_seconds: float = 0.0
    min_bbox_height: float = 0.0
    daily_reset_hour: int = 0
    attach_snapshot: bool = True
    publish_dwell: bool = True
    dwell_period_seconds: int = 60
    # Tier-0 publishes tracked detections on ``opennvr.inference.tier0.>``
    # — the SDK bridges them into on_detections when this is set.
    consume_tier0: bool = False
    # ── discovery ──
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
    """The catalog's polygon for this camera (normalised 0–1 vertices,
    scaled into the camera's pixel space), or None when nothing usable
    is drawn."""
    if not isinstance(drawn, (list, tuple)) or len(drawn) < 3:
        return None
    try:
        return Zone.from_config(name="zone",
                                vertices=scale_vertices(drawn, cam.frame_width, cam.frame_height))
    except (TypeError, ValueError, IndexError):
        return None


def _whole_frame(cam_id: str, w: int, h: int) -> CameraWatch:
    return CameraWatch(cam_id, Zone.from_config("whole frame", [[0, 0], [w, 0], [w, h], [0, h]]),
                       w, h, drawn=False)


def _knobs_from(raw: dict[str, Any], base: AppConfig | None = None) -> dict[str, Any]:
    """The live-editable knobs, parsed and validated from a config dict.
    Keys absent from ``raw`` keep ``base``'s value (or the default)."""
    d = base
    out: dict[str, Any] = {}

    def _get(key, default):
        if key in raw and raw[key] is not None:
            return raw[key]
        return getattr(d, key) if d is not None else default

    sev = str(_get("alert_severity", "medium")).strip().lower() or "medium"
    if sev not in SEVERITIES:
        raise ValueError(f"config: 'alert_severity' must be one of {', '.join(SEVERITIES)}")
    out["alert_severity"] = sev
    try:
        out["escalate_after_seconds"] = max(0.0, float(_get("escalate_after_seconds", 0.0)))
        out["after_hours_threshold_seconds"] = max(
            0.0, float(_get("after_hours_threshold_seconds", 0.0)))
        out["group_size"] = max(0, int(_get("group_size", 0)))
        out["group_seconds"] = max(0.0, float(_get("group_seconds", 30.0)))
        out["alert_cooldown_seconds"] = max(0.0, float(_get("alert_cooldown_seconds", 0.0)))
        out["min_bbox_height"] = min(1.0, max(0.0, float(_get("min_bbox_height", 0.0))))
        out["daily_reset_hour"] = min(23, max(0, int(_get("daily_reset_hour", 0))))
        out["dwell_period_seconds"] = max(10, int(_get("dwell_period_seconds", 60)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"config: numeric knob malformed: {exc}") from None
    out["attach_snapshot"] = bool(_get("attach_snapshot", True))
    out["publish_dwell"] = bool(_get("publish_dwell", True))
    if "active_hours" in raw:
        out["active_hours"] = ActiveHours.parse(raw.get("active_hours"))
    elif d is not None:
        out["active_hours"] = d.active_hours
    else:
        out["active_hours"] = None
    return out


def _positive_float(raw: dict[str, Any], key: str, default: float) -> float:
    try:
        value = float(raw.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"config: '{key}' must be a number") from exc
    if value <= 0:
        raise ValueError(f"config: '{key}' must be > 0")
    return value


def load_config(path: str) -> AppConfig:
    """Parse a YAML config file into a typed AppConfig. Raises
    ``ValueError`` on malformed config."""
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

    threshold = _positive_float(raw, "threshold_seconds", 60.0)
    grace = _positive_float(raw, "grace_period_seconds", 5.0)

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

    nats_alerts_url = str(raw["nats_alerts_url"]).strip() if raw.get("nats_alerts_url") else None
    nats_alerts_token = str(raw["nats_alerts_token"]) if raw.get("nats_alerts_token") else None
    if "nats_alerts_subject_prefix" in raw:
        nats_prefix = str(raw["nats_alerts_subject_prefix"]).strip()
        if not nats_prefix:
            raise ValueError("config: 'nats_alerts_subject_prefix' must not be empty")
    else:
        nats_prefix = "opennvr.alerts"

    knobs = _knobs_from(raw)
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
        contract_port=int(raw["contract_port"]) if raw.get("contract_port") is not None else None,
        contract_bind_host=str(raw["contract_bind_host"]) if raw.get("contract_bind_host") else None,
        contract_host=str(raw["contract_host"]) if raw.get("contract_host") else None,
        opennvr_url=str(raw["opennvr_url"]) if raw.get("opennvr_url") else None,
        opennvr_token=str(raw["opennvr_token"]) if raw.get("opennvr_token") else None,
        consume_tier0=bool(raw.get("consume_tier0", False)),
        auto_cameras=auto_cameras,
        **knobs,
    )


# ── The detector ────────────────────────────────────────────────────


STAGE_WATCHING, STAGE_ALERTED, STAGE_ESCALATED = 0, 1, 2
STAGE_NAMES = {STAGE_WATCHING: "watching", STAGE_ALERTED: "alerted", STAGE_ESCALATED: "escalated"}


def _day_blank() -> dict[str, Any]:
    return {"stays": 0, "alerts": 0, "longest_s": 0.0, "dwell_s": 0.0,
            "histogram": [0] * len(HISTOGRAM_EDGES)}


def _hour_blank() -> dict[str, int]:
    return {"stays": 0, "alerts": 0}


class LoiteringDetector(Detector):
    """Consumes tracked inference events, keeps one stay per
    (camera, track) while the object is inside the zone, and raises
    staged alerts by the configured policy."""

    manifest = MANIFEST

    def setup(self) -> None:
        # One record per (camera, track): first_seen = when the stay began,
        # last_seen = last in-zone sighting. Records outlive a detection
        # gap up to grace_period_seconds; longer, and the stay is over.
        self._stays = keyed_state(ttl=self.cfg.grace_period_seconds, auto_gc=False)
        self._warned_missing_track = False
        self._today: dict[str, dict[str, Any]] = {}
        self._today_key: str = self._day_key(self._now_local())
        self._hourly: dict[str, dict[int, dict[str, int]]] = {}
        self._totals: dict[str, int] = {}          # stays since start, per camera
        self._last: dict[str, float] = {}          # last in-zone sighting per camera
        self._last_alert_at: dict[str, float] = {}
        self._group_alerted: set[str] = set()      # cameras whose current gathering alerted
        self._dwell_delta: dict[str, dict[str, float]] = {}
        self._dwell_published = 0
        self._recent: deque[dict[str, Any]] = deque(maxlen=50)
        self._started_at = time.time()
        self._publisher: DomainEventPublisher | None = None
        self._nvr: Any = None
        self._nvr_tried = False
        self._unknown_cameras: set[str] = set()
        self._last_config: dict[str, Any] | None = None

    # ── time helpers ──

    def _now_local(self) -> _dt.datetime:
        return _dt.datetime.now()

    def _day_key(self, now: _dt.datetime) -> str:
        shifted = now - _dt.timedelta(hours=self.cfg.daily_reset_hour)
        return shifted.date().isoformat()

    def _roll_day(self) -> None:
        key = self._day_key(self._now_local())
        if key != self._today_key:
            self._today_key = key
            self._today = {}

    def _alerts_enabled_now(self) -> tuple[bool, float]:
        """(alerts fire now?, the threshold that applies now)."""
        hours = self.cfg.active_hours
        if hours is None or hours.contains(self._now_local()):
            return True, self.cfg.threshold_seconds
        if self.cfg.after_hours_threshold_seconds > 0:
            return True, self.cfg.after_hours_threshold_seconds
        return False, self.cfg.threshold_seconds

    # ── the rule ──

    def on_detections(
        self,
        camera_id: str,
        detections: list[dict[str, Any]],
        event: dict[str, Any],
    ) -> list[Alert]:
        camera = self.cfg.cameras.get(camera_id)
        if camera is None:
            if camera_id not in self._unknown_cameras:
                self._unknown_cameras.add(camera_id)
                logger.info("events from %s ignored — not one of this app's cameras (%s)",
                            camera_id, sorted(self.cfg.cameras) or "none")
            return []
        event_ts = self.parse_event_ts(event.get("completed_at"))
        self._roll_day()

        # Who is inside the zone in this frame, by track.
        inside: dict[str, str] = {}   # track -> label
        for det in detections:
            if not isinstance(det, dict):
                continue
            label = str(det.get("label", "")).lower()
            if label not in self.cfg.watch_labels:
                continue
            bbox = det.get("bbox")
            if not isinstance(bbox, dict):
                continue
            try:
                h = float(bbox.get("h", 0.0))
            except (TypeError, ValueError):
                h = 0.0
            if self.cfg.min_bbox_height > 0 and 0 < h < self.cfg.min_bbox_height:
                continue
            if not camera.zone.contains(bbox_center(bbox, camera.frame_width, camera.frame_height)):
                continue
            track_id = det.get("track_id")
            if track_id is None:
                if not self._warned_missing_track:
                    logger.warning(
                        "detections have no 'track_id' — dwell is measured per (camera, label) "
                        "instead of per object. Consume Tier-0 or chain a tracking adapter for "
                        "per-person stays."
                    )
                    self._warned_missing_track = True
                track_id = f"label:{label}"
            inside[str(track_id)] = label

        # End the stays whose object has been gone longer than the grace
        # period (only this camera's, never one seen in this frame).
        self._finish_absent(camera_id, set(inside), event_ts)

        fired: list[Alert] = []
        if inside:
            self._last[camera_id] = time.time()
        enabled, threshold = self._alerts_enabled_now()
        for track, label in inside.items():
            key = (camera_id, track)
            existing = self._stays.get(key)
            if existing is not None and event_ts < existing.last_seen:
                continue   # out-of-order event; dwell math needs monotonic time
            state = self._stays.touch(key, at=event_ts)
            state.data.setdefault("label", label)
            state.data.setdefault("stage", STAGE_WATCHING)
            state.data.setdefault("dismissed", False)
            dwell = state.age
            if not enabled or state.data["dismissed"]:
                continue
            stage = state.data["stage"]
            if stage == STAGE_WATCHING and dwell >= threshold:
                if not self._in_cooldown(camera_id):
                    state.data["stage"] = STAGE_ALERTED
                    state.data["alerted_at"] = event_ts
                    fired.append(self._raise(camera, "loitering", label=label, track=track,
                                             dwell=dwell, threshold=threshold, event=event))
            elif (stage == STAGE_ALERTED and self.cfg.escalate_after_seconds > 0
                  and event_ts - state.data.get("alerted_at", event_ts)
                  >= self.cfg.escalate_after_seconds):
                state.data["stage"] = STAGE_ESCALATED
                fired.append(self._raise(camera, "loitering-escalated", label=label, track=track,
                                         dwell=dwell, threshold=threshold, event=event))

        # Gatherings: N objects dwelling together for group_seconds.
        if enabled and self.cfg.group_size > 0:
            together = [t for t in inside
                        if (rec := self._stays.get((camera_id, t))) is not None
                        and rec.age >= self.cfg.group_seconds]
            if len(together) >= self.cfg.group_size:
                if camera_id not in self._group_alerted:
                    self._group_alerted.add(camera_id)
                    fired.append(self._raise(camera, "gathering", label="group", track=",".join(together),
                                             dwell=max(self._stays.get((camera_id, t)).age for t in together),
                                             threshold=self.cfg.group_seconds, event=event,
                                             count=len(together)))
            else:
                self._group_alerted.discard(camera_id)
        return fired

    def _in_cooldown(self, camera_id: str) -> bool:
        cooldown = self.cfg.alert_cooldown_seconds
        last = self._last_alert_at.get(camera_id)
        return cooldown > 0 and last is not None and (time.time() - last) < cooldown

    def _finish_absent(self, camera_id: str, present: set[str], now_ts: float) -> None:
        cutoff = now_ts - self.cfg.grace_period_seconds
        for key, state in list(self._stays.items()):
            if key[0] != camera_id or key[1] in present or state.last_seen >= cutoff:
                continue
            self._stays.pop(key)
            self._record_stay(camera_id, key[1], state.data.get("label", "?"), state.age,
                              state.data.get("stage", STAGE_WATCHING))

    def finish_stale(self, now: float | None = None) -> int:
        """Wall-clock sweep: end every stay whose object has not been seen
        for longer than the grace period. Tier-0 publishes only frames
        with detections, so a camera that empties goes silent — without
        this the last dweller would stay on the page until the next
        event arrived. Runs from the sweep loop; returns how many ended."""
        now = time.time() if now is None else now
        cutoff = now - self.cfg.grace_period_seconds
        ended = 0
        for key, state in list(self._stays.items()):
            if state.last_seen >= cutoff:
                continue
            self._stays.pop(key)
            self._record_stay(key[0], key[1], state.data.get("label", "?"), state.age,
                              state.data.get("stage", STAGE_WATCHING))
            self._group_alerted.discard(key[0])
            ended += 1
        return ended

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                self.finish_stale()
            except Exception:
                logger.warning("stale-stay sweep failed", exc_info=True)

    def _record_stay(self, camera_id: str, track: str, label: str, duration: float,
                     stage: int) -> None:
        """A finished stay: today's figures, the hour bucket, the dwell
        delta for history, and the recent log. Sub-second stays are edge
        flicker and are not counted."""
        if duration < 1.0:
            return
        day = self._today.setdefault(camera_id, _day_blank())
        day["stays"] += 1
        day["dwell_s"] += duration
        day["longest_s"] = max(day["longest_s"], duration)
        for i, (_, edge) in enumerate(HISTOGRAM_EDGES):
            if duration < edge:
                day["histogram"][i] += 1
                break
        self._totals[camera_id] = self._totals.get(camera_id, 0) + 1
        hour = int(time.time() // 3600) * 3600
        buckets = self._hourly.setdefault(camera_id, {})
        buckets.setdefault(hour, _hour_blank())["stays"] += 1
        for old in [h for h in buckets if h < hour - 23 * 3600]:
            buckets.pop(old, None)
        delta = self._dwell_delta.setdefault(camera_id, {"count": 0, "seconds": 0.0, "max": 0.0})
        delta["count"] += 1
        delta["seconds"] += duration
        delta["max"] = max(delta["max"], duration)
        self._recent.append({
            "message": f"{label} left {camera_id} after {int(duration)}s"
                       + (" — alerted" if stage >= STAGE_ALERTED else ""),
            "time": time.time(),
            "level": "high" if stage >= STAGE_ALERTED else "info",
            "camera": camera_id, "label": label, "track": track,
            "dwell_s": round(duration, 1), "stage": STAGE_NAMES.get(stage, "watching"),
        })

    def _raise(self, camera: CameraWatch, kind: str, *, label: str, track: str, dwell: float,
               threshold: float, event: dict[str, Any], count: int = 1) -> Alert:
        cam_id = camera.camera_id
        now = time.time()
        if kind == "loitering":
            self._last_alert_at[cam_id] = now
        day = self._today.setdefault(cam_id, _day_blank())
        day["alerts"] += 1
        hour = int(now // 3600) * 3600
        self._hourly.setdefault(cam_id, {}).setdefault(hour, _hour_blank())["alerts"] += 1
        sev_index = SEVERITIES.index(self.cfg.alert_severity)
        if kind == "loitering":
            severity = self.cfg.alert_severity
            title = f"{label.capitalize()} loitering at {cam_id}"
            description = (f"A {label} has been inside {camera.zone.name!r} on {cam_id} for "
                           f"{int(dwell)}s (threshold {int(threshold)}s).")
        elif kind == "loitering-escalated":
            severity = SEVERITIES[min(sev_index + 1, len(SEVERITIES) - 1)]
            title = f"{label.capitalize()} still loitering at {cam_id}"
            description = (f"Still inside {camera.zone.name!r} on {cam_id} after {int(dwell)}s — "
                           f"{int(self.cfg.escalate_after_seconds)}s past the first alert.")
        else:
            severity = SEVERITIES[min(sev_index + 1, len(SEVERITIES) - 1)]
            title = f"{count} gathered at {cam_id}"
            description = (f"{count} watched objects have been together inside "
                           f"{camera.zone.name!r} on {cam_id} for over {int(threshold)}s.")
        self._recent.append({
            "message": f"{title} — {kind}", "time": now, "level": severity,
            "camera": cam_id, "label": label, "track": track, "dwell_s": round(dwell, 1),
            "stage": kind,
        })
        return Alert(
            title=title,
            description=description,
            camera_id=cam_id,
            severity=severity,
            alert_type=kind,
            correlation_id=str(event.get("correlation_id") or ""),
            evidence={
                "label": label,
                "track_id": track,
                "dwell_seconds": round(dwell, 1),
                "threshold_seconds": threshold,
                "zone_name": camera.zone.name,
                "count": count,
                "adapter": event.get("adapter"),
                "adapter_version": event.get("adapter_version"),
                "model_fingerprint": event.get("model_fingerprint"),
            },
            images=self._evidence(cam_id),
            tags=[kind, camera.zone.name, label],
        )

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

    # ── dwell history ──

    def flush_dwell(self) -> int:
        """Publish finished stays since the last flush as footfall dwell
        (entries/exits zero, so a tripwire on the same camera is not
        double counted)."""
        if not self.cfg.publish_dwell:
            self._dwell_delta = {}
            return 0
        pending = {k: v for k, v in self._dwell_delta.items() if v["count"]}
        self._dwell_delta = {}
        if not pending:
            return 0
        if self._publisher is None:
            url = self.cfg.nats_alerts_url or self.cfg.nats_url
            token = self.cfg.nats_alerts_token if self.cfg.nats_alerts_url else self.cfg.nats_token
            self._publisher = DomainEventPublisher(url, token=token, producer="app:loitering-detection")
        published = 0
        for cam_id, delta in pending.items():
            ok = self._publisher.publish(FOOTFALL_SCHEMA, camera_id=cam_id, payload={
                "entries": 0, "exits": 0,
                "dwell_count": delta["count"],
                "dwell_seconds": round(delta["seconds"], 1),
                "dwell_max_seconds": round(delta["max"], 1),
                "period_seconds": self.cfg.dwell_period_seconds,
                "labels": list(self.cfg.watch_labels),
            })
            if ok:
                published += 1
                self._dwell_published += 1
        return published

    async def _dwell_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.dwell_period_seconds)
            try:
                self.flush_dwell()
            except Exception:
                logger.warning("dwell publish failed", exc_info=True)

    async def run(self, *, once: bool = False) -> None:
        tasks: list[asyncio.Task] = []
        if not once:
            tasks.append(asyncio.create_task(self._dwell_loop()))
            tasks.append(asyncio.create_task(self._sweep_loop()))
        try:
            await super().run(once=once)
        finally:
            for t in tasks:
                t.cancel()
            try:
                self.flush_dwell()
            except Exception:
                pass

    # ── camera discovery ──

    def on_cameras_update(self, camera_ids) -> None:
        super().on_cameras_update(camera_ids)
        self.refresh_cameras(camera_ids)

    def refresh_cameras(self, camera_ids) -> tuple[list[str], list[str]]:
        """Re-derive the camera set from the cameras picked for this app
        (core ids). No-op when cameras were pinned in YAML. Returns
        (added, removed)."""
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
            self._unknown_cameras.discard(cam_id)
            for key in [k for k, _ in self._stays.items() if k[0] == cam_id]:
                self._stays.pop(key)
        if added or removed:
            logger.info("camera set refreshed: +%s -%s (now %s)", added or "-", removed or "-",
                        sorted(self.cfg.cameras) or "(none)")
            if added and self._last_config is not None:
                self.on_config_update(self._last_config)   # pick up their drawn zones
        return added, removed

    # ── live config ──

    def on_config_update(self, config: dict[str, Any]) -> None:
        """Catalog edits, applied live and idempotently."""
        self._last_config = dict(config)
        changed: list[str] = []
        if "watch_labels" in config:
            labels = [str(s).lower() for s in (config.get("watch_labels") or []) if str(s).strip()]
            if labels and labels != self.cfg.watch_labels:
                self.cfg.watch_labels = labels
                changed.append("labels")
        for key in ("threshold_seconds", "grace_period_seconds"):
            if key in config:
                try:
                    value = float(config[key])
                except (TypeError, ValueError):
                    continue
                if value > 0 and value != getattr(self.cfg, key):
                    setattr(self.cfg, key, value)
                    changed.append(key)
                    if key == "grace_period_seconds":
                        self._stays.ttl = value
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
                    new = _whole_frame(cam_id, cam.frame_width, cam.frame_height)
                    zone, is_drawn = new.zone, False
                else:
                    is_drawn = True
                if (is_drawn != cam.drawn
                        or [(p.x, p.y) for p in zone.polygon] != [(p.x, p.y) for p in cam.zone.polygon]):
                    cam.zone, cam.drawn = zone, is_drawn
                    changed.append(f"zone:{cam_id}")
        if changed:
            logger.info("config applied live: %s", ", ".join(changed))

    # ── actions ──

    def on_action(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        if name == "dismiss":
            cam = str(params.get("camera") or "").strip()
            track = str(params.get("track") or "").strip()
            if not cam or not track:
                raise ValueError("camera and track are required")
            rec = self._stays.get((cam, track))
            if rec is None:
                raise KeyError(f"no current stay for {track} on {cam}")
            rec.data["dismissed"] = True
            self._recent.append({"message": f"{rec.data.get('label', 'object')} at {cam} dismissed",
                                 "time": time.time(), "level": "info", "camera": cam,
                                 "track": track, "stage": "dismissed",
                                 "dwell_s": round(rec.age, 1)})
            return {"ok": True, "camera": cam, "track": track}
        if name == "reset_today":
            self._today = {}
            return {"ok": True}
        raise KeyError(name)

    # ── surfaces ──

    def state_snapshot(self) -> dict[str, Any]:
        self._roll_day()
        now = time.time()
        enabled, threshold = self._alerts_enabled_now()
        hour_now = int(now // 3600) * 3600
        hours = [hour_now - i * 3600 for i in range(23, -1, -1)]
        dwelling: list[dict[str, Any]] = []
        live_by_cam: dict[str, int] = {}
        for (cam_id, track), rec in self._stays.items():
            live_by_cam[cam_id] = live_by_cam.get(cam_id, 0) + 1
            stage = "dismissed" if rec.data.get("dismissed") else STAGE_NAMES.get(
                rec.data.get("stage", STAGE_WATCHING), "watching")
            # Dwell is event time; last_seen may lag wall-clock by the
            # inference interval, so extend by the wall-clock gap since
            # the last sighting (bounded by the grace period).
            gap = min(max(0.0, now - rec.last_seen), self.cfg.grace_period_seconds)
            dwell_s = rec.age + gap
            dwelling.append({
                "camera": cam_id, "label": rec.data.get("label", "?"), "track": track,
                "dwell_s": round(dwell_s, 1), "stage": stage,
                "progress": min(1.0, dwell_s / threshold) if threshold > 0 else 1.0,
            })
        dwelling.sort(key=lambda r: -r["dwell_s"])
        per_camera = []
        hourly: dict[str, list[dict[str, Any]]] = {}
        for cam_id, cam in self.cfg.cameras.items():
            day = self._today.get(cam_id, _day_blank())
            per_camera.append({
                "camera": cam_id,
                "zone": cam.zone.name if cam.drawn else "— whole frame",
                "drawn": cam.drawn,
                "dwelling": live_by_cam.get(cam_id, 0),
                "stays_today": day["stays"],
                "alerts_today": day["alerts"],
                "longest_s": round(day["longest_s"], 1),
                "avg_s": round(day["dwell_s"] / day["stays"], 1) if day["stays"] else 0.0,
                "histogram": [{"bucket": b, "n": n} for (b, _), n in zip(HISTOGRAM_EDGES, day["histogram"])],
                "total": self._totals.get(cam_id, 0),
                "last": self._last.get(cam_id),
            })
            buckets = self._hourly.get(cam_id, {})
            hourly[cam_id] = [{"t": h, **buckets.get(h, _hour_blank())} for h in hours]
        today = {
            "stays": sum(d["stays"] for d in self._today.values()),
            "alerts": sum(d["alerts"] for d in self._today.values()),
            "longest_s": round(max([d["longest_s"] for d in self._today.values()] or [0.0]), 1),
            "since": self._today_key,
        }
        return {
            "dwelling_now": len(dwelling),
            "today": today,
            "threshold_seconds": self.cfg.threshold_seconds,
            "threshold_now": threshold,
            "alerts_active_now": enabled,
            "after_hours": (self.cfg.active_hours is not None
                            and not self.cfg.active_hours.contains(self._now_local())),
            "escalate_after_seconds": self.cfg.escalate_after_seconds,
            "group_size": self.cfg.group_size,
            "per_camera": per_camera,
            "dwelling": dwelling,
            "hourly": hourly,
            "needs_zone": [c for c, cam in self.cfg.cameras.items() if not cam.drawn],
            "dwell_published": self._dwell_published,
            "recent": list(self._recent),
            "since": self._started_at,
        }

    def ui_html(self) -> str:
        """One static HTML page, no scripts: who is dwelling now with a
        progress bar to the threshold, per-camera figures with a 24 h
        strip, and the recent stays."""
        snap = self.state_snapshot()
        esc = _html.escape
        now = time.time()

        def ago(ts):
            if not ts:
                return "—"
            m = max(0, int((now - ts) / 60))
            return "just now" if m == 0 else f"{m}m ago" if m < 60 else f"{m // 60}h ago"

        def mmss(s):
            s = int(s)
            return f"{s // 60}:{s % 60:02d}" if s >= 60 else f"{s}s"

        live_rows = "".join(
            f"<tr><td>{esc(d['camera'])}</td><td>{esc(d['label'])}</td>"
            f"<td><div class='prog'><i style='width:{int(100 * d['progress'])}%;"
            f"background:{'#e5484d' if d['stage'] in ('alerted', 'escalated') else '#8b8d98' if d['stage'] == 'dismissed' else '#e5a000'}'></i></div></td>"
            f"<td>{mmss(d['dwell_s'])}</td><td>{esc(d['stage'])}</td></tr>"
            for d in snap["dwelling"]
        )
        live = ("<table><tr><th>Camera</th><th>Object</th><th>Toward threshold</th><th>Dwell</th><th>Stage</th></tr>"
                + live_rows + "</table>") if live_rows else "<p class='dim'>Nobody is inside a zone right now.</p>"
        cards = []
        for row in snap["per_camera"]:
            buckets = snap["hourly"].get(row["camera"], [])
            peak = max([b["stays"] for b in buckets] or [1]) or 1
            bars = "".join(
                f"<div class='bar' title='{esc(_dt.datetime.fromtimestamp(b['t']).strftime('%H:00'))}: "
                f"{b['stays']} stays, {b['alerts']} alerts'>"
                f"<i style='height:{int(100 * b['stays'] / peak)}%'></i>"
                f"<b style='height:{int(100 * b['alerts'] / peak)}%'></b></div>"
                for b in buckets
            )
            warn = ("<p class='warn'>No zone drawn — the whole frame is watched. Draw one in the "
                    "App Catalog's config form.</p>" if not row["drawn"] else "")
            cards.append(
                f"<section class='card'><h2>{esc(row['camera'])} <span class='dim'>{esc(row['zone'])}</span></h2>{warn}"
                f"<div class='stats'><div><b>{row['dwelling']}</b><span class='dim'>now</span></div>"
                f"<div><b>{row['stays_today']}</b><span class='dim'>stays today</span></div>"
                f"<div><b>{row['alerts_today']}</b><span class='dim'>alerts</span></div>"
                f"<div><b>{mmss(row['longest_s'])}</b><span class='dim'>longest</span></div>"
                f"<div><b>{mmss(row['avg_s'])}</b><span class='dim'>average</span></div></div>"
                f"<div class='strip'>{bars}</div>"
                f"<div class='dim small'>last 24 h · last seen {ago(row['last'])}</div></section>")
        recent_rows = "".join(
            f"<tr><td style='color:{'#e5484d' if r.get('level') in ('high', 'critical', 'medium') else '#1a1a1a'}'>"
            f"{esc(str(r.get('message', '')))}</td><td>{ago(r.get('time'))}</td></tr>"
            for r in reversed(snap["recent"][-12:])
        )
        recent = ("<table><tr><th>Event</th><th>When</th></tr>" + recent_rows + "</table>"
                  ) if recent_rows else "<p class='dim'>No stays yet.</p>"
        policy = f"alert after {mmss(snap['threshold_seconds'])}"
        if snap["escalate_after_seconds"]:
            policy += f", escalate {mmss(snap['escalate_after_seconds'])} later"
        if snap["group_size"]:
            policy += f", gathering at {snap['group_size']}"
        if snap["after_hours"]:
            policy += (f" · after hours: {'alert after ' + mmss(snap['threshold_now']) if snap['alerts_active_now'] else 'quiet'}")
        return f"""<title>Loitering Detection</title>
<style>
 body {{ font: 14px system-ui, sans-serif; margin: 1.2rem; color: #1a1a1a; background: #fafafa; }}
 h1 {{ font-size: 1.1rem; margin: 0 0 .2rem }} h2 {{ font-size: .95rem; margin: 0 0 .4rem }}
 .dim {{ color: #6b6f76; font-weight: 400 }} .small {{ font-size: .8rem }} .warn {{ color: #e5a000; margin: .2rem 0 }}
 .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: .8rem; margin: .8rem 0 }}
 .card {{ background: #fff; border: 1px solid #e0e0e0; border-radius: 6px; padding: .7rem .9rem }}
 .stats {{ display: flex; gap: 1rem; margin: .3rem 0 .5rem; flex-wrap: wrap }} .stats b {{ font-size: 1.3rem; display: block }}
 .strip {{ display: flex; gap: 2px; height: 36px; align-items: flex-end }}
 .bar {{ flex: 1; display: flex; gap: 1px; align-items: flex-end; height: 100% }}
 .bar i, .bar b {{ display: block; flex: 1; min-height: 1px }} .bar i {{ background: #46a758 }} .bar b {{ background: #e5484d }}
 .prog {{ width: 140px; height: 8px; background: #eee; border-radius: 4px; overflow: hidden }} .prog i {{ display: block; height: 100% }}
 table {{ border-collapse: collapse; width: 100% }}
 th, td {{ text-align: left; padding: .3rem .6rem; border-bottom: 1px solid #e0e0e0; font-size: .9rem }}
 th {{ color: #6b6f76; font-weight: 500 }}
</style>
<h1>Loitering Detection</h1>
<div class="dim">Now: <b>{snap['dwelling_now']}</b> dwelling · today <b>{snap['today']['stays']}</b> stays,
 <b>{snap['today']['alerts']}</b> alerts, longest <b>{mmss(snap['today']['longest_s'])}</b> · {esc(policy)}</div>
<h2 style="margin-top:.8rem">Dwelling now</h2>
{live}
<div class="grid">{''.join(cards) or "<p class='dim'>No cameras selected. Pick cameras for Loitering Detection in the App Catalog.</p>"}</div>
<div class="dim small">Bars: green stays, red alerts, one per hour.</div>
<h2 style="margin-top:1rem">Recent</h2>
{recent}
"""


Loitering = LoiteringDetector


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point (``[project.scripts]``)."""
    return app(LoiteringDetector, load_config=load_config).run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
