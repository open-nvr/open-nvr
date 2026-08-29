# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""RFC-0002 Phase 0 normaliser: adapter completions → domain events.

``docs/EVENT_CONTRACTS.md`` (the normative source) decided that the
completion publish site also emits the producer-independent domain
event — a thin normaliser at the source, not a new service. KAI-C is
that site: every plate OCR call, whether initiated by Tier-1 dispatch
or by core's visit enrichment, already meets here, so publishing the
domain event here is what makes "a subscriber can consume plates
without knowing which producer fired them" literally true.

The map is deliberately static and small. Growing it is a
docs-reviewed change: add the adapter → domain row to the contracts
doc table first, then here.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("kai-c.domain_events")

#: ``producer`` envelope field. Audit/debugging only — the contract
#: forbids consumers branching on it.
DOMAIN_EVENT_PRODUCER = "kai-c"

#: (subject, envelope) ready to publish, or None for "no domain event
#: for this completion" (not an error — most completions have none).
Normalised = Optional[Tuple[str, Dict[str, Any]]]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _envelope(
    schema: str,
    *,
    camera_id: str,
    correlation_id: Optional[str],
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """The EVENT_CONTRACTS.md envelope. Every field, every time."""
    return {
        "id": "evt_" + uuid.uuid4().hex[:12],
        "schema": schema,
        "correlation_id": correlation_id,
        "camera_id": camera_id,
        "ts": _utcnow_iso(),
        "producer": DOMAIN_EVENT_PRODUCER,
        "payload": payload,
    }


def _normalise_fast_plate_ocr(
    result: Dict[str, Any],
    *,
    camera_id: str,
    correlation_id: Optional[str],
    event_id: Optional[int],
) -> Normalised:
    """``fast_plate_ocr`` completion → ``plate.recognized.v1``.

    Fires only for **accepted** reads (the contract): honours the
    adapter's own ``accepted`` verdict when present and requires a
    non-empty ``plate_text``. Normalisation (uppercase, no separators,
    capped) mirrors core's ``plate_enrichment.extract_plate`` so the
    bus and the timeline column can never disagree about the text.
    """
    if result.get("accepted") is False:
        return None
    text = result.get("plate_text")
    if not isinstance(text, str):
        return None
    text = "".join(text.split()).upper()[:32]
    if not text:
        return None
    confidence = result.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        confidence = None
    payload: Dict[str, Any] = {
        "plate_text": text,
        "confidence": confidence,
        # The chain's upstream detection label. The initiators that know
        # it don't currently thread it through the infer payload; the
        # field is contracted optional so it can arrive additively.
        "vehicle_label": None,
        "event_id": event_id,
    }
    subject = f"opennvr.events.plate.recognized.v1.{camera_id}"
    return subject, _envelope(
        "plate.recognized.v1",
        camera_id=camera_id,
        correlation_id=correlation_id,
        payload=payload,
    )


#: adapter name → normaliser. Mirrors the table in EVENT_CONTRACTS.md.
NORMALISERS: Dict[str, Callable[..., Normalised]] = {
    "fast_plate_ocr": _normalise_fast_plate_ocr,
}


def normalise_completion(
    adapter_name: str,
    result: Any,
    *,
    camera_id: Optional[str],
    correlation_id: Optional[str] = None,
    event_id: Optional[int] = None,
) -> Normalised:
    """Domain event for one successful completion, or None.

    None is the common case: no normaliser for this adapter, no
    camera_id (a domain event without its camera is uncontractable —
    conformance probes land here), or the normaliser judged the result
    not domain-worthy (e.g. a rejected OCR read). Never raises.
    """
    fn = NORMALISERS.get(adapter_name)
    if fn is None:
        return None
    if not camera_id:
        logger.debug(
            "domain event skipped for %s: no camera_id [correlation_id=%s]",
            adapter_name, correlation_id,
        )
        return None
    if not isinstance(result, dict):
        return None
    try:
        return fn(
            result,
            camera_id=camera_id,
            correlation_id=correlation_id,
            event_id=event_id,
        )
    except Exception:  # noqa: BLE001 — normalisation must never hurt the caller
        logger.warning(
            "domain-event normalisation failed for %s [correlation_id=%s]",
            adapter_name, correlation_id, exc_info=True,
        )
        return None
