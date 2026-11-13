# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Core's domain events on the bus: ``visit.recorded.v1`` and
``visit.enriched.v1`` (RFC-0004, step 2).

``docs/EVENT_CONTRACTS.md`` has named ``visit.recorded.v1`` with core as
its producer since RFC-0002, and the SDK has typed it — but nothing
published it: a visit was a database row, and an app that wanted to
know one existed polled the timeline API. ``visit.enriched.v1`` is the
same story one step on: the caption and the claims land in the store
and stay there, so the agent and the Mirror ask the caption model
themselves for what the store already holds. Both events go out from
here, one message each, when the row is committed.

One NATS connection, made on first use and remade on failure; a publish
never raises into the enricher that called it (the row is committed
either way, the store is the memory, the bus is the signal). Without
``NATS_URL`` every publish is a no-op, which is what a single-process
install without a bus wants.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("domain_bus")

PRODUCER = "core"
VISIT_RECORDED = "visit.recorded.v1"
VISIT_ENRICHED = "visit.enriched.v1"

#: For the measurement: published / skipped (no bus) / failed.
STATS: dict[str, int] = {"published": 0, "no_bus": 0, "failed": 0}

_client: Any = None
_client_lock = asyncio.Lock()


def _url_and_token() -> tuple[str, str | None]:
    from core.config import settings

    url = (getattr(settings, "nats_url", "") or "").strip()
    token = (getattr(settings, "internal_api_key", "") or "").strip() or None
    return url, token


async def _connection() -> Any | None:
    """The shared client, connected on first use; None without a bus."""
    global _client
    url, token = _url_and_token()
    if not url:
        return None
    if _client is not None and not getattr(_client, "is_closed", False):
        return _client
    async with _client_lock:
        if _client is not None and not getattr(_client, "is_closed", False):
            return _client
        import nats

        _client = await nats.connect(url, connect_timeout=3, token=token, name="opennvr-core-domain")
        return _client


async def publish_domain(schema: str, *, camera_id: int | str, payload: dict[str, Any],
                         correlation_id: str | None = None, ts: str | None = None) -> bool:
    """Publish one contracted event. Returns True when it left; never
    raises."""
    from opennvr_app_sdk.domain_events import domain_envelope, domain_subject

    handle = str(camera_id) if str(camera_id).startswith("cam") else f"cam{int(camera_id)}"
    try:
        client = await _connection()
    except Exception:                              # noqa: BLE001
        STATS["failed"] += 1
        logger.debug("domain bus: no connection for %s", schema, exc_info=True)
        return False
    if client is None:
        STATS["no_bus"] += 1
        return False
    envelope = domain_envelope(schema, camera_id=handle, payload=payload, producer=PRODUCER,
                               correlation_id=correlation_id)
    if ts:
        envelope["ts"] = ts
    try:
        await client.publish(domain_subject(schema, handle), json.dumps(envelope).encode("utf-8"))
        STATS["published"] += 1
        return True
    except Exception:                              # noqa: BLE001
        STATS["failed"] += 1
        global _client
        _client = None                              # remade on the next publish
        logger.debug("domain bus: publish of %s failed", schema, exc_info=True)
        return False


def publish_soon(schema: str, *, camera_id: int | str, payload: dict[str, Any],
                 correlation_id: str | None = None, ts: str | None = None) -> None:
    """Fire-and-forget from synchronous code (the timeline writer runs in
    a request handler's thread or under the event loop). Never raises."""
    coro = publish_domain(schema, camera_id=camera_id, payload=payload,
                          correlation_id=correlation_id, ts=ts)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.create_task(coro)
        return
    threading.Thread(target=lambda: asyncio.run(coro), name="domain-bus-publish", daemon=True).start()


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return str(value)


def visit_recorded_payload(row: Any) -> dict[str, Any]:
    return {
        "event_id": int(row.id),
        "label": row.label or "",
        "started_at": _iso(row.started_at) or "",
        "ended_at": _iso(row.ended_at) or "",
        "evidence_path": row.evidence_path,
    }


def enriched_payload(db, row: Any) -> dict[str, Any]:
    """The visit's enrichment as the store holds it now: the caption and
    its producer, every descriptor claim as ``kind: value``, and whether
    it was copied from an earlier visit of the same frame."""
    from models import EventText, VisitDescriptor

    text = db.get(EventText, row.id)
    claims = db.query(VisitDescriptor).filter(VisitDescriptor.event_id == row.id).all()
    attributes = {d.kind: d.value for d in claims if d.kind and d.value}
    payload: dict[str, Any] = {
        "event_id": int(row.id),
        "label": row.label or "",
        "caption": (text.caption or None) if text is not None else None,
        "attributes": attributes,
        "evidence_path": row.evidence_path,
        "producer": (text.source or None) if text is not None else None,
        "enriched_by": list((row.payload or {}).get("enriched_by") or []),
    }
    reused = (row.payload or {}).get("reused_from")
    if reused is not None:
        payload["reused_from"] = int(reused)
    return payload


async def publish_visit_enriched(event_id: int) -> bool:
    """Read the visit's enrichment in a short session and publish it.
    Called after every commit that changes a visit's caption or claims;
    each message carries the whole current state, so a subscriber that
    sees one message knows everything the store knows."""
    from core.database import SessionLocal
    from models import TimelineEvent

    db = SessionLocal()
    try:
        row = db.get(TimelineEvent, int(event_id))
        if row is None:
            return False
        payload = enriched_payload(db, row)
        camera_id = int(row.camera_id)
        ts = _iso(row.ended_at or row.started_at)
    finally:
        db.close()
    return await publish_domain(VISIT_ENRICHED, camera_id=camera_id, payload=payload, ts=ts)
