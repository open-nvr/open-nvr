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

# Issue #371: a missing OCR adapter used to be a per-event DEBUG line —
# operator-invisible, so "restart unregistered the adapter" looked
# exactly like "no plates in frame" for hours. Best-effort stays
# best-effort (rows keep plate_text=NULL, ingest never blocks), but a
# broken dependency now WARNs, rate-limited so a busy gate camera can't
# flood the log with one line per vehicle.
_MISSING_ADAPTER_WARN_INTERVAL_SECONDS = 600.0
# None = never warned. NOT 0.0: time.monotonic() counts from HOST BOOT,
# so on a machine up for less than the interval, ``now - 0.0 < 600``
# would swallow the very first warning — the one that matters most.
# (Caught by CI: fresh runner VMs boot seconds before pytest runs.)
_last_missing_adapter_warn: float | None = None


def _warn_adapter_missing(status_code: int) -> None:
    """Rate-limited operator signal that plate OCR is failing."""
    global _last_missing_adapter_warn
    import time as _time

    now = _time.monotonic()
    if (_last_missing_adapter_warn is not None
            and now - _last_missing_adapter_warn
            < _MISSING_ADAPTER_WARN_INTERVAL_SECONDS):
        return
    _last_missing_adapter_warn = now
    logger.warning(
        "plate enrichment: KAI-C answered %s for adapter '%s' — plate "
        "reads are FAILING. The adapter is not registered (or not "
        "approved) with KAI-C; check the AI Models page or "
        "GET /api/v1/adapters. This warning repeats at most every %d s.",
        status_code, PLATE_MODEL,
        int(_MISSING_ADAPTER_WARN_INTERVAL_SECONDS),
    )


#: A plate box within this many pixels of the crop boundary is treated as
#: CLIPPED. Deliberately tiny: only boxes literally abutting the edge are
#: rejected, so a plate that merely sits near the edge still reads. Measured
#: on the fragments that motivated this: every one had a margin of 0-1 px,
#: while whole-plate reads sat hundreds of pixels inside.
_PLATE_EDGE_TOLERANCE_PX = 2

#: JPEG start-of-image magic (0xFFD8).
_SOI = bytes((0xFF, 0xD8))


def jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a JPEG's SOF marker — no pixel decode.

    The clipping test below needs the crop's size, and the adapter reports
    the plate box in that crop's pixel coordinates but not the size itself.
    Parsing the header beats decoding the image on what is a per-visit path.
    Returns None for anything that isn't a JPEG we can read, which the
    caller treats as "cannot judge" (never as "clipped").
    """
    if not isinstance(data, (bytes, bytearray)) or data[:2] != _SOI:
        return None
    i, n = 2, len(data)
    while i + 3 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0x01 or 0xD0 <= marker <= 0xD9:
            i += 2                       # standalone markers carry no length
            continue
        seg = int.from_bytes(data[i + 2:i + 4], "big")
        if seg < 2:
            return None
        # SOF0-SOF15 carry the frame size; C4/C8/CC are DHT/JPG/DAC.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 9 > n:
                return None
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return (w, h) if w > 0 and h > 0 else None
        i += 2 + seg
    return None


def plate_box_is_clipped(
    box, image_size, *, tolerance: int = _PLATE_EDGE_TOLERANCE_PX
) -> bool:
    """Does the detected plate touch the edge of the crop it was found in?

    A vehicle crop is the tracked box plus a margin, clamped to the frame,
    so a vehicle leaving frame yields a crop whose edge cuts through the
    plate. The OCR then reads the CHARACTERS THAT SURVIVED and reports high
    confidence for them — "K884" out of "K884RS" scored 0.98. Confidence
    therefore cannot separate a partial read from a whole one; the geometry
    can. Unknown/misshapen input answers False: never invent a rejection.
    """
    if not image_size or box is None:
        return False
    try:
        x1, y1, x2, y2 = (float(v) for v in box)
        w, h = (float(v) for v in image_size)
    except (TypeError, ValueError):
        return False
    if w <= 0 or h <= 0:
        return False
    return (
        x1 <= tolerance or y1 <= tolerance
        or x2 >= w - tolerance or y2 >= h - tolerance
    )


def extract_plate(
    response: dict | None, *, image_size: tuple[int, int] | None = None
) -> str | None:
    """Pure parser for the adapter's §5 InferResponse → normalized plate.

    Honours the adapter's own ``accepted`` verdict when present (its
    confidence threshold, not ours), requires non-empty ``plate_text``, and
    normalizes to uppercase-no-spaces capped at the column width.

    With ``image_size`` (the OCR'd crop's dimensions) a read whose plate box
    abuts the crop boundary is rejected as a PARTIAL read. Those are worse
    than a miss: a truncated plate is written to the register as though it
    were a whole one, so one vehicle acquires several identities
    ("66HH07" also arriving as "66HH", "H07", "HHO7") and watchlist
    matching silently breaks. Without it the check is skipped.
    """
    if not isinstance(response, dict):
        return None
    result = response.get("result")
    if not isinstance(result, dict):
        return None
    if result.get("accepted") is False:
        return None
    detection = result.get("plate_detection")
    if isinstance(detection, dict) and plate_box_is_clipped(
        detection.get("box"), image_size
    ):
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
        # camera_id and event_id thread through KAI-C untouched: camera_id
        # attributes the audit row + NATS subjects, event_id joins the
        # resulting plate.recognized.v1 back to this timeline row (RFC-0002
        # Phase 0 — this call is now the *fallback* producer; the write can
        # arrive via the bus consumer or the synchronous path below,
        # whichever lands first).
        # camera_id is the platform HANDLE ("cam{N}"), not the bare numeric
        # id: the internal /cameras endpoint, Tier-0, and Tier-1 dispatch
        # all speak handles, so the plate.recognized.v1 this call produces
        # must too — a consumer scoping to assigned cameras (the LPR app)
        # compares against handles, and "3" != "cam3" would silently drop
        # every enrichment-produced event.
        payload = build_infer_payload(
            task=PLATE_TASK, jpeg_bytes=jpeg,
            params={"camera_id": f"cam{row.camera_id}", "event_id": int(row.id)},
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
            if resp.status_code in (403, 404):
                # 404 = adapter unknown to KAI-C (the #371 restart
                # amnesia); 403 = registered but approval pending.
                # Either way plate reads are dead until fixed — say so.
                _warn_adapter_missing(resp.status_code)
            return
        plate = extract_plate(resp.json(), image_size=jpeg_dimensions(jpeg))
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
