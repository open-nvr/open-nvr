# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Intrusion detection on the ``opennvr-app-sdk``.

The perimeter alarm: a watched class enters a drawn zone while the
camera is armed, and the site raises an alarm. The fence line at night,
the yard behind the shutter, the plant room, the roof.

Modelled on the intrusion panel every operator already knows — a state
machine per camera rather than a bare time window:

    disarmed ──arm──▶ arming (exit delay) ──▶ armed
                                               │ watched class in zone
                                               ▼
                                          breach (entry delay, countdown)
                                               │ still there when it expires
                                               ▼
                                             alarm ──▶ (re-arms after
                                                        alarm_reset_seconds)

* **Arming.** ``arm_mode`` is ``schedule`` (armed inside ``armed_hours``,
  which is the classic "restricted hours"), ``always``, ``manual`` (only
  the arm/disarm actions move it) or ``off``. An operator can arm or
  disarm from the Perimeter page at any time; a manual override holds
  until ``override_minutes`` pass or it is cleared, so "disarm for the
  delivery" cannot be forgotten forever.
* **Exit and entry delay.** ``exit_delay_seconds`` is the grace after
  arming before the zone is live — time to walk out. ``entry_delay_seconds``
  is the grace after a breach before the alarm is raised — time for
  someone authorised to be recognised and for the site to be disarmed.
  Set both to 0 for an instant perimeter (a fence line has no door).
* **Presence before breach.** ``min_presence_seconds`` (the AXIS
  "minimum presence in zone" idea) ignores an object that clips the
  zone edge for a frame. This, and the tracking underneath it, is what
  separates a perimeter alarm from a motion sensor.
* **One intruder is one alarm.** A breach belongs to a tracked object.
  A person standing in the zone raises one alarm, not one per frame,
  and ``alarm_cooldown_seconds`` merges a group at the fence into a
  single alarm per camera (the AXIS "post-alarm time").
* **Escalation.** If the intruder is still inside
  ``escalate_after_seconds`` after the alarm, a second, higher-severity
  alarm goes out — the "they are not leaving" signal a monitoring desk
  acts on differently.
* **Bypass.** A camera can be bypassed (a contractor is working in the
  yard) for a stated number of minutes; it stays visible on the page as
  bypassed rather than silently ignored, and un-bypasses itself.
* **Verification.** Every alarm carries a snapshot from the camera, the
  track id, the class, how long they had been inside, and the zone —
  what an operator needs to decide in the ten seconds they have.

Cameras and zones come from OpenNVR: with no ``cameras:`` listed the app
watches exactly the cameras picked for it in the App Catalog and reads
each camera's zone from the catalog's editor, live. A picked camera with
no zone watches its whole frame and says so.

This app rides Tier-0's tracked detections (``consume_tier0: true``,
subject ``opennvr.inference.tier0.>``) — no model of its own, no GPU
cost, and detections arrive at the detector's rate rather than a poll
interval, so an intruder crossing the zone between two polls is no
longer missed.

Run::

    python intrusion_detection.py --config config.yml
    python intrusion_detection.py --config config.yml --once
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
from opennvr_app_sdk.geometry import Zone, bbox_center, scale_vertices
from opennvr_app_sdk.state import keyed_state

logger = logging.getLogger("intrusion-detection")

SEVERITIES: tuple[str, ...] = ("low", "medium", "high", "critical")
ARM_MODES: tuple[str, ...] = ("schedule", "always", "manual", "off")
LABEL_SUGGESTIONS = ["person", "car", "truck", "motorcycle", "bicycle"]

#: Per-camera arming states, in the vocabulary of an intrusion panel.
DISARMED, ARMING, ARMED, BREACH, ALARM, BYPASSED = (
    "disarmed", "arming", "armed", "breach", "alarm", "bypassed")

MANIFEST = AppManifest(
    id="intrusion-detection",
    name="Intrusion Detection",
    version="1.1.0",
    category="perimeter",
    summary=(
        "The perimeter alarm: arms on a schedule or on command, raises an alarm when a "
        "watched class enters a drawn zone, with exit and entry delays, presence "
        "filtering, escalation, bypass and snapshot verification."
    ),
    requires_tasks=["object_detection", "multi_object_tracking"],
    # Lights the first-class Perimeter page (app/src/lib/appVerticals.ts).
    provides=["intrusion"],
    subscribes="opennvr.inference.>",
    params=[
        Param("watch_labels", list, default=["person"], suggestions=LABEL_SUGGESTIONS,
              description="Classes that count as an intruder. A yard that expects "
                          "vehicles by day and nobody by night usually watches person only."),
        Param("zones", "geometry.polygon", per_camera=True,
              description="The protected zone, drawn on the camera. Nothing drawn = the "
                          "whole frame, which on a perimeter camera usually means the fence "
                          "line AND the road behind it."),
        Param("arm_mode", str, default="schedule", choices=list(ARM_MODES),
              description="schedule: armed inside armed_hours. always: armed around the "
                          "clock. manual: only the arm/disarm buttons move it. off: watch "
                          "and report, never alarm."),
        Param("armed_hours", "time_range",
              description="With arm_mode=schedule, the daily window the site is armed "
                          "(cross-midnight allowed, e.g. 19:00–07:00). Empty = always armed."),
        Param("exit_delay_seconds", float, default=0.0,
              description="Grace after arming before the zone goes live — time to walk out. "
                          "0 for a fence line nobody leaves through."),
        Param("entry_delay_seconds", float, default=0.0,
              description="Grace between the breach and the alarm — time to be recognised or "
                          "to disarm. 0 makes the perimeter instant."),
        Param("min_presence_seconds", float, default=1.0,
              description="An object must be inside the zone this long to count as a breach. "
                          "The single most effective false-alarm filter: a box that clips the "
                          "zone edge for one frame is not an intruder."),
        Param("alert_severity", str, default="high", choices=list(SEVERITIES),
              description="Severity of the alarm; the escalation is one step higher."),
        Param("escalate_after_seconds", float, default=0.0,
              description="Still inside this long after the alarm: raise a second, higher "
                          "alarm. 0 = no escalation."),
        Param("alarm_cooldown_seconds", float, default=30.0,
              description="Per camera, the least time between two alarms — a group coming "
                          "over the fence is one alarm, not six. Escalations ignore it."),
        Param("alarm_reset_seconds", float, default=60.0,
              description="After the zone is clear this long, the camera returns to armed "
                          "and can alarm again."),
        Param("min_bbox_height", float, default=0.0,
              description="Ignore objects shorter than this fraction of the frame (0.1 = a "
                          "tenth). Filters traffic on the far road and birds."),
        Param("override_minutes", float, default=60.0,
              description="How long a manual arm/disarm from the page holds before the mode "
                          "takes over again. 0 = until it is cleared."),
        Param("attach_snapshot", bool, default=True,
              description="Fetch a still from the camera when the alarm fires and attach it."),
    ],
    emits=[
        AlertType("intrusion", severity="high",
                  description="A watched class was inside the zone on an armed camera."),
        AlertType("intrusion-escalated", severity="critical",
                  description="Still inside after the escalation delay."),
    ],
    state_schema=[
        StateView("armed_cameras", "Armed", kind="metric", path="armed_count",
                  description="Cameras currently armed and watching."),
        StateView("in_alarm", "In alarm", kind="metric", path="in_alarm",
                  description="Cameras in breach or alarm right now."),
        StateView("alarms_today", "Alarms today", kind="metric", path="today.alarms"),
        StateView("breaches_today", "Breaches today", kind="metric", path="today.breaches",
                  description="Zone entries that passed the presence filter, alarmed or not."),
        StateView("per_camera", "Per camera", kind="table", path="per_camera",
                  columns=["camera", "zone", "state", "intruders", "breaches_today",
                           "alarms_today", "last"],
                  description="Each camera's arming state and today's figures."),
        StateView("intruders", "Inside now", kind="table", path="intruders",
                  columns=["camera", "label", "track", "inside_s", "stage"],
                  description="Every watched object inside a zone right now."),
        StateView("recent", "Recent", kind="log", path="recent", limit=12),
    ],
    actions=[
        Action(
            "arm", "Arm",
            params=[Param("camera", str, default="",
                          description="Leave blank for every camera.")],
            description="Arm now, overriding the schedule until the override expires.",
        ),
        Action(
            "disarm", "Disarm",
            params=[Param("camera", str, default="",
                          description="Leave blank for every camera.")],
            confirm=True,
            description="Disarm now. The site raises no alarms until the override expires "
                        "or the schedule arms it again.",
        ),
        Action(
            "bypass", "Bypass a camera",
            params=[
                Param("camera", str, required=True),
                Param("minutes", float, default=60.0,
                      description="How long to bypass for. 0 clears the bypass."),
            ],
            description="Temporarily exclude one camera — work in the yard, a delivery bay "
                        "open for the afternoon. It stays on the page as bypassed.",
        ),
        Action(
            "acknowledge", "Acknowledge the alarm",
            params=[Param("camera", str, default="")],
            description="Clear the alarm state so the camera re-arms without waiting for "
                        "the reset timer.",
        ),
        Action(
            "clear_override", "Back to schedule", params=[],
            description="Drop every manual arm/disarm and follow arm_mode again.",
        ),
    ],
    has_ui=True,   # GET /ui dashboard, proxied at /api/v1/apps/{id}/ui
)


# ── Config ──────────────────────────────────────────────────────────


@dataclass
class ArmedHours:
    """A daily window in local time; cross-midnight supported."""
    start: _dt.time
    end: _dt.time

    def contains(self, when: _dt.datetime) -> bool:
        t = when.time()
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end

    @classmethod
    def parse(cls, raw: Any) -> "ArmedHours | None":
        if not isinstance(raw, dict):
            return None
        s, e = str(raw.get("start") or "").strip(), str(raw.get("end") or "").strip()
        if not s or not e:
            return None
        try:
            return cls(_dt.time.fromisoformat(s), _dt.time.fromisoformat(e))
        except ValueError as exc:
            raise ValueError(f"armed_hours must be HH:MM start/end: {exc}") from None


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
    # ── arming / alarm policy (all live-editable) ──
    arm_mode: str = "schedule"
    armed_hours: ArmedHours | None = None
    exit_delay_seconds: float = 0.0
    entry_delay_seconds: float = 0.0
    min_presence_seconds: float = 1.0
    alert_severity: str = "high"
    escalate_after_seconds: float = 0.0
    alarm_cooldown_seconds: float = 30.0
    alarm_reset_seconds: float = 60.0
    min_bbox_height: float = 0.0
    override_minutes: float = 60.0
    attach_snapshot: bool = True
    track_ttl_seconds: float = 5.0
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
                                vertices=scale_vertices(drawn, cam.frame_width, cam.frame_height))
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

    mode = str(_get("arm_mode", "schedule")).strip().lower() or "schedule"
    if mode not in ARM_MODES:
        raise ValueError(f"config: 'arm_mode' must be one of {', '.join(ARM_MODES)}")
    out["arm_mode"] = mode
    sev = str(_get("alert_severity", "high")).strip().lower() or "high"
    if sev not in SEVERITIES:
        raise ValueError(f"config: 'alert_severity' must be one of {', '.join(SEVERITIES)}")
    out["alert_severity"] = sev
    try:
        out["exit_delay_seconds"] = max(0.0, float(_get("exit_delay_seconds", 0.0)))
        out["entry_delay_seconds"] = max(0.0, float(_get("entry_delay_seconds", 0.0)))
        out["min_presence_seconds"] = max(0.0, float(_get("min_presence_seconds", 1.0)))
        out["escalate_after_seconds"] = max(0.0, float(_get("escalate_after_seconds", 0.0)))
        out["alarm_cooldown_seconds"] = max(0.0, float(_get("alarm_cooldown_seconds", 30.0)))
        out["alarm_reset_seconds"] = max(1.0, float(_get("alarm_reset_seconds", 60.0)))
        out["min_bbox_height"] = min(1.0, max(0.0, float(_get("min_bbox_height", 0.0))))
        out["override_minutes"] = max(0.0, float(_get("override_minutes", 60.0)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"config: numeric knob malformed: {exc}") from None
    out["attach_snapshot"] = bool(_get("attach_snapshot", True))
    if "armed_hours" in raw:
        out["armed_hours"] = ArmedHours.parse(raw.get("armed_hours"))
    elif d is not None:
        out["armed_hours"] = d.armed_hours
    else:
        out["armed_hours"] = None
    return out


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

    # Back-compat: the pre-1.1 app called the schedule "restricted_hours".
    if "armed_hours" not in raw and "restricted_hours" in raw:
        raw["armed_hours"] = raw["restricted_hours"]

    knobs = _knobs_from(raw)
    return AppConfig(
        nats_url=nats_url,
        nats_token=str(raw["nats_token"]) if raw.get("nats_token") else None,
        subject_pattern=subject,
        watch_labels=watch_labels,
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
        track_ttl_seconds=float(raw.get("track_ttl_seconds", 5.0)),
        consume_tier0=bool(raw.get("consume_tier0", False)),
        auto_cameras=auto_cameras,
        **knobs,
    )


# ── Per-camera arming state ─────────────────────────────────────────


def _day_blank() -> dict[str, int]:
    return {"breaches": 0, "alarms": 0}


@dataclass
class CameraState:
    """The arming state machine for one camera."""
    state: str = DISARMED
    since: float = 0.0            # when the current state began (wall clock)
    #: Set while arming: when the exit delay expires.
    armed_at: float = 0.0
    #: Set on breach: when the entry delay expires and the alarm fires.
    alarm_at: float = 0.0
    #: The track that opened the current breach.
    breach_track: str = ""
    #: When the alarm fired, for escalation and reset.
    alarmed_at: float = 0.0
    escalated: bool = False
    #: Wall-clock deadline of a bypass, 0 when not bypassed.
    bypass_until: float = 0.0
    #: Manual override: True armed / False disarmed / None none, and its deadline.
    override: bool | None = None
    override_until: float = 0.0
    last_clear: float = 0.0       # last moment the zone was empty


class IntrusionDetector(Detector):
    """Consumes tracked detections, keeps an arming state machine per
    camera, and raises alarms when a watched class is inside a drawn
    zone on an armed camera for longer than the presence filter."""

    manifest = MANIFEST

    def setup(self) -> None:
        # One record per (camera, track) while the object is in the zone.
        self._inside = keyed_state(ttl=self.cfg.track_ttl_seconds, auto_gc=False)
        self._warned_missing_track = False
        self._cams: dict[str, CameraState] = {}
        self._today: dict[str, dict[str, int]] = {}
        self._today_key: str = self._now_local().date().isoformat()
        self._last: dict[str, float] = {}
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

    def _cam_state(self, camera_id: str) -> CameraState:
        st = self._cams.get(camera_id)
        if st is None:
            st = CameraState(state=DISARMED, since=time.time(), last_clear=time.time())
            self._cams[camera_id] = st
        return st

    def _day(self, camera_id: str) -> dict[str, int]:
        return self._today.setdefault(camera_id, _day_blank())

    # ── arming ──

    def _should_be_armed(self, st: CameraState, now: float) -> bool:
        """What the policy says, before the state machine's own delays:
        a live manual override wins, then the mode."""
        if st.override is not None:
            if st.override_until and now >= st.override_until:
                st.override = None
                st.override_until = 0.0
            else:
                return st.override
        mode = self.cfg.arm_mode
        if mode == "off":
            return False
        if mode == "always":
            return True
        if mode == "manual":
            return False          # only an override arms a manual site
        hours = self.cfg.armed_hours
        return True if hours is None else hours.contains(self._now_local())

    def tick(self, now: float | None = None) -> list[Alert]:
        """Advance every camera's state machine on the wall clock: exit
        delays expiring, entry delays turning into alarms, escalations,
        alarm resets, bypasses ending. Tier-0 publishes only frames with
        detections, so a quiet camera must still be moved along — this
        runs from the sweep loop as well as after each event."""
        now = time.time() if now is None else now
        self._roll_day()
        # Tier-0 publishes only frames that have detections, so a zone that
        # empties goes silent. Forget tracks nobody has seen for longer than
        # the track TTL, on the wall clock, or a camera would never reset.
        cutoff = now - self.cfg.track_ttl_seconds
        for key, rec in list(self._inside.items()):
            if rec.last_seen < cutoff:
                self._inside.pop(key)
                st = self._cam_state(key[0])
                if not self._intruders_on(key[0]):
                    # The zone emptied when the last track aged out, which is
                    # LATER than any previous clear — take the later of the
                    # two, or an alarm raised long after a quiet spell would
                    # reset against that stale timestamp and re-arm at once
                    # instead of waiting out alarm_reset_seconds.
                    st.last_clear = max(st.last_clear,
                                        rec.last_seen + self.cfg.track_ttl_seconds)
        fired: list[Alert] = []
        for cam_id, cam in self.cfg.cameras.items():
            st = self._cam_state(cam_id)
            # A bypass that has run out returns the camera to the policy.
            if st.bypass_until and now >= st.bypass_until:
                st.bypass_until = 0.0
                if st.state == BYPASSED:
                    self._to(st, DISARMED, now)
                    self._note(cam_id, "bypass ended", "info", now)
            if st.state == BYPASSED:
                continue
            want_armed = self._should_be_armed(st, now)
            if not want_armed:
                if st.state not in (DISARMED,):
                    self._to(st, DISARMED, now)
                continue
            if st.state == DISARMED:
                # Arm, through the exit delay when there is one.
                if self.cfg.exit_delay_seconds > 0:
                    self._to(st, ARMING, now)
                    st.armed_at = now + self.cfg.exit_delay_seconds
                else:
                    self._to(st, ARMED, now)
                continue
            if st.state == ARMING and now >= st.armed_at:
                self._to(st, ARMED, now)
                continue
            if st.state == BREACH and now >= st.alarm_at:
                if self._cooldown_blocks(st, now):
                    # A group at the fence: still one alarm per camera.
                    self._to(st, ALARM, now)
                else:
                    fired.append(self._raise(cam, st, "intrusion", now))
                continue
            if st.state == ALARM:
                if (self.cfg.escalate_after_seconds > 0 and not st.escalated
                        and now - st.alarmed_at >= self.cfg.escalate_after_seconds
                        and self._intruders_on(cam_id)):
                    st.escalated = True
                    fired.append(self._raise(cam, st, "intrusion-escalated", now))
                elif (not self._intruders_on(cam_id)
                      and now - st.last_clear >= self.cfg.alarm_reset_seconds):
                    self._to(st, ARMED, now)
                    self._note(cam_id, "zone clear — re-armed", "info", now)
        return fired

    def _to(self, st: CameraState, state: str, now: float) -> None:
        if st.state == state:
            return
        st.state = state
        st.since = now
        if state in (ARMED, DISARMED, BYPASSED):
            st.breach_track = ""
            st.alarm_at = 0.0
            st.escalated = False

    def _intruders_on(self, camera_id: str) -> int:
        return sum(1 for key, _ in self._inside.items() if key[0] == camera_id)

    def _note(self, camera_id: str, message: str, level: str, now: float,
              **extra: Any) -> None:
        self._recent.append({"message": f"{camera_id}: {message}", "time": now,
                             "level": level, "camera": camera_id, **extra})

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
        now = time.time()
        fired = self.tick(now)
        st = self._cam_state(camera_id)

        # Who is inside the zone in this frame.
        inside: dict[str, str] = {}
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
            if not camera.zone.contains(bbox_center(bbox, camera.frame_width,
                                                    camera.frame_height)):
                continue
            track_id = det.get("track_id")
            if track_id is None:
                if not self._warned_missing_track:
                    logger.warning(
                        "detections have no 'track_id' — presence is measured per "
                        "(camera, label) instead of per object. Consume Tier-0 or chain a "
                        "tracking adapter for per-intruder alarms."
                    )
                    self._warned_missing_track = True
                track_id = f"label:{label}"
            inside[str(track_id)] = label

        # Forget tracks that have left (the zone empties → the camera can reset).
        cutoff = event_ts - self.cfg.track_ttl_seconds
        for key, rec in list(self._inside.items()):
            if key[0] == camera_id and key[1] not in inside and rec.last_seen < cutoff:
                self._inside.pop(key)
        if inside:
            self._last[camera_id] = now
        else:
            st.last_clear = now

        # Presence: how long has each object been inside?
        breached: list[tuple[str, str, float]] = []   # (track, label, inside_s)
        for track, label in inside.items():
            key = (camera_id, track)
            existing = self._inside.get(key)
            if existing is not None and event_ts < existing.last_seen:
                continue
            rec = self._inside.touch(key, at=event_ts)
            rec.data.setdefault("label", label)
            if rec.age >= self.cfg.min_presence_seconds:
                if not rec.data.get("counted"):
                    rec.data["counted"] = True
                    self._day(camera_id)["breaches"] += 1
                breached.append((track, label, rec.age))

        if not breached or st.state not in (ARMED, BREACH, ALARM):
            return fired

        if st.state == ARMED:
            track, label, inside_s = breached[0]
            st.breach_track = track
            if self.cfg.entry_delay_seconds > 0:
                self._to(st, BREACH, now)
                st.alarm_at = now + self.cfg.entry_delay_seconds
                self._note(camera_id, f"{label} in the zone — {int(self.cfg.entry_delay_seconds)}s "
                                      f"to disarm", "medium", now, label=label, track=track)
            elif self._cooldown_blocks(st, now):
                self._to(st, ALARM, now)
            else:
                st.breach_track = track
                fired.append(self._raise(camera, st, "intrusion", now,
                                         label=label, track=track, inside_s=inside_s,
                                         event=event))
        return fired

    def _raise(self, camera: CameraWatch, st: CameraState, kind: str, now: float,
               *, label: str = "", track: str = "", inside_s: float = 0.0,
               event: dict[str, Any] | None = None) -> Alert:
        cam_id = camera.camera_id
        if not label or not track:
            # Raised from the state machine (entry delay expired): describe
            # whoever is still inside.
            for key, rec in self._inside.items():
                if key[0] == cam_id:
                    track = track or key[1]
                    label = label or str(rec.data.get("label", "object"))
                    inside_s = inside_s or rec.age
                    break
            label = label or "object"
            track = track or st.breach_track or "?"
        if kind == "intrusion":
            # Cooldown merges a group at the fence into one alarm.
            self._to(st, ALARM, now)
            st.alarmed_at = now
            st.escalated = False
            severity = self.cfg.alert_severity
            title = f"Intruder at {cam_id}"
            description = (f"A {label} is inside {camera.zone.name!r} on {cam_id} "
                           f"({int(inside_s)}s).")
        else:
            severity = SEVERITIES[min(SEVERITIES.index(self.cfg.alert_severity) + 1,
                                      len(SEVERITIES) - 1)]
            title = f"Intruder still at {cam_id}"
            description = (f"Still inside {camera.zone.name!r} on {cam_id}, "
                           f"{int(self.cfg.escalate_after_seconds)}s after the alarm.")
        self._day(cam_id)["alarms"] += 1
        self._note(cam_id, title, severity, now, label=label, track=track)
        return Alert(
            title=title,
            description=description,
            camera_id=cam_id,
            severity=severity,
            alert_type=kind,
            correlation_id=str((event or {}).get("correlation_id") or ""),
            evidence={
                "label": label,
                "track_id": track,
                "inside_seconds": round(inside_s, 1),
                "zone_name": camera.zone.name,
                "arm_mode": self.cfg.arm_mode,
                "entry_delay_seconds": self.cfg.entry_delay_seconds,
                "adapter": (event or {}).get("adapter"),
                "adapter_version": (event or {}).get("adapter_version"),
                "model_fingerprint": (event or {}).get("model_fingerprint"),
            },
            images=self._evidence(cam_id),
            tags=[kind, camera.zone.name, label],
        )

    def _cooldown_blocks(self, st: CameraState, now: float) -> bool:
        cd = self.cfg.alarm_cooldown_seconds
        return cd > 0 and st.alarmed_at > 0 and (now - st.alarmed_at) < cd

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

    def _tick_and_fire(self, now: float | None = None) -> list[Alert]:
        """Advance the state machine and DISPATCH whatever it raised.

        ``tick`` returns the alerts its transitions produced, and every
        caller that dropped that list dropped real alarms. The losing
        case is not exotic: the dashboard polls ``/state`` about once a
        second, ``state_snapshot`` ticks, and an entry delay expiring in
        that instant goes BREACH -> ALARM inside the poll. The alerts
        were discarded, the camera was left in ALARM, and the sweep a
        second later saw a state that had already moved and raised
        nothing. The page said "in alarm"; nobody was told.

        So every call site that does not itself consume the list comes
        through here, and a test below fails if a new one does not.

        Dispatch failures are swallowed rather than raised, because two
        of the callers are an HTTP GET and an operator action: a webhook
        timing out must not turn the status page into a 500, and it must
        not lose the rest of the batch either.
        """
        fired = self.tick(now)
        for alert in fired:
            try:
                self._dispatcher.fire(alert)
            except Exception:  # noqa: BLE001
                logger.warning("alarm dispatch failed", exc_info=True)
        return fired

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                self._tick_and_fire()
            except Exception:
                logger.warning("arming tick failed", exc_info=True)

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
            self._cams.pop(cam_id, None)
            self._unknown_cameras.discard(cam_id)
            for key, _ in [kv for kv in self._inside.items() if kv[0][0] == cam_id]:
                self._inside.pop(key)
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
        if "watch_labels" in config:
            labels = [str(s).lower() for s in (config.get("watch_labels") or []) if str(s).strip()]
            if labels and labels != self.cfg.watch_labels:
                self.cfg.watch_labels = labels
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
                        or [(p.x, p.y) for p in zone.polygon] != [(p.x, p.y) for p in cam.zone.polygon]):
                    cam.zone, cam.drawn = zone, is_drawn
                    changed.append(f"zone:{cam_id}")
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

    def on_action(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        if name in ("arm", "disarm"):
            want = name == "arm"
            targets = self._targets(params.get("camera"))
            hold = self.cfg.override_minutes * 60.0
            for cam_id in targets:
                st = self._cam_state(cam_id)
                st.override = want
                st.override_until = (now + hold) if hold > 0 else 0.0
                if not want:
                    self._to(st, DISARMED, now)
                self._note(cam_id, "armed by operator" if want else "disarmed by operator",
                           "info", now)
            self._tick_and_fire(now)
            return {"ok": True, "cameras": targets, "armed": want,
                    "until": (now + hold) if hold > 0 else None}
        if name == "bypass":
            cam_id = str(params.get("camera") or "").strip()
            if cam_id not in self.cfg.cameras:
                raise KeyError(f"unknown camera {cam_id!r}")
            try:
                minutes = float(params.get("minutes", 60.0))
            except (TypeError, ValueError):
                raise ValueError("minutes must be a number") from None
            st = self._cam_state(cam_id)
            if minutes <= 0:
                st.bypass_until = 0.0
                self._to(st, DISARMED, now)
                self._note(cam_id, "bypass cleared", "info", now)
            else:
                st.bypass_until = now + minutes * 60.0
                self._to(st, BYPASSED, now)
                self._note(cam_id, f"bypassed for {int(minutes)} min", "info", now)
            self._tick_and_fire(now)
            return {"ok": True, "camera": cam_id, "until": st.bypass_until or None}
        if name == "acknowledge":
            targets = self._targets(params.get("camera"))
            for cam_id in targets:
                st = self._cam_state(cam_id)
                if st.state in (ALARM, BREACH):
                    self._to(st, ARMED, now)
                    # alarmed_at is deliberately left alone: the cooldown
                    # still applies, so acknowledging while the intruder is
                    # still in the zone re-arms without firing a duplicate
                    # alarm on the very next frame.
                    self._note(cam_id, "alarm acknowledged", "info", now)
            return {"ok": True, "cameras": targets}
        if name == "clear_override":
            for st in self._cams.values():
                st.override = None
                st.override_until = 0.0
            self._tick_and_fire(now)
            return {"ok": True}
        raise KeyError(name)

    # ── surfaces ──

    def state_snapshot(self) -> dict[str, Any]:
        # Ticking here is how a quiet camera still advances; firing what
        # that tick raises is how the alarm it just produced reaches
        # somebody. A status page must not be able to swallow an alarm.
        self._tick_and_fire()
        now = time.time()
        intruders: list[dict[str, Any]] = []
        per_cam_intruders: dict[str, int] = {}
        for (cam_id, track), rec in self._inside.items():
            per_cam_intruders[cam_id] = per_cam_intruders.get(cam_id, 0) + 1
            gap = min(max(0.0, now - rec.last_seen), self.cfg.track_ttl_seconds)
            inside_s = rec.age + gap
            st = self._cam_state(cam_id)
            intruders.append({
                "camera": cam_id, "label": rec.data.get("label", "?"), "track": track,
                "inside_s": round(inside_s, 1),
                "stage": st.state if st.state in (BREACH, ALARM) else (
                    "counted" if rec.data.get("counted") else "watching"),
            })
        intruders.sort(key=lambda r: -r["inside_s"])
        per_camera = []
        for cam_id, cam in self.cfg.cameras.items():
            st = self._cam_state(cam_id)
            day = self._today.get(cam_id, _day_blank())
            countdown = None
            if st.state == ARMING and st.armed_at:
                countdown = max(0.0, round(st.armed_at - now, 1))
            elif st.state == BREACH and st.alarm_at:
                countdown = max(0.0, round(st.alarm_at - now, 1))
            elif st.state == BYPASSED and st.bypass_until:
                countdown = max(0.0, round(st.bypass_until - now, 1))
            per_camera.append({
                "camera": cam_id,
                "zone": cam.zone.name if cam.drawn else "— whole frame",
                "drawn": cam.drawn,
                "state": st.state,
                "countdown_s": countdown,
                "override": st.override,
                "intruders": per_cam_intruders.get(cam_id, 0),
                "breaches_today": day["breaches"],
                "alarms_today": day["alarms"],
                "last": self._last.get(cam_id),
            })
        armed_count = sum(1 for r in per_camera if r["state"] in (ARMED, BREACH, ALARM))
        return {
            "armed_count": armed_count,
            "camera_count": len(per_camera),
            "in_alarm": sum(1 for r in per_camera if r["state"] in (BREACH, ALARM)),
            "bypassed": sum(1 for r in per_camera if r["state"] == BYPASSED),
            "today": {
                "breaches": sum(d["breaches"] for d in self._today.values()),
                "alarms": sum(d["alarms"] for d in self._today.values()),
                "since": self._today_key,
            },
            "arm_mode": self.cfg.arm_mode,
            "armed_hours": ({"start": self.cfg.armed_hours.start.strftime("%H:%M"),
                             "end": self.cfg.armed_hours.end.strftime("%H:%M")}
                            if self.cfg.armed_hours else None),
            "entry_delay_seconds": self.cfg.entry_delay_seconds,
            "exit_delay_seconds": self.cfg.exit_delay_seconds,
            "min_presence_seconds": self.cfg.min_presence_seconds,
            "escalate_after_seconds": self.cfg.escalate_after_seconds,
            "overridden": any(st.override is not None for st in self._cams.values()),
            "per_camera": per_camera,
            "intruders": intruders,
            "needs_zone": [c for c, cam in self.cfg.cameras.items() if not cam.drawn],
            "recent": list(self._recent),
            "since": self._started_at,
        }

    def ui_html(self) -> str:
        """One static HTML page, no scripts: the site's arming state, each
        camera's state with its countdown, who is inside, recent events."""
        snap = self.state_snapshot()
        esc = _html.escape
        now = time.time()

        def ago(ts):
            if not ts:
                return "—"
            m = max(0, int((now - ts) / 60))
            return "just now" if m == 0 else f"{m}m ago" if m < 60 else f"{m // 60}h ago"

        colour = {ARMED: "#46a758", ARMING: "#e5a000", BREACH: "#e5a000",
                  ALARM: "#e5484d", BYPASSED: "#8b8d98", DISARMED: "#8b8d98"}
        cards = []
        for row in snap["per_camera"]:
            cd = (f" · {int(row['countdown_s'])}s" if row.get("countdown_s") else "")
            warn = ("<p class='warn'>No zone drawn — the whole frame is armed, which on a "
                    "perimeter camera usually alarms on the road too.</p>"
                    if not row["drawn"] else "")
            cards.append(
                f"<section class='card'><h2>{esc(row['camera'])} "
                f"<span class='pill' style='background:{colour.get(row['state'], '#8b8d98')}'>"
                f"{esc(row['state'])}{esc(cd)}</span></h2>{warn}"
                f"<div class='dim small'>{esc(row['zone'])}</div>"
                f"<div class='stats'><div><b>{row['intruders']}</b><span class='dim'>inside</span></div>"
                f"<div><b>{row['breaches_today']}</b><span class='dim'>breaches today</span></div>"
                f"<div><b>{row['alarms_today']}</b><span class='dim'>alarms</span></div></div>"
                f"<div class='dim small'>last seen {ago(row['last'])}</div></section>")
        rows = "".join(
            f"<tr><td>{esc(i['camera'])}</td><td>{esc(i['label'])}</td>"
            f"<td>{int(i['inside_s'])}s</td><td>{esc(i['stage'])}</td></tr>"
            for i in snap["intruders"]
        )
        inside = ("<table><tr><th>Camera</th><th>Object</th><th>Inside</th><th>Stage</th></tr>"
                  + rows + "</table>") if rows else "<p class='dim'>Nothing inside a zone.</p>"
        recent_rows = "".join(
            f"<tr><td style='color:{'#e5484d' if r.get('level') in ('high', 'critical') else '#1a1a1a'}'>"
            f"{esc(str(r.get('message', '')))}</td><td>{ago(r.get('time'))}</td></tr>"
            for r in reversed(snap["recent"][-12:])
        )
        recent = ("<table><tr><th>Event</th><th>When</th></tr>" + recent_rows + "</table>"
                  ) if recent_rows else "<p class='dim'>Nothing yet.</p>"
        hours = snap["armed_hours"]
        policy = {"schedule": f"armed {hours['start']}–{hours['end']}" if hours else "armed always",
                  "always": "armed always", "manual": "manual arming",
                  "off": "watching only"}[snap["arm_mode"]]
        return f"""<title>Intrusion Detection</title>
<style>
 body {{ font: 14px system-ui, sans-serif; margin: 1.2rem; color: #1a1a1a; background: #fafafa; }}
 h1 {{ font-size: 1.1rem; margin: 0 0 .2rem }} h2 {{ font-size: .95rem; margin: 0 0 .4rem }}
 .dim {{ color: #6b6f76; font-weight: 400 }} .small {{ font-size: .8rem }} .warn {{ color: #e5a000; margin:.2rem 0 }}
 .pill {{ color: #fff; border-radius: 10px; padding: 1px 8px; font-size: .75rem; text-transform: uppercase }}
 .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: .8rem; margin: .8rem 0 }}
 .card {{ background: #fff; border: 1px solid #e0e0e0; border-radius: 6px; padding: .7rem .9rem }}
 .stats {{ display: flex; gap: 1.2rem; margin: .4rem 0 }} .stats b {{ font-size: 1.3rem; display: block }}
 table {{ border-collapse: collapse; width: 100% }}
 th, td {{ text-align: left; padding: .3rem .6rem; border-bottom: 1px solid #e0e0e0; font-size: .9rem }}
 th {{ color: #6b6f76; font-weight: 500 }}
</style>
<h1>Intrusion Detection</h1>
<div class="dim"><b>{snap['armed_count']}</b> of {snap['camera_count']} armed ·
 <b>{snap['in_alarm']}</b> in alarm · today <b>{snap['today']['breaches']}</b> breaches,
 <b>{snap['today']['alarms']}</b> alarms · {esc(policy)}
 {'· <span class="warn">manual override in force</span>' if snap['overridden'] else ''}</div>
<div class="grid">{''.join(cards) or "<p class='dim'>No cameras selected.</p>"}</div>
<h2>Inside now</h2>
{inside}
<h2 style="margin-top:1rem">Recent</h2>
{recent}
"""


Intrusion = IntrusionDetector


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point (``[project.scripts]``)."""
    return app(IntrusionDetector, load_config=load_config).run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
