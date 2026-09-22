# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The back catalogue: captions and claims for visits already recorded.

``caption_enrichment`` and ``descriptor_enrichment`` run off the INGEST
path. Everything that happened before they were deployed — every visit
on the box today — has a row in ``events`` and nothing in ``event_text``
or ``visit_descriptors``. So the first thing an operator does after
upgrading is search yesterday for "white van", get nothing, and conclude
the feature does not work. It works; it has simply never been pointed at
history.

This sweep walks that history and hands each qualifying visit to the
SAME enricher the ingest path calls.

Not a fourth enricher
---------------------
There is no second copy of any rule here. The labels, the per-camera
assignment gate, the plan lookup, the vocabulary, the three-phase session
discipline, the "already done" short-circuit and the write itself all
stay where they are: this module decides only WHICH visits to offer and
HOW FAST. If the two ever disagreed about what a caption is worth, a
search would return different answers for last week and last hour, and
nothing in the UI would explain why.

Newest first, and it never comes back
-------------------------------------
The walk is by descending event id from a persisted cursor, so it starts
at the most recent history and works backwards. Two reasons, both
practical. An operator searching "last Tuesday" wants the last fortnight
long before they want last spring; and retention is deleting the oldest
rows anyway, so a forward walk would spend inference on visits that age
out before anyone searches them.

The cursor is why "looked and found nothing" costs nothing twice. The
enrichers record that they ran — ``EventText`` for a caption,
``ran_tasks`` in ``payload["enriched_by"]`` for claims — and skip a visit
they have already handled, but a visit that was looked at and yielded
nothing usable still matches the cheap SQL filter below. Without a cursor
the sweep would re-offer that visit on every pass and never reach the one
behind it. With one, every visit is considered exactly once, and the pass
that reaches the oldest row is the last pass.

Off by default, and deliberately slow
-------------------------------------
This is the one enrichment path that can spend a lot of inference without
anybody doing anything, so ``events_enrichment_backfill`` defaults to
False: upgrading a running site must never quietly start working through
its back catalogue on the GPU the live cameras are using. When it is
switched on, items are processed one at a time with a pause between them
— live ingest and this sweep share one adapter and one semaphore, and
live work must always win. History has waited this long; it can wait a
few more hours.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("enrichment_backfill")

#: Progress lives in the generic site-settings key/JSON table, so this
#: needs no migration and an operator can read it straight out of the DB.
#: Key length is capped at 50 characters there.
STATE_KEY = "enrichment_backfill"

#: Rows examined per pass. Small on purpose: a pass holds a session only
#: long enough to read the batch, and a crash costs at most this many
#: visits' worth of re-examination (which is cheap — the enrichers
#: short-circuit on anything already done).
DEFAULT_BATCH = 50

#: Between items, so the live ingest path is never starved of the
#: adapter. At two enrichments a second this is still ~150k visits a day.
DEFAULT_PAUSE_SECONDS = 0.5

#: Between passes. Only reached when a pass found candidates; a finished
#: sweep stops rather than idling.
DEFAULT_INTERVAL_SECONDS = 5.0


def _labels_of_interest() -> set[str]:
    """Union of the labels the two enrichers would act on.

    Read from the enrichers rather than restated, so a label added to
    either one is swept without a second edit here — the failure mode of
    a private copy is that history silently lags the live path for
    exactly the class somebody just enabled.
    """
    from services.caption_enrichment import CAPTIONABLE_LABELS
    from services.descriptor_enrichment import DESCRIBABLE_LABELS

    return set(CAPTIONABLE_LABELS) | set(DESCRIBABLE_LABELS)


def read_state(db) -> dict[str, Any]:
    """Persisted progress. Empty dict before the first pass."""
    from services import site_settings

    state = site_settings.get_json(db, STATE_KEY, default=None)
    return state if isinstance(state, dict) else {}


def _write_state(db, state: dict[str, Any]) -> None:
    from services import site_settings

    site_settings.set_json(db, STATE_KEY, state)


def plan_batch(db, before_id: int | None, limit: int) -> list[dict[str, Any]]:
    """The next batch of candidate visits, newest first.

    Returns plain dicts, never ORM rows: the caller closes this session
    before calling any adapter (the rule ``plate_enrichment`` documents —
    holding a connection across a 15-second inference exhausted core's
    pool at roughly one visit a second), and a detached row would raise
    the moment it was touched afterwards.

    ``before_id`` is exclusive and ``None`` means "start at the newest".
    The SQL filter is deliberately coarse — label and evidence only. Which
    visits actually deserve work is decided by ``wants_caption`` and
    ``wants_descriptors`` below, and whether the work was already done is
    decided inside the enrichers. Duplicating either test as a JOIN here
    would be a second opinion that can drift.
    """
    from models import Camera, TimelineEvent
    from services.caption_enrichment import CAPTIONABLE_LABELS, wants_caption
    from services.descriptor_enrichment import DESCRIBABLE_LABELS, wants_descriptors
    from services.skill_assignments import camera_skills

    labels = _labels_of_interest()
    q = (
        db.query(TimelineEvent, Camera)
        # INNER join on purpose: the gate is a property of the camera, and
        # a visit whose camera is gone cannot be shown to have been
        # assigned the skill. Failing closed here is the same choice
        # wants_caption makes for an unresolvable camera.
        .join(Camera, Camera.id == TimelineEvent.camera_id)
        .filter(TimelineEvent.label.in_(sorted(labels)))
        .filter(TimelineEvent.evidence_path.isnot(None))
    )
    if before_id is not None:
        q = q.filter(TimelineEvent.id < int(before_id))
    rows = q.order_by(TimelineEvent.id.desc()).limit(int(limit)).all()

    out: list[dict[str, Any]] = []
    for row, camera in rows:
        label = (row.label or "").lower()
        skills = camera_skills(camera)
        item = {
            "event_id": int(row.id),
            "caption": (label in CAPTIONABLE_LABELS
                        and wants_caption(row.label, row.evidence_path,
                                          True, skills)),
            "descriptors": (label in DESCRIBABLE_LABELS
                            and wants_descriptors(row.label, row.evidence_path,
                                                  True, skills)),
        }
        out.append(item)
    return out


async def backfill_once(batch: int = DEFAULT_BATCH,
                        pause: float = DEFAULT_PAUSE_SECONDS) -> dict[str, Any]:
    """One pass: read a batch, enrich it, advance the cursor.

    Returns the updated state. ``state["done"]`` is True once the pass
    walked off the oldest row — there is no more history, so the loop
    stops rather than rescanning a table that only grows at the top
    (which the live ingest path is already handling).
    """
    from core.config import settings
    from core.database import SessionLocal

    caption_on = bool(getattr(settings, "events_caption_enrichment", True))
    descriptor_on = bool(getattr(settings, "events_descriptor_enrichment", True))

    # ── Phase 1: read the batch, briefly ────────────────────────────
    db = SessionLocal()
    try:
        state = dict(read_state(db))
        if state.get("done"):
            return state
        items = plan_batch(db, state.get("cursor"), batch)
    finally:
        db.close()

    if not items:
        # Walked off the oldest row. Record it so a restart does not
        # begin the whole sweep again from the newest visit.
        db = SessionLocal()
        try:
            state = dict(read_state(db))
            state["done"] = True
            _write_state(db, state)
        finally:
            db.close()
        logger.info("enrichment backfill: history complete (%s visits examined)",
                    state.get("examined", 0))
        return state

    # ── Phase 2: enrich, one at a time, no session held ─────────────
    from services.caption_enrichment import enrich_event_caption
    from services.descriptor_enrichment import enrich_event_descriptors

    captioned = 0
    described = 0
    for item in items:
        event_id = item["event_id"]
        if caption_on and item["caption"]:
            try:
                await enrich_event_caption(event_id)
                captioned += 1
            except Exception:  # noqa: BLE001
                # One unreachable adapter or one unreadable evidence file
                # must not end the sweep — the cursor still advances past
                # this visit, because retrying it forever would stall
                # every visit behind it.
                logger.warning("enrichment backfill: caption failed for %s",
                               event_id, exc_info=True)
        if descriptor_on and item["descriptors"]:
            try:
                await enrich_event_descriptors(event_id)
                described += 1
            except Exception:  # noqa: BLE001
                logger.warning("enrichment backfill: descriptors failed for %s",
                               event_id, exc_info=True)
        if pause > 0:
            # Live ingest shares this adapter and its semaphore. Yielding
            # between items is what keeps a sweep of the back catalogue
            # from delaying the caption of somebody at the door now.
            await asyncio.sleep(pause)

    # ── Phase 3: reopen and advance ─────────────────────────────────
    lowest = min(int(i["event_id"]) for i in items)
    db = SessionLocal()
    try:
        state = dict(read_state(db))
        state["cursor"] = lowest
        state["examined"] = int(state.get("examined", 0)) + len(items)
        state["captioned"] = int(state.get("captioned", 0)) + captioned
        state["described"] = int(state.get("described", 0)) + described
        _write_state(db, state)
    except Exception:  # noqa: BLE001
        logger.exception("enrichment backfill: could not record progress")
        db.rollback()
    finally:
        db.close()

    logger.info(
        "enrichment backfill: %s examined, %s captioned, %s described, "
        "cursor now %s", len(items), captioned, described, lowest,
    )
    return state


async def run_backfill_loop(batch: int = DEFAULT_BATCH,
                            pause: float = DEFAULT_PAUSE_SECONDS,
                            interval: float = DEFAULT_INTERVAL_SECONDS) -> None:
    """Sweep history until it is done, then return.

    Returning is the point: this is finite work, not a consumer. Once the
    cursor reaches the oldest visit, new visits are the ingest path's job
    and there is nothing left for a loop to do.
    """
    from core.config import settings

    if not getattr(settings, "events_enrichment_backfill", False):
        return
    if not (getattr(settings, "events_caption_enrichment", True)
            or getattr(settings, "events_descriptor_enrichment", True)):
        # Nothing to write. Saying so beats a loop that walks the whole
        # table calling two enrichers that both return immediately.
        logger.info("enrichment backfill: both enrichers are off; nothing to do")
        return

    logger.info("enrichment backfill: starting (batch=%s, pause=%ss)",
                batch, pause)
    previous: Any = object()
    while True:
        state = await backfill_once(batch=batch, pause=pause)
        if state.get("done"):
            return
        cursor = state.get("cursor")
        if cursor == previous:
            # The cursor is the only thing guaranteeing forward progress.
            # If a pass ends where the last one did, something is wrong
            # with the walk, and a loop that spins on the same batch
            # forever is worse than one that stops and says so: it would
            # re-enrich the same visits for the life of the process.
            logger.error("enrichment backfill: cursor stuck at %s; stopping",
                         cursor)
            return
        previous = cursor
        await asyncio.sleep(interval)
