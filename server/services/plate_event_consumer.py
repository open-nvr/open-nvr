# Copyright (c) 2026 OpenNVR
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""RFC-0002 Phase 0: core consumes ``plate.recognized.v1`` from the bus.

Producer convergence (EVENT_CONTRACTS.md): KAI-C's normaliser emits one
domain event per accepted OCR read regardless of who initiated the call.
This consumer is core's side of the deal — the timeline's ``plate_text``
column is written from the *contract event*, so core no longer cares
whether its own enrichment fallback or Tier-1 dispatch ran the OCR.

Wiring: ``run_consumer_loop`` is a lifespan background task. It is
best-effort by design — no NATS URL configured, nats-py missing, or the
broker being down degrades to "rows are written by the enrichment
fallback's synchronous path only", exactly the pre-RFC behaviour.
Both writers write the same normalised text, and both skip rows whose
``plate_text`` is already set, so delivery order and redelivery are
harmless.
"""

from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)

SUBJECT = "opennvr.events.plate.recognized.v1.>"

#: Reconnect cadence after a connect/subscribe failure. Deliberately slow —
#: a down bus should cost one warning line a minute, not a hot loop.
_RETRY_SECONDS = 60.0


def _read_is_clipped(box, evidence_path: str | None,
                     box_image_size=None) -> bool:
    """Partial-read guard for the bus path — see plate_box_is_clipped.

    ``box_image_size`` is the event's own statement of which image the
    box is measured in (``plate_box_image``, [w, h]). When present it is
    the ONLY correct denominator: multi-frame OCR reads plate CANDIDATE
    crops whose size differs from the visit's evidence frame, so judging
    such a box against the evidence file measures in the wrong image.
    Only without it do we fall back to the evidence file's size —
    correct for the single-evidence producers that predate the field.

    Best-effort by construction: no box, no usable size, or an
    unreadable crop all answer False, so a missing optional field can
    never start silently dropping good plates.
    """
    if box is None:
        return False
    try:
        from services.plate_enrichment import (
            jpeg_dimensions, plate_box_is_clipped,
        )

        if (isinstance(box_image_size, (list, tuple))
                and len(box_image_size) == 2):
            return plate_box_is_clipped(box, tuple(box_image_size))
        if not evidence_path:
            return False
        from services.evidence_store import resolve_evidence

        path = resolve_evidence(evidence_path)
        if path is None:
            return False
        return plate_box_is_clipped(box, jpeg_dimensions(path.read_bytes()))
    except Exception:  # noqa: BLE001
        logger.debug("plate consumer: clipping check failed", exc_info=True)
        return False


def apply_plate_event(envelope: object) -> str:
    """Apply one ``plate.recognized.v1`` envelope to the timeline.

    Pure-decision core of the consumer, unit-testable without a bus.
    Returns a status token (logged, asserted in tests):

    * ``"applied"``      — wrote ``plate_text`` onto the referenced row.
    * ``"already-set"``  — row already enriched (fallback raced us, or a
      redelivery); left untouched.
    * ``"no-event-id"``  — event carries no timeline reference (e.g. a
      dispatch-initiated read; joining those is Phase 4's job).
    * ``"no-plate"`` / ``"malformed"`` — nothing usable in the payload.
    * ``"not-found"``    — referenced row does not exist (deleted, or a
      foreign initiator's reference); nothing to do.
    * ``"clipped"``      — the plate box abuts its crop's edge: a
      partial read (#378); row left untouched.
    * ``"duplicate"``    — same plate seen on this camera within the
      dedup window (a fragmented track re-reading the car we just
      read); sighting folded, row left untouched.
    """
    if not isinstance(envelope, dict):
        return "malformed"
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return "malformed"
    plate = payload.get("plate_text")
    if not isinstance(plate, str) or not plate.strip():
        return "no-plate"
    event_id = payload.get("event_id")
    if not isinstance(event_id, int) or isinstance(event_id, bool):
        return "no-event-id"

    from core.database import SessionLocal
    from models import TimelineEvent

    db = SessionLocal()
    try:
        row = (
            db.query(TimelineEvent)
            .filter(TimelineEvent.id == event_id)
            .first()
        )
        if row is None:
            return "not-found"
        if row.plate_text:
            return "already-set"
        # Same partial-read rule as the synchronous producer. The two are
        # racing writers for one column, so a guard on only one of them is
        # no guard at all — a clipped read rejected by plate_enrichment
        # would simply land here instead. KAI-C forwards the plate box
        # (optional, additive); the crop it was measured in is this row's
        # evidence, so the geometry is reproducible here.
        if _read_is_clipped(payload.get("plate_box"), row.evidence_path,
                            payload.get("plate_box_image")):
            return "clipped"
        normalized = "".join(plate.split()).upper()[:32]
        # Duplicate-sighting dedup — same rule as the synchronous
        # writers (racing writers for one column need the same policy;
        # see plate_enrichment). Folded, not an error.
        from services.plate_enrichment import (
            is_duplicate_sighting, note_sighting,
        )

        if is_duplicate_sighting(row.camera_id, normalized):
            note_sighting(row.camera_id, normalized)
            return "duplicate"
        row.plate_text = plate.strip()[:32]
        note_sighting(row.camera_id, normalized)
        db.commit()
        logger.info(
            "plate event applied: event %s -> %s [correlation_id=%s]",
            event_id, row.plate_text, envelope.get("correlation_id"),
        )
        return "applied"
    finally:
        db.close()


async def _handle_message(msg) -> None:
    """One bus message → one ``apply_plate_event``. Never raises — a bad
    message is a debug line, not a dead subscription."""
    try:
        envelope = json.loads(msg.data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        logger.debug("plate consumer: undecodable message on %s", msg.subject)
        return
    try:
        status = await asyncio.to_thread(apply_plate_event, envelope)
        if status not in ("applied", "already-set", "no-event-id"):
            logger.debug("plate consumer: %s for %s", status, msg.subject)
    except Exception:  # noqa: BLE001
        logger.warning("plate consumer: apply failed", exc_info=True)


async def run_consumer_loop() -> None:
    """Subscribe to ``plate.recognized.v1`` and keep the subscription
    alive for the process lifetime. Returns immediately when no NATS URL
    is configured; retries (slowly) on every other failure."""
    from core.config import settings

    url = (getattr(settings, "nats_url", "") or "").strip()
    if not url:
        logger.info(
            "plate event consumer disabled (no NATS_URL) — plate_text is "
            "written by the enrichment fallback only")
        return
    try:
        import nats
    except ImportError:
        logger.warning(
            "plate event consumer disabled: nats-py not installed")
        return

    # The compose broker runs token auth (--auth $INTERNAL_API_KEY):
    # connecting without the token is an Authorization Violation and this
    # loop would retry forever without ever subscribing. Same key the
    # rest of the stack uses (detect-pipeline's _nats_connect_options
    # documents the identical lesson).
    token = (getattr(settings, "internal_api_key", "") or "").strip() or None

    while True:
        try:
            client = await nats.connect(url, connect_timeout=5, token=token)
            sub = await client.subscribe(SUBJECT, cb=_handle_message)
            logger.info("plate event consumer subscribed to %s", SUBJECT)
            try:
                # nats-py reconnects transparently; this just parks the
                # task until cancellation (shutdown) unwinds us.
                await asyncio.Event().wait()
            finally:
                try:
                    await sub.unsubscribe()
                    await client.drain()
                except Exception:  # noqa: BLE001
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "plate event consumer: connect/subscribe failed (%s); "
                "retrying in %.0fs", exc, _RETRY_SECONDS)
            await asyncio.sleep(_RETRY_SECONDS)
