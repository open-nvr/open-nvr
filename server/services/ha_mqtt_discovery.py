# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Home Assistant MQTT discovery, generated from the entity descriptors (HA-402/403).

The zero-install way into Home Assistant: HA's own MQTT integration finds
OpenNVR's devices and entities on the broker. It is the lighter path (no
live video, media browser, actions or Assist; the native integration has
those), and it describes exactly what the native one does, because both
come from the same descriptors (design §6.10, §8).

* **Discovery:** device-based, one retained config per device
  (``<discovery>/device/opennvr_<site>_<device>/config``) with ``dev``, ``o``
  and ``cmps``: the server, each camera, each zone, each app device.
  Re-published when Home Assistant announces itself (``<discovery>/status``
  = ``online``) and when the catalogue changes; a component that disappears
  is removed (sent with its platform only).
* **States:** ``<prefix>/<site>/<key>/state`` (retained, plain: ``ON``/``OFF``,
  a number, an option) and ``.../attributes`` (JSON), from the same pushes the
  events socket carries.
* **Events:** ``<prefix>/<site>/<key>/event``, a CloudEvents 1.0 envelope
  (structured mode); HA reads ``data``.
* **Availability:** ``<prefix>/<site>/status``: ``online``, and the broker's
  Last Will says ``offline``.
* **Commands:** ``<prefix>/<site>/<key>/set`` runs the descriptor's typed
  command (services/entity_commands.py) as the integration's API token:
  its scopes and cameras, audited with actor ``mqtt:<integration name>``.
  The site mode panel's ``site_mode/set`` needs ``settings.manage`` (without
  it, site mode is a read-only sensor). Retained command messages are
  ignored (they would run again at every connect); commands queue (32) and
  are rate-limited per bridge. Anyone who can publish to the broker can send
  what the token allows: secure the broker. A token restricted to source
  addresses can't be bound (commands arrive from the broker, not an address).
* **Keys in topics:** a descriptor key is one topic level (``topic_key``:
  anything but letters, digits, ``_ . -`` becomes ``_``).
* **Removal:** a component or device that disappears is removed from Home
  Assistant and its retained state cleared. A deleted, disabled or moved
  integration has everything it published cleared by a one-shot connection
  (``clear_published``), whether or not its bridge was connected.

Only what the bound token may see is published; a revoked token stops the
bridge (it goes ``offline``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

#: Platforms MQTT discovery renders (image and camera need the native integration).
MQTT_PLATFORMS = ("sensor", "binary_sensor", "switch", "select", "button", "number", "event")
SITE_MODE_KEY = "site_mode"
_ALARM_COMMANDS = {"ARM_HOME": "armed_home", "ARM_AWAY": "armed_away", "DISARM": "disarmed"}
RECHECK_S = 60.0
#: Commands per bridge: at most COMMAND_LIMIT per COMMAND_WINDOW_S; a
#: bounded queue in front.
COMMAND_LIMIT = 20
COMMAND_WINDOW_S = 10.0
COMMAND_QUEUE = 32
#: How long ``clear_published`` listens for retained topics.
CLEAR_COLLECT_S = 2.0


def _slug(value: Any) -> str:
    return "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in str(value))


def topic_key(key: str) -> str:
    """A descriptor key as one topic level: app ids are free text, and
    ``+``, ``#`` or ``/`` in a topic would break publishing or routing."""
    return "".join(ch if ch.isascii() and (ch.isalnum() or ch in "_.-") else "_"
                   for ch in str(key))


@dataclass(frozen=True)
class Topics:
    prefix: str
    discovery: str
    site: str

    @property
    def site_slug(self) -> str:
        return self.site.replace("-", "")[:12]

    @property
    def status(self) -> str:
        return f"{self.prefix}/{self.site_slug}/status"

    def state(self, key: str) -> str:
        return f"{self.prefix}/{self.site_slug}/{topic_key(key)}/state"

    def attributes(self, key: str) -> str:
        return f"{self.prefix}/{self.site_slug}/{topic_key(key)}/attributes"

    def command(self, key: str) -> str:
        return f"{self.prefix}/{self.site_slug}/{topic_key(key)}/set"

    def event(self, key: str) -> str:
        return f"{self.prefix}/{self.site_slug}/{topic_key(key)}/event"

    @property
    def command_filter(self) -> str:
        return f"{self.prefix}/{self.site_slug}/+/set"

    def key_of_command(self, topic: str) -> str | None:
        """The topic level of a command topic (a ``topic_key``), or None."""
        head, tail = f"{self.prefix}/{self.site_slug}/", "/set"
        if topic.startswith(head) and topic.endswith(tail):
            key = topic[len(head):-len(tail)]
            return key if key and "/" not in key else None
        return None

    def device_config(self, device_key: str) -> str:
        return f"{self.discovery}/device/opennvr_{self.site_slug}_{device_key}/config"


def device_key(device: dict[str, Any]) -> str:
    kind, dev_id = device.get("kind"), device.get("id")
    if kind in ("camera", "zone", "app") and dev_id is not None:
        return f"{kind}_{_slug(dev_id)}"
    return "site"


def _device_block(topics: Topics, key: str, device: dict[str, Any], names: dict[str, str],
                  version: str) -> dict[str, Any]:
    ident = f"opennvr_{topics.site_slug}_{key}"
    block: dict[str, Any] = {"ids": [ident], "mf": "OpenNVR", "sw": version,
                             "name": names.get(key) or device.get("name") or key}
    kind = device.get("kind")
    if kind == "zone" and device.get("camera_id") is not None:
        block["via_device"] = f"opennvr_{topics.site_slug}_camera_{device['camera_id']}"
        block["mdl"] = "Zone"
    elif kind in ("camera", "app"):
        block["via_device"] = f"opennvr_{topics.site_slug}_site"
        block["mdl"] = "Camera" if kind == "camera" else "App"
    else:
        block["mdl"] = "OpenNVR server"
    return block


def component_id(key: str) -> str:
    """The component's id within its device's ``cmps``."""
    return f"opennvr_{_slug(key)}"


def component(topics: Topics, desc: dict[str, Any]) -> dict[str, Any] | None:
    """The discovery component for one descriptor (its ``to_dict()``), or
    None for a platform MQTT doesn't render."""
    platform, key = desc["platform"], desc["key"]
    if platform not in MQTT_PLATFORMS:
        return None
    c: dict[str, Any] = {"p": platform, "uniq_id": f"opennvr_{topics.site_slug}_{_slug(key)}",
                         "name": desc["name"], "en": bool(desc.get("enabled_default", True))}
    for src, dst in (("device_class", "dev_cla"), ("unit", "unit_of_meas"),
                     ("state_class", "stat_cla"), ("entity_category", "ent_cat"),
                     ("icon", "ic")):
        if desc.get(src):
            c[dst] = desc[src]
    if platform == "event":
        c["stat_t"] = topics.event(key)
        c["evt_typ"] = list(desc.get("event_types") or [])
        c["val_tpl"] = "{{ value_json.data | tojson }}"
        return c
    if platform != "button":
        c["stat_t"] = topics.state(key)
        c["json_attr_t"] = topics.attributes(key)
    if platform in ("switch", "select", "button", "number"):
        c["cmd_t"] = topics.command(key)
    if platform in ("select",) or (platform == "sensor" and desc.get("device_class") == "enum"):
        c["ops"] = [str(o) for o in desc.get("options") or []]
    if platform == "number" and isinstance(desc.get("options"), dict):
        for src, dst in (("min", "min"), ("max", "max"), ("step", "step")):
            if isinstance(desc["options"].get(src), (int, float)):
                c[dst] = desc["options"][src]
    return c


def build_discovery(topics: Topics, version: str, descriptors: list[dict[str, Any]],
                    device_names: dict[str, str], *, site_mode: bool,
                    site_mode_control: bool = True) -> dict[str, dict]:
    """``{device_key: discovery payload}`` for every device with components.
    Site mode is an alarm panel when the token may change it, else a sensor
    (HA's MQTT alarm panel requires a command topic)."""
    devices: dict[str, dict[str, Any]] = {}
    for desc in descriptors:
        comp = component(topics, desc)
        if comp is None:
            continue
        key = device_key(desc.get("device") or {})
        dev = devices.setdefault(key, {
            "dev": _device_block(topics, key, desc.get("device") or {}, device_names, version),
            "o": {"name": "OpenNVR", "sw": version,
                  "url": "https://github.com/open-nvr/open-nvr"},
            "avty_t": topics.status, "cmps": {}})
        dev["cmps"][component_id(desc["key"])] = comp
    site = devices.setdefault("site", {
        "dev": _device_block(topics, "site", {"kind": "site"}, device_names, version),
        "o": {"name": "OpenNVR", "sw": version, "url": "https://github.com/open-nvr/open-nvr"},
        "avty_t": topics.status, "cmps": {}})
    if site_mode and site_mode_control:
        site["cmps"]["opennvr_site_mode"] = {
            "p": "alarm_control_panel", "uniq_id": f"opennvr_{topics.site_slug}_site_mode",
            "name": "Site mode",
            "stat_t": topics.state(SITE_MODE_KEY), "cmd_t": topics.command(SITE_MODE_KEY),
            "code_arm_required": False, "sup_feat": ["arm_home", "arm_away"]}
    elif site_mode:
        site["cmps"]["opennvr_site_mode_state"] = {
            "p": "sensor", "uniq_id": f"opennvr_{topics.site_slug}_site_mode_state",
            "name": "Site mode", "dev_cla": "enum", "stat_t": topics.state(SITE_MODE_KEY),
            "ops": ["disarmed", "armed_home", "armed_away"]}
    return devices


def state_payload(platform: str, state: Any) -> str:
    """The plain value HA's MQTT entity reads."""
    if state is None:
        return "None"
    if platform in ("binary_sensor", "switch"):
        return "ON" if state else "OFF"
    if isinstance(state, bool):
        return "true" if state else "false"
    if isinstance(state, (dict, list)):
        return json.dumps(state)
    return str(state)


def command_value(platform: str, payload: str) -> Any:
    """What a ``.../set`` payload means for the descriptor's command; raises
    ValueError for one that doesn't fit."""
    text = payload.strip()
    if platform == "switch":
        if text.upper() in ("ON", "TRUE", "1"):
            return True
        if text.upper() in ("OFF", "FALSE", "0"):
            return False
        raise ValueError("switch: ON or OFF")
    if platform == "button":
        return None
    if platform == "number":
        value = float(text)
        return int(value) if value.is_integer() else value
    if platform == "select":
        return text
    raise ValueError(f"{platform} takes no commands")


def cloud_event(site_id: str, key: str, event: dict[str, Any]) -> dict[str, Any]:
    """A CloudEvents 1.0 envelope (structured JSON) for an entity event."""
    kind = str(event.get("type") or "event")
    attrs = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
    return {"specversion": "1.0", "id": uuid.uuid4().hex,
            "source": f"/opennvr/{site_id}/entities/{key}",
            "type": f"com.opennvr.{key.split('.')[-1]}", "subject": kind,
            "time": datetime.now(UTC).isoformat(), "datacontenttype": "application/json",
            "data": {"event_type": kind, **attrs}}


# ── the bridge ───────────────────────────────────────────────────────────


class MqttBridge:
    """One MQTT integration with Home Assistant discovery on: keeps the
    broker connection, publishes, and runs commands, until stopped."""

    def __init__(self, integration_id: int, name: str, config: dict[str, Any]) -> None:
        from services.mqtt_settings import MqttSettings

        self.integration_id = integration_id
        self.name = name
        self.settings = MqttSettings.from_config(config)
        #: device key -> {component id: (descriptor key, platform)} as published.
        self._published: dict[str, dict[str, tuple[str, str]]] = {}
        self._platform: dict[str, str] = {}
        #: topic level -> descriptor key (``topic_key`` is lossy).
        self._by_topic: dict[str, str] = {}
        self._stop = asyncio.Event()
        self._commands: asyncio.Queue[tuple[str, str]] = asyncio.Queue(COMMAND_QUEUE)
        self._command_times: deque[float] = deque()
        self.config = dict(config)
        self.state = "starting"

    # -- principal and catalogue (sync, run in a thread) --

    def _principal(self, db):
        from models import ApiToken, User
        from services import api_tokens

        row = db.query(ApiToken).filter(ApiToken.id == self.settings.api_token_id,
                                        ApiToken.parent_id.is_(None)).first()
        if row is None or not api_tokens._row_live(row):
            return None
        if row.allowed_cidrs:  # its commands would come from the broker, not an address
            return None
        owner = db.query(User).filter(User.id == row.owner_user_id).first()
        if owner is None or not owner.is_active:
            return None
        return api_tokens._principal(owner, row)

    def _snapshot(self) -> dict[str, Any] | None:
        from core.database import SessionLocal
        from models import Camera, CameraZone
        from services import (
            api_tokens,
            entity_descriptors as ed,
            site_mode,
            site_settings,
        )

        with SessionLocal() as db:
            principal = self._principal(db)
            if principal is None:
                return None
            descs = ed.descriptors_for(db, principal)
            names = {"site": site_settings.get_site_name(db)}
            for cam in db.query(Camera).filter(Camera.deleted_at.is_(None)).all():
                names[f"camera_{cam.id}"] = cam.name
            for zone in db.query(CameraZone).all():
                names[f"zone_{zone.id}"] = zone.name
            held = ed.held_scopes(principal)
            return {
                "site_id": site_settings.get_site_id(db),
                "descriptors": [d.to_dict() for d in descs],
                "names": names, "held": held, "cameras": principal.camera_ids,
                "event_types": api_tokens.token_event_types(principal),
                "site_mode": site_mode.get(db) if "settings.view" in held else None,
            }

    # -- lifecycle --

    async def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # reconnect whatever broke
                self.state = "disconnected"
                logger.warning("MQTT bridge %r: %s", self.name, exc)
            if self._stop.is_set():
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), backoff * random.uniform(0.8, 1.2))
            backoff = min(backoff * 2, 60.0)
        self.state = "stopped"

    async def _session(self) -> None:
        import aiomqtt

        from services.event_bus_service import get_event_bus
        from services.mqtt_settings import check_resolved, client
        from services.version import server_version

        await check_resolved(self.settings.host)
        snap = await asyncio.to_thread(self._snapshot)
        if snap is None:
            self.state = "token_invalid"
            logger.warning("MQTT bridge %r: its API token is revoked, expired or gone",
                           self.name)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), RECHECK_S)
            return
        topics = Topics(self.settings.topic_prefix, self.settings.discovery_prefix,
                        snap["site_id"])
        will = aiomqtt.Will(topics.status, "offline", qos=1, retain=True)
        async with client(self.settings, identifier=f"opennvr-{topics.site_slug}-"
                                                    f"{self.integration_id}",
                          will=will) as mq, get_event_bus().subscribe(
                with_seq=True, allowed_camera_ids=snap["cameras"],
                allowed_event_types=snap["event_types"],
                event_types={"entity_state", "descriptors_changed", "site_mode"}) as sub:
            self.state = "connected"
            await mq.publish(topics.status, "online", qos=1, retain=True)
            await mq.subscribe(f"{self.settings.discovery_prefix}/status")
            await mq.subscribe(topics.command_filter)
            await self._publish_all(mq, topics, snap, server_version())
            tasks = [asyncio.create_task(self._messages(mq, topics)),
                     asyncio.create_task(self._command_worker()),
                     asyncio.create_task(self._pump(mq, topics, sub)),
                     asyncio.create_task(self._recheck(mq, topics)),
                     asyncio.create_task(self._stop.wait())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if not task.cancelled() and task.exception():
                        raise task.exception()
            finally:
                for task in tasks:
                    task.cancel()
                with contextlib.suppress(Exception):
                    await mq.publish(topics.status, "offline", qos=1, retain=True)

    # -- publishing --

    async def _publish_all(self, mq, topics: Topics, snap: dict[str, Any], version: str,
                           states: dict[str, dict] | None = None) -> None:
        from services import entity_state_publisher as pub

        self._snap = snap
        control = "settings.manage" in snap["held"]
        devices = build_discovery(topics, version, snap["descriptors"], snap["names"],
                                  site_mode=snap["site_mode"] is not None,
                                  site_mode_control=control)
        self._platform = {d["key"]: d["platform"] for d in snap["descriptors"]}
        self._by_topic = {topic_key(k): k for k in self._platform}
        key_of = {component_id(k): k for k in self._platform}
        key_of["opennvr_site_mode"] = key_of["opennvr_site_mode_state"] = SITE_MODE_KEY
        gone_keys: set[str] = set()
        published: dict[str, dict[str, tuple[str, str]]] = {}
        for dev, payload in devices.items():
            now = {obj: (key_of.get(obj, obj), c["p"]) for obj, c in payload["cmps"].items()}
            for obj, (key, platform) in self._published.get(dev, {}).items():
                if obj not in now:  # removal: the component with its platform only
                    payload["cmps"][obj] = {"p": platform}
                    gone_keys.add(key)
            await mq.publish(topics.device_config(dev), json.dumps(payload), qos=1,
                             retain=True)
            published[dev] = now
        for dev in set(self._published) - set(devices):  # a whole device went
            await mq.publish(topics.device_config(dev), None, qos=1, retain=True)
            gone_keys.update(key for key, _ in self._published[dev].values())
        self._published = published
        still = {key for comps in published.values() for key, _ in comps.values()}
        for key in gone_keys - still:  # no stale retained values on the broker
            await mq.publish(topics.state(key), None, qos=1, retain=True)
            await mq.publish(topics.attributes(key), None, qos=1, retain=True)
        await pub.warm()
        allowed = {d["key"] for d in snap["descriptors"]}
        for key, value in (states if states is not None else pub.current_states()).items():
            if key in allowed:
                await self._publish_state(mq, topics, key, value)
        if snap["site_mode"] is not None:
            await mq.publish(topics.state(SITE_MODE_KEY), snap["site_mode"]["mode"], qos=1,
                             retain=True)

    async def _publish_state(self, mq, topics: Topics, key: str, value: dict) -> None:
        platform = self._platform.get(key)
        if platform is None or platform in ("button", "event") \
                or platform not in MQTT_PLATFORMS:
            return
        await mq.publish(topics.state(key), state_payload(platform, value.get("state")),
                         qos=1, retain=True)
        await mq.publish(topics.attributes(key), json.dumps(value.get("attributes") or {},
                                                            default=str),
                         qos=1, retain=True)

    async def _pump(self, mq, topics: Topics, sub) -> None:
        from services.version import server_version

        while True:
            _seq, event = await sub.queue.get()
            kind = event.get("event_type")
            payload = event.get("payload") or {}
            if kind == "entity_state":
                if event.get("required_scope") not in self._snap["held"]:
                    continue
                key = payload.get("key")
                if not isinstance(key, str) or key not in self._platform:
                    continue
                if isinstance(payload.get("event"), dict):
                    await mq.publish(topics.event(key), json.dumps(
                        cloud_event(self._snap["site_id"], key, payload["event"]),
                        default=str), qos=1)
                else:
                    await self._publish_state(mq, topics, key, payload)
            elif kind == "site_mode" and self._snap["site_mode"] is not None:
                if payload.get("mode"):
                    await mq.publish(topics.state(SITE_MODE_KEY), payload["mode"], qos=1,
                                     retain=True)
            elif kind == "descriptors_changed":
                snap = await asyncio.to_thread(self._snapshot)
                if snap is None:
                    return  # token gone: end the session
                await self._publish_all(mq, topics, snap, server_version())

    async def _recheck(self, mq, topics: Topics) -> None:
        """A revoked token ends the session (and publishing) within a minute."""
        while True:
            await asyncio.sleep(RECHECK_S)
            if await asyncio.to_thread(self._token_alive) is False:
                logger.warning("MQTT bridge %r: its API token is no longer valid", self.name)
                return

    def _token_alive(self) -> bool:
        from core.database import SessionLocal

        with SessionLocal() as db:
            return self._principal(db) is not None

    # -- incoming --

    async def _messages(self, mq, topics: Topics) -> None:
        from services.version import server_version

        async for message in mq.messages:
            if getattr(message, "retain", False):
                # A retained command would run again at every connect (and a
                # retained birth adds nothing: discovery went out on connect).
                continue
            topic = str(message.topic)
            payload = message.payload.decode("utf-8", "replace") \
                if isinstance(message.payload, (bytes, bytearray)) else str(message.payload)
            if topic == f"{self.settings.discovery_prefix}/status":
                if payload.strip().lower() == "online":
                    # Home Assistant restarted: it wants discovery again.
                    snap = await asyncio.to_thread(self._snapshot)
                    if snap is None:
                        return
                    await self._publish_all(mq, topics, snap, server_version())
                continue
            level = topics.key_of_command(topic)
            key = SITE_MODE_KEY if level == SITE_MODE_KEY else self._by_topic.get(level or "")
            if key is None:
                continue
            try:
                self._commands.put_nowait((key, payload[:256]))
            except asyncio.QueueFull:
                logger.warning("MQTT bridge %r: command queue full, %s dropped",
                               self.name, key)

    async def _command_worker(self) -> None:
        """One command at a time, at most COMMAND_LIMIT per window."""
        while True:
            key, payload = await self._commands.get()
            now = time.monotonic()
            while self._command_times and now - self._command_times[0] > COMMAND_WINDOW_S:
                self._command_times.popleft()
            if len(self._command_times) >= COMMAND_LIMIT:
                logger.warning("MQTT bridge %r: too many commands, %s dropped", self.name, key)
                continue
            self._command_times.append(now)
            await self._command(key, payload)

    async def _command(self, key: str, payload: str) -> None:
        from fastapi import HTTPException

        try:
            await asyncio.wait_for(self._run_command(key, payload), 30)
        except (HTTPException, ValueError, TimeoutError) as exc:
            detail = getattr(exc, "detail", None) or str(exc)
            logger.info("MQTT bridge %r: command %s refused: %s", self.name, key, detail)
        except Exception:  # never end the session over one command
            logger.exception("MQTT bridge %r: command %s failed", self.name, key)

    async def _run_command(self, key: str, payload: str) -> None:
        from fastapi import HTTPException

        from core.database import SessionLocal
        from core.request_context import begin_request, end_request
        from services.audit_service import write_audit_log

        # Everything that needs no database first: a malformed flood costs
        # nothing but parsing.
        if key == SITE_MODE_KEY:
            mode = _ALARM_COMMANDS.get(payload.strip().upper())
            if mode is None:
                raise ValueError("site mode: ARM_HOME, ARM_AWAY or DISARM")
        else:
            platform = self._platform.get(key)
            if platform is None:
                raise HTTPException(status_code=404, detail="No such entity")
            value = command_value(platform, payload)
        ctx, token = begin_request(f"mqtt-{uuid.uuid4().hex[:12]}")
        ctx.actor = f"mqtt:{self.name}"
        try:
            with SessionLocal() as db:
                principal = self._principal(db)
                if principal is None:
                    raise HTTPException(status_code=401, detail="API token not valid")
                if key == SITE_MODE_KEY:
                    detail = await self._set_site_mode(db, principal, mode)
                else:
                    from services.entity_commands import run_command

                    desc, _result = await run_command(db, principal, key, value)
                    detail = {"key": key, "command": desc.command, "value": value,
                              "camera_id": desc.camera_id}
                write_audit_log(db, action=("site_mode.set" if key == SITE_MODE_KEY
                                            else "entity.command"),
                                user_id=principal.id, entity_type="entity", details=detail)
        finally:
            end_request(token)

    async def _set_site_mode(self, db, principal, mode: str) -> dict[str, Any]:
        from fastapi import HTTPException

        from services import api_tokens, site_mode
        from services.event_bus_service import publish_site_mode

        if not api_tokens.token_has_permission(principal, "settings.manage"):
            raise HTTPException(status_code=403, detail="Needs settings.manage")
        value = site_mode.set_mode(db, mode, f"mqtt:{self.name}")
        await publish_site_mode(value)
        return {"key": SITE_MODE_KEY, "mode": mode}


# ── the manager ──────────────────────────────────────────────────────────


async def clear_published(settings) -> int:
    """Remove everything this site published with ``settings``: every retained
    discovery config of its devices (Home Assistant drops them), then its
    retained states and status. One short connection of its own, so it works
    whether or not a bridge was connected, and after a restart. Returns how
    many topics were cleared."""
    from services import site_settings
    from services.mqtt_settings import client

    def site_id() -> str:
        from core.database import SessionLocal

        with SessionLocal() as db:
            return site_settings.get_site_id(db)

    topics = Topics(settings.topic_prefix, settings.discovery_prefix,
                    await asyncio.to_thread(site_id))
    device_head = f"{settings.discovery_prefix}/device/opennvr_{topics.site_slug}_"
    configs: set[str] = set()
    others: set[str] = set()
    async with client(settings) as mq:
        # A ``+`` must be a whole level, so the device filter is narrowed here.
        await mq.subscribe(f"{settings.discovery_prefix}/device/+/config")
        await mq.subscribe(f"{settings.topic_prefix}/{topics.site_slug}/#")

        async def collect() -> None:
            async for message in mq.messages:
                topic = str(message.topic)
                if not getattr(message, "retain", False) or not message.payload:
                    continue
                if topic.startswith(device_head):
                    configs.add(topic)
                elif topic.startswith(f"{settings.topic_prefix}/{topics.site_slug}/"):
                    others.add(topic)

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(collect(), CLEAR_COLLECT_S)
        for topic in sorted(configs) + sorted(others):  # devices first
            await mq.publish(topic, None, qos=1, retain=True)
    return len(configs) + len(others)


class _Manager:
    def __init__(self) -> None:
        self._bridges: dict[int, tuple[MqttBridge, asyncio.Task]] = {}
        self._lock: asyncio.Lock | None = None

    def active(self) -> bool:
        return any(b.state == "connected" for b, _ in self._bridges.values())

    def states(self) -> dict[int, str]:
        return {i: b.state for i, (b, _) in self._bridges.items()}

    async def reload(self) -> None:
        """Start, restart or stop bridges to match the MQTT integrations.
        One reload at a time: overlapping ones would start a bridge twice."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            await self._reload()

    async def _reload(self) -> None:
        from core.background_tasks import spawn_background

        wanted: dict[int, MqttBridge] = {}
        for iid, name, config in await asyncio.to_thread(_wanted_integrations):
            try:
                bridge = MqttBridge(iid, name, config)
            except ValueError as exc:
                logger.warning("MQTT integration %r not started: %s", name, exc)
                continue
            if bridge.settings.ha_discovery:
                wanted[iid] = bridge
        for iid in list(self._bridges):
            old = self._bridges[iid][0]
            new = wanted.get(iid)
            if new is not None and (new.name, new.config) == (old.name, old.config):
                wanted.pop(iid)  # unchanged: keep it running
                continue
            # Gone, discovery off, or published elsewhere now: take its devices
            # out of Home Assistant. A restart in place keeps them.
            await self._stop_one(iid, clear=new is None or _moved(old, new))
        for iid, bridge in wanted.items():
            task = spawn_background(bridge.run(), name=f"mqtt-bridge-{iid}")
            self._bridges[iid] = (bridge, task)

    async def _stop_one(self, iid: int, *, clear: bool = False) -> None:
        bridge, task = self._bridges.pop(iid)
        await bridge.stop()
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
            await asyncio.wait_for(task, 5)
        if clear:
            try:
                n = await asyncio.wait_for(clear_published(bridge.settings), 15)
                logger.info("MQTT bridge %r: cleared %d topics", bridge.name, n)
            except Exception as exc:  # the broker is down: nothing more to do
                logger.warning("MQTT bridge %r: could not clear its topics: %s",
                               bridge.name, exc)

    async def stop(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            for iid in list(self._bridges):
                await self._stop_one(iid)


def _moved(old: MqttBridge, new: MqttBridge) -> bool:
    """Would the new settings publish to a different place (or as a
    different token, so a different set of entities)?"""
    a, b = old.settings, new.settings
    return (a.host, a.port, a.discovery_prefix, a.topic_prefix, a.api_token_id) != \
        (b.host, b.port, b.discovery_prefix, b.topic_prefix, b.api_token_id)


def _wanted_integrations() -> list[tuple[int, str, dict]]:
    from core.database import SessionLocal
    from models import Integration

    with SessionLocal() as db:
        return [(r.id, r.name, dict(r.config or {})) for r in db.query(Integration)
                .filter(Integration.enabled.is_(True)).all()
                if str(getattr(r.type, "value", r.type)) == "mqtt"]


manager = _Manager()
