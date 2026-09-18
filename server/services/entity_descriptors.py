# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Server-described entities (HA-114, design §6.10).

The server describes what Home Assistant should show, resolves the values,
and the integration renders them generically, so a new OpenNVR feature or a
new AI app needs no integration release.

* :func:`descriptors_for` — every descriptor the caller may see: core's own
  (site, camera, zone) plus those AI apps declare in their manifest's
  ``entities:`` section. A descriptor is visible only when the caller holds
  its ``required_scope`` and can see its camera; it grants nothing by itself.
* :func:`resolve_states` — ``{key: {"state", "attributes"}}`` for every
  descriptor, from live state, the event store, camera status, the alerts
  inbox and (for app entities) the app's cached ``/state``.
* Event entities (``platform: event``) have no resolved state: core pushes
  an ``entity_state`` with an ``event`` when one fires (detections, alerts).

Commands are typed (``core_control`` / ``app_action``), never URLs, and are
executed and scope-checked by routers/entities.py.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

DESCRIPTOR_VERSION = 1
PLATFORMS = ("sensor", "binary_sensor", "switch", "select", "button", "number",
             "event", "image")
#: Labels a camera gets per-label entities for when its assignments name none.
DEFAULT_LABELS = ("person", "car", "truck", "dog", "cat")
#: Every scope a descriptor may require.
DESCRIPTOR_SCOPES = ("cameras.view", "cameras.manage", "recordings.view", "recordings.pause",
                     "alerts.view", "alerts.manage", "events.create", "ptz.control",
                     "settings.view", "apps.view", "apps.actions")
#: Core controls a descriptor command may name (design §6.10).
CORE_CONTROLS = ("detection", "recording_pause", "manual_event", "ack_alerts",
                 "ptz_preset", "site_mode", "camera_on", "ptz_move")


@dataclass
class Descriptor:
    key: str
    platform: str
    name: str
    device: dict[str, Any]
    required_scope: str
    origin: str = "core"
    camera_id: int | None = None
    translation_key: str | None = None
    device_class: str | None = None
    unit: str | None = None
    state_class: str | None = None
    entity_category: str | None = None
    enabled_default: bool = True
    icon: str | None = None
    options: Any = None
    event_types: list[str] | None = None
    command: dict[str, Any] | None = None
    #: App entities: where the value is in the app's /state (dot path).
    state_path: str | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if v is not None and k != "state_path"}
        d["descriptor_version"] = DESCRIPTOR_VERSION
        return d


# ── core descriptors ─────────────────────────────────────────────────────


def _camera_labels(cam) -> list[str]:
    labels: list[str] = []
    for a in cam.assignments or []:
        if isinstance(a, dict):
            labels += [str(x).lower() for x in (a.get("labels") or [])]
    return sorted(set(labels)) or list(DEFAULT_LABELS)


def _site_descriptors() -> list[Descriptor]:
    site = {"kind": "site", "id": "site"}
    return [
        Descriptor("site.alerts_unacknowledged", "binary_sensor", "Unacknowledged alerts",
                   site, "alerts.view", device_class="problem",
                   translation_key="alerts_unacknowledged"),
        Descriptor("site.alerts_unacknowledged_count", "sensor", "Unacknowledged alert count",
                   site, "alerts.view", state_class="measurement",
                   translation_key="alerts_unacknowledged_count"),
        Descriptor("site.alerts_highest_severity", "sensor", "Highest open alert severity",
                   site, "alerts.view", device_class="enum",
                   options=["none", "low", "medium", "high", "critical"],
                   translation_key="alerts_highest_severity"),
        Descriptor("site.ack_all_alerts", "button", "Acknowledge all alerts", site,
                   "alerts.manage", command={"type": "core_control", "control": "ack_alerts"},
                   translation_key="ack_all_alerts"),
        Descriptor("site.storage_used", "sensor", "Storage used", site, "settings.view",
                   unit="%", state_class="measurement", translation_key="storage_used"),
        Descriptor("site.cpu", "sensor", "CPU", site, "settings.view", unit="%",
                   state_class="measurement", entity_category="diagnostic",
                   enabled_default=False, translation_key="cpu"),
        Descriptor("site.memory", "sensor", "Memory", site, "settings.view", unit="%",
                   state_class="measurement", entity_category="diagnostic",
                   enabled_default=False, translation_key="memory"),
    ]


def _camera_descriptors(cam, recording_pause: bool) -> list[Descriptor]:
    cid = int(cam.id)
    dev = {"kind": "camera", "id": cid}
    k = f"camera.{cid}"
    labels = _camera_labels(cam)
    out = [
        Descriptor(f"{k}.online", "binary_sensor", "Online", dev, "cameras.view",
                   camera_id=cid, device_class="connectivity",
                   entity_category="diagnostic", translation_key="online"),
        Descriptor(f"{k}.motion", "binary_sensor", "Motion", dev, "cameras.view",
                   camera_id=cid, device_class="motion", translation_key="motion"),
        Descriptor(f"{k}.occupancy.all", "binary_sensor", "All occupancy", dev,
                   "cameras.view", camera_id=cid, device_class="occupancy",
                   translation_key="occupancy_all"),
        Descriptor(f"{k}.recording_problem", "binary_sensor", "Recording problem", dev,
                   "recordings.view", camera_id=cid, device_class="problem",
                   translation_key="recording_problem"),
        Descriptor(f"{k}.last_plate", "sensor", "Last plate", dev, "recordings.view",
                   camera_id=cid, icon="mdi:car-info", translation_key="last_plate"),
        Descriptor(f"{k}.last_object", "image", "Last object", dev, "recordings.view",
                   camera_id=cid, translation_key="last_object"),
        Descriptor(f"{k}.detections", "event", "Detection", dev, "cameras.view",
                   camera_id=cid, event_types=labels, translation_key="detection"),
        Descriptor(f"{k}.alerts", "event", "Alert", dev, "alerts.view",
                   camera_id=cid, event_types=["alert"], translation_key="alert"),
        Descriptor(f"{k}.detection", "switch", "Object detection", dev, "cameras.manage",
                   camera_id=cid, command={"type": "core_control", "control": "detection"},
                   translation_key="detection_switch"),
        Descriptor(f"{k}.manual_event", "button", "Trigger manual event", dev,
                   "events.create", camera_id=cid,
                   command={"type": "core_control", "control": "manual_event"},
                   translation_key="manual_event"),
        Descriptor(f"{k}.detect_fps", "sensor", "Detection FPS", dev, "cameras.view",
                   camera_id=cid, state_class="measurement", entity_category="diagnostic",
                   enabled_default=False, translation_key="detect_fps"),
        Descriptor(f"{k}.inference_ms", "sensor", "Inference time", dev, "cameras.view",
                   camera_id=cid, unit="ms", state_class="measurement",
                   entity_category="diagnostic", enabled_default=False,
                   translation_key="inference_ms"),
        Descriptor(f"{k}.bitrate", "sensor", "Bitrate", dev, "cameras.view",
                   camera_id=cid, unit="kbit/s", device_class="data_rate",
                   state_class="measurement", entity_category="diagnostic",
                   enabled_default=False, translation_key="bitrate"),
    ]
    if recording_pause:
        # Only where pausing recording is allowed at all (HA-108); off by
        # default in HA even then.
        out.append(Descriptor(f"{k}.recording", "switch", "Recording", dev,
                              "recordings.pause", camera_id=cid, enabled_default=False,
                              command={"type": "core_control", "control": "recording_pause"},
                              translation_key="recording_switch"))
    for label in labels:
        out += [
            Descriptor(f"{k}.occupancy.{label}", "binary_sensor", f"{label} occupancy", dev,
                       "cameras.view", camera_id=cid, device_class="occupancy",
                       translation_key="occupancy_label"),
            Descriptor(f"{k}.count.{label}", "sensor", f"{label} count", dev,
                       "cameras.view", camera_id=cid, state_class="measurement",
                       translation_key="count_label"),
            Descriptor(f"{k}.active_count.{label}", "sensor", f"{label} active count", dev,
                       "cameras.view", camera_id=cid, state_class="measurement",
                       enabled_default=False, translation_key="active_count_label"),
        ]
    return out


def _zone_descriptors(zone, cam) -> list[Descriptor]:
    cid = int(zone.camera_id)
    dev = {"kind": "zone", "id": int(zone.id), "camera_id": cid, "name": zone.name}
    k = f"zone.{zone.id}"
    labels = sorted(zone.labels) if zone.labels else _camera_labels(cam)
    out = [
        Descriptor(f"{k}.occupancy.all", "binary_sensor", "All occupancy", dev,
                   "cameras.view", camera_id=cid, device_class="occupancy",
                   translation_key="occupancy_all"),
        Descriptor(f"{k}.detections", "event", "Detection", dev, "cameras.view",
                   camera_id=cid, event_types=labels, translation_key="detection"),
    ]
    for label in labels:
        out += [
            Descriptor(f"{k}.occupancy.{label}", "binary_sensor", f"{label} occupancy", dev,
                       "cameras.view", camera_id=cid, device_class="occupancy",
                       translation_key="occupancy_label"),
            Descriptor(f"{k}.count.{label}", "sensor", f"{label} count", dev,
                       "cameras.view", camera_id=cid, state_class="measurement",
                       translation_key="count_label"),
        ]
    return out


def _ptz_descriptors(cam, presets: list[dict] | None) -> list[Descriptor]:
    cid = int(cam.id)
    dev = {"kind": "camera", "id": cid}
    k = f"camera.{cid}"
    out = []
    for direction in ("up", "down", "left", "right", "zoom_in", "zoom_out"):
        out.append(Descriptor(f"{k}.ptz_{direction}", "button",
                              f"PTZ {direction.replace('_', ' ')}", dev, "ptz.control",
                              camera_id=cid,
                              command={"type": "core_control", "control": "ptz_move",
                                       "args": {"direction": direction}},
                              translation_key=f"ptz_{direction}"))
    if presets:
        out.append(Descriptor(f"{k}.ptz_preset", "select", "PTZ preset", dev, "ptz.control",
                              camera_id=cid, options=[p["name"] for p in presets],
                              command={"type": "core_control", "control": "ptz_preset"},
                              translation_key="ptz_preset"))
    return out


def _is_ptz(cam) -> bool:
    cap = getattr(cam, "capability", None)
    areas = getattr(cap, "supported_areas", None) or {}
    return bool(isinstance(areas, dict) and areas.get("ptz"))


# ── app descriptors (manifest ``entities:``) ─────────────────────────────

#: Platforms an app may declare.
APP_PLATFORMS = ("sensor", "binary_sensor", "button", "switch", "select", "number", "event")


def _app_descriptors(db: Session, app) -> list[Descriptor]:
    """Descriptors an installed, enabled app declares. Anything malformed is
    skipped, never an error (design: unknown fields/platforms are skipped)."""
    from services.app_keys import app_camera_ids

    manifest = app.manifest_json or {}
    declared_actions = {a.get("name") for a in manifest.get("actions") or []
                        if isinstance(a, dict)}
    entities = manifest.get("entities") or []
    if not isinstance(entities, list) or not entities:
        return []
    cams = None
    out: list[Descriptor] = []
    for e in entities:
        if not isinstance(e, dict) or e.get("platform") not in APP_PLATFORMS:
            continue
        ekey = str(e.get("key") or "")
        if not ekey or not ekey.replace("_", "").isalnum():
            continue
        action = e.get("action")
        if action is not None and action not in declared_actions:
            continue  # an entity can only drive its own app's declared actions
        if e["platform"] in ("button", "switch", "select", "number") and action is None:
            continue
        command = {"type": "app_action", "action": action} if action else None
        common = dict(
            platform=e["platform"], name=str(e.get("name") or ekey),
            origin=f"app:{app.id}", device_class=e.get("device_class"), unit=e.get("unit"),
            state_class=e.get("state_class"), entity_category=e.get("entity_category"),
            enabled_default=bool(e.get("enabled_default", True)), icon=e.get("icon"),
            options=e.get("options"), event_types=e.get("event_types"), command=command,
            state_path=e.get("state_path"),
            required_scope="apps.actions" if command else "apps.view",
        )
        if e.get("per_camera"):
            if cams is None:
                cams = sorted(app_camera_ids(db, app) or [])
            for cid in cams:
                out.append(Descriptor(key=f"app.{app.id}.{cid}.{ekey}",
                                      device={"kind": "camera", "id": cid}, camera_id=cid,
                                      **common))
        else:
            out.append(Descriptor(key=f"app.{app.id}.{ekey}",
                                  device={"kind": "app", "id": app.id, "name": app.name},
                                  **common))
    return out


# ── the full catalogue, and what one caller sees ─────────────────────────


def all_descriptors(db: Session) -> list[Descriptor]:
    """Every descriptor on the site (unfiltered)."""
    from models import Camera, CameraZone, InstalledApp
    from services import ptz_presets_cache
    from services.site_settings import recording_pause_enabled

    pause = recording_pause_enabled(db)
    cams = (db.query(Camera).filter(Camera.deleted_at.is_(None), Camera.is_active.is_(True))
            .order_by(Camera.id).all())
    by_id = {c.id: c for c in cams}
    out = _site_descriptors()
    for cam in cams:
        out += _camera_descriptors(cam, pause)
        if _is_ptz(cam):
            out += _ptz_descriptors(cam, ptz_presets_cache.get(cam.id))
    for zone in db.query(CameraZone).order_by(CameraZone.id).all():
        if zone.camera_id in by_id:
            out += _zone_descriptors(zone, by_id[zone.camera_id])
    for app in db.query(InstalledApp).filter(InstalledApp.enabled.is_(True)).all():
        out += _app_descriptors(db, app)
    return out


def has_state(d: Descriptor) -> bool:
    """Whether a descriptor has a resolved state (buttons and events don't,
    nor do app entities without a state_path)."""
    if d.platform in ("button", "event"):
        return False
    if d.origin.startswith("app:"):
        return bool(d.state_path)
    return not (d.platform == "select" and d.key.endswith(".ptz_preset"))


#: Site-wide counts over EVERY camera's alerts. A caller limited to some
#: cameras must not learn how many alerts other cameras have, so these are
#: shown only to callers who see the whole fleet.
FLEET_ONLY_KEYS = frozenset({"site.alerts_unacknowledged", "site.alerts_unacknowledged_count",
                             "site.alerts_highest_severity"})


def held_scopes(principal) -> set[str]:
    """The descriptor scopes this principal holds (token-aware)."""
    from core.permissions import user_has_permission

    return {s for s in DESCRIPTOR_SCOPES if user_has_permission(principal, s)}


def visible(db: Session, principal, descriptors: list[Descriptor]) -> list[Descriptor]:
    from services.camera_scope import visible_camera_ids

    scope = visible_camera_ids(db, principal)
    held = held_scopes(principal)
    return [d for d in descriptors
            if d.required_scope in held
            and (d.camera_id is None or scope is None or d.camera_id in scope)
            and (d.key not in FLEET_ONLY_KEYS or scope is None)]


def descriptors_for(db: Session, principal) -> list[Descriptor]:
    return visible(db, principal, all_descriptors(db))


def etag_of(descriptors: list[Descriptor]) -> str:
    blob = json.dumps([d.to_dict() for d in descriptors], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ── app /state cache (filled by the resolver) ────────────────────────────

_app_state: dict[str, dict[str, Any]] = {}


def set_app_state(app_id: str, state: dict[str, Any] | None) -> None:
    if state is None:
        _app_state.pop(app_id, None)
    else:
        _app_state[app_id] = state


def eval_path(state: Any, path: str | None, camera_id: int | None = None) -> Any:
    """A value from an app's /state by dot path. Supports list indexes and
    ``[field=value]`` row selectors; ``{camera}`` / ``{camera_id}`` become
    this entity's camera handle (``cam3``) / id. Missing → None."""
    if not path:
        return None
    if camera_id is not None:
        path = path.replace("{camera}", f"cam{camera_id}").replace("{camera_id}", str(camera_id))
    cur = state
    for part in _split_path(path):
        if cur is None:
            return None
        if part.startswith("[") and part.endswith("]") and "=" in part:
            field_name, want = part[1:-1].split("=", 1)
            cur = (next((row for row in cur if isinstance(row, dict)
                         and str(row.get(field_name)) == want), None)
                   if isinstance(cur, list) else None)
        elif isinstance(cur, list):
            cur = cur[int(part)] if part.isdigit() and int(part) < len(cur) else None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _split_path(path: str) -> list[str]:
    parts, buf, in_sel = [], "", False
    for ch in path:
        if ch == "[" and not in_sel:
            if buf:
                parts.append(buf)
            buf, in_sel = "[", True
        elif ch == "]" and in_sel:
            parts.append(buf + "]")
            buf, in_sel = "", False
        elif ch == "." and not in_sel:
            if buf:
                parts.append(buf)
            buf = ""
        else:
            buf += ch
    if buf:
        parts.append(buf)
    return parts


# ── state resolution ─────────────────────────────────────────────────────

_SEV = {"low": 0, "medium": 1, "high": 2, "critical": 3}
#: How far back "last plate" / "last object" look: bounded and ordered by
#: (camera_id, started_at) so ix_events_cam_start serves it, instead of a
#: full-table walk on a camera that never read a plate.
LAST_LOOKBACK = timedelta(days=7)


def _site_states(db: Session) -> dict[str, dict]:
    from models import AppAlert
    from services.system_monitor_service import get_system_monitor

    from sqlalchemy import func

    counts = dict(db.query(AppAlert.severity, func.count(AppAlert.id))
                  .filter(AppAlert.acknowledged_at.is_(None))
                  .group_by(AppAlert.severity).all())
    total = sum(counts.values())
    top = max(counts, key=lambda s: _SEV.get(s, -1)) if counts else "none"
    sample = getattr(get_system_monitor(), "_last_sample", None) or {}
    disk = sample.get("disk") or {}
    mem = sample.get("memory") or {}
    return {
        "site.alerts_unacknowledged": {"state": total > 0, "attributes": {}},
        "site.alerts_unacknowledged_count": {"state": total, "attributes": {}},
        "site.alerts_highest_severity": {"state": top, "attributes": {}},
        "site.storage_used": {"state": disk.get("percent"),
                              "attributes": {"free_bytes": disk.get("free"),
                                             "total_bytes": disk.get("total")}},
        "site.cpu": {"state": sample.get("cpu_percent"), "attributes": {}},
        "site.memory": {"state": mem.get("percent"), "attributes": {}},
    }


def _counts_states(prefix: str, objects: dict, labels: list[str]) -> dict[str, dict]:
    out = {f"{prefix}.occupancy.all": {"state": any(v["total"] for v in objects.values()),
                                       "attributes": {"objects": objects}}}
    for label in labels:
        c = objects.get(label) or {"total": 0, "active": 0}
        out[f"{prefix}.occupancy.{label}"] = {"state": c["total"] > 0, "attributes": {}}
        out[f"{prefix}.count.{label}"] = {"state": c["total"], "attributes": {}}
        out[f"{prefix}.active_count.{label}"] = {"state": c["active"], "attributes": {}}
    return out


def resolve_states(db: Session, descriptors: list[Descriptor],
                   stats: dict[int, dict] | None = None) -> dict[str, dict]:
    """``{key: {"state", "attributes"}}`` for the given descriptors (event
    entities have none). ``stats`` is the cached per-camera /stats."""
    from datetime import UTC, datetime

    from sqlalchemy import func

    from models import Camera, CameraConfig, Recording, TimelineEvent
    from routers.cameras import _derive_recording_state
    from services.camera_status_service import get_camera_status_service
    from services.live_state import get_live_state
    from services.recording_pause import paused

    keys = {d.key for d in descriptors}
    states = {k: v for k, v in _site_states(db).items() if k in keys}
    cam_ids = sorted({d.camera_id for d in descriptors
                      if d.camera_id is not None and d.origin == "core"})
    zone_labels: dict[int, list[str]] = {}
    for d in descriptors:
        if d.key.startswith("zone.") and ".count." in d.key:
            zid = int(d.key.split(".")[1])
            zone_labels.setdefault(zid, []).append(d.key.rsplit(".", 1)[1])
    if cam_ids:
        now = datetime.now(UTC)
        live = get_live_state()
        status = get_camera_status_service()
        online = status.snapshot(cam_ids)
        since = status.online_since(cam_ids)
        cams = {c.id: c for c in db.query(Camera).filter(Camera.id.in_(cam_ids)).all()}
        rec_on = dict(db.query(CameraConfig.camera_id, CameraConfig.recording_enabled)
                      .filter(CameraConfig.camera_id.in_(cam_ids)).all())
        latest = dict(db.query(Recording.camera_id, func.max(Recording.start_time))
                      .filter(Recording.camera_id.in_(cam_ids))
                      .group_by(Recording.camera_id).all())
        pause = paused(db)
        for cid in cam_ids:
            cam = cams.get(cid)
            if cam is None:
                continue
            k = f"camera.{cid}"
            ls = live.camera(cid)
            states[f"{k}.online"] = {"state": online.get(cid), "attributes": {}}
            states[f"{k}.motion"] = {"state": ls["motion"], "attributes": {}}
            states.update(_counts_states(k, ls["objects"], _camera_labels(cam)))
            for z in ls["zones"]:
                states.update(_counts_states(f"zone.{z['zone_id']}", z["objects"],
                                             sorted(zone_labels.get(z["zone_id"], []))))
            last = latest.get(cid)
            if last is not None and last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            rstate = _derive_recording_state(cam, bool(rec_on.get(cid)), last, now,
                                             online.get(cid), since.get(cid))
            states[f"{k}.recording_problem"] = {"state": rstate in ("stalled", "never"),
                                                "attributes": {"recording_state": rstate}}
            states[f"{k}.detection"] = {"state": cam.detection_enabled is not False,
                                        "attributes": {}}
            states[f"{k}.recording"] = {"state": str(cid) not in pause,
                                        "attributes": pause.get(str(cid)) or {}}
            recent = (db.query(TimelineEvent)
                      .filter(TimelineEvent.camera_id == cid,
                              TimelineEvent.started_at >= now - LAST_LOOKBACK)
                      .order_by(TimelineEvent.started_at.desc()))
            if f"{k}.last_plate" in keys:
                plate = recent.filter(TimelineEvent.plate_text.isnot(None)).first()
                states[f"{k}.last_plate"] = {
                    "state": plate.plate_text if plate else None,
                    "attributes": {"event_id": plate.id} if plate else {}}
            if f"{k}.last_object" in keys:
                obj = recent.filter(TimelineEvent.event_type == "track",
                                    TimelineEvent.evidence_path.isnot(None)).first()
                seen = (obj.ended_at or obj.started_at) if obj else None
                states[f"{k}.last_object"] = {
                    "state": seen.isoformat() if seen else None,
                    "attributes": ({"event_id": obj.id, "label": obj.label, "image": "evidence",
                                    "zone_ids": obj.zone_ids} if obj else {})}
            st = (stats or {}).get(cid) or {}
            states[f"{k}.detect_fps"] = {"state": st.get("detect_fps"), "attributes": {}}
            states[f"{k}.inference_ms"] = {"state": st.get("inference_ms"), "attributes": {}}
            states[f"{k}.bitrate"] = {"state": st.get("bitrate_kbps"), "attributes": {}}
    for d in descriptors:
        if d.origin.startswith("app:") and d.state_path and d.platform != "event":
            states[d.key] = {"state": eval_path(_app_state.get(d.origin[4:]), d.state_path,
                                                d.camera_id),
                             "attributes": {}}
    return {k: v for k, v in states.items() if k in keys}
