# Copyright (c) 2026 OpenNVR
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Core consumes ``occupancy.changed.v1`` into the history store.

The occupancy app measures; core remembers. Contracted samples
(docs/EVENT_CONTRACTS.md) land as ``occupancy_samples`` rows so the
Occupancy page can chart trends and busiest hours — the producer keeps
zero history and can restart freely.

Best-effort by design, exactly like the plate consumer: no NATS URL,
missing nats-py, or a down broker degrades to "no history accrues";
live state on the page keeps working. Retention is enforced HERE (the
only writer): every ``_PRUNE_EVERY`` applied samples, rows older than
``RETENTION_DAYS`` are deleted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

SUBJECT = "opennvr.events.occupancy.changed.v1.>"
HEATMAP_SUBJECT = "opennvr.events.occupancy.heatmap.v1.>"

#: Largest grid a producer may send — a guard on memory and JSON size,
#: not a format promise. The reference app sends 48x27.
MAX_HEATMAP_CELLS = 256 * 144

RETENTION_DAYS = 90
_PRUNE_EVERY = 500
_RETRY_SECONDS = 60.0

_CAMERA_HANDLE = re.compile(r"^cam(\d+)$")
_applies_since_prune = 0


def apply_occupancy_event(envelope: object) -> str:
    """Apply one ``occupancy.changed.v1`` envelope to the history store.

    Pure-decision core, unit-testable without a bus. Status tokens:

    * ``"applied"``   — sample row written.
    * ``"bad-camera"``— camera_id is not a core handle (``camN``);
      nothing to join history to.
    * ``"malformed"`` — no usable count in the payload.
    """
    global _applies_since_prune
    if not isinstance(envelope, dict):
        return "malformed"
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return "malformed"
    count = payload.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return "malformed"
    handle = str(envelope.get("camera_id") or "")
    m = _CAMERA_HANDLE.match(handle)
    if not m:
        return "bad-camera"
    camera_id = int(m.group(1))
    level = str(payload.get("level") or "normal")[:16]

    ts = datetime.now(timezone.utc)
    raw_ts = envelope.get("ts")
    if isinstance(raw_ts, str):
        try:
            parsed = datetime.fromisoformat(raw_ts)
            if parsed.tzinfo is not None:
                ts = parsed
        except ValueError:
            pass

    from core.database import SessionLocal
    from models import OccupancySample

    db = SessionLocal()
    try:
        db.add(OccupancySample(camera_id=camera_id, count=count,
                               level=level, ts=ts))
        _applies_since_prune += 1
        if _applies_since_prune >= _PRUNE_EVERY:
            _applies_since_prune = 0
            cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
            db.query(OccupancySample).filter(
                OccupancySample.ts < cutoff).delete()
            prune_heatmaps(db)
        db.commit()
        return "applied"
    finally:
        db.close()


def _event_ts(envelope: dict) -> datetime:
    ts = datetime.now(timezone.utc)
    raw_ts = envelope.get("ts")
    if isinstance(raw_ts, str):
        try:
            parsed = datetime.fromisoformat(raw_ts)
            if parsed.tzinfo is not None:
                ts = parsed
        except ValueError:
            pass
    return ts


def apply_heatmap_event(envelope: object) -> str:
    """Apply one ``occupancy.heatmap.v1`` sparse delta to the camera-hour
    grid it falls in. Status tokens as for ``apply_occupancy_event``,
    plus ``"empty"`` (a delta with no cells — nothing to add) and
    ``"shape-mismatch"`` (a producer changed grid size mid-hour; the
    hour keeps the shape it started with and the delta is dropped —
    a new hour picks up the new shape).
    """
    if not isinstance(envelope, dict):
        return "malformed"
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return "malformed"
    cols, rows = payload.get("cols"), payload.get("rows")
    if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0
               for v in (cols, rows)) or cols * rows > MAX_HEATMAP_CELLS:
        return "malformed"
    raw_cells = payload.get("cells")
    if not isinstance(raw_cells, list):
        return "malformed"
    n = cols * rows
    delta: dict[int, int] = {}
    for entry in raw_cells:
        if (not isinstance(entry, (list, tuple)) or len(entry) != 2
                or not all(isinstance(v, int) and not isinstance(v, bool)
                           for v in entry)):
            continue
        idx, hits = entry
        if 0 <= idx < n and hits > 0:
            delta[idx] = delta.get(idx, 0) + hits
    if not delta:
        return "empty"
    frames = payload.get("frames")
    frames = frames if isinstance(frames, int) and not isinstance(frames, bool) \
        and frames > 0 else 0
    handle = str(envelope.get("camera_id") or "")
    m = _CAMERA_HANDLE.match(handle)
    if not m:
        return "bad-camera"
    camera_id = int(m.group(1))
    ts = _event_ts(envelope)
    hour_start = ts.replace(minute=0, second=0, microsecond=0)

    from core.database import SessionLocal
    from models import OccupancyHeatmap

    db = SessionLocal()
    try:
        row = (
            db.query(OccupancyHeatmap)
            .filter(OccupancyHeatmap.camera_id == camera_id,
                    OccupancyHeatmap.hour_start == hour_start)
            .first()
        )
        if row is None:
            cells = [0] * n
            row = OccupancyHeatmap(camera_id=camera_id, hour_start=hour_start,
                                   cols=cols, rows=rows, cells=cells,
                                   frames=0, updated_at=ts)
            db.add(row)
        elif row.cols != cols or row.rows != rows:
            return "shape-mismatch"
        else:
            cells = list(row.cells or [0] * n)
            if len(cells) != n:
                cells = (cells + [0] * n)[:n]
        for idx, hits in delta.items():
            cells[idx] += hits
        # A fresh list, not an in-place edit: JSON columns only notice
        # reassignment.
        row.cells = cells
        row.frames = int(row.frames or 0) + frames
        row.updated_at = ts
        db.commit()
        return "applied"
    finally:
        db.close()


def prune_heatmaps(db, *, now: datetime | None = None) -> int:
    """Drop camera-hours older than the retention window. Called by the
    sample pruner so the two histories age together."""
    from models import OccupancyHeatmap

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=RETENTION_DAYS)
    return db.query(OccupancyHeatmap).filter(
        OccupancyHeatmap.hour_start < cutoff).delete()


async def _handle_message(msg) -> None:
    try:
        envelope = json.loads(msg.data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        logger.debug("occupancy consumer: undecodable message on %s",
                     msg.subject)
        return
    apply = (apply_heatmap_event if ".occupancy.heatmap." in msg.subject
             else apply_occupancy_event)
    try:
        status = await asyncio.to_thread(apply, envelope)
        if status != "applied":
            logger.debug("occupancy consumer: %s for %s", status, msg.subject)
    except Exception:  # noqa: BLE001
        logger.warning("occupancy consumer: apply failed", exc_info=True)


async def run_consumer_loop() -> None:
    """Subscribe to ``occupancy.changed.v1`` for the process lifetime.
    Returns immediately with no NATS URL; retries slowly otherwise."""
    from core.config import settings

    url = (getattr(settings, "nats_url", "") or "").strip()
    if not url:
        logger.info("occupancy consumer disabled (no NATS_URL) — no "
                    "occupancy history accrues")
        return
    try:
        import nats
    except ImportError:
        logger.warning("occupancy consumer disabled: nats-py not installed")
        return

    token = (getattr(settings, "internal_api_key", "") or "").strip() or None

    # No awaiting ``finally`` on this path: it would run under
    # GeneratorExit when the coroutine is closed (task GC'd) and blow up
    # as "coroutine ignored GeneratorExit" — the failure that silently
    # unsubscribed the alerts-inbox consumer in the field. Teardown is
    # explicit on the exception paths instead (see alerts_inbox).
    while True:
        client = None
        try:
            client = await nats.connect(url, connect_timeout=5, token=token)
            await client.subscribe(SUBJECT, cb=_handle_message)
            await client.subscribe(HEATMAP_SUBJECT, cb=_handle_message)
            logger.info("occupancy consumer subscribed to %s and %s",
                        SUBJECT, HEATMAP_SUBJECT)
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await _teardown(client)
            raise
        except Exception as exc:  # noqa: BLE001
            await _teardown(client)
            logger.warning(
                "occupancy consumer: connect/subscribe failed (%s); "
                "retrying in %ss", exc, int(_RETRY_SECONDS))
            await asyncio.sleep(_RETRY_SECONDS)


async def _teardown(client) -> None:
    """Best-effort drain; every failure swallowed."""
    try:
        if client is not None:
            await client.drain()
    except Exception:  # noqa: BLE001
        pass
