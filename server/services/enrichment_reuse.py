# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Do not infer twice for the same frame (RFC-0004, step 2).

The evidence store is content-addressed: ``events.evidence_path`` is the
SHA-256 of the JPEG. Two visits with the same path are the same pixels,
and the same pixels get the same answer from the same model — so a visit
whose frame was already described takes the earlier visit's caption,
descriptor claims and "looked" marks, and the captioner is not called.

Measured on a Ventuno Q (2026-11-13, bench/rfc-0004-infer-once-log.md):
9,921 of 10,892 captioner calls in a day went to a frame byte-identical
to one already captioned — a looping clip re-detecting the same 450
frames — while a real office camera had none. So this costs nothing on a
real camera and removes nine calls in ten on a loop, a static scene
re-detected, or a replayed recording, with no quality question: identical
input, identical output. Near-repeats (the same person in the same chair,
a different JPEG) were measured too and are NOT reused: at a cosine safe
enough to trust (≥ 0.97) a real camera offers 3 % of its visits, and
below that one visit's words would land on another's.

What is copied: the caption and its source; the VQA claims; the
``enriched_by`` marks of those two skills. What is not: embeddings (they
are milliseconds and their enrichers run as usual) and plates (a plate
read is a different question, and the plate lane has its own store).
The copy is recorded on the visit (``reused_from``) so an operator can
see it was not a fresh look.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("enrichment_reuse")

SETTING = "events_enrichment_reuse_same_frame"
MARK = "reused_from"

#: Counters for the measurement.
STATS: dict[str, int] = {"caption_reused": 0, "descriptors_reused": 0, "looked": 0}


def enabled() -> bool:
    from core.config import settings

    return bool(getattr(settings, SETTING, True))


def find_twin(db, row) -> Any | None:
    """An EARLIER visit with the same evidence file and something to
    copy: a caption, or VQA recorded as ran. Earlier, so that two visits
    arriving together cannot each wait for the other; the first one in
    does the work."""
    if not enabled() or not getattr(row, "evidence_path", None):
        return None
    from models import EventText, TimelineEvent
    from services.descriptor_enrichment import VQA_TASK

    STATS["looked"] += 1
    candidates = (
        db.query(TimelineEvent)
        .filter(TimelineEvent.evidence_path == row.evidence_path,
                TimelineEvent.id < row.id)
        .order_by(TimelineEvent.id.desc())
        .limit(5)
        .all()
    )
    for cand in candidates:
        text = db.get(EventText, cand.id)
        if text is not None and (text.caption or "").strip():
            return cand
        if VQA_TASK in ((cand.payload or {}).get("enriched_by") or []):
            return cand
    return None


def reuse_caption(db, row, twin) -> bool:
    """Copy the twin's caption onto ``row`` if the twin has one and the
    row has none. Returns True when a caption was written (the caller
    commits and embeds)."""
    from models import EventText

    src = db.get(EventText, twin.id)
    if src is None or not (src.caption or "").strip():
        return False
    dst = db.get(EventText, row.id)
    if dst is None:
        dst = EventText(event_id=row.id)
        db.add(dst)
    elif (dst.caption or "").strip():
        return False
    dst.caption = src.caption
    dst.source = src.source
    dst.updated_at = datetime.now(UTC)
    _mark(row, twin)
    STATS["caption_reused"] += 1
    logger.debug("enrichment reuse: caption of visit %s copied to %s (same frame)", twin.id, row.id)
    return True


def reuse_descriptors(db, row, twin) -> bool:
    """Copy the twin's VQA claims and its VQA 'ran' mark onto ``row``.
    Returns True when the twin had VQA ran (even with no claims: "looked
    and found nothing" is copied too, as it should be)."""
    from models import VisitDescriptor
    from services.descriptor_enrichment import VQA_TASK
    from services.descriptor_store import apply_descriptors

    if VQA_TASK not in ((twin.payload or {}).get("enriched_by") or []):
        return False
    claims = (db.query(VisitDescriptor)
              .filter(VisitDescriptor.event_id == twin.id,
                      VisitDescriptor.source_task == VQA_TASK).all())
    apply_descriptors(db, row, claims, [VQA_TASK])
    _mark(row, twin)
    STATS["descriptors_reused"] += 1
    logger.debug("enrichment reuse: %d claim(s) of visit %s copied to %s (same frame)",
                 len(claims), twin.id, row.id)
    return True


def _mark(row, twin) -> None:
    payload = dict(row.payload or {})
    payload[MARK] = int(twin.id)
    row.payload = payload
