# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Line-crossing (tripwire) app on the ``opennvr-app-sdk``.

Counts and alerts when a *tracked* object crosses an operator-drawn
oriented line — the perimeter tripwire, the directional people or
vehicle counter, the one-way corridor, the loading-dock gate. One app,
two jobs that every product in this segment ends up offering together:

* **Counting.** Every crossing is tallied per camera and per direction
  (A→B and B→A, labelled ``in`` / ``out`` by default), since boot, for
  today (reset at ``daily_reset_hour``), and per hour for the last 24 h.
  Deltas are published as ``occupancy.footfall.v1`` domain events, which
  core already sums into 90-day per-camera-hour history, so the
  Tripwires page can chart last week without this app remembering it.
* **Alerting.** ``alert_mode`` decides what a crossing does beyond
  counting: ``every`` crossing alerts (the perimeter case),
  ``threshold`` alerts when today's count reaches ``passthrough_threshold``
  (the "tell me at the 100th visitor" case), ``off`` only counts. Alerts
  honour ``active_hours`` (count all day, alarm only at night), a
  per-camera ``alert_cooldown_seconds`` (one alert per burst, not one
  per person in a group), a chosen ``alert_severity``, and carry a
  snapshot from the camera as evidence.

Filters, because a tripwire on a real camera sees more than people:
``watch_labels`` (what to count), ``min_track_age_seconds`` (ignore
tracks that flicker into existence on the line), ``min_bbox_height``
(ignore objects too small to be what you are counting).

Cameras come from OpenNVR, not from YAML: with no ``cameras:`` listed the
app asks core which cameras carry the ``line_crossing`` assignment,
re-checks every five minutes, and reads each camera's line from the
catalog's per-camera tripwire editor — live, no restart. A camera with
no line drawn yet is shown as such on the dashboard rather than silently
counting nothing.

How a crossing is decided
-------------------------
Per (camera, track_id) the previous bbox center is remembered (the SDK's
``keyed_state``, TTL ``track_ttl_seconds``). When the next center
arrives, the segment previous→current is tested against the line: a
crossing is a segment that intersects the line AND ends on the other
side (``opennvr_app_sdk.geometry.Tripwire``). One crossing fires once.
Identity is what makes this well-defined, so detections without a
``track_id`` are ignored with a one-time warning. The stock stack's
Tier-0 detector already tracks (``consume_tier0: true`` and subscribe
to ``opennvr.inference.tier0.>`` — what the compose config does);
otherwise chain the ``bytetrack`` adapter after the detector.

Run::

    python line_crossing.py --config config.yml
    python line_crossing.py --config config.yml --once
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import html as _html
import logging
import time
from collections import deque
from dataclasses import dataclass, field
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
from opennvr_app_sdk.cameras import UNIT_FRAME, discover_cameras, filter_cameras_for_skill
from opennvr_app_sdk.config import load_yaml
from opennvr_app_sdk.domain_events import DomainEventPublisher
from opennvr_app_sdk.geometry import Point, Tripwire, bbox_center, scale_vertices
from opennvr_app_sdk.state import keyed_state

logger = logging.getLogger("line-crossing")

#: The camera assignment this app scopes to (docs/CAMERA_ASSIGNMENTS.md).
SKILL: str = "line_crossing"
DISCOVERY_REFRESH_S: int = 300
#: Counts ride the platform's footfall history (EVENT_CONTRACTS.md):
#: a→b is an entry, b→a an exit, on the same contract the occupancy app
#: publishes — consumers do not branch on the producer.
FOOTFALL_SCHEMA = "occupancy.footfall.v1"
DIRECTIONS: tuple[str, str] = ("a_to_b", "b_to_a")
ALERT_MODES: tuple[str, ...] = ("every", "threshold", "off")
SEVERITIES: tuple[str, ...] = ("low", "medium", "high", "critical")
LABEL_SUGGESTIONS = ["person", "car", "truck", "bus", "motorcycle", "bicycle"]

MANIFEST = AppManifest(
    id="line-crossing",
    name="Line Crossing",
    version="1.1.0",
    category="perimeter",
    summary=(
        "Tripwire counting and alerts: tallies every tracked object that "
        "crosses a drawn line, per direction and per hour, and alarms on "
        "each crossing, on a count threshold, or only at night."
    ),
    requires_tasks=["object_detection", "multi_object_tracking"],
    # Lights the first-class Tripwires page (app/src/lib/appVerticals.ts).
    provides=["crossings"],
    subscribes="opennvr.inference.>",
    params=[
        Param("watch_labels", list, default=["person"], suggestions=LABEL_SUGGESTIONS,
              description="Object classes to count. Vehicles and people usually want separate lines."),
        Param("line", "geometry.tripwire", per_camera=True,
              description="The tripwire, drawn on the camera. Its arrow is A→B."),
        Param("label_a_to_b", str, default="in",
              description="What an A→B crossing is called on the page and in alerts."),
        Param("label_b_to_a", str, default="out"),
        Param("alert_mode", str, default="every", choices=list(ALERT_MODES),
              description="every: alert on each crossing (perimeter). threshold: alert when today's "
                          "count reaches passthrough_threshold. off: count only."),
        Param("alert_severity", str, default="high", choices=list(SEVERITIES)),
        Param("alert_cooldown_seconds", float, default=0.0,
              description="Per camera, the least time between two alerts. Crossings inside the "
                          "window are still counted. A group at a gate becomes one alert, not six."),
        Param("active_hours", "time_range",
              description="Alerts fire only inside this daily window (cross-midnight allowed). "
                          "Counting runs all day regardless. Leave empty for always."),
        Param("passthrough_threshold", int, default=0,
              description="With alert_mode=threshold: alert each time today's crossings in the "
                          "counted direction reach a multiple of this."),
        Param("min_track_age_seconds", float, default=0.0,
              description="Ignore tracks younger than this when they cross — a detector flicker "
                          "that appears on the line is not a crossing."),
        Param("min_bbox_height", float, default=0.0,
              description="Ignore objects whose box is shorter than this fraction of the frame "
                          "(0.1 = a tenth of the height). Filters birds, far traffic."),
        Param("track_ttl_seconds", float, default=30.0,
              description="Idle time after which a track's last position is forgotten."),
        Param("daily_reset_hour", int, default=0,
              description="Local hour at which 'today' starts over (0 = midnight, 6 = 06:00)."),
        Param("attach_snapshot", bool, default=True,
              description="Fetch a still from the camera when an alert fires and attach it."),
        Param("publish_footfall", bool, default=True,
              description="Publish per-direction counts as footfall history so the Tripwires page "
                          "can chart past days. Turn off if the occupancy app already has an entry "
                          "line on the same camera, or both will be summed."),
        Param("footfall_period_seconds", int, default=60),
    ],
    emits=[
        AlertType("line-crossing", severity="high",
                  description="A watched object crossed the line in a counted direction."),
        AlertType("passthrough", severity="low",
                  description="Today's count reached the passthrough threshold."),
    ],
    state_schema=[
        StateView("today_in", "Today in", kind="metric", path="today.a_to_b"),
        StateView("today_out", "Today out", kind="metric", path="today.b_to_a"),
        StateView("today_net", "Net inside", kind="metric", path="today.net",
                  description="Today's in minus out across all cameras."),
        StateView("total_crossings", "Since start", kind="metric", path="total_crossings"),
        StateView("alerts_today", "Alerts today", kind="metric", path="alerts_today"),
        StateView("active_tracks", "Active tracks", kind="metric", path="active_tracks"),
        StateView("per_camera", "Per camera", kind="table", path="per_camera",
                  columns=["camera", "line", "today_in", "today_out", "net", "total", "last"],
                  description="Counts per tripwire. A camera with no line drawn counts nothing."),
        StateView("recent", "Recent crossings", kind="log", path="recent", limit=12,
                  description="Newest first; alerted crossings show red."),
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
class CameraWire:
    """One camera + its tripwire (None until one is drawn) + the pixel
    space the line was drawn in."""
    camera_id: str
    wire: Tripwire | None
    frame_width: int
    frame_height: int


@dataclass
class AppConfig:
    nats_url: str
    nats_token: str | None
    subject_pattern: str
    watch_labels: list[str]
    track_ttl_seconds: float
    cameras: dict[str, CameraWire]  # keyed by camera_id
    webhook_url: str | None
    nats_alerts_url: str | None = None
    nats_alerts_token: str | None = None
    nats_alerts_subject_prefix: str = "opennvr.alerts"
    contract_port: int | None = None
    contract_bind_host: str | None = None
    contract_host: str | None = None
    opennvr_url: str | None = None
    opennvr_token: str | None = None
    # ── counting / alerting knobs (all live-editable) ──
    label_a_to_b: str = "in"
    label_b_to_a: str = "out"
    alert_mode: str = "every"
    alert_severity: str = "high"
    alert_cooldown_seconds: float = 0.0
    active_hours: ActiveHours | None = None
    passthrough_threshold: int = 0
    min_track_age_seconds: float = 0.0
    min_bbox_height: float = 0.0
    daily_reset_hour: int = 0
    attach_snapshot: bool = True
    publish_footfall: bool = True
    footfall_period_seconds: int = 60
    # Tier-0 publishes tracked detections (stable track_id per object) on
    # ``opennvr.inference.tier0.>`` — the SDK bridges them into
    # on_detections when this is set. It is the zero-cost tracking source
    # a stock stack has; a chained bytetrack adapter is the alternative.
    consume_tier0: bool = False
    # ── discovery ──
    auto_cameras: bool = False
    opennvr_url_for_discovery: str = ""
    internal_api_key: str | None = None


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


def _wire_from_drawn(drawn: Any, cam: CameraWire) -> Tripwire | None:
    if not (isinstance(drawn, dict) and drawn.get("a") and drawn.get("b")):
        return None
    (pa, pb) = scale_vertices([drawn["a"], drawn["b"]], cam.frame_width, cam.frame_height)
    direction = str(drawn.get("count_direction") or "both")
    if direction not in ("both", "a_to_b", "b_to_a"):
        direction = "both"
    return Tripwire.from_config(name=str(drawn.get("name") or "line"), a=pa, b=pb,
                                count_direction=direction)


def _scope_to_assignment(discovered: list[dict]) -> list[dict]:
    """Only the cameras assigned ``line_crossing``. Closed by default:
    nothing assigned means count nowhere, never the whole fleet."""
    assigned = filter_cameras_for_skill(discovered, SKILL)
    if assigned is None:
        return discovered
    keep = set(assigned)
    return [c for c in discovered if str(c.get("camera_id")) in keep]


def _knobs_from(raw: dict[str, Any], base: AppConfig | None = None) -> dict[str, Any]:
    """The live-editable knobs, parsed and validated from a config dict.
    Keys absent from ``raw`` keep ``base``'s value (or the default)."""
    d = base
    out: dict[str, Any] = {}

    def _get(key, default):
        if key in raw and raw[key] is not None:
            return raw[key]
        return getattr(d, key) if d is not None else default

    out["label_a_to_b"] = str(_get("label_a_to_b", "in")).strip() or "in"
    out["label_b_to_a"] = str(_get("label_b_to_a", "out")).strip() or "out"
    mode = str(_get("alert_mode", "every")).strip().lower() or "every"
    if mode not in ALERT_MODES:
        raise ValueError(f"config: 'alert_mode' must be one of {', '.join(ALERT_MODES)}")
    out["alert_mode"] = mode
    sev = str(_get("alert_severity", "high")).strip().lower() or "high"
    if sev not in SEVERITIES:
        raise ValueError(f"config: 'alert_severity' must be one of {', '.join(SEVERITIES)}")
    out["alert_severity"] = sev
    try:
        out["alert_cooldown_seconds"] = max(0.0, float(_get("alert_cooldown_seconds", 0.0)))
        out["passthrough_threshold"] = max(0, int(_get("passthrough_threshold", 0)))
        out["min_track_age_seconds"] = max(0.0, float(_get("min_track_age_seconds", 0.0)))
        out["min_bbox_height"] = min(1.0, max(0.0, float(_get("min_bbox_height", 0.0))))
        out["daily_reset_hour"] = min(23, max(0, int(_get("daily_reset_hour", 0))))
        out["footfall_period_seconds"] = max(10, int(_get("footfall_period_seconds", 60)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"config: numeric knob malformed: {exc}") from None
    out["attach_snapshot"] = bool(_get("attach_snapshot", True))
    out["publish_footfall"] = bool(_get("publish_footfall", True))
    if "active_hours" in raw:
        out["active_hours"] = ActiveHours.parse(raw.get("active_hours"))
    elif d is not None:
        out["active_hours"] = d.active_hours
    else:
        out["active_hours"] = None
    return out


def load_config(path: str) -> AppConfig:
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
        track_ttl = float(raw.get("track_ttl_seconds", 30.0))
    except (TypeError, ValueError) as exc:
        raise ValueError("config: 'track_ttl_seconds' must be a number") from exc
    if track_ttl <= 0:
        raise ValueError("config: 'track_ttl_seconds' must be > 0")

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

    line_override = raw.get("line")
    line_map = line_override if isinstance(line_override, dict) else {}

    cameras_raw = raw.get("cameras") or []
    auto_cameras = False
    if not cameras_raw:
        # No cameras listed → ask OpenNVR which carry this app's assignment
        # rather than refusing to boot. Hand-copied ids are the classic way
        # a tripwire counts nothing all day (``cam-1`` vs ``cam1``). Lines
        # come from the catalog's editor; a camera without one is reported
        # on the dashboard, not silently skipped.
        discovered = _scope_to_assignment(discover_cameras(
            str(raw.get("opennvr_url") or ""), api_key=raw.get("internal_api_key")))
        cameras_raw = [
            {"camera_id": c["camera_id"], "frame_width": UNIT_FRAME, "frame_height": UNIT_FRAME}
            for c in discovered
        ]
        auto_cameras = True

    cameras: dict[str, CameraWire] = {}
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
            cam = CameraWire(camera_id=camera_id, wire=None,
                             frame_width=frame_width, frame_height=frame_height)
            drawn = None
            for raw_key, val in line_map.items():
                if _camera_key(raw_key, {camera_id: cam}) == camera_id:
                    drawn = val
                    break
            wire = _wire_from_drawn(drawn, cam)
            if wire is None and isinstance(c.get("line"), dict):
                wire = Tripwire.from_config(
                    name=str(c.get("wire_name", f"wire-{idx}")),
                    a=c["line"]["a"], b=c["line"]["b"],
                    count_direction=str(c["line"].get("count_direction", "both")),
                )
            elif wire is not None and c.get("wire_name"):
                wire = Tripwire.from_config(name=str(c["wire_name"]), a=(wire.a.x, wire.a.y),
                                            b=(wire.b.x, wire.b.y),
                                            count_direction=wire.count_direction)
            cam.wire = wire
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
        track_ttl_seconds=track_ttl,
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
        opennvr_url_for_discovery=str(raw.get("opennvr_url") or ""),
        internal_api_key=raw.get("internal_api_key") or None,
        **knobs,
    )


# ── The detector ────────────────────────────────────────────────────


def _blank() -> dict[str, int]:
    return {"a_to_b": 0, "b_to_a": 0}


class LineCrossingDetector(Detector):
    """Consumes tracked inference events, remembers each track's last
    center, counts crossings per camera and direction, and alerts by
    the configured policy."""

    manifest = MANIFEST

    def setup(self) -> None:
        self._tracks = keyed_state(ttl=self.cfg.track_ttl_seconds, auto_gc=False)
        self._warned_missing_track = False
        # Counters: since start, today, per hour (last 24 buckets), and
        # the delta since the last footfall publish.
        self._totals: dict[str, dict[str, int]] = {}
        self._today: dict[str, dict[str, int]] = {}
        self._today_key: str = self._day_key(self._now_local())
        self._hourly: dict[str, dict[int, dict[str, int]]] = {}
        self._footfall_delta: dict[str, dict[str, int]] = {}
        self._footfall_published = 0
        self._recent: deque[dict[str, Any]] = deque(maxlen=50)
        self._last: dict[str, float] = {}
        self._last_alert_at: dict[str, float] = {}
        self._alerts_today = 0
        self._started_at = time.time()
        self._publisher: DomainEventPublisher | None = None
        self._nvr: Any = None
        self._nvr_tried = False
        self._unknown_cameras: set[str] = set()

    # ── time helpers ──

    def _now_local(self) -> _dt.datetime:
        return _dt.datetime.now()

    def _day_key(self, now: _dt.datetime) -> str:
        """'Today' starts at daily_reset_hour, so 03:00 belongs to the
        previous day when the reset is at 06:00."""
        shifted = now - _dt.timedelta(hours=self.cfg.daily_reset_hour)
        return shifted.date().isoformat()

    def _roll_day(self) -> None:
        key = self._day_key(self._now_local())
        if key != self._today_key:
            self._today_key = key
            self._today = {}
            self._alerts_today = 0

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
        self._tracks.gc(event_ts)
        self._roll_day()
        if camera.wire is None:
            return []

        fired: list[Alert] = []
        for det in detections:
            if not isinstance(det, dict):
                continue
            label = str(det.get("label", "")).lower()
            if label not in self.cfg.watch_labels:
                continue
            track_id = det.get("track_id")
            if track_id is None:
                if not self._warned_missing_track:
                    logger.warning(
                        "detections have no 'track_id' — line-crossing needs a tracking "
                        "adapter (e.g. bytetrack) upstream. Ignoring untracked detections."
                    )
                    self._warned_missing_track = True
                continue
            bbox = det.get("bbox")
            if not isinstance(bbox, dict):
                continue
            curr = bbox_center(bbox, camera.frame_width, camera.frame_height)
            key = (camera_id, str(track_id))
            record = self._tracks.get(key)
            prev_point: Point | None = record.data.get("last_point") if record else None
            first_seen: float = record.data.get("first_seen", event_ts) if record else event_ts
            state = self._tracks.touch(key, at=event_ts)
            state.data["last_point"] = curr
            state.data["first_seen"] = first_seen
            if prev_point is None:
                continue  # first sighting — no segment to test yet
            direction = camera.wire.crossing(prev_point, curr)
            if direction is None:
                continue
            # Filters: a track that has only just appeared, or an object
            # too small to be what we count, does not cross.
            if self.cfg.min_track_age_seconds > 0 and (event_ts - first_seen) < self.cfg.min_track_age_seconds:
                continue
            try:
                h = float(bbox.get("h", 0.0))
            except (TypeError, ValueError):
                h = 0.0
            if self.cfg.min_bbox_height > 0 and 0 < h < self.cfg.min_bbox_height:
                continue
            alert = self._count(camera, label=label, track_id=str(track_id),
                                direction=direction, event=event, event_ts=event_ts)
            if alert is not None:
                fired.append(alert)
        return fired

    def _count(self, camera: CameraWire, *, label: str, track_id: str,
               direction: str, event: dict[str, Any], event_ts: float) -> Alert | None:
        cam_id = camera.camera_id
        self._totals.setdefault(cam_id, _blank())[direction] += 1
        self._today.setdefault(cam_id, _blank())[direction] += 1
        self._footfall_delta.setdefault(cam_id, _blank())[direction] += 1
        hour = int(time.time() // 3600) * 3600
        buckets = self._hourly.setdefault(cam_id, {})
        buckets.setdefault(hour, _blank())[direction] += 1
        for old in [h for h in buckets if h < hour - 23 * 3600]:
            buckets.pop(old, None)
        now = time.time()
        self._last[cam_id] = now
        word = self.cfg.label_a_to_b if direction == "a_to_b" else self.cfg.label_b_to_a

        alert: Alert | None = None
        kind = self._alert_kind(cam_id, direction, now)
        if kind is not None:
            self._last_alert_at[cam_id] = now
            self._alerts_today += 1
            alert = self._build_alert(camera=camera, label=label, track_id=track_id,
                                      direction=direction, word=word, event=event, kind=kind)
        self._recent.append({
            "message": f"{label} {word} at {cam_id}"
                       + (" — alert" if alert is not None else ""),
            "time": now,
            "level": "high" if alert is not None else "info",
            "camera": cam_id,
            "label": label,
            "direction": direction,
            "word": word,
            "alerted": alert is not None,
        })
        return alert

    def _alert_kind(self, cam_id: str, direction: str, now: float) -> str | None:
        """Which alert this crossing raises, or None: the alert policy."""
        mode = self.cfg.alert_mode
        if mode == "off":
            return None
        if self.cfg.active_hours is not None and not self.cfg.active_hours.contains(self._now_local()):
            return None
        if mode == "threshold":
            n = self.cfg.passthrough_threshold
            if n <= 0:
                return None
            today = self._today.get(cam_id, _blank())
            counted = today["a_to_b"] + today["b_to_a"]
            return "passthrough" if counted % n == 0 else None
        cooldown = self.cfg.alert_cooldown_seconds
        last = self._last_alert_at.get(cam_id)
        if cooldown > 0 and last is not None and (now - last) < cooldown:
            return None
        return "line-crossing"

    def _build_alert(self, *, camera: CameraWire, label: str, track_id: str,
                     direction: str, word: str, event: dict[str, Any], kind: str) -> Alert:
        correlation_id = str(event.get("correlation_id") or "")
        wire_name = camera.wire.name if camera.wire else "line"
        today = self._today.get(camera.camera_id, _blank())
        if kind == "passthrough":
            title = (f"{today['a_to_b'] + today['b_to_a']} crossings today at "
                     f"{camera.camera_id}")
            description = (f"Today's count on {camera.camera_id} reached "
                           f"{today['a_to_b'] + today['b_to_a']} ({today['a_to_b']} "
                           f"{self.cfg.label_a_to_b}, {today['b_to_a']} {self.cfg.label_b_to_a}).")
            severity = "low"
        else:
            title = f"{label.capitalize()} {word} at {camera.camera_id}"
            description = (f"Track {track_id} ({label}) crossed {wire_name!r} on "
                           f"{camera.camera_id} going {word} ({direction}).")
            severity = self.cfg.alert_severity
        images = self._evidence(camera.camera_id)
        return Alert(
            title=title,
            description=description,
            camera_id=camera.camera_id,
            severity=severity,
            alert_type=kind,
            correlation_id=correlation_id,
            evidence={
                "label": label,
                "track_id": track_id,
                "direction": direction,
                "direction_label": word,
                "wire_name": wire_name,
                "today": dict(today),
                "adapter": event.get("adapter"),
                "adapter_version": event.get("adapter_version"),
                "model_fingerprint": event.get("model_fingerprint"),
            },
            images=images,
            tags=["line-crossing", wire_name, direction, label],
        )

    # ── evidence ──

    def _platform(self):
        """The platform client, created once; None when OPENNVR_URL is
        not set (tests, bare-metal runs)."""
        if self._nvr is None and not self._nvr_tried:
            self._nvr_tried = True
            try:
                from opennvr_app_sdk.client import OpenNVR
                self._nvr = OpenNVR(self.cfg.opennvr_url or None, timeout=3.0)
            except Exception as exc:
                logger.info("no platform client for snapshots: %s", exc)
        return self._nvr

    def _evidence(self, camera_id: str) -> dict[str, str]:
        """A still from the camera at the moment of the alert, stored as
        evidence and cited by path (never bytes — the alert is a NATS
        message with a 1 MB ceiling). Best-effort: the alert goes out
        without the photo rather than not at all."""
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

    # ── footfall history ──

    def flush_footfall(self) -> int:
        """Publish the per-direction delta since the last flush as
        footfall events, one per camera with anything to say."""
        if not self.cfg.publish_footfall:
            self._footfall_delta = {}
            return 0
        pending = {k: v for k, v in self._footfall_delta.items() if v["a_to_b"] or v["b_to_a"]}
        self._footfall_delta = {}
        if not pending:
            return 0
        if self._publisher is None:
            url = self.cfg.nats_alerts_url or self.cfg.nats_url
            token = self.cfg.nats_alerts_token if self.cfg.nats_alerts_url else self.cfg.nats_token
            self._publisher = DomainEventPublisher(url, token=token, producer="app:line-crossing")
        published = 0
        for cam_id, delta in pending.items():
            ok = self._publisher.publish(FOOTFALL_SCHEMA, camera_id=cam_id, payload={
                "entries": delta["a_to_b"],
                "exits": delta["b_to_a"],
                "dwell_count": 0,
                "dwell_seconds": 0.0,
                "dwell_max_seconds": 0.0,
                "period_seconds": self.cfg.footfall_period_seconds,
                "labels": list(self.cfg.watch_labels),
            })
            if ok:
                published += 1
                self._footfall_published += 1
        return published

    async def _footfall_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.footfall_period_seconds)
            try:
                self.flush_footfall()
            except Exception:
                logger.warning("footfall publish failed", exc_info=True)

    # ── camera discovery ──

    def refresh_cameras(self, discovered: list[dict[str, Any]] | None = None
                        ) -> tuple[list[str], list[str]]:
        """Re-derive the camera set from OpenNVR's assignments. No-op when
        cameras were pinned in YAML. Returns (added, removed)."""
        if not self.cfg.auto_cameras:
            return [], []
        if discovered is None:
            discovered = discover_cameras(self.cfg.opennvr_url_for_discovery,
                                          api_key=self.cfg.internal_api_key)
        if not discovered:
            return [], []   # a blip is not "delete every camera"
        ids = {str(c["camera_id"]) for c in _scope_to_assignment(discovered)}
        current = set(self.cfg.cameras)
        added = sorted(ids - current)
        removed = sorted(current - ids)
        for cam_id in added:
            self.cfg.cameras[cam_id] = CameraWire(cam_id, None, UNIT_FRAME, UNIT_FRAME)
        for cam_id in removed:
            self.cfg.cameras.pop(cam_id, None)
            self._unknown_cameras.discard(cam_id)
        if added or removed:
            logger.info("camera set refreshed: +%s -%s (now %s)", added or "-", removed or "-",
                        sorted(self.cfg.cameras) or "(none)")
            if added and self._last_config is not None:
                self.on_config_update(self._last_config)   # pick up their drawn lines
        return added, removed

    async def _discovery_loop(self) -> None:
        while True:
            await asyncio.sleep(DISCOVERY_REFRESH_S)
            try:
                discovered = await asyncio.to_thread(
                    discover_cameras, self.cfg.opennvr_url_for_discovery,
                    api_key=self.cfg.internal_api_key)
                self.refresh_cameras(discovered=discovered)
            except Exception:
                logger.warning("camera refresh failed", exc_info=True)

    _last_config: dict[str, Any] | None = None

    async def run(self, *, once: bool = False) -> None:
        tasks: list[asyncio.Task] = []
        if not once:
            if self.cfg.auto_cameras:
                tasks.append(asyncio.create_task(self._discovery_loop()))
            tasks.append(asyncio.create_task(self._footfall_loop()))
        try:
            await super().run(once=once)
        finally:
            for t in tasks:
                t.cancel()
            try:
                self.flush_footfall()
            except Exception:
                pass

    # ── live config ──

    def on_config_update(self, config: dict[str, Any]) -> None:
        """Catalog edits, applied live and idempotently: the lines drawn
        per camera, the labels, and every alert / filter knob."""
        self._last_config = dict(config)
        changed: list[str] = []
        if "watch_labels" in config:
            labels = [str(s).lower() for s in (config.get("watch_labels") or []) if str(s).strip()]
            if labels and labels != self.cfg.watch_labels:
                self.cfg.watch_labels = labels
                changed.append("labels")
        if "track_ttl_seconds" in config:
            try:
                ttl = float(config["track_ttl_seconds"])
                if ttl > 0 and ttl != self.cfg.track_ttl_seconds:
                    self.cfg.track_ttl_seconds = ttl
                    self._tracks = keyed_state(ttl=ttl, auto_gc=False)
                    changed.append("ttl")
            except (TypeError, ValueError):
                pass
        try:
            knobs = _knobs_from(config, self.cfg)
        except ValueError as exc:
            logger.warning("config edit ignored: %s", exc)
            knobs = {}
        for key, value in knobs.items():
            if getattr(self.cfg, key) != value:
                setattr(self.cfg, key, value)
                changed.append(key)
        if "line" in config:
            lines = config.get("line")
            lines = lines if isinstance(lines, dict) else {}
            for cam_id, cam in self.cfg.cameras.items():
                drawn = None
                for raw_key, val in lines.items():
                    if _camera_key(raw_key, self.cfg.cameras) == cam_id:
                        drawn = val
                        break
                try:
                    wire = _wire_from_drawn(drawn, cam)
                except (TypeError, ValueError, KeyError) as exc:
                    logger.warning("line for %s ignored: %s", cam_id, exc)
                    continue
                before = cam.wire
                same = (before is None and wire is None) or (
                    before is not None and wire is not None
                    and (before.a, before.b, before.count_direction)
                    == (wire.a, wire.b, wire.count_direction))
                if not same:
                    cam.wire = wire
                    changed.append(f"line:{cam_id}")
        if changed:
            logger.info("config applied live: %s", ", ".join(changed))

    # ── surfaces ──

    def state_snapshot(self) -> dict[str, Any]:
        self._roll_day()
        today_in = sum(v["a_to_b"] for v in self._today.values())
        today_out = sum(v["b_to_a"] for v in self._today.values())
        hour_now = int(time.time() // 3600) * 3600
        hours = [hour_now - i * 3600 for i in range(23, -1, -1)]
        per_camera = []
        hourly: dict[str, list[dict[str, Any]]] = {}
        for cam_id, cam in self.cfg.cameras.items():
            t = self._totals.get(cam_id, _blank())
            d = self._today.get(cam_id, _blank())
            per_camera.append({
                "camera": cam_id,
                "line": cam.wire.name if cam.wire else "— not drawn",
                "count_direction": cam.wire.count_direction if cam.wire else None,
                "today_in": d["a_to_b"], "today_out": d["b_to_a"],
                "net": d["a_to_b"] - d["b_to_a"],
                "total": t["a_to_b"] + t["b_to_a"],
                "last": self._last.get(cam_id),
            })
            buckets = self._hourly.get(cam_id, {})
            hourly[cam_id] = [
                {"t": h, **buckets.get(h, _blank())} for h in hours
            ]
        now_local = self._now_local()
        return {
            "today": {"a_to_b": today_in, "b_to_a": today_out, "net": today_in - today_out,
                      "since": self._today_key},
            "total_crossings": sum(v["a_to_b"] + v["b_to_a"] for v in self._totals.values()),
            "alerts_today": self._alerts_today,
            "active_tracks": len(self._tracks),
            "labels": {"a_to_b": self.cfg.label_a_to_b, "b_to_a": self.cfg.label_b_to_a},
            "alert_mode": self.cfg.alert_mode,
            "alerts_active_now": (self.cfg.alert_mode != "off" and
                                  (self.cfg.active_hours is None
                                   or self.cfg.active_hours.contains(now_local))),
            "per_camera": per_camera,
            "hourly": hourly,
            "needs_line": [c for c, cam in self.cfg.cameras.items() if cam.wire is None],
            "footfall_published": self._footfall_published,
            "recent": list(self._recent),
            "since": self._started_at,
        }

    def ui_html(self) -> str:
        """One static HTML page, no scripts: counts per tripwire, a 24 h
        bar strip per camera, the recent crossings."""
        snap = self.state_snapshot()
        esc = _html.escape
        now = time.time()

        def ago(ts):
            if not ts:
                return "—"
            m = max(0, int((now - ts) / 60))
            return "just now" if m == 0 else f"{m}m ago" if m < 60 else f"{m // 60}h ago"

        lin, lout = esc(snap["labels"]["a_to_b"]), esc(snap["labels"]["b_to_a"])
        cards = []
        for row in snap["per_camera"]:
            buckets = snap["hourly"].get(row["camera"], [])
            peak = max([b["a_to_b"] + b["b_to_a"] for b in buckets] or [1]) or 1
            bars = "".join(
                f"<div class='bar' title='{esc(_dt.datetime.fromtimestamp(b['t']).strftime('%H:00'))}: "
                f"{b['a_to_b']} {lin}, {b['b_to_a']} {lout}'>"
                f"<i style='height:{int(100 * b['a_to_b'] / peak)}%'></i>"
                f"<b style='height:{int(100 * b['b_to_a'] / peak)}%'></b></div>"
                for b in buckets
            )
            if row["line"].startswith("—"):
                body = "<p class='warn'>No line drawn yet — draw one in the App Catalog's config form.</p>"
            else:
                body = (f"<div class='stats'><div><b>{row['today_in']}</b><span class='dim'>{lin}</span></div>"
                        f"<div><b>{row['today_out']}</b><span class='dim'>{lout}</span></div>"
                        f"<div><b>{row['net']:+d}</b><span class='dim'>net</span></div>"
                        f"<div><b>{row['total']}</b><span class='dim'>since start</span></div></div>"
                        f"<div class='strip'>{bars}</div>"
                        f"<div class='dim small'>last 24 h · last crossing {ago(row['last'])}</div>")
            cards.append(f"<section class='card'><h2>{esc(row['camera'])} "
                         f"<span class='dim'>{esc(row['line'])}</span></h2>{body}</section>")
        rows = "".join(
            f"<tr><td style='color:{'#e5484d' if r.get('alerted') else '#1a1a1a'};font-weight:600'>"
            f"{esc(str(r.get('label', '')))} {esc(str(r.get('word', '')))}</td>"
            f"<td>{esc(str(r.get('camera', '')))}</td>"
            f"<td>{'alert' if r.get('alerted') else 'counted'}</td><td>{ago(r.get('time'))}</td></tr>"
            for r in reversed(snap["recent"][-12:])
        )
        table = ("<table><tr><th>Crossing</th><th>Camera</th><th>Result</th><th>When</th></tr>"
                 + rows + "</table>") if rows else "<p class='dim'>No crossings yet.</p>"
        policy = {"every": "every crossing alerts", "threshold":
                  f"alert at every {self.cfg.passthrough_threshold} crossings today",
                  "off": "counting only"}[snap["alert_mode"]]
        if self.cfg.active_hours is not None:
            policy += (f", {self.cfg.active_hours.start.strftime('%H:%M')}–"
                       f"{self.cfg.active_hours.end.strftime('%H:%M')} only")
        return f"""<title>Line Crossing</title>
<style>
 body {{ font: 14px system-ui, sans-serif; margin: 1.2rem; color: #1a1a1a; background: #fafafa; }}
 h1 {{ font-size: 1.1rem; margin: 0 0 .2rem }}
 h2 {{ font-size: .95rem; margin: 0 0 .4rem }}
 .dim {{ color: #6b6f76; font-weight: 400 }} .small {{ font-size: .8rem }} .warn {{ color: #e5a000 }}
 .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: .8rem; margin: .8rem 0 }}
 .card {{ background: #fff; border: 1px solid #e0e0e0; border-radius: 6px; padding: .7rem .9rem }}
 .stats {{ display: flex; gap: 1.2rem; margin: .3rem 0 .5rem }} .stats b {{ font-size: 1.3rem; display: block }}
 .strip {{ display: flex; gap: 2px; height: 36px; align-items: flex-end }}
 .bar {{ flex: 1; display: flex; gap: 1px; align-items: flex-end; height: 100% }}
 .bar i, .bar b {{ display: block; flex: 1; min-height: 1px }} .bar i {{ background: #46a758 }} .bar b {{ background: #8b8d98 }}
 table {{ border-collapse: collapse; width: 100% }}
 th, td {{ text-align: left; padding: .3rem .6rem; border-bottom: 1px solid #e0e0e0; font-size: .9rem }}
 th {{ color: #6b6f76; font-weight: 500 }}
</style>
<h1>Line Crossing</h1>
<div class="dim">Today: <b>{snap['today']['a_to_b']}</b> {lin} · <b>{snap['today']['b_to_a']}</b> {lout}
 · net <b>{snap['today']['net']:+d}</b> · {snap['alerts_today']} alerts · policy: {esc(policy)}
 {'· <span class="warn">outside alert hours</span>' if not snap['alerts_active_now'] and snap['alert_mode'] != 'off' else ''}</div>
<div class="grid">{''.join(cards) or "<p class='dim'>No cameras assigned. Assign the line_crossing skill to a camera.</p>"}</div>
<div class="dim small">Bars: green {lin}, grey {lout}, one per hour.</div>
<h2 style="margin-top:1rem">Recent crossings</h2>
{table}
"""


LineCrossing = LineCrossingDetector


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point (``[project.scripts]``). The SDK
    runner owns argparse, logging, signals, and the dispatcher."""
    return app(LineCrossingDetector, load_config=load_config).run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
