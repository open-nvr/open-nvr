# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
home-assistant-relay — bridge OpenNVR alerts into Home Assistant,
now on the ``opennvr-app-sdk``.

Subscribes to ``opennvr.alerts.>`` on NATS, runs each alert through
the HA mapper (alert envelope → HA entity definition + state),
publishes via either MQTT discovery or HA's REST API, and schedules
an auto-off flip for binary_sensors after a configurable window so
the dashboard reads like an event log, not a sticky alarm.

What lives where after the migration
------------------------------------

This app is an :class:`~opennvr_app_sdk.AlertSubscriber` (App SDK spec
§02, the "pass-through" shape — same archetype as the reference
``alerts-subscriber``). The SDK base owns the NATS connect / subscribe
/ drain loop, the §03 contract endpoints, and the CLI / signal
lifecycle behind ``alert_app(HomeAssistantRelay).run()``.

Deliberately app-side (the "don't force it" clause):

* ``ha_mapper.py`` — the alert → HA-entity mapping rules and override
  table. HA vocabulary (device_class, ``binary_sensor`` vs ``sensor``)
  is this bridge's business, not the SDK's.
* ``publishers.py`` — the MQTT-discovery and HA-REST clients. Both
  are **async** (paho bridged via ``asyncio.to_thread``, httpx's
  AsyncClient), which is also why this app overrides the base's
  per-message hook: ``_handle_raw`` here is a coroutine (the shared
  NATS loop awaits awaitable results), so the sink can ``await`` its
  publisher instead of squeezing async I/O through the sync
  ``on_alert`` hook.
* the auto-off generation machinery — HA-entity semantics, not alert
  semantics.

Why MQTT discovery is the default
----------------------------------
HA's MQTT integration discovers entities on first publish to a magic
topic (default ``homeassistant/<component>/<unique_id>/config``). HA
auto-creates the entity with the right device_class + friendly name
+ device card. Subsequent state publishes flow on a separate
``state`` topic. Zero HA UI clicks required — the entity appears
the first time the alert fires.

REST API is offered as a fallback for users who can't run MQTT.
Entities still surface, just without the device card.

Run:
    python home_assistant_relay.py --config config.yml
    python home_assistant_relay.py --config config.yml --once   # one alert then exit
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opennvr_app_sdk import (
    AlertSubscriber,
    AppManifest,
    Param,
    StateView,
    alert_app,
)
from opennvr_app_sdk.config import load_yaml

from ha_mapper import HaEntity, HaMapper, MappingOverride, parse_overrides
from publishers import (
    MqttConfig,
    MqttPublisher,
    Publisher,
    RestConfig,
    RestPublisher,
)

logger = logging.getLogger("home-assistant-relay")


MANIFEST = AppManifest(
    id="home-assistant-relay",
    # No camera picker: this app acts on other apps' alerts/events,
    # which those apps have already limited to the cameras they picked.
    camera_picker=False,
    name="Home Assistant Relay",
    version="1.0.0",
    category="integration",
    summary=(
        "Bridges the opennvr.alerts.* fan-out into Home Assistant "
        "entities via MQTT discovery or the HA REST API, with "
        "auto-off windows so sensors read like an event log."
    ),
    requires_tasks=[],  # rides the alert bus; no adapter prerequisites
    subscribes="opennvr.alerts.>",
    params=[
        Param("subject_pattern", str, default="opennvr.alerts.>"),
        Param("backend", str, default="mqtt",
              description="'mqtt' (discovery; preferred) or 'rest'."),
        Param("default_auto_off_seconds", int, default=30,
              description="Auto-off window for binary_sensors when a "
                          "mapping override doesn't set one."),
    ],
    emits=[],  # pass-through: consumes alerts, emits none
    state_schema=[
        StateView(name="published", label="Forwarded to HA", kind="metric",
                  path="published",
                  description="Alerts successfully relayed to Home Assistant."),
        StateView(name="received", label="Alerts received", kind="metric",
                  path="received"),
        StateView(name="failed", label="Failed", kind="metric", path="failed",
                  description="Relays that errored (network / auth)."),
        StateView(name="pending", label="Auto-off pending", kind="metric",
                  path="pending_auto_off",
                  description="Binary sensors waiting to flip back off."),
        StateView(name="recent", label="Recent relays", kind="log",
                  path="recent", limit=10,
                  description="The latest alerts bridged to Home Assistant."),
    ],
)


# ── Config ─────────────────────────────────────────────────────────


@dataclass
class AppConfig:
    """Operator-tunable settings. Validated in ``load_config``."""

    # NATS — where we read alerts from.
    nats_url: str
    nats_token: str | None = None
    subject_pattern: str = "opennvr.alerts.>"

    # Backend selection — "mqtt" or "rest". Validated in load_config.
    backend: str = "mqtt"

    # Either mqtt_config or rest_config is populated; the other is
    # None depending on the backend choice.
    mqtt_config: MqttConfig | None = None
    rest_config: RestConfig | None = None

    # Per-(source, camera_id) overrides for the mapper.
    overrides: list[MappingOverride] = field(default_factory=list)

    # Default auto-off window when an override doesn't specify one.
    default_auto_off_seconds: int = 30

    # App contract (spec §03) — all optional; the SDK's ContractMixin
    # reads these via ``getattr``. ``contract_port`` serves /health
    # /manifest /state; ``opennvr_url`` triggers registry
    # self-registration on boot (token from ``opennvr_token`` or the
    # OPENNVR_INTERNAL_API_KEY env var).
    contract_port: int | None = None
    contract_bind_host: str | None = None
    contract_host: str | None = None
    opennvr_url: str | None = None
    opennvr_token: str | None = None


def load_config(path: str | Path) -> AppConfig:
    # Historical quirk kept on purpose: this app's validation raises
    # ``SystemExit`` with an operator-facing message (the other
    # examples raise ``ValueError``); its tests pin that contract.
    raw = load_yaml(path)

    nats_url = str(raw.get("nats_url") or "").strip()
    if not nats_url:
        raise SystemExit("config: nats_url is required")

    backend = str(raw.get("backend") or "mqtt").strip().lower()
    if backend not in ("mqtt", "rest"):
        raise SystemExit(
            f"config: backend must be 'mqtt' or 'rest'; got {backend!r}"
        )

    mqtt_config: MqttConfig | None = None
    rest_config: RestConfig | None = None
    default_auto_off = 30

    if backend == "mqtt":
        mqtt_raw = raw.get("mqtt") or {}
        if not isinstance(mqtt_raw, dict):
            raise SystemExit("config: mqtt block must be a mapping")
        host = str(mqtt_raw.get("host") or "127.0.0.1").strip()
        if not host:
            raise SystemExit("config: mqtt.host must not be empty")
        try:
            port = int(mqtt_raw.get("port", 1883))
            qos = int(mqtt_raw.get("qos", 1))
            auto_off = int(mqtt_raw.get("auto_off_seconds", 30))
        except (TypeError, ValueError) as exc:
            raise SystemExit(f"config: mqtt numeric field invalid: {exc}")
        if qos not in (0, 1, 2):
            raise SystemExit(f"config: mqtt.qos must be 0, 1 or 2; got {qos}")
        if auto_off < 0:
            raise SystemExit(
                f"config: mqtt.auto_off_seconds must be >= 0; got {auto_off}"
            )
        mqtt_config = MqttConfig(
            host=host,
            port=port,
            username=(str(mqtt_raw["username"]) if mqtt_raw.get("username") else None),
            password=(str(mqtt_raw["password"]) if mqtt_raw.get("password") else None),
            discovery_prefix=str(
                mqtt_raw.get("discovery_prefix") or "homeassistant"
            ).strip() or "homeassistant",
            auto_off_seconds=auto_off,
            qos=qos,
            retain=bool(mqtt_raw.get("retain", True)),
        )
        default_auto_off = auto_off

    else:  # rest
        rest_raw = raw.get("rest") or {}
        if not isinstance(rest_raw, dict):
            raise SystemExit("config: rest block must be a mapping when backend=rest")
        url = str(rest_raw.get("url") or "").strip()
        token = str(rest_raw.get("token") or "").strip()
        if not url:
            raise SystemExit("config: rest.url is required when backend=rest")
        if not token:
            raise SystemExit("config: rest.token is required when backend=rest")
        try:
            timeout = float(rest_raw.get("timeout_seconds", 5.0))
            auto_off = int(rest_raw.get("auto_off_seconds", 30))
        except (TypeError, ValueError) as exc:
            raise SystemExit(f"config: rest numeric field invalid: {exc}")
        if auto_off < 0:
            raise SystemExit(
                f"config: rest.auto_off_seconds must be >= 0; got {auto_off}"
            )
        rest_config = RestConfig(
            url=url,
            token=token,
            timeout_seconds=timeout,
            auto_off_seconds=auto_off,
        )
        default_auto_off = auto_off

    overrides_raw = raw.get("mappings") or []
    if overrides_raw and not isinstance(overrides_raw, list):
        raise SystemExit("config: mappings must be a list")
    overrides = parse_overrides(overrides_raw)

    subject = str(raw.get("subject_pattern") or "opennvr.alerts.>").strip()
    if not subject:
        raise SystemExit("config: subject_pattern must not be empty")

    nats_token_raw = raw.get("nats_token")
    # Strip trailing whitespace / newline — a common foot-gun when
    # the operator's token came from ``cat token | yq``.
    nats_token = str(nats_token_raw).strip() if nats_token_raw else None
    if nats_token == "":
        nats_token = None

    # App contract keys (spec §03) — optional, mirroring the other
    # overlay apps. Unset ⇒ no contract server, no self-registration.
    contract_port: int | None = None
    if raw.get("contract_port") is not None:
        try:
            contract_port = int(raw["contract_port"])
        except (TypeError, ValueError):
            raise SystemExit(
                f"config: contract_port must be an integer; "
                f"got {raw.get('contract_port')!r}"
            )

    def _opt_str(key: str) -> str | None:
        val = raw.get(key)
        if val is None:
            return None
        val = str(val).strip()
        return val or None

    return AppConfig(
        nats_url=nats_url,
        nats_token=nats_token,
        subject_pattern=subject,
        backend=backend,
        mqtt_config=mqtt_config,
        rest_config=rest_config,
        overrides=overrides,
        default_auto_off_seconds=default_auto_off,
        contract_port=contract_port,
        contract_bind_host=_opt_str("contract_bind_host"),
        contract_host=_opt_str("contract_host"),
        opennvr_url=_opt_str("opennvr_url"),
        opennvr_token=_opt_str("opennvr_token"),
    )


# ── Relay daemon ───────────────────────────────────────────────────


class HomeAssistantRelay(AlertSubscriber):
    """The daemon. One instance per process.

    Lifecycle:
      ``await relay.run()`` — the SDK base connects to NATS,
      subscribes, and drives every alert through the mapper into the
      publisher. Blocks until ``stop()`` (or SIGINT / SIGTERM via the
      runner) fires.

    Async sink:
      Both publishers are awaitable, so this app overrides the base's
      ``_handle_raw`` as a *coroutine* (the SDK's NATS loop awaits
      awaitable results) instead of implementing the sync ``on_alert``
      hook. Decode + isolation semantics mirror the base method.

    Auto-off:
      For each binary_sensor we publish, schedule a task that flips
      it back to OFF after ``entity.auto_off_seconds``. If a fresh
      alert arrives for the same entity while a timer is pending,
      we cancel the old timer and start a new one — keeps the
      entity "ON" as long as alerts keep firing.
    """

    manifest = MANIFEST

    def __init__(
        self,
        config: AppConfig,
        mapper: HaMapper | None = None,
        publisher: Publisher | None = None,
    ) -> None:
        # ``mapper`` / ``publisher`` are injectable for the tests (the
        # historical 3-arg constructor); the SDK runner constructs the
        # relay with config alone and the defaults are built from it.
        self._mapper = mapper or HaMapper(
            overrides=config.overrides,
            default_auto_off_seconds=config.default_auto_off_seconds,
        )
        self._publisher = publisher or build_publisher(config)
        super().__init__(config)

    def setup(self) -> None:
        self._auto_off_tasks: dict[str, asyncio.Task] = {}
        # Per-entity monotonic generation counter. Each fresh alert
        # bumps the entity's generation; the auto-off task captures
        # its generation at schedule-time and re-checks at fire-time.
        # If a newer generation has appeared in between (operator
        # alert refired between sleep-return and publish), the late
        # OFF is suppressed so it can't override the fresh ON.
        self._entity_generation: dict[str, int] = {}
        # Stop one-off mode after the first published alert.
        self._once_mode = False
        # Counters for the shutdown summary line — useful for
        # operators tailing the log.
        self._received_count = 0
        self._published_count = 0
        self._failed_count = 0
        self._skipped_count = 0
        # Rolling feed of the most recently forwarded alerts — powers
        # the "Recent relays" log on the app's dashboard.
        self._recent: deque[dict[str, Any]] = deque(maxlen=25)

    async def run(self, *, once: bool = False) -> None:
        # The historical ``--once`` contract stops after the first
        # *published* alert, not the first message — unmappable or
        # failed alerts keep the smoke test waiting. Map the runner's
        # ``once`` onto that instead of the loop-level stop.
        if once:
            self._once_mode = True
        logger.info(
            "subscribing to %r on %s (backend=%s)",
            self.cfg.subject_pattern,
            self.cfg.nats_url,
            self.cfg.backend,
        )
        try:
            await super().run(once=False)
        finally:
            # Cancel any pending auto-off tasks so we don't block on
            # them — operator wants a fast exit.
            for task in self._auto_off_tasks.values():
                task.cancel()
            self._auto_off_tasks.clear()
            try:
                await self._publisher.aclose()
            except Exception:
                logger.exception("publisher close failed")
            logger.info(
                "home-assistant-relay shutting down — received=%d "
                "published=%d failed=%d skipped=%d",
                self._received_count, self._published_count,
                self._failed_count, self._skipped_count,
            )

    def state_snapshot(self) -> dict[str, Any]:
        """``GET /state`` — running relay counters."""
        return {
            "received": self._received_count,
            "published": self._published_count,
            "failed": self._failed_count,
            "skipped": self._skipped_count,
            "pending_auto_off": len(self._auto_off_tasks),
            "recent": list(self._recent),
        }

    # ── Per-message handling (async override of the SDK hook) ─────

    async def _handle_raw(self, data: bytes, *, subject: str = "") -> bool:
        """Async twin of :meth:`AlertSubscriber._handle_raw` — same
        decode + isolation contract, but the sink is awaited so the
        publishers' async I/O stays on the event loop."""
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("skipping non-JSON message on %r: %s", subject, exc)
            return False
        self._contract_note_event()
        try:
            await self._handle_alert(payload)
        except Exception:
            # No single alert failure should kill the bridge.
            logger.exception("alert handling failed for subject=%s", subject)
            return False
        return True

    async def step(self, alert: dict[str, Any]) -> None:
        """Process one alert. Public so tests can drive without NATS."""
        await self._handle_alert(alert)

    async def _handle_alert(self, alert: dict[str, Any]) -> None:
        self._received_count += 1
        entity = self._mapper.map(alert)
        if entity is None:
            self._skipped_count += 1
            return
        ok = await self._publisher.publish_state(entity)
        if not ok:
            self._failed_count += 1
            return
        self._published_count += 1
        self._recent.append({
            "message": f"{entity.full_entity_id} ← "
                       f"{alert.get('title') or alert.get('type') or 'alert'}",
            "time": time.time(),
            "level": str(alert.get("severity", "")),
        })
        self._maybe_schedule_auto_off(entity)
        if self._once_mode:
            self.stop()

    def _maybe_schedule_auto_off(self, entity: HaEntity) -> None:
        # sensor entities don't get an auto-off; they hold their
        # last value until something changes it.
        if entity.component != "binary_sensor":
            return
        if entity.auto_off_seconds <= 0:
            return

        key = entity.full_entity_id
        # Bump the generation for this entity. Any in-flight auto-off
        # task captured the prior generation and will short-circuit
        # before touching the publisher.
        generation = self._entity_generation.get(key, 0) + 1
        self._entity_generation[key] = generation

        # Cancel any pending timer so a rapid succession of alerts
        # keeps the sensor ON for the full window from the last one.
        # The generation check below is a belt-and-braces guard for
        # the case where the cancel races a timer that just woke up.
        existing = self._auto_off_tasks.pop(key, None)
        if existing is not None and not existing.done():
            existing.cancel()

        async def _flip_off_later() -> None:
            try:
                await asyncio.sleep(entity.auto_off_seconds)
            except asyncio.CancelledError:
                return
            # Generation re-check: if a newer alert bumped the
            # counter while we were sleeping (or while we were about
            # to call publish_off), suppress this OFF — it would
            # land *after* the fresh ON otherwise.
            if self._entity_generation.get(key) != generation:
                return
            try:
                await self._publisher.publish_off(entity)
            except Exception:
                logger.exception(
                    "auto-off publish failed for %s", entity.full_entity_id,
                )
            finally:
                # Self-cleanup — don't leave finished tasks in the
                # dict, otherwise memory creeps over a long run.
                self._auto_off_tasks.pop(key, None)

        self._auto_off_tasks[key] = asyncio.create_task(
            _flip_off_later(), name=f"ha-auto-off-{key}",
        )


# ── Publisher factory ──────────────────────────────────────────────


def build_publisher(config: AppConfig) -> Publisher:
    if config.backend == "mqtt":
        assert config.mqtt_config is not None
        return MqttPublisher(config.mqtt_config)
    if config.backend == "rest":
        assert config.rest_config is not None
        return RestPublisher(config.rest_config)
    raise SystemExit(f"config: unknown backend {config.backend!r}")


# ── CLI ────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point. The SDK runner owns argparse,
    logging, signals, and the loop lifecycle."""
    return alert_app(HomeAssistantRelay, load_config=load_config).run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
