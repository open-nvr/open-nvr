# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
package-delivery — what is waiting at each door, who brought it, and
who took it.

The question a person has about their doorstep is not "was a suitcase
detected" but "is my parcel still there, and if not, who has it". This
app answers that with two kinds of evidence, used for what each is good
at:

* **Tier-0, always on, for WHO and WHEN.** The platform's tracked
  detection stream already says when a person walks up to the door and
  leaves, and whether a van or a car stopped outside. That is the
  trigger: somebody left the doorstep, so the doorstep may have changed.
  It costs nothing extra — no frames are fetched and no model is run
  for the hours in which nobody comes.
* **A KAI-C skill, on demand, for WHAT.** COCO — what Tier-0 runs —
  has no package class at all. So when the trigger fires, the app takes
  ONE snapshot and asks the best skill this box has to count the
  parcels in the drawn porch zone: a dedicated package detector if one
  is registered, an object detector whose classes include boxes, a
  visual-question model ("how many parcels are on the doorstep?"), and
  only as a last resort the COCO bag classes on the Tier-0 stream as a
  stand-in. Which one is in use is shown on the page, because it decides
  how much to trust the counts.

Delivered is a count going up after someone left; collected is a count
going down. The severity of a pick-up is decided by evidence, each
reason kept and shown: a known face at the door (the doorbell app's
alerts), the same person who brought it taking it back (a courier
correcting a mis-delivery), a delivery vehicle, the expected delivery
hours, whether the site is armed away, and how soon after delivery it
vanished. No single signal is trusted alone, and a pick-up with no
identity information is reported as unknown rather than dressed up as
theft — the operator can say who it was from the page.

Packages are STATE, not events: a parcel waiting since 12:18 is one
parcel with one clock, however many times a shadow moves over it, and
reminders come on a cadence the operator sets, stop when acknowledged or
snoozed, and stop for good when it is collected.

It runs standalone on a config file or under the App Catalog, where the
cameras and the porch zones are picked and drawn.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import html as _html
import logging
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from opennvr_app_sdk import (
    Action,
    Alert,
    AlertType,
    AppManifest,
    Detector,
    Entity,
    Param,
    StateView,
    app,
)
from opennvr_app_sdk.cameras import UNIT_FRAME
from opennvr_app_sdk.config import load_yaml
from opennvr_app_sdk.geometry import Zone, bbox_center, scale_vertices

logger = logging.getLogger("package-delivery")

#: Alert kinds. A subscriber routes on these without parsing titles.
EVENT_DELIVERED = "package_delivered"
EVENT_PICKED_UP = "package_picked_up"
EVENT_TAKEN = "package_taken"
EVENT_REMINDER = "package_reminder"

#: Event kinds as the page shows them.
DELIVERED, PICKED_UP, TAKEN, REMINDER, CLEARED, FALSE_ALARM = (
    "delivered", "picked_up", "taken", "reminder", "cleared", "false_alarm")

#: Who did it, in the words a person would use.
COURIER, KNOWN, OWNER, STRANGER, UNKNOWN, OPERATOR, NOBODY = (
    "courier", "known", "owner", "stranger", "unknown", "operator", "nobody")

#: Door states as the page shows them.
CLEAR, WAITING, REMINDER_DUE, SNOOZED, ACKNOWLEDGED = (
    "clear", "waiting", "reminder-due", "snoozed", "acknowledged")

#: How parcels get counted, best first. The app picks the best one this
#: box can do right now and says so.
METHOD_PACKAGE, METHOD_OBJECT, METHOD_VQA, METHOD_PROXY, METHOD_NONE = (
    "package_detection", "object_detection", "vqa", "tier0_proxy", "none")

#: Classes a dedicated or box-capable detector might emit for a parcel.
PACKAGE_CLASSES = ("package", "parcel", "box", "cardboard box", "delivery")
# Every spelling of the VQA task the catalog treats as one capability
# (server/config/tasks.yml: canonical ``vqa``, aliases below). The shipped
# Moondream adapter advertises ``visual_qa``; matching only the canonical
# name would skip it and fall to the bag proxy.
VQA_TASKS = ("vqa", "visual_qa", "visual_question_answering")
#: What COCO offers when nothing better is installed. Tier-0 is widened
#: to these on the cameras picked for this app (``tier0_labels``).
PROXY_LABELS = ["suitcase", "backpack", "handbag"]
#: Vehicles that stop outside a door. COCO has no "van"; a van comes out
#: as a truck or a car depending on the model's mood, so both count.
VEHICLE_LABELS = ["truck", "car", "bus"]

VQA_QUESTION = (
    "How many delivery packages, parcels or cardboard boxes are on the "
    "doorstep or the ground in this picture? Answer with only a number."
)

MANIFEST = AppManifest(
    id="package-delivery",
    name="Package Delivery",
    version="1.1.0",
    category="doorstep",
    summary=(
        "What is waiting at each door, who brought it and who took it. Triggered by "
        "the people and vehicles Tier-0 already sees; the parcels are counted on "
        "demand by the best skill the box has, and every pick-up is rated by "
        "evidence — a known face, the courier, a vehicle, the hours, the site mode."
    ),
    requires_tasks=["object_detection", "multi_object_tracking"],
    # Lights the first-class Deliveries page (app/src/lib/appVerticals.ts).
    provides=["deliveries"],
    subscribes="opennvr.inference.>",
    # Widened onto the cameras picked for this app so the COCO stand-in
    # works on a stock install; a package-capable skill is used over it
    # whenever one is registered.
    tier0_labels=list(PROXY_LABELS),
    params=[
        Param("zone", "geometry.polygon", per_camera=True,
              description="The porch: draw it around the step where parcels are left. "
                          "Nothing drawn = the whole frame, which counts the plant "
                          "pots and the doormat too."),
        Param("delivery_hours", dict, default={"start": "08:00", "end": "20:00"},
              description="When deliveries are expected (local time; cross-midnight "
                          "allowed). A pick-up outside these hours by nobody known "
                          "weighs towards theft. Empty = any time."),
        Param("recheck_minutes", float, default=15.0,
              description="While parcels wait, re-count this often — catches a "
                          "collection nobody walked past the camera for. 0 disables."),
        Param("idle_recheck_minutes", float, default=60.0,
              description="With nothing waiting, re-count this often to catch a "
                          "delivery Tier-0 missed (a courier out of view). 0 disables."),
        Param("reminder_minutes", float, default=60.0,
              description="Remind about parcels still waiting this often, until "
                          "acknowledged, snoozed or collected. 0 disables."),
        Param("settle_seconds", float, default=4.0,
              description="Wait this long after someone leaves before counting, so "
                          "the count is of the doorstep and not of their back."),
        Param("courier_grace_seconds", float, default=120.0,
              description="A parcel that vanishes this soon after arriving, with the "
                          "same person still about, was taken back by the courier — "
                          "a mis-delivery, not a theft."),
        Param("quick_grab_minutes", float, default=10.0,
              description="A parcel taken this soon after delivery by someone else "
                          "is the classic follow-the-van theft; it weighs towards "
                          "taken even inside delivery hours."),
        Param("known_face_window_seconds", float, default=180.0,
              description="A known face reported at this door (Smart Doorbell's "
                          "known_visitor alert) within this window makes a pick-up "
                          "an owner pick-up."),
        Param("vehicle_window_seconds", float, default=120.0,
              description="A vehicle that stopped outside within this window is "
                          "remembered as context for the event."),
        Param("person_label", str, default="person"),
        Param("vehicle_labels", list, default=list(VEHICLE_LABELS)),
        Param("proxy_labels", list, default=list(PROXY_LABELS),
              description="COCO classes to count as parcels when no package-capable "
                          "skill is installed."),
        Param("attach_snapshot", bool, default=True),
        Param("alert_cooldown_seconds", float, default=20.0),
    ],
    emits=[
        AlertType(EVENT_DELIVERED, severity="low"),
        AlertType(EVENT_PICKED_UP, severity="low"),
        AlertType(EVENT_TAKEN, severity="high"),
        AlertType(EVENT_REMINDER, severity="low"),
    ],
    state_schema=[
        StateView("waiting_now", "Waiting now", kind="metric", path="waiting_now",
                  description="Parcels on doorsteps right now, across every camera."),
        StateView("delivered_today", "Delivered today", kind="metric", path="today.delivered"),
        StateView("picked_up_today", "Collected today", kind="metric", path="today.picked_up"),
        StateView("taken_today", "Taken today", kind="metric", path="today.taken"),
        StateView("counted_by", "Counted by", kind="metric", path="counted_by.adapter",
                  description="The skill counting parcels, chosen from what KAI-C has."),
        StateView("per_camera", "Doors", kind="table", path="per_camera",
                  description="Each door: what is waiting, since when, the last event."),
        StateView("events", "Today", kind="table", path="events"),
        StateView("recent", "Recent", kind="log", path="recent", limit=12),
    ],
    actions=[
        Action("picked_up", "Collected",
               params=[Param("camera", str, required=True)],
               description="You have it: closes the parcels waiting at this door and "
                           "stops the reminders."),
        Action("not_a_package", "Not a package",
               params=[Param("camera", str, required=True)],
               description="A plant pot, a doormat, a shadow: clears the count and "
                           "records the false alarm so the counter can be judged."),
        Action("snooze", "Snooze reminders",
               params=[Param("camera", str, required=True),
                       Param("minutes", float, default=60.0)],
               description="Stop reminders for this door for a while."),
        Action("check_now", "Check now",
               params=[Param("camera", str, required=True)],
               description="Count the doorstep right now."),
        Action("acknowledge", "Acknowledge",
               params=[Param("camera", str, required=True)],
               description="Seen it, will collect later: no more reminders for these "
                           "parcels; the next delivery starts them again."),
    ],
    # Home Assistant entities: what people ask their house — is there a
    # parcel waiting, how many, when was the last delivery — plus the
    # "collected" button so a wall panel can close it.
    entities=[
        Entity("waiting_now", "sensor", "Parcels waiting", state_path="waiting_now",
               state_class="measurement", icon="mdi:package-variant"),
        Entity("delivered_today", "sensor", "Deliveries today", state_path="today.delivered",
               state_class="total_increasing", icon="mdi:package-down"),
        Entity("taken_today", "sensor", "Parcels taken today", state_path="today.taken",
               state_class="total_increasing", icon="mdi:package-variant-remove"),
        Entity("package_present", "binary_sensor", "Parcel waiting", per_camera=True,
               state_path="per_camera[camera={camera}].waiting",
               device_class="occupancy", icon="mdi:package-variant"),
        Entity("package_count", "sensor", "Parcels waiting", per_camera=True,
               state_path="per_camera[camera={camera}].count",
               state_class="measurement", icon="mdi:package-variant"),
        Entity("last_delivery", "sensor", "Last delivery", per_camera=True,
               state_path="per_camera[camera={camera}].last_delivery_iso",
               device_class="timestamp", icon="mdi:package-down"),
        Entity("collected", "button", "Parcel collected", per_camera=True,
               action="picked_up", icon="mdi:package-check"),
        Entity("acknowledge", "button", "Acknowledge parcels", per_camera=True,
               action="acknowledge"),
    ],
    has_ui=True,
)


# ── Config ──────────────────────────────────────────────────────────


@dataclass
class DailyHours:
    """A daily window in local time; cross-midnight supported."""
    start: _dt.time
    end: _dt.time

    def contains(self, when: _dt.datetime) -> bool:
        t = when.time()
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end

    def as_dict(self) -> dict[str, str]:
        return {"start": self.start.strftime("%H:%M"), "end": self.end.strftime("%H:%M")}

    @classmethod
    def parse(cls, raw: Any) -> "DailyHours | None":
        if not isinstance(raw, dict):
            return None
        s, e = str(raw.get("start") or "").strip(), str(raw.get("end") or "").strip()
        if not s or not e:
            return None
        try:
            return cls(_dt.time.fromisoformat(s), _dt.time.fromisoformat(e))
        except ValueError as exc:
            raise ValueError(f"delivery_hours must be HH:MM start/end: {exc}") from None


@dataclass
class CameraWatch:
    """One door: its porch zone and the pixel space it was drawn in."""
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
    kaic_url: str | None = None
    kaic_api_key: str | None = None
    # ── the rule (all live-editable) ──
    person_label: str = "person"
    vehicle_labels: list[str] = field(default_factory=lambda: list(VEHICLE_LABELS))
    proxy_labels: list[str] = field(default_factory=lambda: list(PROXY_LABELS))
    delivery_hours: DailyHours | None = None
    recheck_minutes: float = 15.0
    idle_recheck_minutes: float = 60.0
    reminder_minutes: float = 60.0
    settle_seconds: float = 4.0
    courier_grace_seconds: float = 120.0
    quick_grab_minutes: float = 10.0
    known_face_window_seconds: float = 180.0
    vehicle_window_seconds: float = 120.0
    attach_snapshot: bool = True
    alert_cooldown_seconds: float = 20.0
    track_ttl_seconds: float = 10.0
    consume_tier0: bool = False
    auto_cameras: bool = False


def _camera_key(raw_key: object, known: dict[str, Any]) -> str | None:
    """The catalog keys per-camera values by the numeric id (``"3"``),
    the app by the handle (``"cam3"``); hand-written config may use
    either."""
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
        return Zone.from_config(name="porch",
                                vertices=scale_vertices(drawn, cam.frame_width,
                                                        cam.frame_height))
    except (TypeError, ValueError, IndexError):
        return None


def _whole_frame(cam_id: str, w: int, h: int) -> CameraWatch:
    return CameraWatch(cam_id, Zone.from_config("whole frame", [[0, 0], [w, 0], [w, h], [0, h]]),
                       w, h, drawn=False)


def _labels(raw: Any, default: list[str]) -> list[str]:
    if raw is None:
        return list(default)
    out = [str(s).strip().lower() for s in raw if str(s).strip()]
    return out or list(default)


def _knobs_from(raw: dict[str, Any], base: AppConfig | None = None) -> dict[str, Any]:
    """The live-editable knobs, parsed and validated from a config dict."""
    d = base
    out: dict[str, Any] = {}

    def _get(key, default):
        if key in raw and raw[key] is not None:
            return raw[key]
        return getattr(d, key) if d is not None else default

    out["person_label"] = str(_get("person_label", "person")).strip().lower() or "person"
    out["vehicle_labels"] = _labels(_get("vehicle_labels", None), VEHICLE_LABELS)
    out["proxy_labels"] = _labels(_get("proxy_labels", None), PROXY_LABELS)
    try:
        out["recheck_minutes"] = max(0.0, float(_get("recheck_minutes", 15.0)))
        out["idle_recheck_minutes"] = max(0.0, float(_get("idle_recheck_minutes", 60.0)))
        out["reminder_minutes"] = max(0.0, float(_get("reminder_minutes", 60.0)))
        out["settle_seconds"] = max(0.0, float(_get("settle_seconds", 4.0)))
        out["courier_grace_seconds"] = max(0.0, float(_get("courier_grace_seconds", 120.0)))
        out["quick_grab_minutes"] = max(0.0, float(_get("quick_grab_minutes", 10.0)))
        out["known_face_window_seconds"] = max(
            0.0, float(_get("known_face_window_seconds", 180.0)))
        out["vehicle_window_seconds"] = max(0.0, float(_get("vehicle_window_seconds", 120.0)))
        out["alert_cooldown_seconds"] = max(0.0, float(_get("alert_cooldown_seconds", 20.0)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"config: numeric knob malformed: {exc}") from None
    out["attach_snapshot"] = bool(_get("attach_snapshot", True))
    if "delivery_hours" in raw:
        out["delivery_hours"] = DailyHours.parse(raw.get("delivery_hours"))
    elif d is not None:
        out["delivery_hours"] = d.delivery_hours
    else:
        out["delivery_hours"] = DailyHours.parse({"start": "08:00", "end": "20:00"})
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

    # The 1.0 app called the porch ``roi``; the catalog draws ``zone``.
    zones_override = raw.get("zone", raw.get("zones", raw.get("roi")))
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
                raise ValueError("frame_width and frame_height must be > 0")
            cam = _whole_frame(camera_id, frame_width, frame_height)
            drawn = None
            for raw_key, val in zone_map.items():
                if _camera_key(raw_key, {camera_id: cam}) == camera_id:
                    drawn = val
                    break
            zone = _zone_from_drawn(drawn, cam)
            polygon = c.get("zone", c.get("roi"))
            if zone is None and polygon:
                zone = Zone.from_config(name="porch",
                                        vertices=scale_vertices(polygon, frame_width,
                                                                frame_height))
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
        kaic_url=str(raw["kaic_url"]) if raw.get("kaic_url") else None,
        kaic_api_key=str(raw["kaic_api_key"]) if raw.get("kaic_api_key") else None,
        track_ttl_seconds=float(raw.get("track_ttl_seconds", 10.0)),
        consume_tier0=bool(raw.get("consume_tier0", False)),
        auto_cameras=auto_cameras,
        **knobs,
    )


# ── Counting parcels: the best skill this box has ───────────────────


@dataclass
class Count:
    """One answer to "how many parcels are on this doorstep?"."""
    count: int
    method: str
    adapter: str | None = None
    task: str | None = None
    confidence: float | None = None


@dataclass
class Counter:
    """Which skill counts parcels, chosen from what KAI-C has registered.

    Registered adapters come from ``ai.capabilities()``; the choice is
    re-made every ``ttl`` seconds because skills come and go (an adapter
    restarted, a package model installed this afternoon). The page shows
    the choice, since it decides how far the counts can be trusted: a
    dedicated package detector is good, a VQA model is fair, the COCO
    bag classes are a stand-in, and nothing at all is nothing.
    """
    method: str = METHOD_NONE
    adapter: str | None = None
    task: str | None = None
    labels: tuple[str, ...] = ()
    checked_at: float = 0.0
    ttl: float = 300.0

    @property
    def quality(self) -> str:
        return {METHOD_PACKAGE: "good", METHOD_OBJECT: "good", METHOD_VQA: "fair",
                METHOD_PROXY: "proxy"}.get(self.method, "none")

    def describe(self, cfg: AppConfig) -> str:
        what = {
            METHOD_PACKAGE: f"a package detector ({self.adapter})",
            METHOD_OBJECT: f"{self.adapter} (object detection with a box class)",
            METHOD_VQA: f"{self.adapter} (visual question answering)",
            METHOD_PROXY: "the COCO bag classes on the Tier-0 stream — no package-capable "
                          "skill is registered, so suitcases, backpacks and handbags "
                          "stand in for parcels",
            METHOD_NONE: "nothing — no skill on this box can count parcels and Tier-0 is "
                         "not being consumed for the stand-in classes",
        }[self.method]
        cadence = []
        if cfg.recheck_minutes > 0:
            cadence.append(f"every {int(cfg.recheck_minutes)} min while parcels wait")
        if cfg.idle_recheck_minutes > 0:
            cadence.append(f"every {int(cfg.idle_recheck_minutes)} min otherwise")
        return (f"Counted by {what} when someone leaves the doorstep"
                + (", " + " and ".join(cadence) if cadence else "") + ".")

    def choose(self, capabilities: dict[str, Any] | None, now: float) -> None:
        """Pick the best method from a ``/capabilities`` body (either the
        list or the dict shape KAI-C has used), skipping adapters that
        report themselves unhealthy."""
        self.checked_at = now
        best: tuple[int, str, str | None, str | None, tuple[str, ...]] = (
            0, METHOD_PROXY, None, None, ())
        for name, entry in _adapters(capabilities):
            nested = entry.get("capabilities")
            contract = nested if isinstance(nested, dict) else entry
            tasks = {str(t).lower() for t in
                     (contract.get("tasks_advertised") or contract.get("tasks") or [])}
            if entry.get("healthy", contract.get("healthy", True)) is False:
                continue
            classes = {str(c).lower() for c in
                       (contract.get("labels") or contract.get("classes") or [])}
            if "package_detection" in tasks:
                cand = (3, METHOD_PACKAGE, name, "package_detection", tuple(sorted(classes)))
            elif "object_detection" in tasks and classes & set(PACKAGE_CLASSES):
                cand = (2, METHOD_OBJECT, name, "object_detection",
                        tuple(sorted(classes & set(PACKAGE_CLASSES))))
            elif tasks & set(VQA_TASKS):
                # ask with the name the adapter itself advertises
                spelled = next(t for t in VQA_TASKS if t in tasks)
                cand = (1, METHOD_VQA, name, spelled, ())
            else:
                continue
            if cand[0] > best[0]:
                best = cand
        _, self.method, self.adapter, self.task, self.labels = best

    def stale(self, now: float) -> bool:
        return (now - self.checked_at) >= self.ttl


def _adapters(capabilities: dict[str, Any] | None) -> list[tuple[str, dict[str, Any]]]:
    raw = (capabilities or {}).get("adapters")
    out: list[tuple[str, dict[str, Any]]] = []
    if isinstance(raw, dict):
        for name, entry in raw.items():
            if isinstance(entry, dict):
                out.append((str(name), entry))
    elif isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                out.append((str(entry.get("name") or entry.get("adapter") or "?"), entry))
    return out


_NUMBER_WORDS = {"zero": 0, "no": 0, "none": 0, "one": 1, "a": 1, "an": 1, "two": 2,
                 "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}


def parse_count(answer: Any) -> int | None:
    """A VQA model answers in words as often as in digits ("There are two
    boxes.", "No packages"). Take the first number it commits to; None
    when it does not commit at all, which the caller treats as "could
    not count" rather than as zero."""
    text = str(answer or "").strip().lower()
    if not text:
        return None
    m = re.search(r"\d+", text)
    if m:
        return min(int(m.group()), 50)
    for word in re.findall(r"[a-z]+", text):
        if word in _NUMBER_WORDS:
            return _NUMBER_WORDS[word]
    return None


# ── The app ─────────────────────────────────────────────────────────


@dataclass
class Door:
    """One camera's doorstep, as state."""
    count: int = 0
    present_since: float = 0.0
    last_delivery: float = 0.0
    last_check: float = 0.0
    last_check_reason: str | None = None
    last_count: Count | None = None
    before: str | None = None          # evidence path of the last check's snapshot
    pending_check: tuple[float, str] | None = None   # (due at, reason)
    next_reminder: float = 0.0
    snoozed_until: float = 0.0
    acked: bool = False
    last_event: dict[str, Any] | None = None
    last_alert: float = 0.0
    #: track -> {first, last (event time), wall (wall clock), in_zone}; the
    #: last one to leave is the courier or the collector.
    people: dict[str, dict[str, Any]] = field(default_factory=dict)
    vehicles: deque = field(default_factory=lambda: deque(maxlen=20))   # (label, first, last)
    delivered_by: str = ""             # track of the person who brought the last parcel
    delivered_at: float = 0.0
    proxy_seen: dict[str, float] = field(default_factory=dict)   # stationary bags in zone


def _day_blank() -> dict[str, int]:
    return {"delivered": 0, "picked_up": 0, "taken": 0, "reminders": 0, "checks": 0,
            "false_alarms": 0}


class PackageDeliveryDetector(Detector):
    """Tier-0 for who and when; a KAI-C skill for what; evidence for who."""

    manifest = MANIFEST

    def setup(self) -> None:
        self._doors: dict[str, Door] = {cam: Door() for cam in self.cfg.cameras}
        self._counter = Counter()
        self._today: dict[str, int] = _day_blank()
        self._today_key: str = self._now_local().date().isoformat()
        self._events: deque[dict[str, Any]] = deque(maxlen=50)
        self._recent: deque[dict[str, Any]] = deque(maxlen=50)
        self._known: deque[tuple[str, str, float]] = deque(maxlen=100)   # (camera, name, ts)
        self._site_mode: dict[str, Any] | None = None
        self._site_mode_at: float = 0.0
        self._started_at = time.time()
        self._nvr: Any = None
        self._nvr_tried = False
        self._unknown_cameras: set[str] = set()
        self._last_config: dict[str, Any] | None = None
        self._warned_no_track = False
        # The loop runs checks off the event loop (a frame fetch and a
        # model call); everywhere else — tests, a /state poll — they run
        # inline.
        self._defer_checks = False
        for cam in self._doors:
            self._doors[cam].pending_check = (self._started_at, "startup")

    # ── time ──

    def _now_local(self) -> _dt.datetime:
        return _dt.datetime.now()

    def _roll_day(self) -> None:
        key = self._now_local().date().isoformat()
        if key != self._today_key:
            self._today_key = key
            self._today = _day_blank()

    def _in_delivery_hours(self, when: _dt.datetime | None = None) -> bool:
        hours = self.cfg.delivery_hours
        return True if hours is None else hours.contains(when or self._now_local())

    def _note(self, camera_id: str, message: str, level: str, now: float, **extra: Any) -> None:
        self._recent.append({"message": f"{camera_id}: {message}", "time": now,
                             "level": level, "camera": camera_id, **extra})

    # ── the platform ──

    def _platform(self):
        if self._nvr is None and not self._nvr_tried:
            self._nvr_tried = True
            try:
                from opennvr_app_sdk.client import OpenNVR
                self._nvr = OpenNVR(self.cfg.opennvr_url or None, timeout=8.0,
                                    kaic_url=self.cfg.kaic_url or None,
                                    kaic_api_key=self.cfg.kaic_api_key or None)
            except Exception as exc:
                logger.info("no platform client: %s", exc)
        return self._nvr

    def refresh_counter(self, now: float | None = None, capabilities: Any = None) -> Counter:
        """Re-choose the counting skill from KAI-C's registry. Tests pass
        ``capabilities`` directly; the loop asks the platform."""
        now = time.time() if now is None else now
        if capabilities is None:
            nvr = self._platform()
            if nvr is not None:
                try:
                    capabilities = nvr.ai.capabilities()
                except Exception as exc:
                    logger.info("KAI-C capabilities unavailable: %s", exc)
        self._counter.choose(capabilities, now)
        if self._counter.method == METHOD_PROXY and not self.cfg.consume_tier0:
            # Without the Tier-0 stream there are no bag tracks to stand in.
            self._counter.method = METHOD_NONE
        return self._counter

    def _site(self, now: float) -> dict[str, Any] | None:
        """The site mode (disarmed / armed_home / armed_away), cached for
        30 s. A site that never set one reads as armed_away by default,
        which says nothing about anybody being out — so only a mode that
        was actually set is evidence (``changed_at`` present)."""
        if now - self._site_mode_at < 30.0:
            return self._site_mode
        self._site_mode_at = now
        nvr = self._platform()
        if nvr is None or not hasattr(nvr, "site_mode"):
            return self._site_mode
        try:
            self._site_mode = nvr.site_mode()
        except Exception:
            pass
        return self._site_mode

    # ── evidence about people ──

    def note_known_visitor(self, camera_id: str, name: str, ts: float) -> None:
        """A known face at this door, from Smart Doorbell's alerts."""
        self._known.append((camera_id, name, ts))

    def on_alert_envelope(self, alert: dict[str, Any]) -> None:
        """Alerts on the bus from other apps. Only a known visitor at one
        of our doors is evidence here; everything else is ignored."""
        try:
            kind = str(alert.get("alert_type") or alert.get("event_kind") or "")
            if kind != "known_visitor":
                return
            cam = str(alert.get("camera_id") or "")
            if cam not in self.cfg.cameras:
                return
            ev = alert.get("evidence") or {}
            name = str(ev.get("name") or ev.get("person") or ev.get("label")
                       or alert.get("title") or "known person")
            self.note_known_visitor(cam, name, time.time())
        except Exception:
            logger.debug("ignoring malformed alert", exc_info=True)

    def _known_face(self, camera_id: str, now: float) -> str | None:
        window = self.cfg.known_face_window_seconds
        if window <= 0:
            return None
        for cam, name, ts in reversed(self._known):
            if cam == camera_id and now - ts <= window:
                return name
        return None

    def _vehicle_near(self, door: Door, now: float) -> str | None:
        window = self.cfg.vehicle_window_seconds
        for label, _first, last in reversed(door.vehicles):
            if now - last <= window:
                return label
        return None

    def _leaver_window(self) -> float:
        """How long after leaving a person can still be "the one who just
        left": long enough for the settle delay and a courier's second
        trip, short enough that a scheduled re-count an hour later does
        not blame whoever walked past this morning."""
        return max(self.cfg.courier_grace_seconds, self.cfg.settle_seconds + 60.0)

    def _last_leaver(self, door: Door, now: float) -> tuple[str, float] | None:
        """The person who most recently left the porch: (track, wall)."""
        best: tuple[str, float] | None = None
        for track, p in door.people.items():
            if not (p.get("in_zone") and p.get("gone")):
                continue
            if now - float(p["wall"]) > self._leaver_window():
                continue
            if best is None or p["wall"] > best[1]:
                best = (track, float(p["wall"]))
        return best

    # ── Tier-0: who came and went ──

    def on_detections(self, camera_id: str, detections: list[dict[str, Any]],
                      event: dict[str, Any]) -> list[Alert]:
        cam = self.cfg.cameras.get(camera_id)
        if cam is None:
            if camera_id not in self._unknown_cameras:
                self._unknown_cameras.add(camera_id)
                logger.info("events from %s ignored — not one of this app's cameras (%s)",
                            camera_id, sorted(self.cfg.cameras) or "none")
            return []
        door = self._doors.setdefault(camera_id, Door())
        now = time.time()
        event_ts = self.parse_event_ts(event.get("completed_at")) or now
        fired = self.tick(now)

        seen_people: set[str] = set()
        seen_vehicles: dict[str, bool] = {}
        proxy_now: set[str] = set()
        for det in detections:
            if not isinstance(det, dict):
                continue
            label = str(det.get("label", "")).lower()
            bbox = det.get("bbox")
            track = det.get("track_id")
            if not isinstance(bbox, dict):
                continue
            centre = bbox_center(bbox, cam.frame_width, cam.frame_height)
            inside = cam.zone.contains(centre)
            if label == self.cfg.person_label:
                if track is None:
                    if not self._warned_no_track:
                        logger.warning("detections carry no track_id — a person cannot be "
                                       "followed to the door and back. Consume Tier-0.")
                        self._warned_no_track = True
                    continue
                t = str(track)
                seen_people.add(t)
                p = door.people.setdefault(t, {"first": event_ts, "last": event_ts,
                                               "in_zone": False, "wall": now, "gone": False})
                p["last"], p["wall"], p["gone"] = event_ts, now, False
                # Once at the door, always "was at the door" for this visit.
                p["in_zone"] = bool(p["in_zone"]) or inside
            elif label in self.cfg.vehicle_labels:
                seen_vehicles[label] = True
            elif label in self.cfg.proxy_labels and inside and track is not None:
                if det.get("stationary") is not False:
                    proxy_now.add(str(track))

        # Vehicles: one sighting per stop, remembered with its last time.
        for label in seen_vehicles:
            if door.vehicles and door.vehicles[-1][0] == label \
                    and event_ts - door.vehicles[-1][2] <= self.cfg.track_ttl_seconds:
                door.vehicles[-1] = (label, door.vehicles[-1][1], event_ts)
            else:
                door.vehicles.append((label, event_ts, event_ts))

        # People who were at the door and are no longer in the frame have
        # left: the doorstep may have changed. One settled check per door.
        left = False
        for t, p in door.people.items():
            if t in seen_people or not p["in_zone"] or p.get("gone"):
                continue
            p["gone"] = True
            p["wall"] = now
            left = True
        if left:
            self._schedule(door, now + self.cfg.settle_seconds, "person-left")

        # The stand-in: stationary bags on the Tier-0 stream, when that is
        # all this box has. A change there is a delivery or a pick-up too.
        if self._counter.method == METHOD_PROXY:
            for t in proxy_now:
                door.proxy_seen[t] = now
            for t in [t for t, ts in door.proxy_seen.items()
                      if now - ts > self.cfg.track_ttl_seconds]:
                door.proxy_seen.pop(t, None)
            if len(door.proxy_seen) != door.count and door.pending_check is None:
                self._schedule(door, now + self.cfg.settle_seconds, "scheduled")
        return fired

    @staticmethod
    def _schedule(door: Door, due: float, reason: str) -> None:
        if door.pending_check is None or due < door.pending_check[0]:
            door.pending_check = (due, reason)
        elif reason == "person-left":
            door.pending_check = (door.pending_check[0], reason)

    # ── the sweep ──

    def tick(self, now: float | None = None) -> list[Alert]:
        """Schedule re-counts, run the checks that are due (unless the
        loop owns them), and send the reminders that are due."""
        now = time.time() if now is None else now
        self._roll_day()
        if self._counter.stale(now):
            self.refresh_counter(now)
        fired: list[Alert] = []
        keep_for = max(self.cfg.track_ttl_seconds * 3, self._leaver_window())
        for cam_id, door in self._doors.items():
            if cam_id not in self.cfg.cameras:
                continue
            for t in [t for t, p in door.people.items() if now - float(p["wall"]) > keep_for]:
                door.people.pop(t, None)
            if door.pending_check is None and door.last_check:
                period = (self.cfg.recheck_minutes if door.count > 0
                          else self.cfg.idle_recheck_minutes) * 60.0
                if period > 0 and now - door.last_check >= period:
                    door.pending_check = (now, "scheduled")
            if not self._defer_checks and door.pending_check is not None \
                    and now >= door.pending_check[0]:
                _due, reason = door.pending_check
                door.pending_check = None
                result = self._run_check(cam_id, reason, now)
                if result and result.get("alert") is not None:
                    fired.append(result["alert"])
            alert = self._reminder_due(cam_id, door, now)
            if alert is not None:
                fired.append(alert)
        return fired

    def _reminder_due(self, cam_id: str, door: Door, now: float) -> Alert | None:
        period = self.cfg.reminder_minutes * 60.0
        if period <= 0 or door.count <= 0 or door.acked or now < door.snoozed_until:
            return None
        if not door.next_reminder:
            door.next_reminder = (door.present_since or now) + period
        if now < door.next_reminder:
            return None
        door.next_reminder = now + period
        self._today["reminders"] += 1
        waited = now - door.present_since if door.present_since else 0.0
        ev = self._event(cam_id, REMINDER, now, who={"kind": NOBODY, "name": None, "reasons": []},
                         before=door.count, after=door.count, severity="low",
                         method=door.last_count.method if door.last_count else METHOD_NONE,
                         dwell=waited)
        title = (f"{door.count} parcel{'s' if door.count != 1 else ''} still waiting at "
                 f"{cam_id} ({_hm(waited)})")
        self._note(cam_id, title, "low", now)
        return self._alert(cam_id, door, EVENT_REMINDER, "low", title,
                           f"Waiting since {_clock(door.present_since)}. Collect it, or "
                           f"acknowledge or snooze the reminders on the Deliveries page.",
                           ev, now)

    # ── one check: snapshot, count, compare ──

    def _run_check(self, cam_id: str, reason: str, now: float) -> dict[str, Any] | None:
        count, after_path = self.fetch_and_count(cam_id)
        return self.apply_check(cam_id, reason, now, count, after_path)

    def fetch_and_count(self, cam_id: str) -> tuple[Count | None, str | None]:
        """The blocking half: a frame and a model call. Returns the count
        (None when nothing could count) and the saved snapshot's path."""
        door = self._doors[cam_id]
        cam = self.cfg.cameras[cam_id]
        jpeg: bytes | None = None
        nvr = self._platform()
        if self._counter.method in (METHOD_PACKAGE, METHOD_OBJECT, METHOD_VQA) and nvr is not None:
            try:
                jpeg = nvr.snapshot(cam_id)
            except Exception as exc:
                logger.info("snapshot for %s failed: %s", cam_id, exc)
        count = self.count_parcels(cam, jpeg, door)
        return count, (self._save(nvr, jpeg) if count is not None else None)

    def apply_check(self, cam_id: str, reason: str, now: float, count: Count | None,
                    after_path: str | None) -> dict[str, Any] | None:
        """The state half: record the check and apply its count."""
        door = self._doors[cam_id]
        door.last_check = now
        door.last_check_reason = reason
        self._today["checks"] += 1
        if count is None:
            self._note(cam_id, f"could not count the doorstep ({reason})", "info", now)
            return None
        result = self.apply_count(cam_id, count, now, after_path=after_path)
        door.before = after_path or door.before
        door.last_count = count
        return result

    def count_parcels(self, cam: CameraWatch, jpeg: bytes | None, door: Door) -> Count | None:
        """Ask the chosen skill. None = could not count (no frame, adapter
        down, an answer with no number in it) — never a silent zero."""
        c = self._counter
        if c.method == METHOD_PROXY:
            return Count(len(door.proxy_seen), METHOD_PROXY, "tier0", "object_detection")
        if c.method == METHOD_NONE or jpeg is None:
            return None
        nvr = self._platform()
        if nvr is None:
            return None
        handle = cam.camera_id
        try:
            if c.method == METHOD_VQA:
                body = nvr.ai.infer(c.adapter, jpeg, task=c.task or "vqa", camera_id=handle,
                                    params={"question": VQA_QUESTION, "prompt": VQA_QUESTION})
                result = body.get("result", body) if isinstance(body, dict) else {}
                n = parse_count(result.get("answer") or result.get("text")
                                or result.get("caption") or body.get("answer"))
                if n is None:
                    return None
                conf = result.get("confidence")
                return Count(n, METHOD_VQA, c.adapter, c.task or "vqa",
                             float(conf) if isinstance(conf, (int, float)) else None)
            body = nvr.ai.infer(c.adapter, jpeg, task=c.task or "object_detection",
                                camera_id=handle)
            result = body.get("result", body) if isinstance(body, dict) else {}
            dets = result.get("detections") or body.get("detections") or []
            wanted = set(c.labels) if c.method == METHOD_OBJECT else set(PACKAGE_CLASSES)
            n, confs = 0, []
            for det in dets:
                if not isinstance(det, dict):
                    continue
                label = str(det.get("label", "")).lower()
                if c.method == METHOD_OBJECT and label not in wanted:
                    continue
                if c.method == METHOD_PACKAGE and label not in wanted \
                        and not any(w in label for w in ("package", "parcel", "box")):
                    continue
                bbox = det.get("bbox")
                if isinstance(bbox, dict):
                    centre = bbox_center(bbox, cam.frame_width, cam.frame_height)
                    if not cam.zone.contains(centre):
                        continue
                n += 1
                conf = det.get("confidence", det.get("score"))
                if isinstance(conf, (int, float)):
                    confs.append(float(conf))
            return Count(n, c.method, c.adapter, c.task,
                         (sum(confs) / len(confs)) if confs else None)
        except Exception as exc:
            logger.info("%s could not count parcels on %s: %s", c.adapter, handle, exc)
            return None

    def _save(self, nvr, jpeg: bytes | None) -> str | None:
        if not jpeg or nvr is None or not self.cfg.attach_snapshot:
            return None
        try:
            return nvr.save_evidence(jpeg)
        except Exception as exc:
            logger.info("evidence save failed: %s", exc)
            return None

    def apply_count(self, cam_id: str, count: Count, now: float, *,
                    after_path: str | None = None) -> dict[str, Any]:
        """The state machine: this count against the last one."""
        door = self._doors[cam_id]
        before, after = door.count, count.count
        images = {k: v for k, v in (("before", door.before), ("after", after_path)) if v}
        alert: Alert | None = None
        ev: dict[str, Any] | None = None
        if after > before:
            who = self._who_brought(cam_id, door, now)
            n = after - before
            door.count = after
            door.present_since = door.present_since or now
            door.last_delivery = now
            door.acked = False
            door.snoozed_until = 0.0
            door.next_reminder = now + self.cfg.reminder_minutes * 60.0
            leaver = self._last_leaver(door, now)
            door.delivered_by, door.delivered_at = (leaver[0] if leaver else ""), now
            self._today["delivered"] += n
            ev = self._event(cam_id, DELIVERED, now, who=who, before=before, after=after,
                             severity="low", method=count.method, confidence=count.confidence,
                             vehicle=self._vehicle_near(door, now))
            title = f"{n} parcel{'s' if n != 1 else ''} delivered at {cam_id}"
            if who["name"]:
                title += f" by {who['name']}"
            self._note(cam_id, title, "low", now)
            alert = self._alert(cam_id, door, EVENT_DELIVERED, "low", title,
                                _sentence(who, "left", n) + " " + _because(who), ev, now, images)
        elif after < before:
            who, severity = self._who_took(cam_id, door, now)
            waited = (now - door.present_since) if door.present_since else None
            kind = TAKEN if severity == "high" else PICKED_UP
            n = before - after
            door.count = after
            if after == 0:
                door.present_since = 0.0
                door.next_reminder = 0.0
                door.acked = False
            self._today[kind] += n
            ev = self._event(cam_id, kind, now, who=who, before=before, after=after,
                             severity=severity, method=count.method,
                             confidence=count.confidence, vehicle=self._vehicle_near(door, now),
                             dwell=waited)
            verb = "taken from" if kind == TAKEN else "collected from"
            title = f"{n} parcel{'s' if n != 1 else ''} {verb} {cam_id}"
            if who["name"]:
                title += f" by {who['name']}"
            self._note(cam_id, title, severity, now)
            alert = self._alert(cam_id, door, EVENT_TAKEN if kind == TAKEN else EVENT_PICKED_UP,
                                severity, title,
                                _sentence(who, "took", n) + " " + _because(who)
                                + (f" It had waited {_hm(waited)}." if waited else ""),
                                ev, now, images)
        return {"before": before, "after": after, "event": ev, "alert": alert}

    # ── who ──

    def _who_brought(self, cam_id: str, door: Door, now: float) -> dict[str, Any]:
        reasons: list[str] = []
        name = self._known_face(cam_id, now)
        vehicle = self._vehicle_near(door, now)
        leaver = self._last_leaver(door, now)
        if vehicle:
            reasons.append(f"a {vehicle} stopped outside {_ago(now - door.vehicles[-1][2])}")
        if leaver:
            reasons.append(f"a person left the doorstep {_ago(now - leaver[1])}")
        if name:
            reasons.append(f"known face: {name}")
            return {"kind": KNOWN, "name": name, "reasons": reasons}
        reasons.append("within delivery hours" if self._in_delivery_hours()
                       else "outside delivery hours")
        if leaver or vehicle:
            return {"kind": COURIER, "name": None, "reasons": reasons}
        reasons.append("nobody was seen leaving it")
        return {"kind": UNKNOWN, "name": None, "reasons": reasons}

    def _who_took(self, cam_id: str, door: Door, now: float) -> tuple[dict[str, Any], str]:
        """Who has the parcel, and how loudly to say so. Each signal is
        kept as a reason; the severity is the sum, not any one of them."""
        reasons: list[str] = []
        name = self._known_face(cam_id, now)
        leaver = self._last_leaver(door, now)
        vehicle = self._vehicle_near(door, now)
        since_delivery = now - door.delivered_at if door.delivered_at else None

        if name:
            reasons.append(f"known face: {name}")
            return {"kind": KNOWN, "name": name, "reasons": reasons}, "low"

        if leaver and door.delivered_by and leaver[0] == door.delivered_by \
                and since_delivery is not None \
                and since_delivery <= self.cfg.courier_grace_seconds:
            reasons.append("the same person who brought it took it back")
            return {"kind": COURIER, "name": None, "reasons": reasons}, "low"

        score = 0
        if leaver:
            reasons.append(f"a person left the doorstep {_ago(now - leaver[1])}")
        else:
            reasons.append("nobody was seen taking it")
        if not self._in_delivery_hours():
            reasons.append("outside delivery hours")
            score += 2
        else:
            reasons.append("within delivery hours")
        if since_delivery is not None and self.cfg.quick_grab_minutes > 0 \
                and since_delivery <= self.cfg.quick_grab_minutes * 60.0 and leaver:
            reasons.append(f"taken {_hm(since_delivery)} after delivery, by someone else")
            score += 2
        site = self._site(now)
        if isinstance(site, dict) and site.get("changed_at"):
            mode = str(site.get("mode") or "")
            if mode == "armed_away":
                reasons.append("the site is armed away")
                score += 2
            elif mode == "disarmed":
                reasons.append("the site is disarmed — somebody is home")
                score -= 1
        if vehicle:
            reasons.append(f"a {vehicle} stopped outside {_ago(now - door.vehicles[-1][2])}")
        if not leaver:
            # Nothing seen: wind, a courier out of view, a count that
            # wobbled. Not an accusation.
            return {"kind": NOBODY, "name": None, "reasons": reasons}, "low"
        reasons.append("no known face matched")
        if score >= 2:
            return {"kind": STRANGER, "name": None, "reasons": reasons}, "high"
        return {"kind": UNKNOWN, "name": None, "reasons": reasons}, "low"

    # ── events and alerts ──

    def _event(self, cam_id: str, kind: str, now: float, *, who: dict[str, Any],
               before: int, after: int, severity: str, method: str,
               confidence: float | None = None, vehicle: str | None = None,
               dwell: float | None = None) -> dict[str, Any]:
        ev = {
            "id": f"e-{uuid.uuid4().hex[:10]}",
            "camera": cam_id, "kind": kind, "time": now, "severity": severity,
            "who": who, "count_before": before, "count_after": after,
            "method": method,
            "confidence": round(confidence, 3) if confidence is not None else None,
            "vehicle": vehicle,
            "dwell_seconds": round(dwell, 1) if dwell is not None else None,
            "acked": False,
        }
        self._events.appendleft(ev)
        self._doors[cam_id].last_event = ev
        return ev

    def _alert(self, cam_id: str, door: Door, kind: str, severity: str, title: str,
               description: str, ev: dict[str, Any], now: float,
               images: dict[str, str] | None = None) -> Alert | None:
        cd = self.cfg.alert_cooldown_seconds
        if kind != EVENT_TAKEN and cd > 0 and door.last_alert and now - door.last_alert < cd:
            return None
        door.last_alert = now
        cam = self.cfg.cameras[cam_id]
        return Alert(
            title=title, description=description, camera_id=cam_id, severity=severity,
            alert_type=kind,
            evidence={
                "event_kind": ev["kind"], "who": ev["who"]["kind"], "name": ev["who"]["name"],
                "reasons": ev["who"]["reasons"], "count_before": ev["count_before"],
                "count_after": ev["count_after"], "method": ev["method"],
                "confidence": ev["confidence"], "vehicle": ev["vehicle"],
                "dwell_seconds": ev["dwell_seconds"], "zone_name": cam.zone.name,
                "event_id": ev["id"],
            },
            images=images or {},
            tags=[kind, ev["who"]["kind"], cam.zone.name],
        )

    # ── the loop ──

    async def run(self, *, once: bool = False) -> None:
        tasks: list[asyncio.Task] = []
        if not once:
            self._defer_checks = True
            tasks.append(asyncio.create_task(self._tick_loop()))
            tasks.append(asyncio.create_task(self._alerts_loop()))
        try:
            await super().run(once=once)
        finally:
            for t in tasks:
                t.cancel()

    async def _tick_loop(self) -> None:
        """Once a second: reminders and scheduling on the loop; the
        checks — a frame fetch and a model call — in a thread, one door
        at a time, applied back on the loop."""
        while True:
            await asyncio.sleep(1.0)
            try:
                now = time.time()
                for alert in self.tick(now):
                    self._dispatcher.fire(alert)
                for cam_id, door in list(self._doors.items()):
                    if cam_id not in self.cfg.cameras or door.pending_check is None \
                            or now < door.pending_check[0]:
                        continue
                    _due, reason = door.pending_check
                    door.pending_check = None
                    count, path = await asyncio.to_thread(self.fetch_and_count, cam_id)
                    result = self.apply_check(cam_id, reason, time.time(), count, path)
                    if result and result.get("alert") is not None:
                        self._dispatcher.fire(result["alert"])
            except Exception:
                logger.warning("doorstep sweep failed", exc_info=True)

    async def _alerts_loop(self) -> None:
        """Listen for other apps' alerts (Smart Doorbell's known visitor)
        on the bus. Optional: with no alerts bus the app simply has one
        signal fewer."""
        url = self.cfg.nats_alerts_url or self.cfg.nats_url
        if not url:
            return
        try:
            import json

            import nats
            nc = await nats.connect(url, token=self.cfg.nats_alerts_token or self.cfg.nats_token)
            sub = await nc.subscribe(f"{self.cfg.nats_alerts_subject_prefix}.>")
            async for msg in sub.messages:
                try:
                    self.on_alert_envelope(json.loads(msg.data))
                except Exception:
                    logger.debug("bad alert on the bus", exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("not listening for known visitors: %s", exc)

    # ── camera discovery ──

    def on_cameras_update(self, camera_ids) -> None:
        super().on_cameras_update(camera_ids)
        self.refresh_cameras(camera_ids)

    def refresh_cameras(self, camera_ids) -> tuple[list[str], list[str]]:
        if not self.cfg.auto_cameras:
            return [], []
        ids = {f"cam{int(i)}" for i in camera_ids}
        current = set(self.cfg.cameras)
        added = sorted(ids - current)
        removed = sorted(current - ids)
        for cam_id in added:
            self.cfg.cameras[cam_id] = _whole_frame(cam_id, UNIT_FRAME, UNIT_FRAME)
            self._doors.setdefault(cam_id, Door()).pending_check = (time.time(), "startup")
        for cam_id in removed:
            self.cfg.cameras.pop(cam_id, None)
            self._doors.pop(cam_id, None)
            self._unknown_cameras.discard(cam_id)
        if added or removed:
            logger.info("camera set refreshed: +%s -%s", added or "-", removed or "-")
            if added and self._last_config is not None:
                self.on_config_update(self._last_config)
        return added, removed

    # ── live config ──

    def on_config_update(self, config: dict[str, Any]) -> None:
        self._last_config = dict(config)
        try:
            knobs = _knobs_from(config, self.cfg)
        except ValueError as exc:
            logger.warning("config edit ignored: %s", exc)
            return
        for key, value in knobs.items():
            setattr(self.cfg, key, value)
        drawn = config.get("zone", config.get("zones", config.get("roi")))
        if isinstance(drawn, dict):
            for raw_key, polygon in drawn.items():
                cam_id = _camera_key(raw_key, self.cfg.cameras)
                if cam_id is None:
                    continue
                cam = self.cfg.cameras[cam_id]
                if not polygon:
                    if cam.drawn:
                        self.cfg.cameras[cam_id] = _whole_frame(cam_id, cam.frame_width,
                                                                cam.frame_height)
                    continue
                zone = _zone_from_drawn(polygon, cam)
                if zone is not None:
                    cam.zone, cam.drawn = zone, True

    # ── actions ──

    def _door(self, params: dict[str, Any]) -> tuple[str, Door]:
        cam_id = str(params.get("camera") or "").strip()
        if cam_id not in self.cfg.cameras:
            raise KeyError(f"unknown camera {cam_id!r}")
        return cam_id, self._doors.setdefault(cam_id, Door())

    def on_action(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        cam_id, door = self._door(params)
        if name == "picked_up":
            n = door.count
            if n > 0:
                waited = now - door.present_since if door.present_since else None
                who = {"kind": OPERATOR, "name": None, "reasons": ["marked collected on the page"]}
                self._event(cam_id, PICKED_UP, now, who=who, before=n, after=0, severity="low",
                            method="operator", dwell=waited)
                self._today["picked_up"] += n
            door.count, door.present_since, door.next_reminder = 0, 0.0, 0.0
            door.acked, door.snoozed_until = False, 0.0
            self._note(cam_id, "collected (marked on the page)", "info", now)
            return {"ok": True, "camera": cam_id, "collected": n}
        if name == "not_a_package":
            n = door.count
            who = {"kind": OPERATOR, "name": None,
                   "reasons": [f"not a parcel, said the operator ({self._counter.method})"]}
            self._event(cam_id, FALSE_ALARM, now, who=who, before=n, after=0, severity="low",
                        method="operator")
            self._today["false_alarms"] += 1
            door.count, door.present_since, door.next_reminder = 0, 0.0, 0.0
            self._note(cam_id, "false alarm recorded", "info", now)
            return {"ok": True, "camera": cam_id, "cleared": n}
        if name == "snooze":
            try:
                minutes = max(1.0, float(params.get("minutes") or 60.0))
            except (TypeError, ValueError):
                minutes = 60.0
            door.snoozed_until = now + minutes * 60.0
            self._note(cam_id, f"reminders snoozed for {int(minutes)} min", "info", now)
            return {"ok": True, "camera": cam_id, "snoozed_until": door.snoozed_until}
        if name == "check_now":
            door.pending_check = (now, "action")
            self._note(cam_id, "check requested", "info", now)
            return {"ok": True, "camera": cam_id}
        if name == "acknowledge":
            door.acked = True
            self._note(cam_id, "parcels acknowledged", "info", now)
            return {"ok": True, "camera": cam_id}
        raise KeyError(name)

    # ── surfaces ──

    def state_snapshot(self) -> dict[str, Any]:
        # A poll advances the machine too; whatever it fires must go out.
        for alert in self.tick():
            try:
                self._dispatcher.fire(alert)
            except Exception:
                logger.warning("alert from a /state poll failed to dispatch", exc_info=True)
        now = time.time()
        rows = []
        waiting = 0
        needs_zone = []
        for cam_id in sorted(self.cfg.cameras):
            cam = self.cfg.cameras[cam_id]
            door = self._doors.setdefault(cam_id, Door())
            if not cam.drawn:
                needs_zone.append(cam_id)
            waiting += door.count
            if door.count <= 0:
                state = CLEAR
            elif door.acked:
                state = ACKNOWLEDGED
            elif now < door.snoozed_until:
                state = SNOOZED
            elif self.cfg.reminder_minutes > 0 and door.next_reminder \
                    and now >= door.next_reminder:
                state = REMINDER_DUE
            else:
                state = WAITING
            rows.append({
                "camera": cam_id,
                "count": door.count,
                "waiting": door.count > 0,
                "present_since": door.present_since or None,
                "last_delivery_iso": _iso(door.last_delivery) if door.last_delivery else None,
                "state": state,
                "snoozed_until": door.snoozed_until or None,
                "last_check": door.last_check or None,
                "last_check_reason": door.last_check_reason,
                "next_reminder": (door.next_reminder if door.count > 0 and not door.acked
                                  and self.cfg.reminder_minutes > 0 else None),
                "last_event": door.last_event,
                "zone_drawn": cam.drawn,
                "people_now": sum(1 for p in door.people.values() if not p.get("gone")
                                  and now - float(p["wall"]) <= self.cfg.track_ttl_seconds),
                "vehicle_now": self._vehicle_near(door, now),
            })
        c = self._counter
        return {
            "waiting_now": waiting,
            "today": dict(self._today),
            "counted_by": {"method": c.method, "adapter": c.adapter, "task": c.task,
                           "quality": c.quality, "note": c.describe(self.cfg)},
            "hours": self.cfg.delivery_hours.as_dict() if self.cfg.delivery_hours else None,
            "per_camera": rows,
            "events": list(self._events),
            "recent": list(self._recent)[-50:],
            "needs_zone": needs_zone,
            "since": self._started_at,
        }

    def ui_html(self) -> str:
        """One static page: what is waiting, since when, and today."""
        snap = self.state_snapshot()
        esc = _html.escape
        doors = "".join(
            f"<div class='card'><b>{esc(r['camera'])}</b> "
            f"<span class='pill'>{r['count']} waiting</span>"
            + (f" since {esc(_clock(r['present_since']))}" if r['present_since'] else "")
            + f"<div class='dim'>{esc(r['state'])}"
            + (f" · last: {esc(r['last_event']['kind'])} by "
               f"{esc(r['last_event']['who'].get('name') or r['last_event']['who']['kind'])}"
               if r['last_event'] else "") + "</div></div>"
            for r in snap["per_camera"]) or "<p class='dim'>No cameras selected.</p>"
        events = "".join(
            f"<li>{esc(_clock(e['time']))} {esc(e['kind'])} at {esc(e['camera'])} by "
            f"{esc(e['who'].get('name') or e['who']['kind'])} "
            f"({e['count_before']}→{e['count_after']})"
            f"<div class='dim'>{esc(' · '.join(e['who']['reasons']))}</div></li>"
            for e in snap["events"][:20]) or "<li class='dim'>Nothing yet today.</li>"
        return f"""<!doctype html>
<meta charset="utf-8"><title>Package Delivery</title>
<style>
body{{font:14px system-ui,sans-serif;margin:1.2rem;color:#e6e6ea;background:#0f1115}}
.dim{{color:#8b8d98}} .pill{{padding:.1rem .5rem;border-radius:999px;background:#2b6cb0;color:#fff}}
.card{{border:1px solid #2a2d36;border-radius:.6rem;padding:.7rem;margin:.4rem 0}}
li{{margin:.35rem 0}} h1,h2{{margin:.3rem 0}}
</style>
<h1>Package Delivery</h1>
<div class="dim"><b>{snap['waiting_now']}</b> waiting · today <b>{snap['today']['delivered']}</b>
 delivered, <b>{snap['today']['picked_up']}</b> collected, <b>{snap['today']['taken']}</b> taken ·
 {esc(snap['counted_by']['note'])}</div>
<h2>Doors</h2>{doors}
<h2>Today</h2><ul>{events}</ul>
"""


# ── small helpers ────────────────────────────────────────────────────


def _sentence(who: dict[str, Any], verb: str, n: int) -> str:
    what = f"{n} parcel{'s' if n != 1 else ''}"
    kind = who["kind"]
    name = who.get("name")
    if kind == NOBODY:
        return f"Nobody was seen; {what} {'was' if n == 1 else 'were'} {verb}."
    subject = {
        COURIER: "A courier", KNOWN: f"{name} (a known face)", OWNER: name or "The owner",
        STRANGER: "Somebody not recognised", UNKNOWN: "Somebody", OPERATOR: "The operator",
    }.get(kind, "Somebody")
    return f"{subject} {verb} {what}."


def _because(who: dict[str, Any]) -> str:
    reasons = who.get("reasons") or []
    return ("Evidence: " + "; ".join(reasons) + ".") if reasons else ""


def _clock(ts: float | None) -> str:
    if not ts:
        return "—"
    return _dt.datetime.fromtimestamp(ts).strftime("%H:%M")


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat()


def _hm(seconds: float | None) -> str:
    s = int(seconds or 0)
    if s < 60:
        return f"{s}s"
    m = s // 60
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60} min"


def _ago(seconds: float) -> str:
    return f"{_hm(max(0.0, seconds))} ago"


# Spec-preferred short name.
PackageDelivery = PackageDeliveryDetector


def main(argv: list[str] | None = None) -> int:
    return app(PackageDeliveryDetector, load_config=load_config).run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
