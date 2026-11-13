# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Backend publishers — push HaEntity records into Home Assistant.

Two backends share one interface (``Publisher``):

* **MqttPublisher** — preferred. Publishes an MQTT discovery payload
  on the first sighting of each entity (HA auto-creates the entity
  with the right device_class / friendly name / unique_id), then
  pushes state + attribute updates on the standard state and
  attributes topics. Pairs with HA's MQTT integration, the bundled
  Mosquitto add-on, or any standalone broker.

* **RestPublisher** — fallback. POSTs to HA's
  ``/api/states/<entity_id>`` endpoint with a long-lived access
  token. Simpler to test against — no broker needed — but the
  entities don't get a device card (they're surfaced as "Set up via
  REST API"). Use only when MQTT isn't an option.

Both publishers are async-safe and reusable across many entities;
construct once at startup and call ``publish_state`` / ``publish_off``
per alert.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from opennvr_app_sdk import connect_via_proxy

from ha_mapper import HaEntity

logger = logging.getLogger(__name__)


# ── Common publisher interface ─────────────────────────────────────


class Publisher(Protocol):
    """Both backends implement this. The daemon doesn't know or care
    which one is in use."""

    async def publish_state(self, entity: HaEntity) -> bool:
        """Publish the entity's current ``state`` + attributes.
        Returns True on success, False on a recoverable failure
        (logged; the daemon decides whether to retry). Raises only
        on programmer error."""

    async def publish_off(self, entity: HaEntity) -> bool:
        """Flip the entity's state to OFF (for binary_sensor) or to
        an empty value (for sensor). Only called by the auto-off
        timer in the daemon. Same return shape as publish_state."""

    async def aclose(self) -> None:
        """Shut down the underlying connection / client. Idempotent."""


# ── MQTT backend ───────────────────────────────────────────────────


@dataclass
class MqttConfig:
    host: str = "127.0.0.1"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    discovery_prefix: str = "homeassistant"
    auto_off_seconds: int = 30
    qos: int = 1
    retain: bool = True
    client_id: str = "opennvr-ha-relay"
    keepalive_seconds: int = 60


class MqttPublisher:
    """paho-mqtt v2 client wrapped for async use. paho's API is
    callback-driven and synchronous; we drive a single loop thread
    internally and bridge with ``asyncio.to_thread`` for the
    publish call so the daemon's event loop stays unblocked.

    The set of entity ids we've already published discovery for is
    tracked in-memory — HA caches discovery payloads itself so we
    only need to publish once per entity per process lifetime.
    """

    def __init__(self, cfg: MqttConfig) -> None:
        self._cfg = cfg
        self._discovered: set[str] = set()
        self._client: Any = None
        self._connect_lock = asyncio.Lock()
        # Set by ``_on_connect`` once the broker returns CONNACK.
        # First publish awaits this so we don't return ``ok=True``
        # for a packet that was only queued locally (paho's
        # publish() returns rc=0 for "queued", not "ack from
        # broker"). 5-second cap aligns with paho's connect timeout.
        self._connack_event: asyncio.Event | None = None
        self._connack_loop: asyncio.AbstractEventLoop | None = None

    async def _ensure_connected(self) -> None:
        if self._client is not None:
            # Fast path — already connected. CONNACK was awaited the
            # first time around; subsequent reconnects are handled
            # by paho's loop thread.
            return
        async with self._connect_lock:
            if self._client is not None:
                return
            # Lazy import — keeps the module importable on test envs
            # that don't have paho-mqtt yet (CI matrix doesn't ship
            # it for the unit tests).
            import paho.mqtt.client as mqtt  # type: ignore

            # paho-mqtt 2.x default protocol is MQTTv311; we rely on
            # the default rather than passing it explicitly because
            # the constant has moved between minor versions
            # (``mqtt.MQTTv311`` exists in 2.1 but newer versions
            # surface it as ``mqtt.MQTTProtocolVersion.MQTTv311``).
            # Relying on the default keeps us compatible across the
            # whole >=2.0,<3.0 range. ``clean_session=False`` only
            # has meaning under v3.x — its purpose is "broker
            # remembers our subscriptions across reconnects", which
            # is what we want for the discovery topics. For v5
            # session-expiry semantics, an operator would fork to
            # use protocol=MQTTProtocolVersion.MQTTv5 + clean_start.
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=self._cfg.client_id,
                clean_session=False,
            )
            if self._cfg.username:
                client.username_pw_set(self._cfg.username, self._cfg.password or "")
            # On a deployment that runs apps on the internal network the
            # broker is only reachable through the egress proxy: MQTT is
            # plain TCP, so it rides an HTTP CONNECT tunnel like every
            # other outbound call (paho + PySocks). Direct when no proxy
            # is set or the broker is on NO_PROXY.
            via = connect_via_proxy(self._cfg.host, self._cfg.port)
            if via is not None:
                import socks  # type: ignore  # PySocks, a declared dependency

                client.proxy_set(proxy_type=socks.HTTP, proxy_addr=via[0], proxy_port=via[1])
                logger.info("mqtt: tunnelling to %s:%d through the egress proxy %s:%d",
                            self._cfg.host, self._cfg.port, via[0], via[1])

            # CONNACK signalling: paho's _on_connect fires on its
            # network thread, not on our event loop. We stash the
            # loop here so the callback can schedule the
            # ``set()`` thread-safely.
            self._connack_event = asyncio.Event()
            self._connack_loop = asyncio.get_running_loop()

            def _on_connect(client_, userdata, flags, reason_code, properties):
                if reason_code != 0:
                    logger.warning(
                        "mqtt connect failed: %s (broker=%s:%d)",
                        reason_code, self._cfg.host, self._cfg.port,
                    )
                    # Don't set the event — first publish will time
                    # out and return False, which is what we want.
                    return
                logger.info(
                    "mqtt connected to %s:%d (discovery_prefix=%s)",
                    self._cfg.host, self._cfg.port, self._cfg.discovery_prefix,
                )
                ev = self._connack_event
                loop = self._connack_loop
                if ev is not None and loop is not None:
                    loop.call_soon_threadsafe(ev.set)

            client.on_connect = _on_connect

            def _connect_blocking() -> None:
                client.connect(
                    self._cfg.host, self._cfg.port, self._cfg.keepalive_seconds,
                )
                # paho's loop_start spins a background thread that
                # services the network reads + reconnects. Cheaper
                # than driving the loop ourselves.
                client.loop_start()

            try:
                await asyncio.to_thread(_connect_blocking)
            except Exception as exc:
                # Sanitise — paho exceptions can include the URL
                # but never (we hope) the password; still, scrub
                # by class name + host only.
                logger.warning(
                    "mqtt connect to %s:%d raised %s",
                    self._cfg.host, self._cfg.port, type(exc).__name__,
                )
                raise
            self._client = client

        # Wait for CONNACK with a generous timeout. Outside the lock
        # so a concurrent caller doesn't block on the same wait.
        try:
            await asyncio.wait_for(self._connack_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning(
                "mqtt: no CONNACK from %s:%d within 5s; publishes may "
                "queue locally without broker ack until reconnect",
                self._cfg.host, self._cfg.port,
            )

    async def publish_state(self, entity: HaEntity) -> bool:
        try:
            await self._ensure_connected()
        except Exception:
            logger.exception("mqtt: could not connect")
            return False

        await self._maybe_publish_discovery(entity)

        state_topic = self._state_topic(entity)
        attrs_topic = self._attributes_topic(entity)
        ok_state = await self._publish_raw(
            state_topic, entity.state, retain=self._cfg.retain
        )
        ok_attrs = await self._publish_raw(
            attrs_topic, json.dumps(entity.attributes), retain=self._cfg.retain
        )
        return ok_state and ok_attrs

    async def publish_off(self, entity: HaEntity) -> bool:
        try:
            await self._ensure_connected()
        except Exception:
            logger.exception("mqtt: could not connect for off-publish")
            return False
        off_value = "OFF" if entity.component == "binary_sensor" else ""
        return await self._publish_raw(
            self._state_topic(entity), off_value, retain=self._cfg.retain
        )

    async def aclose(self) -> None:
        if self._client is None:
            return
        client = self._client
        self._client = None

        def _disconnect_blocking() -> None:
            try:
                client.loop_stop()
            except Exception:
                logger.exception("mqtt loop_stop failed")
            try:
                client.disconnect()
            except Exception:
                logger.exception("mqtt disconnect failed")

        await asyncio.to_thread(_disconnect_blocking)

    # ── Internals ──────────────────────────────────────────────────

    async def _maybe_publish_discovery(self, entity: HaEntity) -> None:
        if entity.full_entity_id in self._discovered:
            return
        topic = self._discovery_topic(entity)
        payload = self._discovery_payload(entity)
        ok = await self._publish_raw(topic, json.dumps(payload), retain=True)
        if ok:
            self._discovered.add(entity.full_entity_id)
            logger.info(
                "mqtt discovery published for %s", entity.full_entity_id,
            )

    def _discovery_topic(self, entity: HaEntity) -> str:
        return (
            f"{self._cfg.discovery_prefix}/{entity.component}/"
            f"{entity.unique_id}/config"
        )

    def _state_topic(self, entity: HaEntity) -> str:
        return f"opennvr/{entity.component}/{entity.object_id}/state"

    def _attributes_topic(self, entity: HaEntity) -> str:
        return f"opennvr/{entity.component}/{entity.object_id}/attributes"

    def _discovery_payload(self, entity: HaEntity) -> dict[str, Any]:
        # Device identifier is derived from the discovery_prefix so
        # two OpenNVR instances publishing to the same HA via the
        # same broker (an operator running a primary + secondary
        # NVR) get two distinct devices instead of grafting onto
        # one. The recommended multi-instance setup is to ALSO
        # remap discovery_prefix per-instance — see README.
        device_identifier = f"opennvr_{self._cfg.discovery_prefix}"
        payload: dict[str, Any] = {
            "name": entity.name,
            "unique_id": entity.unique_id,
            "state_topic": self._state_topic(entity),
            "json_attributes_topic": self._attributes_topic(entity),
            # Group everything under one virtual device so HA's
            # device-card view shows "OpenNVR" with all sensors
            # underneath, not N loose entities.
            "device": {
                "identifiers": [device_identifier],
                "name": "OpenNVR",
                "manufacturer": "OpenNVR",
                "model": "ai-adapter relay",
            },
        }
        if entity.device_class:
            payload["device_class"] = entity.device_class
        if entity.component == "binary_sensor":
            payload["payload_on"] = "ON"
            payload["payload_off"] = "OFF"
        return payload

    async def _publish_raw(
        self, topic: str, value: str, *, retain: bool
    ) -> bool:
        client = self._client
        if client is None:
            return False

        def _publish() -> int:
            info = client.publish(topic, value, qos=self._cfg.qos, retain=retain)
            # paho returns an MQTTMessageInfo; .rc == 0 means queued.
            return int(info.rc)

        try:
            rc = await asyncio.to_thread(_publish)
        except Exception:
            logger.exception("mqtt publish to %s failed", topic)
            return False
        if rc != 0:
            logger.warning("mqtt publish to %s returned rc=%d", topic, rc)
            return False
        return True


# ── REST backend ───────────────────────────────────────────────────


@dataclass
class RestConfig:
    url: str                       # base, e.g. http://homeassistant.local:8123
    token: str                     # long-lived access token
    timeout_seconds: float = 5.0
    auto_off_seconds: int = 30


class RestPublisher:
    """POST to HA's ``/api/states/<entity_id>``. HA creates the entity
    on the first request and updates state on subsequent ones. No
    discovery dance — entities surface as "Set up via REST API"
    which works but doesn't get a device card. Useful for testing
    or for users who can't run MQTT."""

    def __init__(self, cfg: RestConfig) -> None:
        self._cfg = cfg
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._cfg.timeout_seconds,
                base_url=self._cfg.url.rstrip("/"),
                headers={
                    "Authorization": f"Bearer {self._cfg.token}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def publish_state(self, entity: HaEntity) -> bool:
        return await self._post_state(entity, entity.state)

    async def publish_off(self, entity: HaEntity) -> bool:
        off_value = "OFF" if entity.component == "binary_sensor" else ""
        return await self._post_state(entity, off_value)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── Internals ──────────────────────────────────────────────────

    async def _post_state(self, entity: HaEntity, state_value: str) -> bool:
        path = f"/api/states/{entity.full_entity_id}"
        body: dict[str, Any] = {
            "state": state_value,
            "attributes": {
                **entity.attributes,
                "friendly_name": entity.name,
            },
        }
        if entity.device_class:
            body["attributes"]["device_class"] = entity.device_class
        try:
            resp = await self._http().post(path, json=body)
        except Exception:
            logger.exception("rest publish to %s failed", path)
            return False
        if resp.status_code >= 400:
            # Deliberately NOT logging resp.text — some reverse
            # proxies (Traefik, nginx) echo the original request's
            # Authorization header in their 401/403 error pages,
            # which would leak the long-lived access token into
            # ops logs. Status + path is enough to debug.
            logger.warning(
                "ha REST %s returned %d", path, resp.status_code,
            )
            return False
        return True
