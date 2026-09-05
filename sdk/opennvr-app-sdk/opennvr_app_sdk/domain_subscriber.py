# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Consume contracted domain events — the read half of ``domain_events``.

``docs/EVENT_CONTRACTS.md`` events (``plate.recognized.v1``,
``occupancy.changed.v1``, …) are how apps build on each other's
results: a gate controller reacts to plates without knowing which OCR
produced them. Publishing had an SDK path; consuming meant raw NATS.
Now::

    class Gate(DomainEventSubscriber):
        subscriptions = ["plate.recognized.v1"]

        def on_event(self, event: DomainEvent) -> None:
            if event.camera_id in self.gate_cameras:
                self.open_gate(event.payload["plate_text"])

    domain_event_app(Gate)()          # the same CLI runner shape as alert_app

Subjects: ``opennvr.events.<schema>.<camera_id>``; ``subscriptions``
may name schemas (all cameras) or full subjects. Every message is
decoded into a :class:`DomainEvent` and checked against the envelope
contract; anything else is logged and skipped — a long-lived consumer
never dies to one bad message.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .alert_subscriber import AlertSubscriberRunner
from .contract import ContractMixin
from .manifest import AppManifest
from .nats_loop import NatsSubscriberMixin

logger = logging.getLogger(__name__)

DOMAIN_SUBJECT_PREFIX = "opennvr.events."


@dataclass(frozen=True)
class DomainEvent:
    """One EVENT_CONTRACTS.md envelope."""

    id: str
    schema: str            # "plate.recognized.v1"
    camera_id: str         # "cam3"
    ts: str                # ISO 8601
    payload: dict[str, Any]
    producer: str | None = None
    correlation_id: str | None = None
    subject: str = ""
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


def parse_domain_event(data: bytes | str | dict, *, subject: str = "") -> DomainEvent | None:
    """Decode + validate an envelope; ``None`` for anything off-contract."""
    try:
        env = data if isinstance(data, dict) else json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(env, dict):
        return None
    schema, camera_id, payload = env.get("schema"), env.get("camera_id"), env.get("payload")
    if not isinstance(schema, str) or not isinstance(camera_id, str) or not isinstance(payload, dict):
        return None
    return DomainEvent(
        id=str(env.get("id") or ""), schema=schema, camera_id=camera_id,
        ts=str(env.get("ts") or ""), payload=payload,
        producer=env.get("producer"), correlation_id=env.get("correlation_id"),
        subject=subject, raw=env)


def subscription_subject(name: str) -> str:
    """``"plate.recognized.v1"`` → ``"opennvr.events.plate.recognized.v1.>"``;
    a full subject (already prefixed) passes through."""
    if name.startswith(DOMAIN_SUBJECT_PREFIX) or name.startswith("opennvr."):
        return name
    return f"{DOMAIN_SUBJECT_PREFIX}{name}.>"


class DomainEventSubscriber(ContractMixin, NatsSubscriberMixin):
    """Base class for apps that react to contracted domain events.

    Set ``subscriptions`` (schemas or subjects), implement
    :meth:`on_event`. Reads ``cfg.nats_url`` / ``cfg.nats_token``; a
    ``cfg.subject_pattern`` overrides ``subscriptions`` when present.
    The contract server, self-registration and the config poll run
    exactly as for the other archetypes.
    """

    manifest: AppManifest | None = None
    subscriptions: list[str] = []

    def __init__(self, config: Any) -> None:
        self.cfg = config
        self._stop_event = asyncio.Event()
        self._nc: Any = None
        self._contract_init()
        self.setup()

    def setup(self) -> None:
        """Optional: allocate state after ``cfg`` is set."""

    def on_event(self, event: DomainEvent) -> None:
        raise NotImplementedError

    def subjects(self) -> list[str]:
        explicit = getattr(self.cfg, "subject_pattern", None)
        if explicit:
            return [explicit] if isinstance(explicit, str) else list(explicit)
        return [subscription_subject(s) for s in self.subscriptions]

    def _nats_subjects(self) -> list[str]:
        subjects = self.subjects()
        if not subjects:
            raise ValueError(
                f"{type(self).__name__}.subscriptions is empty — name the "
                "schemas to consume (e.g. ['plate.recognized.v1'])")
        return subjects

    def _handle_raw(self, data: bytes, *, subject: str = "") -> bool:
        event = parse_domain_event(data, subject=subject)
        if event is None:
            logger.warning("skipping off-contract message on %r", subject)
            return False
        self._contract_note_event()
        try:
            self.on_event(event)
        except Exception:
            logger.exception("on_event failed for %s on %s", event.schema, subject)
            return False
        return True

    async def run(self, *, once: bool = False) -> None:
        self.start_contract_server()
        self.register_with_opennvr()
        self.start_config_poll()
        try:
            await self._run_nats_loop(once=once)
        finally:
            self.stop_config_poll()
            self.stop_contract_server()


def domain_event_app(app_cls, *, load_config=None) -> AlertSubscriberRunner:
    """CLI runner factory, the same shape as :func:`alert_app`::

        if __name__ == "__main__":
            raise SystemExit(domain_event_app(Gate, load_config=load_config).run())
    """
    return AlertSubscriberRunner(app_cls, load_config=load_config)
