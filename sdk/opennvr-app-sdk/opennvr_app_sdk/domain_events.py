# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""Publish contracted domain events (``opennvr.events.*``) from an app.

``docs/EVENT_CONTRACTS.md`` is the normative source: every domain
event travels as the envelope ``{id, schema, correlation_id,
camera_id, ts, producer, payload}`` on the subject
``opennvr.events.<domain>.<event>.v<N>.<camera_id>``. This module
gives apps the producing side of that contract — the App SDK's
subscriber loop already gives them the consuming side — so an app can
put a FACT on the bus (an access decision, an occupancy change)
without hand-rolling NATS plumbing or envelope fields.

CI's contract ratchet (``server/tests/test_event_contracts.py``)
fails any first-party publish on a subject the contracts doc does not
list, so adding a new schema starts at the doc, not here.

The transport reuses :class:`opennvr_app_sdk.alerts.NatsAlertChannel`
machinery (background loop thread, lazy connect, hard timeouts,
log-never-raise) via its generic ``publish_json``.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from .alerts import NatsAlertChannel

#: schema names look like ``access.decided.v1`` — noun.verb-past.vN
#: (the contracts doc's naming rule).
_SCHEMA_RE = re.compile(r"^[a-z_]+\.[a-z_]+\.v\d+$")
_CAMERA_TOKEN_BAD = re.compile(r"[^A-Za-z0-9_-]")


def domain_subject(schema: str, camera_id: str) -> str:
    """The contracted subject for one event instance.

    ``schema`` must match ``<domain>.<event>.v<N>`` and ``camera_id``
    must be a single valid NATS token (the platform handle, ``camN``)
    — both fail loudly, because a malformed subject silently reaches
    no subscriber.
    """
    if not _SCHEMA_RE.match(schema):
        raise ValueError(
            f"domain event schema {schema!r} must look like "
            "'<domain>.<event>.v<N>' (see docs/EVENT_CONTRACTS.md)")
    if not camera_id or _CAMERA_TOKEN_BAD.search(camera_id):
        raise ValueError(
            f"camera_id {camera_id!r} is not a valid subject token")
    return f"opennvr.events.{schema}.{camera_id}"


def domain_envelope(
    schema: str,
    *,
    camera_id: str,
    payload: dict[str, Any],
    producer: str,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """The EVENT_CONTRACTS.md envelope — every field, every time
    (mirrors the KAI-C normaliser's builder)."""
    return {
        "id": "evt_" + uuid.uuid4().hex[:12],
        "schema": schema,
        "correlation_id": correlation_id,
        "camera_id": camera_id,
        "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "producer": producer,
        "payload": payload,
    }


class DomainEventPublisher:
    """Publish contracted domain events onto the platform bus.

    One instance per app process; ``publish`` never raises on bus
    trouble (logged inside the channel, returns False) — an app's
    decision loop must not crash because the broker blinked.
    """

    def __init__(self, url: str, *, token: str | None = None,
                 producer: str = "app") -> None:
        self._producer = producer
        self._channel = NatsAlertChannel(url, token=token)

    def publish(
        self,
        schema: str,
        *,
        camera_id: str,
        payload: dict[str, Any],
        correlation_id: str | None = None,
    ) -> bool:
        subject = domain_subject(schema, camera_id)
        envelope = domain_envelope(
            schema,
            camera_id=camera_id,
            payload=payload,
            producer=self._producer,
            correlation_id=correlation_id,
        )
        return self._channel.publish_json(subject, envelope)

    def close(self) -> None:
        self._channel.close()
