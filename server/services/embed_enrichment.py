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

"""The producer for ``event_embeddings`` — one vector per visit.

Structurally identical to :mod:`services.caption_enrichment`, and
deliberately so. Same three phases (READ briefly, CLOSE, call the
adapter holding NO session, REOPEN to write), same burst semaphore, same
skill-assignment gate, same "no adapter registered is a silent no-op,
never an error". An enricher that invented its own shape would be one
more thing to reason about, and the shape it would have invented is
this one.

CORE DOES NOT KNOW WHAT A CLIP IS
---------------------------------

There is no model name in this file. It asks KAI-C which registered
adapters advertise the ``embed`` task and calls the first healthy one,
exactly as the captioner asks for ``scene_caption``. Swapping CLIP for
SigLIP, or running a face-embedding adapter instead of a scene one, is a
KAI-C registration change and nothing here moves. That is the whole
point of the adapter contract, and semantic search is not a good enough
reason to make an exception to it.

Which also means: no adapter advertising ``embed`` → this returns
immediately, no row is written, ``embedding_store.capability()`` reports
``none``, and search matches words. That is not a degraded mode, it is
the mode most deployments will run in, and it is the mode every test in
``test_hybrid_search.py`` treats as normal.

TEXT AND IMAGES, THE SAME SPACE
-------------------------------

:func:`embed_text` exists because a query is words and a visit is
pixels, and fusing them requires both to land in one space. That is
what a joint image-text model gives you; an adapter that can only embed
images will fail the text call and the vector arm simply will not run.
It is asked for through the same contract, so an adapter that does both
needs no special registration.
"""

from __future__ import annotations

import asyncio as _asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["EMBED_SKILL", "EMBED_TASK", "EMBEDDABLE_LABELS",
           "embed_text", "enrich_event_embedding", "wants_embedding"]

from services.embedding_store import EMBED_TASK

#: Aliases an adapter might advertise instead of the canonical name.
#: Mirrors the captioner's tolerance for the same reason: refusing to
#: recognise ``image_embedding`` because the registry says ``embed``
#: produces a site where semantic search is silently off and nothing
#: says why.
EMBED_TASK_NAMES = {EMBED_TASK, "embedding", "image_embedding", "clip"}

#: The skill an operator assigns to a camera to turn this on, same name
#: as the task. No assignment, no embedding, no cost — the gate the
#: captioner and the plate reader both grew after a thirty-camera site
#: paid for inference on every camera to watch one gate.
EMBED_SKILL = EMBED_TASK

#: Burst guard. Enrichment is background work with no latency SLA, so a
#: crowd finishing their tracks together waits rather than fanning out.
_EMBED_CONCURRENCY = _asyncio.Semaphore(2)

#: Labels worth embedding. Same set the captioner uses, because the two
#: describe the same things by different means and a visit worth a
#: sentence is a visit worth a vector.
from services.caption_enrichment import CAPTIONABLE_LABELS as EMBEDDABLE_LABELS


def wants_embedding(label: str | None, evidence_path: str | None,
                    enabled: bool = True,
                    camera_skills: set[str] | None = None,
                    camera_id: int | None = None) -> bool:
    """Should this freshly-ingested visit be queued for a vector?

    Pure and tested, the exact shape of ``wants_caption`` and
    ``wants_plate``. ``None`` camera_skills means the caller could not
    resolve the camera and is treated as NOT assigned — failing closed,
    because a wrong False costs a visit with no vector (search degrades
    over that gracefully) and a wrong True costs an inference per visit
    on cameras nobody asked about.
    """
    if not (enabled and evidence_path
            and (label or "").lower() in EMBEDDABLE_LABELS):
        return False
    if EMBED_SKILL in (camera_skills or set()):
        return True
    _note_unassigned(camera_id)
    return False


#: Same shape as the captioner's counter, for the same reason: the gate
#: is deliberate, silence about it was not. See caption_enrichment.
_unassigned_skipped: int = 0


def _note_unassigned(camera_id: int | None) -> None:
    global _unassigned_skipped
    _unassigned_skipped += 1
    if _unassigned_skipped == 1 or _unassigned_skipped % 1000 == 0:
        logger.warning(
            "embed enrichment is ON but camera %s has no %r skill assigned — "
            "%d qualifying visit(s) skipped with no vector; semantic search "
            "cannot rank them. Assign the skill (camera settings → Skills, or "
            "PUT /api/v1/cameras/{id} with assignments=[{\"skill\": %r}]); "
            "nothing recorded so far is embedded until "
            "EVENTS_ENRICHMENT_BACKFILL=true.",
            camera_id if camera_id is not None else "?", EMBED_SKILL,
            _unassigned_skipped, EMBED_SKILL,
        )


async def _resolve_embed_adapter() -> str | None:
    """A registered, healthy adapter advertising ``embed``. None if there
    is none — a no-op, not an error."""
    try:
        from routers.skills import _kai_c_view

        health, caps = await _kai_c_view()
    except Exception as exc:                      # noqa: BLE001
        logger.debug("embed enrichment: capabilities unreadable (%s)", exc)
        return None

    if not isinstance(caps, dict):
        return None
    healthy = {
        name for name, entry in (health or {}).items()
        if isinstance(entry, dict) and entry.get("status") == "ok"
    } if isinstance(health, dict) else None

    # Deterministic: sorted, so the same box embeds with the same adapter
    # every time. This matters more here than for captions — a store
    # holding vectors from two models ranks incoherently, and the
    # capability note exists to surface exactly that.
    for name in sorted(caps):
        entry = caps.get(name)
        caps_entry = (entry or {}).get("capabilities") if isinstance(entry, dict) else None
        tasks = caps_entry.get("tasks_advertised") if isinstance(caps_entry, dict) else None
        if not (isinstance(tasks, list) and any(
                isinstance(t, str) and t.lower() in EMBED_TASK_NAMES
                for t in tasks)):
            continue
        # Health unknown → try anyway. A failed call is cheaper than
        # refusing to embed because a probe was unavailable.
        if healthy is not None and name not in healthy:
            continue
        return name
    return None


def _vector_from(body: Any) -> list[float] | None:
    """Pull a vector out of whatever shape the adapter replied with.

    Adapters disagree about the key, the same way captioners disagree
    about ``caption`` vs ``text``. Accepting the shapes that exist beats
    insisting on one and storing nothing for the others — with one
    limit: the value has to be a flat list of numbers. A nested list is
    a batch response to a single-item request, which is a contract
    breach worth failing on rather than silently taking ``[0]`` of.
    """
    result = (body or {}).get("result") if isinstance(body, dict) else None
    if not isinstance(result, dict):
        return None
    for key in ("embedding", "vector", "features", "embeddings"):
        value = result.get(key)
        if not isinstance(value, list) or not value:
            continue
        if all(isinstance(v, (int, float)) and not isinstance(v, bool)
               for v in value):
            return [float(v) for v in value]
        logger.warning("embed enrichment: %r is not a flat vector", key)
        return None
    return None


async def _infer(adapter: str, payload: dict, *, what: str) -> list[float] | None:
    """One embedding attempt through KAI-C. None on any failure."""
    from core.config import settings

    import httpx

    try:
        async with _EMBED_CONCURRENCY:
            async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
                resp = await client.post(
                    f"{settings.kai_c_url}/api/v1/infer/{adapter}",
                    json=payload,
                    headers={"X-Internal-Api-Key": settings.internal_api_key},
                )
    except Exception as exc:                      # noqa: BLE001
        logger.warning("embed enrichment: %s unreachable (%s)", adapter, exc)
        return None
    if resp.status_code != 200:
        logger.warning("embed enrichment: %s returned %s for %s",
                       adapter, resp.status_code, what)
        return None
    try:
        body = resp.json()
    except Exception:                             # noqa: BLE001
        return None
    return _vector_from(body)


async def embed_text(text: str) -> tuple[list[float] | None, str | None]:
    """Embed a QUERY, for the search-side arm.

    Returns ``(vector, adapter_name)``. ``(None, None)`` when no adapter
    advertises ``embed``, when the one that does cannot embed text, or
    when it is unreachable — all of which mean the same thing to the
    caller: run the word search, which is what it did before.

    The adapter name comes back so the caller can record WHICH model
    produced the query vector. A query embedded by one model and
    compared against a store embedded by another produces a confident
    ordering of noise, and the only way to notice is to have both names.
    """
    text = (text or "").strip()
    if not text:
        return None, None
    adapter = await _resolve_embed_adapter()
    if adapter is None:
        return None, None
    payload = {"task": EMBED_TASK, "text": text}
    vector = await _infer(adapter, payload, what="a text query")
    return vector, (adapter if vector else None)


async def enrich_event_embedding(event_id: int,
                                 evidence_jpeg: bytes | None = None) -> None:
    """Background task: embed the visit's best frame, once.

    Three phases. The middle one waits on a semaphore and then on a 15s
    HTTP timeout; holding a database connection across it is what
    exhausted core's pool when visits arrived at roughly one a second,
    and reads stopped while events kept flowing. ``plate_enrichment``
    learned that; nothing here gets to relearn it.
    """
    from core.config import settings

    if not getattr(settings, "events_embed_enrichment", False):
        return

    # ── Phase 1: read, briefly ──────────────────────────────────────
    from core.database import SessionLocal
    from models import EventEmbedding, TimelineEvent

    db = SessionLocal()
    try:
        row = db.get(TimelineEvent, int(event_id))
        if row is None:
            return
        if (row.label or "").lower() not in EMBEDDABLE_LABELS:
            return
        # Already embedded. Re-embedding is what the backfill is for,
        # deliberately and in bulk, not a side effect of a retry.
        if db.get(EventEmbedding, row.id) is not None:
            return
        evidence_path = row.evidence_path
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
            logger.debug("embed enrichment: evidence unreadable (%s)", exc)
            return

    adapter = await _resolve_embed_adapter()
    if adapter is None:
        return

    from services.adapter_contract import build_infer_payload

    payload = build_infer_payload(
        task=EMBED_TASK, jpeg_bytes=jpeg,
        params={"camera_id": camera_handle, "event_id": int(event_id)})
    vector = await _infer(adapter, payload, what=f"event {event_id}")
    if not vector:
        return

    # ── Phase 3: reopen and write ───────────────────────────────────
    from services.embedding_store import put_embedding

    db = SessionLocal()
    try:
        if db.get(TimelineEvent, int(event_id)) is None:
            # Retention deleted the visit while the adapter was thinking.
            return
        put_embedding(db, event_id=int(event_id), vector=vector, model=adapter)
    except Exception:                              # noqa: BLE001
        logger.exception("embed enrichment: write failed for event %s", event_id)
        db.rollback()
    finally:
        db.close()
