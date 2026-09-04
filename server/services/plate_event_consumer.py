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
    * ``"weak-detection"`` — the adapter barely localised the plate it
      read: a false localisation such as a manufacturer badge (#386);
      row left untouched.
    * ``"duplicate"``    — same plate seen on this camera within the
      dedup window (a fragmented track re-reading the car we just
      read); sighting folded, row left untouched.
    * ``"deferred-to-sweep"`` — core's own enrichment sweep is still
      working this row (this event is one of its attempts, echoed by
      KAI-C); the sweep writes text AND evidence, so the row is left
      to it.
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
        # The sweep that initiated this very read still owns the row.
        # KAI-C republishes every accepted read core asks for, so this
        # event IS the sweep's own attempt coming back round the bus —
        # and the sweep holds the bytes (plate crop + read frame) that
        # this consumer does not. Writing here won the race and left
        # the row a number with no proof; defer, and the sweep writes
        # text and evidence together (with consensus, when several
        # looks are in play). A state check on core itself, not a
        # producer check — consumers must not branch on producer.
        from services.plate_enrichment import sweep_is_pending

        if sweep_is_pending(event_id):
            return "deferred-to-sweep"
        # Same partial-read rule as the synchronous producer. The two are
        # racing writers for one column, so a guard on only one of them is
        # no guard at all — a clipped read rejected by plate_enrichment
        # would simply land here instead. KAI-C forwards the plate box
        # (optional, additive); the crop it was measured in is this row's
        # evidence, so the geometry is reproducible here.
        if _read_is_clipped(payload.get("plate_box"), row.evidence_path,
                            payload.get("plate_box_image")):
            return "clipped"
        # Same false-localisation rule as the synchronous producer, for
        # the same reason the clip guard is duplicated here: two racing
        # writers for one column, so a guard on one of them is no guard
        # at all. Absent field = older producer = no opinion (#386).
        from services.plate_enrichment import detection_confidence_is_weak

        if detection_confidence_is_weak(payload.get("plate_box_confidence")):
            return "weak-detection"
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
        # A single forwarded read: no images, one look. The sweep (if
        # any) may attach evidence or, with several disagreeing looks,
        # replace it — see plate_enrichment's precedence rules.
        from services.plate_enrichment import stamp_plate_evidence

        stamp_plate_evidence(row, None, reads=1, source="bus",
                             confidence=payload.get("confidence")
                             if isinstance(payload.get("confidence"), (int, float))
                             else None)
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

    # No awaiting ``finally`` on this path: it would run under
    # GeneratorExit when the coroutine is closed (task GC'd) and blow up
    # as "coroutine ignored GeneratorExit" — the failure that silently
    # unsubscribed the alerts-inbox consumer in the field. Teardown is
    # explicit on the exception paths instead (see alerts_inbox).
    while True:
        client = sub = None
        try:
            client = await nats.connect(url, connect_timeout=5, token=token)
            sub = await client.subscribe(SUBJECT, cb=_handle_message)
            logger.info("plate event consumer subscribed to %s", SUBJECT)
            # nats-py reconnects transparently; this just parks the
            # task until cancellation (shutdown) unwinds us.
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await _teardown(sub, client)
            raise
        except Exception as exc:  # noqa: BLE001
            await _teardown(sub, client)
            logger.warning(
                "plate event consumer: connect/subscribe failed (%s); "
                "retrying in %.0fs", exc, _RETRY_SECONDS)
            await asyncio.sleep(_RETRY_SECONDS)


async def _teardown(sub, client) -> None:
    """Best-effort unsubscribe + drain; every failure swallowed."""
    try:
        if sub is not None:
            await sub.unsubscribe()
    except Exception:  # noqa: BLE001
        pass
    try:
        if client is not None:
            await client.drain()
    except Exception:  # noqa: BLE001
        pass
