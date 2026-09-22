# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
Caption enrichment for the event store (RFC-0001 C1).

The sibling of ``plate_enrichment``, and deliberately the same shape: when
a visit is ingested, caption its best frame ONCE through KAI-C (in a
FastAPI background task — never on the ingest request path) and write the
words into ``event_text``. "Red truck at the dock yesterday" then answers
from the canonical store, because that is where the words now live.

Why this exists at all
----------------------
``event_text`` had a table, a GIN index (migration ``c1d2e3f4a5b6``), an
ingest endpoint and a search service matching against it — and no
producer. Nothing in the repo wrote a row except the tests. Meanwhile the
detect-pipeline already routes a ``caption`` task over people and common
vehicles, and those captions went out on the bus where the footage-search
example caught them and wrote them into its own private SQLite index.

So the platform's descriptions were being diverted into an optional app's
private store while the canonical store's text column sat empty, and
core's search had nothing to match "red" against. This closes that: the
words land on the visit, once, next to the evidence they describe.

Best-effort by design, exactly like the plate sweep: adapter missing or
unreachable, a caption that comes back empty, or a label nobody captions
→ the row simply keeps no text. History must never depend on an optional
adapter, and search degrades to label-and-time, which is what it does
today.

Nothing that already works changes shape. The search service's join to
``event_text`` is an OUTER one (see its own comment — an inner join
"would silently restrict"), so filling this table can only ADD matches
and refine ranking; it can never remove a result that returns today.
"""

from __future__ import annotations

import asyncio as _asyncio
import logging
from typing import Any

logger = logging.getLogger("caption_enrichment")

#: The taxonomy task a captioner advertises (server/config/tasks.yml).
#: Both BLIP Scene Caption and Moondream VLM advertise it, which is why
#: the adapter is resolved by task below instead of being named here the
#: way plate_enrichment names fast_plate_ocr — either may be the one
#: registered, and a site may run neither.
CAPTION_TASK = "scene_caption"

#: Its canonical name in ``server/config/tasks.yml`` (``scene_caption``
#: is an alias there). An adapter may advertise either, so resolution
#: below accepts both rather than betting on one spelling.
CAPTION_TASK_CANONICAL = "image_captioning"
CAPTION_TASK_NAMES = {CAPTION_TASK, CAPTION_TASK_CANONICAL}

#: The per-camera ASSIGNMENT gate, and the whole reason this feature is
#: free for sites that have not asked for it. ``wants_plate`` grew the
#: same argument after every vehicle on every camera bought a full OCR
#: inference — "a thirty-camera site paid plate recognition thirty times
#: over to watch one gate". Captioning has exactly that shape, so it
#: takes exactly that gate: no assignment, no caption, no cost.
CAPTION_SKILL = CAPTION_TASK_CANONICAL

#: Burst guard, same reasoning as the OCR one: a crowd finishing tracks
#: together must not fan out into unbounded concurrent caption calls.
#: Enrichment is background work with no latency SLA, so waiting is free.
_CAPTION_CONCURRENCY = _asyncio.Semaphore(2)

#: Labels worth a caption. Mirrors the detect-pipeline's own default
#: caption routing (``dispatch.py`` ``_DEFAULT_ROUTES``) rather than
#: inventing a second opinion about what is describable — if the two
#: disagree, an operator sees captions on the bus that never reach the
#: store, or pays for inference on visits the pipeline considered not
#: worth describing.
CAPTIONABLE_LABELS = {
    "person", "bicycle", "car", "motorcycle", "bus", "truck",
}


async def _resolve_caption_adapter() -> str | None:
    """Name of a registered adapter advertising ``scene_caption``.

    None when capabilities cannot be read or nothing advertises it —
    which is a no-op, not an error: a box with no captioner keeps
    label-and-time search and loses nothing it had.
    """
    try:
        # The skills view's accessor rather than a raw get_capabilities():
        # it is TTL-cached and returns health alongside. This runs once per
        # captioned visit, so an uncached fetch here would hammer KAI-C at
        # roughly the visit rate — and it already swallows its own failures
        # instead of raising. Importing a router helper into a service is
        # the established direction here (internal_camera_agent does the
        # same for the same function).
        from routers.skills import _kai_c_view

        health, caps = await _kai_c_view()
    except Exception as exc:                      # noqa: BLE001
        logger.debug("caption enrichment: capabilities unreadable (%s)", exc)
        return None

    if not isinstance(caps, dict):
        return None
    healthy = {
        name for name, entry in (health or {}).items()
        if isinstance(entry, dict) and entry.get("status") == "ok"
    } if isinstance(health, dict) else None

    # Deterministic pick when a site runs both: sorted, so the same box
    # captions with the same adapter every time and a changed caption
    # style is traceable to a config change rather than to chance.
    for name in sorted(caps):
        entry = caps.get(name)
        caps_entry = (entry or {}).get("capabilities") if isinstance(entry, dict) else None
        tasks = caps_entry.get("tasks_advertised") if isinstance(caps_entry, dict) else None
        if not (isinstance(tasks, list) and any(
                isinstance(t, str) and t.lower() in CAPTION_TASK_NAMES
                for t in tasks)):
            continue
        # Skip one KAI-C reports unhealthy; when health is unknown, try
        # anyway — the call failing is cheaper than refusing to caption
        # because a health probe was unavailable.
        if healthy is not None and name not in healthy:
            continue
        return name
    return None


async def _caption_jpeg(jpeg: bytes, adapter: str, camera_handle: str,
                        event_id: int | None = None) -> str | None:
    """One caption attempt through KAI-C. None on any failure."""
    from core.config import settings
    from services.adapter_contract import build_infer_payload

    import httpx

    params: dict[str, Any] = {"camera_id": camera_handle}
    if event_id is not None:
        params["event_id"] = int(event_id)
    payload = build_infer_payload(task=CAPTION_TASK, jpeg_bytes=jpeg,
                                  params=params)
    try:
        async with _CAPTION_CONCURRENCY:
            async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
                resp = await client.post(
                    f"{settings.kai_c_url}/api/v1/infer/{adapter}",
                    json=payload,
                    headers={"X-Internal-Api-Key": settings.internal_api_key},
                )
    except Exception as exc:                      # noqa: BLE001
        logger.warning("caption enrichment: %s unreachable (%s)", adapter, exc)
        return None
    if resp.status_code != 200:
        logger.warning("caption enrichment: %s returned %s",
                       adapter, resp.status_code)
        return None
    try:
        body = resp.json()
    except Exception:                             # noqa: BLE001
        return None
    result = (body or {}).get("result")
    if not isinstance(result, dict):
        return None
    # Adapters differ in where they put the sentence; accept the shapes
    # the two registered captioners actually use rather than insisting on
    # one and silently storing nothing for the other.
    for key in ("caption", "text", "answer", "description"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def wants_caption(label: str | None, evidence_path: str | None,
                  enabled: bool = True,
                  camera_skills: set[str] | None = None) -> bool:
    """Should this freshly-ingested visit be queued for a caption? Pure,
    tested, and the exact shape of ``wants_plate`` for the same reasons.

    ``None`` camera_skills means "caller could not resolve the camera"
    and is treated as NOT assigned. Failing closed is right here too:
    the cost of a wrong False is a visit with no words, which search
    degrades over gracefully; the cost of a wrong True is describing
    people on cameras nobody asked to have described — and paying an
    inference per visit to do it.
    """
    if not (enabled and evidence_path and (label or "").lower() in CAPTIONABLE_LABELS):
        return False
    return CAPTION_SKILL in (camera_skills or set())


async def enrich_event_caption(event_id: int, evidence_jpeg: bytes | None = None) -> None:
    """Background task: describe the visit's best frame, once.

    Three phases, and the split is not stylistic — ``plate_enrichment``
    learned it the hard way. READ what is needed with a short session,
    CLOSE it, call the adapter with NO session held, then REOPEN to
    write. The middle phase waits on a semaphore and then on a 15s HTTP
    timeout; holding a connection across it is what exhausted core's
    pool when visits arrived at roughly one a second, and reads stopped
    while events kept flowing.
    """
    from core.config import settings

    if not getattr(settings, "events_caption_enrichment", True):
        return

    # ── Phase 1: read, briefly ──────────────────────────────────────
    from core.database import SessionLocal
    from models import TimelineEvent

    db = SessionLocal()
    try:
        row = db.get(TimelineEvent, int(event_id))
        if row is None:
            return
        label = (row.label or "").lower()
        if label not in CAPTIONABLE_LABELS:
            return
        # Already described (a re-run, or an app got there first): leave
        # it. This task never overwrites someone else's words — the
        # endpoint exists for a deliberate re-caption.
        from models import EventText

        if db.get(EventText, row.id) is not None:
            return
        evidence_path = row.evidence_path
        # Same derivation plate_enrichment uses (f"cam{camera_id}") — the
        # adapter-facing handle convention, not a DB column.
        camera_handle = f"cam{row.camera_id}"
    finally:
        db.close()

    # ── Phase 2: adapter call, no session held ──────────────────────
    jpeg = evidence_jpeg
    if jpeg is None:
        if not evidence_path:
            return
        from services.evidence_store import resolve_evidence

        path = resolve_evidence(evidence_path)
        if path is None:
            return
        try:
            jpeg = path.read_bytes()
        except OSError as exc:
            logger.debug("caption enrichment: evidence unreadable (%s)", exc)
            return

    adapter = await _resolve_caption_adapter()
    if adapter is None:
        return
    caption = await _caption_jpeg(jpeg, adapter, camera_handle, event_id=event_id)
    if not caption:
        return

    # ── Phase 3: reopen and write ───────────────────────────────────
    from datetime import UTC, datetime

    from models import EventText

    db = SessionLocal()
    try:
        row = db.get(TimelineEvent, int(event_id))
        if row is None:
            # The visit was deleted by retention while we captioned it.
            return
        existing = db.get(EventText, row.id)
        if existing is None:
            existing = EventText(event_id=row.id)
            db.add(existing)
        elif existing.source and existing.source != adapter:
            # Somebody else described it while the adapter was thinking.
            # Theirs stands: a later caption is not a better one.
            return
        existing.caption = caption
        existing.source = adapter[:60]
        existing.updated_at = datetime.now(UTC)
        db.commit()
    except Exception:                              # noqa: BLE001
        logger.exception("caption enrichment: write failed for event %s", event_id)
        db.rollback()
    finally:
        db.close()
