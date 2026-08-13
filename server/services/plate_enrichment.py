# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
#
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""
LPR enrichment for the event store (RFC-0001 C1, PR-C).

A vehicle visit's best frame is, by construction, the largest/sharpest look
Tier-0 had at the vehicle — the ideal OCR input. When such a visit is
ingested, this module runs fast-plate-ocr over the evidence crop ONCE (in a
FastAPI background task — never on the ingest request path) and writes
``plate_text`` onto the same row. "Which car entered at 3pm?" then answers
with a registration number, from one OCR call per visit instead of per
frame.

Best-effort by design: adapter missing/unreachable, OCR rejects the read,
or no plate visible → the row simply keeps ``plate_text = NULL``. History
must never depend on an optional adapter.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("plate_enrichment")

# Burst guard: a convoy of vehicles finishing tracks together must not fan
# out into unbounded concurrent OCR calls against the adapter. Two in
# flight, the rest queue on the semaphore — enrichment is background work
# with no latency SLA, so waiting is free.
import asyncio as _asyncio

_OCR_CONCURRENCY = _asyncio.Semaphore(2)

# COCO vehicle classes worth an OCR attempt.
VEHICLE_LABELS = {"car", "truck", "bus", "motorcycle"}

PLATE_MODEL = "fast_plate_ocr"
PLATE_TASK = "license_plate_recognition"


def extract_plate(response: dict | None) -> str | None:
    """Pure parser for the adapter's §5 InferResponse → normalized plate.

    Honours the adapter's own ``accepted`` verdict when present (its
    confidence threshold, not ours), requires non-empty ``plate_text``, and
    normalizes to uppercase-no-spaces capped at the column width.
    """
    if not isinstance(response, dict):
        return None
    result = response.get("result")
    if not isinstance(result, dict):
        return None
    if result.get("accepted") is False:
        return None
    text = result.get("plate_text")
    if not isinstance(text, str):
        return None
    text = "".join(text.split()).upper()
    return text[:32] or None


async def enrich_event_plate(event_id: int) -> None:
    """Background task: OCR the visit's evidence crop, store plate_text.

    Opens its own DB session (the request's session is gone by the time a
    background task runs). Every failure path is a debug/warning log and a
    clean return — never an exception escaping into the task runner.
    """
    from core.config import settings
    from core.database import SessionLocal
    from models import TimelineEvent
    from services.adapter_contract import build_infer_payload
    from services.evidence_store import resolve_evidence

    db = SessionLocal()
    try:
        row = db.query(TimelineEvent).filter(TimelineEvent.id == event_id).first()
        if (
            row is None
            or row.plate_text            # already enriched (retry raced)
            or (row.label or "") not in VEHICLE_LABELS
            or not row.evidence_path
        ):
            return
        path = resolve_evidence(row.evidence_path)
        if path is None:
            return
        jpeg = path.read_bytes()

        import httpx

        # camera_id threaded through so KAI-C's audit row and NATS subject
        # attribute this OCR call to the right camera (same convention as
        # process_inference's governed path).
        payload = build_infer_payload(
            task=PLATE_TASK, jpeg_bytes=jpeg,
            params={"camera_id": str(row.camera_id)},
        )
        try:
            async with _OCR_CONCURRENCY:
                async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
                    resp = await client.post(
                        f"{settings.kai_c_url}/api/v1/infer/{PLATE_MODEL}",
                        json=payload,
                        headers={
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                            "X-Internal-Api-Key": settings.internal_api_key or "",
                        },
                    )
        except httpx.HTTPError as e:
            logger.debug("plate enrichment: KAI-C unreachable for event %s: %s",
                         event_id, e)
            return
        if resp.status_code != 200:
            logger.debug("plate enrichment: adapter answered %s for event %s "
                         "(fast_plate_ocr not registered?)",
                         resp.status_code, event_id)
            return
        plate = extract_plate(resp.json())
        if not plate:
            return
        row.plate_text = plate
        db.commit()
        logger.info("plate enrichment: event %s -> %s", event_id, plate)
    except Exception:
        logger.warning("plate enrichment failed for event %s", event_id,
                       exc_info=True)
    finally:
        db.close()


def wants_plate(label: str | None, evidence_path: str | None,
                enabled: bool = True) -> bool:
    """Should this freshly-ingested visit be queued for OCR? Pure, tested."""
    return bool(enabled and evidence_path and (label or "") in VEHICLE_LABELS)
