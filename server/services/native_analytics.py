# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The camera's own analytics as OpenNVR alerts.

Most IP cameras run line crossing, intrusion, loitering, tamper and
motion rules on board and announce them over ONVIF (pull-point), ISAPI
(Hikvision) or the Dahua event stream. ``camera_event_manager`` already
subscribes; this module decides what those events become.

Per camera, ``Camera.analytics_source`` picks the detection source:

* ``opennvr`` (the default, NULL in the database): native events are
  history only (``camera_events``); OpenNVR's own detection runs.
* ``camera``: native events become alerts; Tier-0 detection on the
  camera is paused so the CPU is spent elsewhere.
* ``both``: native events become alerts and OpenNVR's detection runs
  too; the inbox shows the source of each.

An event is mapped to an OpenNVR alert type by keywords in its topic /
event type (``tns1:RuleEngine/LineDetector/Crossed`` → ``line_crossing``,
Hikvision ``fielddetection`` → ``intrusion``, Dahua ``CrossLineDetection``
→ ``line_crossing``). Only the ``active`` edge raises; the ``inactive``
edge is the end of the same thing, not a second alert. The alert goes
through :func:`services.alerts_inbox.apply_alert` like an app's, with
``source.kind = "camera"``, and is published on the alerts bus so apps
and automations see it; the inbox's ``alert_id`` dedupe swallows the
echo.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("opennvr.native_analytics")

SOURCES = ("opennvr", "camera", "both")
DEFAULT_SOURCE = "opennvr"

#: OpenNVR alert type → (label, severity, keywords found in the native
#: event's topic / type / description, lower-cased). First match wins, in
#: this order, so the specific rules sit above plain motion.
ALERT_TYPES: list[tuple[str, str, str, tuple[str, ...]]] = [
    ("line_crossing", "Line crossed", "medium",
     ("linedetector", "linedetection", "crossline", "linecross", "tripwire", "crossing")),
    ("intrusion", "Intrusion", "high",
     ("fielddetector", "fielddetection", "objectsinside", "intrusion", "regionentrance",
      "regionexiting", "crossregion", "perimeter")),
    ("loitering", "Loitering", "medium", ("loitering", "wander", "dwell")),
    ("object_left", "Object left", "medium",
     ("unattendedbaggage", "objectleft", "leftdetection", "abandoned", "leftobject")),
    ("object_removed", "Object removed", "medium",
     ("objectremoval", "takenaway", "takendetection", "missingobject", "removed")),
    ("tamper_covered", "Camera tampered", "high",
     ("videoblind", "tamper", "shelteralarm", "scenechange", "imagetoo", "defocus")),
    ("video_loss", "Video lost", "high", ("videoloss", "signalloss")),
    ("audio_alarm", "Audio alarm", "medium", ("audioexception", "audiodetector", "audioanomaly", "audio")),
    ("face_detected", "Face detected", "low", ("facedetect", "facecapture", "face")),
    ("motion", "Motion", "low",
     ("cellmotion", "motionalarm", "videomotion", "motiondetect", "vmd", "motion", "pir")),
]

_LABELS = {t: label for t, label, _, _ in ALERT_TYPES}
_SEVERITY = {t: sev for t, _, sev, _ in ALERT_TYPES}


def alert_type_catalog() -> list[dict[str, str]]:
    """What the UI lists under "alerts from this camera"."""
    return [{"type": t, "label": label, "severity": sev} for t, label, sev, _ in ALERT_TYPES] + [
        {"type": "camera_event", "label": "Other camera event", "severity": "low"}]


def classify(event: dict[str, Any]) -> str:
    """The OpenNVR alert type for a native event, ``camera_event`` when
    none of the keywords apply."""
    text = " ".join(str(event.get(k) or "") for k in ("event_type", "description", "topic", "rule")).lower()
    text = text.replace("_", "").replace("-", "").replace(" ", "")
    for alert_type, _, _, keywords in ALERT_TYPES:
        if any(k in text for k in keywords):
            return alert_type
    return "camera_event"


def is_active_edge(event: dict[str, Any]) -> bool:
    """Raise on the start of a condition, not its end. Drivers that send
    one-shot events without a state (Hikvision line crossing) count."""
    state = str(event.get("event_state") or "").strip().lower()
    return state in ("", "active", "true", "start", "begin", "on")


def effective_source(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    return value if value in SOURCES else DEFAULT_SOURCE


def wants_alerts(source: Any, allowlist: Any, alert_type: str) -> bool:
    if effective_source(source) == "opennvr":
        return False
    if isinstance(allowlist, list) and allowlist:
        return alert_type in {str(a) for a in allowlist}
    return True


def envelope_for(*, camera_id: int, camera_name: str, driver_name: str | None,
                 event: dict[str, Any], alert_type: str, now: datetime | None = None) -> dict[str, Any]:
    """The §11.5 alert envelope for one native event, as an app would send."""
    now = now or datetime.now(UTC)
    occurred = str(event.get("occurred_at") or now.isoformat())
    rule = str(event.get("rule") or "").strip()
    label = _LABELS.get(alert_type, "Camera event")
    what = f"{label}: {rule}" if rule else label
    if alert_type == "camera_event":
        what = f"Camera event: {event.get('event_type') or 'unknown'}"
    # One alert per type per camera per second: the stream repeats the
    # active edge on every pull/heartbeat of a long condition.
    key = f"{camera_id}|{alert_type}|{rule}|{occurred[:19]}"
    alert_id = "cam-" + hashlib.sha1(key.encode()).hexdigest()[:20]
    return {
        "alert_id": alert_id,
        "title": f"{what} at {camera_name} (camera)",
        "description": (f"The camera reported {label.lower()}"
                        + (f" on rule '{rule}'" if rule else "")
                        + f" ({event.get('event_type') or event.get('description') or 'event'})."),
        "severity": _SEVERITY.get(alert_type, "low"),
        "alert_type": alert_type,
        "camera_id": f"cam{camera_id}",
        "fired_at": now.isoformat(),
        "source": {"kind": "camera", "name": driver_name or "camera"},
        "evidence": {"observed_at": occurred, "topic": event.get("description") or event.get("topic"),
                     "rule": rule or None, "event_type": event.get("event_type"),
                     "state": event.get("event_state"), "native": True},
        "tags": ["native", alert_type],
    }


# ── routing ─────────────────────────────────────────────────────────

async def route(camera_id: int, event: dict[str, Any]) -> str | None:
    """Turn one native event into an alert when the camera's source asks
    for it. Returns the alert type raised, or None. Never raises."""
    try:
        from core.database import SessionLocal
        from models import Camera, CameraCapability

        with SessionLocal() as db:
            camera = db.query(Camera).filter(Camera.id == camera_id).first()
            if camera is None:
                return None
            source, allowlist, name = camera.analytics_source, camera.native_event_types, camera.name
            cap = db.query(CameraCapability).filter(CameraCapability.camera_id == camera_id).first()
            driver = (cap.driver_name if cap else None) or camera.manufacturer
    except Exception:
        logger.debug("native analytics: camera %s lookup failed", camera_id, exc_info=True)
        return None
    if effective_source(source) == "opennvr" or not is_active_edge(event):
        return None
    alert_type = classify(event)
    if not wants_alerts(source, allowlist, alert_type):
        return None
    envelope = envelope_for(camera_id=camera_id, camera_name=name, driver_name=driver,
                            event=event, alert_type=alert_type)
    await deliver(envelope)
    return alert_type


async def deliver(envelope: dict[str, Any]) -> None:
    """Into the inbox (and the live bell) directly, then onto the alerts
    bus for apps and automations. The bus echo lands back in the inbox
    consumer and is dropped as a duplicate alert_id."""
    import asyncio

    from services.alerts_inbox import apply_alert

    stored: dict = {}
    try:
        status = await asyncio.to_thread(apply_alert, envelope, None, stored)
    except Exception:
        logger.warning("native analytics: inbox write failed", exc_info=True)
        return
    if status == "stored":
        try:
            from services.event_bus_service import publish_app_alert

            await publish_app_alert(
                camera_id=int(str(envelope["camera_id"]).removeprefix("cam")),
                severity=stored.get("severity") or envelope["severity"],
                alert_type=envelope["alert_type"],
                payload={"title": envelope["title"], "alert_id": envelope["alert_id"], "id": stored.get("id"),
                         "source": envelope["source"], "images": []})
        except Exception:
            logger.debug("native analytics: live push failed", exc_info=True)
    await _publish_bus(envelope)


async def _publish_bus(envelope: dict[str, Any]) -> None:
    """``opennvr.alerts.camera.<driver>.<camera>``, the subject apps use."""
    try:
        from core.config import settings

        url = (getattr(settings, "nats_url", "") or "").strip()
        if not url:
            return
        import nats

        token = (getattr(settings, "internal_api_key", "") or "").strip() or None
        name = "".join(ch if ch.isalnum() else "_" for ch in str(envelope["source"]["name"]).lower()) or "camera"
        subject = f"opennvr.alerts.camera.{name}.{envelope['camera_id']}"
        client = await nats.connect(url, connect_timeout=3, token=token)
        try:
            await client.publish(subject, json.dumps(envelope).encode())
            await client.flush(timeout=3)
        finally:
            await client.close()
    except Exception:
        logger.debug("native analytics: bus publish skipped", exc_info=True)


# ── the setting ─────────────────────────────────────────────────────

def apply_source(camera: Any, source: str) -> dict[str, Any]:
    """Set the camera's detection source on the ORM row (caller commits).
    ``camera`` pauses Tier-0 on it; the other two leave Tier-0 as the
    operator set it. Returns what changed, for the audit log."""
    source = effective_source(source)
    before = {"analytics_source": effective_source(camera.analytics_source),
              "detection_enabled": camera.detection_enabled is not False}
    camera.analytics_source = None if source == DEFAULT_SOURCE else source
    if source == "camera":
        camera.detection_enabled = False
    elif before["analytics_source"] == "camera":
        camera.detection_enabled = True          # coming back from camera-only: detection returns
    return {"from": before, "to": {"analytics_source": source,
                                   "detection_enabled": camera.detection_enabled is not False}}


async def sync_subscription(camera_id: int, source: Any) -> None:
    """Listen to the camera when its events are wanted, stop when not."""
    from services.camera_event_manager import get_camera_event_manager

    manager = get_camera_event_manager()
    if effective_source(source) == "opennvr":
        await manager.stop(camera_id)
    else:
        await manager.start(camera_id)


async def restore_on_startup() -> int:
    """Re-arm the subscriptions of every camera whose source wants native
    events; they are asyncio tasks and do not survive a restart."""
    from core.database import SessionLocal
    from models import Camera

    with SessionLocal() as db:
        rows = (db.query(Camera.id, Camera.analytics_source)
                .filter(Camera.deleted_at.is_(None), Camera.is_active.is_(True))
                .all())
    wanted = [cid for cid, src in rows if effective_source(src) != "opennvr"]
    for cid in wanted:
        try:
            await sync_subscription(cid, "camera")
        except Exception:
            logger.warning("native analytics: could not re-arm camera %s", cid, exc_info=True)
    if wanted:
        logger.info("native analytics: listening to %d camera(s): %s", len(wanted), wanted)
    return len(wanted)


async def describe(db: Any, camera: Any) -> dict[str, Any]:
    """What the Events tab shows: the chosen source, the allowlist, what
    the camera advertises (events service, analytics rules) and the alert
    types native events can become."""
    native: dict[str, Any] = {"events": False, "rules": [], "driver": None, "error": None}
    try:
        from services.camera_drivers.registry import get_driver_for_camera

        driver = await get_driver_for_camera(db, camera.id)
        native["driver"] = driver.driver_name
        caps = await driver.get_capabilities()
        native["events"] = bool((caps.supported_areas or {}).get("events"))
        try:
            smart = await driver.get_smart()
            native["rules"] = [{"key": d.key, "label": d.label, "enabled": d.enabled, "configured": d.configured}
                               for d in (smart.detectors or [])]
        except Exception as exc:  # the rules list is a bonus; the events service is what matters
            native["error"] = f"rules: {exc}"[:200]
    except Exception as exc:
        native["error"] = str(getattr(exc, "detail", exc))[:200]
    from services.camera_event_manager import get_camera_event_manager

    return {
        "camera_id": camera.id,
        "analytics_source": effective_source(camera.analytics_source),
        "native_event_types": camera.native_event_types,
        "detection_enabled": camera.detection_enabled is not False,
        "listening": get_camera_event_manager().is_running(camera.id),
        "native": native,
        "alert_types": alert_type_catalog(),
    }
