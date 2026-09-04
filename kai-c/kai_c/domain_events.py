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
import os
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


#: A plate box within this many pixels of its crop's edge is CLIPPED —
#: the same tolerance core's plate_enrichment applies (#378).
_PLATE_EDGE_TOLERANCE_PX = 2
#: Default floor for the localiser's confidence — same value as core's
#: OPENNVR_PLATE_MIN_DETECTION_CONFIDENCE (#386). 0 disables.
_DETECTION_FLOOR_DEFAULT = 0.6


def _detection_floor() -> float:
    raw = os.environ.get("KAI_C_PLATE_MIN_DETECTION_CONFIDENCE", "")
    try:
        value = float(raw)
    except ValueError:
        return _DETECTION_FLOOR_DEFAULT
    return value if value > 0 else 0.0


def _require_localisation() -> bool:
    raw = os.environ.get("KAI_C_PLATE_REQUIRE_LOCALISATION", "").strip()
    return raw.lower() not in ("0", "false", "no", "off")


def _detection_disqualifies(detection: Any) -> bool:
    """Is this read one the contract should never carry?

    ``plate.recognized.v1`` promises a *recognised plate*, and every
    subscriber — the LPR app raising "Unknown vehicle K884", the gate
    controller opening a barrier — acts on it as one. Three kinds of
    accepted OCR output are demonstrably not that, and each used to be
    published and then filtered by ONE consumer (core's timeline) while
    the others alerted on it:

    * **clipped** — the plate box abuts the crop edge: a fragment
      ("K884" of "K884RS") read at full confidence (#378);
    * **weak localisation** — the detector barely believed it was a
      plate: a badge OCR'd into characters (#386);
    * **not localised** — the detector looked at a vehicle crop and
      found no plate, and the OCR read the car body instead.

    All three need the adapter's ``plate_detection`` block; without it
    (an OCR-only adapter, or an older one) there is no opinion and the
    read passes, exactly as before. The judgement is pure arithmetic on
    fields the adapter already reports, so it belongs at the one place
    every initiator's reads already meet.
    """
    if not isinstance(detection, dict):
        return False
    attempted = detection.get("attempted")
    found = detection.get("found")
    if attempted is True and found is not True:
        return _require_localisation()
    box = detection.get("box")
    size = detection.get("image_size")
    try:
        if (isinstance(box, (list, tuple)) and len(box) == 4
                and isinstance(size, (list, tuple)) and len(size) == 2):
            x1, y1, x2, y2 = (float(v) for v in box)
            w, h = (float(v) for v in size)
            if w > 0 and h > 0 and (
                x1 <= _PLATE_EDGE_TOLERANCE_PX
                or y1 <= _PLATE_EDGE_TOLERANCE_PX
                or x2 >= w - _PLATE_EDGE_TOLERANCE_PX
                or y2 >= h - _PLATE_EDGE_TOLERANCE_PX
            ):
                return True
    except (TypeError, ValueError):
        pass
    conf = detection.get("confidence")
    floor = _detection_floor()
    if (floor > 0 and isinstance(conf, (int, float))
            and not isinstance(conf, bool) and float(conf) < floor):
        return True
    return False


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
    detection = result.get("plate_detection")
    if _detection_disqualifies(detection):
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
    # Additive optional field (EVENT_CONTRACTS.md "additive-only"): the
    # plate box the adapter localised, in the OCR'd crop's pixel space.
    # Consumers use it to reject PARTIAL reads — a crop whose edge cuts
    # through the plate still OCRs the surviving characters at high
    # confidence, so only the geometry can tell "K884" (a fragment of
    # "K884RS") from a whole plate. Still forwarded for consumers with
    # stricter policies; the obvious junk is no longer published at all
    # (see _detection_disqualifies).
    if isinstance(detection, dict):
        # How sure the localiser was that this was a plate at all.
        # Consumers reject FALSE localisations with it (#386): a
        # manufacturer badge OCRs into plausible characters at plausible
        # read confidence, from a box nowhere near a crop edge, so
        # neither the read's own confidence nor the geometry above can
        # tell it from a plate. The detector's doubt can.
        conf = detection.get("confidence")
        if isinstance(conf, (int, float)) and not isinstance(conf, bool):
            payload["plate_box_confidence"] = float(conf)
        box = detection.get("box")
        if isinstance(box, (list, tuple)) and len(box) == 4:
            payload["plate_box"] = list(box)
            # The frame of reference travels WITH the box: multi-frame
            # OCR reads plate CANDIDATE crops whose size differs from
            # the visit's evidence frame, so a consumer judging the box
            # against the evidence would measure in the wrong image.
            # Optional + additive, like plate_box itself.
            size = detection.get("image_size")
            if isinstance(size, (list, tuple)) and len(size) == 2:
                payload["plate_box_image"] = list(size)
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
