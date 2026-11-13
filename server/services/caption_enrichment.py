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

import logging
from typing import Any

logger = logging.getLogger("caption_enrichment")

from services.enrichment_gate import DROPPED  # noqa: E402

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

#: Burst control lives in ``services.enrichment_gate``, one gate per
#: adapter shared with the VQA and embedding callers. A module-local
#: semaphore used to sit here; it was released on TIMEOUT, so a slow
#: captioner got a fresh request every 15 s while still computing the
#: last one (#583). The gate holds a slot until the adapter answers.

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
                        event_id: int | None = None, *,
                        priority: str = "live") -> str | None:
    """One caption attempt through KAI-C. None on any failure."""
    from services.adapter_contract import build_infer_payload


    params: dict[str, Any] = {"camera_id": camera_handle}
    if event_id is not None:
        params["event_id"] = int(event_id)
    payload = build_infer_payload(task=CAPTION_TASK, jpeg_bytes=jpeg,
                                  params=params)
    # One call, through the adapter's gate: admission (a refusal is a
    # plain None — the visit keeps no caption today and the backfill
    # finds it later), the timeout-vs-unreachable distinction, and the
    # slot held until the adapter answered all live in the gate (#583).
    from services.enrichment_gate import infer_through_gate

    body = await infer_through_gate(adapter, payload, kind="caption", log=logger,
                                    caller="caption enrichment", priority=priority)
    if body is DROPPED:
        return DROPPED
    if body is None:
        return None                   # the call failed: nobody has looked yet
    # From here the adapter ANSWERED. "" means it had nothing usable to
    # say — that is "looked and found nothing", recorded on the row, and
    # distinct from None above, which must be retried. A 200 with no
    # result object in it (an error body, a model mid-reload) is the
    # adapter NOT answering, not an empty answer: None, so the visit is
    # not branded "looked at" by a minute of malformed replies.
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
    return ""


def wants_caption(label: str | None, evidence_path: str | None,
                  enabled: bool = True,
                  camera_skills: set[str] | None = None,
                  camera_id: int | None = None) -> bool:
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
    if CAPTION_SKILL in (camera_skills or set()):
        return True
    _note_unassigned(camera_id)
    return False


#: Visits that qualified on every count except the assignment. A site with
#: EVENTS_CAPTION_ENRICHMENT=true, a captioner registered and healthy, and
#: not one camera carrying the skill produces nothing and — until this —
#: said nothing: the flag looked on, the adapter looked fine, the store
#: stayed empty, and the operator concluded the feature was broken. The
#: gate is deliberate ("no assignment, no caption, no cost"); being
#: silent about it was not. One line the first time, then one per
#: thousand, so it is findable without becoming the log.
_unassigned_skipped: int = 0


def _note_unassigned(camera_id: int | None) -> None:
    global _unassigned_skipped
    _unassigned_skipped += 1
    if _unassigned_skipped == 1 or _unassigned_skipped % 1000 == 0:
        logger.warning(
            "caption enrichment is ON but camera %s has no %r skill assigned "
            "— %d qualifying visit(s) skipped with no caption. Assign the skill "
            "(camera settings → Skills, or PUT /api/v1/cameras/{id} with "
            "assignments=[{\"skill\": %r}]) to start captioning; nothing "
            "recorded so far is captioned unless Footage Search → Configure → "
            "Backfill history is on.",
            camera_id if camera_id is not None else "?", CAPTION_SKILL,
            _unassigned_skipped, CAPTION_SKILL,
        )


def _mark_attempted(event_id: int) -> None:
    """The call was made and yielded no caption. Not a failure worth a
    log line each time, but worth a mark: "looked and found nothing" is
    a different row from "never looked" — the same distinction the
    descriptor enricher keeps in ran_tasks."""
    from core.database import SessionLocal
    from models import TimelineEvent

    db = SessionLocal()
    try:
        row = db.get(TimelineEvent, int(event_id))
        if row is None:
            return
        seen = dict(row.payload or {})
        ran = sorted({*(seen.get("enriched_by") or []), CAPTION_SKILL})
        seen["enriched_by"] = ran
        row.payload = seen
        db.commit()
    except Exception:                              # noqa: BLE001
        logger.debug("caption enrichment: could not mark %s attempted", event_id,
                     exc_info=True)
        db.rollback()
    finally:
        db.close()


#: Payload key: wall-clock ISO time of the last caption call that failed.
CAPTION_FAILED_AT = "caption_failed_at"


def _mark_failed(event_id: int) -> None:
    """The call was made and never answered. Stamped with the time so the
    request lane applies its cooldown; cleared by the next caption."""
    from datetime import UTC, datetime
    from core.database import SessionLocal
    from models import TimelineEvent
    db = SessionLocal()
    try:
        row = db.get(TimelineEvent, int(event_id))
        if row is None:
            return
        seen = dict(row.payload or {})
        seen[CAPTION_FAILED_AT] = datetime.now(UTC).isoformat()
        row.payload = seen
        db.commit()
    except Exception:                              # noqa: BLE001
        logger.debug("caption enrichment: could not mark %s failed", event_id, exc_info=True)
        db.rollback()
    finally:
        db.close()


async def enrich_event_caption(event_id: int,
                               evidence_jpeg: bytes | None = None, *,
                               priority: str = "live") -> str | None:
    """Background task: describe the visit's best frame, once.

    Returns ``"dropped"`` when the call never reached the adapter (the
    gate refused it) — the backfill holds its cursor on that — and None
    otherwise, whether a caption was written or there was nothing to
    write.

    ``priority="requested"`` is the on-demand lane: somebody asked a
    question this visit could answer. It ignores the site-wide
    EVENTS_CAPTION_ENRICHMENT switch — that switch says whether EVERY
    visit is described unasked, and this one was asked for — and gets
    the governor's requested priority.

    Three phases, and the split is not stylistic — ``plate_enrichment``
    learned it the hard way. READ what is needed with a short session,
    CLOSE it, call the adapter with NO session held, then REOPEN to
    write. The middle phase waits at the adapter gate and then on an HTTP
    timeout; holding a connection across it is what exhausted core's
    pool when visits arrived at roughly one a second, and reads stopped
    while events kept flowing.
    """
    from core.config import settings

    if priority != "requested" and not getattr(settings, "events_caption_enrichment", True):
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
        # endpoint exists for a deliberate re-caption. The ROW existing is
        # not that: the descriptor enricher creates a caption-less
        # EventText for its claims, and a visit with a colour and no
        # sentence is exactly one the lane must be able to caption.
        from models import EventText

        existing = db.get(EventText, row.id)
        if existing is not None and (existing.caption or "").strip():
            return
        # Looked, and the captioner had nothing to say (an empty answer,
        # a frame it could not read): recorded on the row like the
        # descriptor enricher's ran_tasks, so the requested lane and the
        # catch-up do not ask about the same visit forever.
        if CAPTION_SKILL in ((row.payload or {}).get("enriched_by") or []):
            return
        # The same frame described before (RFC-0004 step 2): take that
        # visit's words and claims, do not call. Same pixels, same answer.
        from services import enrichment_reuse

        twin = enrichment_reuse.find_twin(db, row)
        reused = False
        if twin is not None:
            reused = enrichment_reuse.reuse_caption(db, row, twin)
            enrichment_reuse.reuse_descriptors(db, row, twin)
            db.commit()
        # The same signal, read the other way: visit after visit from a
        # frame already seen is a camera whose picture has stopped
        # changing (services/frame_health.py).
        try:
            from services import frame_health

            frozen_transition = frame_health.note(db, row, twin)
        except Exception:                          # noqa: BLE001
            logger.debug("caption enrichment: frame health note failed", exc_info=True)
            frozen_transition = None
        evidence_path = row.evidence_path
        # Same derivation plate_enrichment uses (f"cam{camera_id}") — the
        # adapter-facing handle convention, not a DB column.
        camera_handle = f"cam{row.camera_id}"
        # RFC-0004 step 1: when the single structured call is on, this
        # task also carries the descriptor questions for the visit, so it
        # needs what the descriptor enricher would have read — which
        # kinds are already answered, and whether VQA already ran.
        from services import structured_enrichment

        single = structured_enrichment.enabled()
        answered: set[str] = set()
        vqa_done = False
        if single:
            from models import VisitDescriptor
            from services.descriptor_enrichment import VQA_TASK

            vqa_done = VQA_TASK in ((row.payload or {}).get("enriched_by") or [])
            answered = {
                k for (k,) in db.query(VisitDescriptor.kind)
                .filter(VisitDescriptor.event_id == row.id,
                        VisitDescriptor.source_task == VQA_TASK).all()
            }
    finally:
        db.close()
    if frozen_transition:
        from services import frame_health

        await frame_health.publish(frozen_transition)
    if reused:
        # The caption is committed; its vector is the same best-effort
        # step as after a fresh caption.
        await _embed_caption(event_id)
        return "done"

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
    # One call for the sentence AND the attributes when the flag is on
    # and this class has questions worth asking on this adapter (RFC-0004
    # step 1). Otherwise the plain caption call, as before.
    claims: list = []
    json_ok = False
    kinds: list[str] = []
    if single and not vqa_done:
        kinds = await structured_enrichment.kinds_for_visit(label, adapter, answered)
    if kinds:
        structured = await structured_enrichment.ask_structured(
            jpeg, adapter, kinds, camera_handle, event_id, priority=priority)
        if structured is DROPPED:
            return "dropped"
        if structured is None:
            caption = None
        else:
            caption = structured.caption
            claims = structured.claims
            json_ok = structured.json_ok
    else:
        caption = await _caption_jpeg(jpeg, adapter, camera_handle, event_id=event_id,
                                      priority=priority)
    if caption is DROPPED:
        return "dropped"
    if caption is None:
        # Timed out, unreachable, a non-200: the adapter never answered.
        # NOT recorded as looked — a transient failure must not read as
        # "looked and found nothing" and hide the visit from every later
        # pass — but stamped, so the on-demand lane leaves this visit
        # alone for events_caption_retry_cooldown_s instead of asking
        # again on the next question.
        _mark_failed(event_id)
        return
    if not caption:
        _mark_attempted(event_id)
        if not (claims or json_ok):
            return "done"             # looked; nothing to say — a result

    # ── Phase 3: reopen and write ───────────────────────────────────
    from datetime import UTC, datetime

    from models import EventText

    written = False
    db = SessionLocal()
    try:
        row = db.get(TimelineEvent, int(event_id))
        if row is None:
            # The visit was deleted by retention while we captioned it.
            return
        if claims or json_ok:
            # The structured call's answers: the same rows, the same
            # ran_tasks rule as descriptor_enrichment — VQA counts as
            # ran only when the model answered in JSON, so a prose reply
            # leaves the questions for the per-question path.
            from services.descriptor_enrichment import VQA_TASK
            from services.descriptor_store import apply_descriptors

            apply_descriptors(db, row, claims, [VQA_TASK] if json_ok else [])
            db.commit()
            if not caption:
                return "done"
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
        written = True
    except Exception:                              # noqa: BLE001
        logger.exception("caption enrichment: write failed for event %s", event_id)
        db.rollback()
    finally:
        db.close()
    if not written:
        return
    # The caption is committed. Its vector is a separate, best-effort
    # step AFTER the session is closed (an adapter call holds no
    # session — the rule above), and nothing it does can take the
    # caption back: it is off unless EVENTS_EMBED_ENRICHMENT is on, a
    # no-op without an adapter, and any failure is a debug line.
    await _embed_caption(event_id)
    return "done"


async def _embed_caption(event_id: int) -> None:
    """Fold a freshly written caption into the vector store, so search
    can rank the visit by what the caption MEANS and not only by the
    words in it. Never raises."""
    try:
        from services.embed_enrichment import enrich_event_caption_embedding

        await enrich_event_caption_embedding(int(event_id))
    except Exception:                              # noqa: BLE001
        logger.debug("caption enrichment: caption vector for %s not written",
                     event_id, exc_info=True)
