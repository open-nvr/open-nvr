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

It also repairs, for free, what the live path now does on every write:
projecting a visit's claims into ``event_text.attributes`` so they are
findable as words and not only filterable as rows, and turning a
``plate_text`` column into the ``plate`` claim the attr filter and
``journey.py``'s identity anchor both read. Neither needs an adapter, so
neither is gated by a skill assignment — the gate exists to stop
unasked-for inference, and there is none in a projection.

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
    from services.embed_enrichment import EMBEDDABLE_LABELS

    return set(CAPTIONABLE_LABELS) | set(DESCRIBABLE_LABELS) | set(EMBEDDABLE_LABELS)


def read_state(db) -> dict[str, Any]:
    """Persisted progress. Empty dict before the first pass."""
    from services import site_settings

    state = site_settings.get_json(db, STATE_KEY, default=None)
    return state if isinstance(state, dict) else {}


def _write_state(db, state: dict[str, Any]) -> None:
    from services import site_settings

    site_settings.set_json(db, STATE_KEY, state)


def plan_batch(db, before_id: int | None, limit: int,
               people: bool = False, *,
               after_id: int | None = None,
               older_than: Any = None) -> list[dict[str, Any]]:
    """The next batch of candidate visits, newest first.

    With ``after_id`` the walk turns around: visits ABOVE that id, oldest
    first, and only those that ``started_at`` before ``older_than`` — the
    catch-up's view of what the live path dropped, see ``catch_up_once``.

    ``people`` is passed straight through to ``wants_descriptors`` — a
    scope question rather than a kill switch, so it belongs with the
    other gates here rather than being applied a phase later.

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
    from services.embed_enrichment import EMBEDDABLE_LABELS, wants_embedding
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
    if after_id is not None:
        q = q.filter(TimelineEvent.id > int(after_id))
        if older_than is not None:
            q = q.filter(TimelineEvent.started_at < older_than)
        q = q.order_by(TimelineEvent.id.asc())
    else:
        if before_id is not None:
            q = q.filter(TimelineEvent.id < int(before_id))
        q = q.order_by(TimelineEvent.id.desc())
    rows = q.limit(int(limit)).all()

    out: list[dict[str, Any]] = []
    plated: set[int] = set()
    for row, camera in rows:
        label = (row.label or "").lower()
        skills = camera_skills(camera)
        if (row.plate_text or "").strip():
            plated.add(int(row.id))
        item = {
            "event_id": int(row.id),
            "caption": (label in CAPTIONABLE_LABELS
                        and wants_caption(row.label, row.evidence_path,
                                          True, skills)),
            "descriptors": (label in DESCRIBABLE_LABELS
                            and wants_descriptors(row.label, row.evidence_path,
                                                  True, skills, people)),
            # The embedder shipped after this sweep was written, and the
            # sweep was never taught about it: a site that turned on
            # embeddings and assigned the skill got vectors for new visits
            # only, and the back catalogue — the whole reason this sweep
            # exists — stayed unrankable forever. Same gate as ingest.
            "embed": (label in EMBEDDABLE_LABELS
                      and wants_embedding(row.label, row.evidence_path,
                                          True, skills)),
            "repair": False,
        }
        out.append(item)

    # ── the free repair ────────────────────────────────────────────
    # Claims written before the projection existed are filterable but not
    # findable: search matches free text against caption || attributes,
    # and attributes was empty. Plates have the same shape — they lived
    # on the row as a column and never as the claim journey.py anchors
    # on. Both are fixed from data already in the store, so this costs no
    # inference, and it is deliberately NOT gated by a skill assignment:
    # the gate exists to stop unasked-for inference, and there is none
    # here. It is the same work the live path now does on every write.
    #
    # Two set queries per batch rather than a check per visit: at a batch
    # of fifty that is two indexed IN lookups instead of a hundred.
    ids = [int(i["event_id"]) for i in out]
    if ids:
        from models import EventText, VisitDescriptor

        claimed = {
            r[0] for r in db.query(VisitDescriptor.event_id)
            .filter(VisitDescriptor.event_id.in_(ids)).distinct().all()
        }
        worded = {
            r[0] for r in db.query(EventText.event_id)
            .filter(EventText.event_id.in_(ids),
                    EventText.attributes.isnot(None),
                    EventText.attributes != "").all()
        }
        for item in out:
            event_id = int(item["event_id"])
            item["repair"] = bool(
                event_id in plated
                or (event_id in claimed and event_id not in worded))
    return out


#: A visit the live path dropped is offered to the catch-up only once
#: it is this old: a live enrichment still in flight (up to the gate's
#: 90 s limit) must not be raced by a second one for the same visit.
CATCH_UP_MIN_AGE_S = 600.0

#: How long the loop waits before retrying a pass the gate held.
HELD_RETRY_S = 5.0

#: Cadence of the catch-up once history is done. Slow on purpose: it is
#: a safety net for drops, not a second live path.
CATCH_UP_INTERVAL_S = 60.0


async def _enrich_items(items: list[dict[str, Any]], *, caption_on: bool,
                        descriptor_on: bool, embed_on: bool,
                        pause: float) -> dict[str, Any]:
    """Hand each item to the SAME enrichers the ingest path calls.

    Stops at the first visit the gate refuses (``"dropped"``) and says
    which: nobody looked at it, so the caller must not count it or move
    past it. ``handled`` is every visit that was actually offered.
    """
    import asyncio

    from services.caption_enrichment import enrich_event_caption
    from services.descriptor_enrichment import enrich_event_descriptors
    from services.embed_enrichment import (
        enrich_event_caption_embedding,
        enrich_event_embedding,
    )
    import time as _time
    from services.enrichment_governor import pace_sleep, wait_for_backfill_slot

    out: dict[str, Any] = {"captioned": 0, "described": 0, "embedded": 0,
                           "caption_embedded": 0,
                           "handled": [], "dropped_at": None}
    for item in items:
        event_id = int(item["event_id"])
        _t_item = _time.monotonic()
        # Live always wins, and "wins" means more than yielding between
        # items: nothing from the back catalogue goes to an adapter while
        # a live visit is in flight or waiting, while a breaker is
        # cooling, while the governor says the box is busy, or outside
        # the operator's window. History has waited this long (#583).
        await wait_for_backfill_slot()
        dropped = False
        if caption_on and item.get("caption"):
            try:
                if await enrich_event_caption(event_id) == "dropped":
                    dropped = True
                else:
                    out["captioned"] += 1
            except Exception:  # noqa: BLE001
                # One unreachable adapter or one unreadable evidence file
                # must not end the sweep — the cursor still advances past
                # this visit, because retrying it forever would stall
                # every visit behind it.
                logger.warning("enrichment backfill: caption failed for %s",
                               event_id, exc_info=True)
        if descriptor_on and item.get("descriptors"):
            try:
                if await enrich_event_descriptors(event_id) == "dropped":
                    dropped = True
                else:
                    out["described"] += 1
            except Exception:  # noqa: BLE001
                logger.warning("enrichment backfill: descriptors failed for %s",
                               event_id, exc_info=True)
        if embed_on and item.get("embed"):
            try:
                # Skips itself if the visit already has a vector, so a
                # second pass costs nothing — same contract as the other two.
                if await enrich_event_embedding(event_id) == "dropped":
                    dropped = True
                else:
                    out["embedded"] += 1
            except Exception:  # noqa: BLE001
                logger.warning("enrichment backfill: embedding failed for %s",
                               event_id, exc_info=True)
        if embed_on and not dropped:
            # The caption's own vector. Offered to every visit, not only
            # the ones the `embed` skill covers (see the enricher for
            # why): it reads one row and returns when there is no
            # caption or the vector is already this producer's, so a
            # visit captioned by the live path before embeddings were
            # switched on — or before a model change — is picked up here.
            try:
                outcome = await enrich_event_caption_embedding(event_id)
                if outcome == "dropped":
                    dropped = True
                elif outcome == "done":
                    out["caption_embedded"] += 1
            except Exception:  # noqa: BLE001
                logger.warning("enrichment backfill: caption embedding failed for %s",
                               event_id, exc_info=True)
        if dropped:
            out["dropped_at"] = event_id
            logger.info("enrichment backfill: visit %s refused by the gate — "
                        "holding here, retrying next pass", event_id)
            break
        out["handled"].append(event_id)
        # Live ingest and the camera agent share these adapters (and, on
        # a Qualcomm box, one NPU). The rest after each visit is sized
        # to what the visit cost, so the sweep takes at most its share
        # of the clock (events_enrichment_backfill_share) — history is
        # swept in the gaps, never at full tilt (Ventuno Q, 2026-11-10:
        # 100 % NPU for the morning the embedder came on).
        rest = pace_sleep(_time.monotonic() - _t_item, floor_s=pause)
        if rest > 0:
            await asyncio.sleep(rest)
    return out


async def catch_up_once(batch: int = DEFAULT_BATCH,
                        pause: float = DEFAULT_PAUSE_SECONDS) -> dict[str, Any]:
    """One pass over what arrived AFTER history and was not described.

    The walk below is a one-shot sweep downward from where history ended
    when it started (``state["top"]``); a live visit the gate dropped is
    always newer than that, so without this it would never be looked at
    again and the gate's "the backfill describes it later" would be a
    lie. This offers, oldest first, every qualifying visit above ``top``
    that is older than ``CATCH_UP_MIN_AGE_S`` (a live enrichment still in
    flight must not be raced) and moves ``top`` past each one handled.
    Visits the live path DID enrich short-circuit inside the enrichers
    for the price of one row read. A drop holds ``top`` exactly as the
    walk holds its cursor.
    """
    from datetime import UTC, datetime, timedelta

    from core.config import settings
    from core.database import SessionLocal

    caption_on = bool(getattr(settings, "events_caption_enrichment", True))
    descriptor_on = bool(getattr(settings, "events_descriptor_enrichment", True))
    embed_on = bool(getattr(settings, "events_embed_enrichment", False))

    from sqlalchemy import func

    from models import TimelineEvent

    db = SessionLocal()
    try:
        state = dict(read_state(db))
        if "top" not in state:
            # A box whose walk finished before "top" existed: start from
            # where history ends NOW, not from id 0 — a catch-up that
            # re-offers 200k visits oldest-first reaches the dropped
            # ones at the top of the table in days.
            state["top"] = int(db.query(func.max(TimelineEvent.id)).scalar() or 0)
            _write_state(db, state)
            return state
        top = int(state.get("top") or 0)
        cutoff = datetime.now(UTC) - timedelta(seconds=CATCH_UP_MIN_AGE_S)
        items = plan_batch(db, None, batch,
                           bool(getattr(settings, "events_descriptor_people", False)),
                           after_id=top, older_than=cutoff)
        # Ids are assigned when a track ENDS; started_at is when it began.
        # A long visit can carry a lower id than a short one that started
        # later, so the youngest visit still inside the in-flight window
        # may sit BELOW something handled now. top must not step over it.
        youngest_held = db.query(func.min(TimelineEvent.id)).filter(
            TimelineEvent.id > top,
            TimelineEvent.started_at >= cutoff).scalar()
    finally:
        db.close()
    if not items:
        return state

    res = await _enrich_items(items, caption_on=caption_on,
                              descriptor_on=descriptor_on, embed_on=embed_on,
                              pause=pause)
    handled = res["handled"]
    new_top = max(handled) if handled else top
    if youngest_held is not None:
        # Anything handled above it is re-offered later and short-
        # circuits inside the enrichers for one row read — cheaper than
        # a visit that is never looked at again.
        new_top = min(new_top, int(youngest_held) - 1)
    new_top = max(new_top, top)

    db = SessionLocal()
    try:
        state = dict(read_state(db))
        state["top"] = int(new_top)
        state["caught_up"] = int(state.get("caught_up", 0)) + len(handled)
        state["captioned"] = int(state.get("captioned", 0)) + res["captioned"]
        state["described"] = int(state.get("described", 0)) + res["described"]
        state["embedded"] = int(state.get("embedded", 0)) + res["embedded"]
        state["caption_embedded"] = (int(state.get("caption_embedded", 0))
                                     + res["caption_embedded"])
        _write_state(db, state)
    except Exception:  # noqa: BLE001
        logger.exception("enrichment backfill: could not record catch-up progress")
        db.rollback()
    finally:
        db.close()
    if handled:
        logger.info("enrichment backfill: catch-up handled %s visit(s) the live "
                    "path missed, top now %s%s", len(handled), new_top,
                    f" (held at {res['dropped_at']})" if res["dropped_at"] else "")
    return state


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
    # Off by default, like the flag it mirrors: the embedder is the one
    # enricher most sites do not run.
    embed_on = bool(getattr(settings, "events_embed_enrichment", False))

    # ── Phase 1: read the batch, briefly ────────────────────────────
    db = SessionLocal()
    try:
        state = dict(read_state(db))
        if state.get("done"):
            return state
        if "top" not in state:
            # Where history ends and "new" begins, fixed on the first
            # pass. Everything above it is the live path's — and the
            # catch-up's, for what the live path dropped (#583).
            from sqlalchemy import func

            from models import TimelineEvent

            state["top"] = int(db.query(func.max(TimelineEvent.id)).scalar() or 0)
            _write_state(db, state)
        items = plan_batch(db, state.get("cursor"), batch,
                           bool(getattr(settings, "events_descriptor_people",
                                        False)))
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
    res = await _enrich_items(items, caption_on=caption_on,
                              descriptor_on=descriptor_on, embed_on=embed_on,
                              pause=pause)
    captioned, described, embedded = res["captioned"], res["described"], res["embedded"]
    caption_embedded = res["caption_embedded"]
    dropped_at = res["dropped_at"]

    # ── Phase 2b: the free repair ───────────────────────────────────
    # No adapter is called here, so this holds a session without the
    # objection that shaped the phases above. Committed per visit: one
    # unrepairable row must not roll back the batch's other repairs.
    repaired = 0
    to_repair = [int(i["event_id"]) for i in items if i.get("repair")]
    if to_repair:
        from models import TimelineEvent
        from services.descriptor_store import (
            project_attributes,
            sync_plate_claim,
        )

        db = SessionLocal()
        try:
            for event_id in to_repair:
                try:
                    row = db.get(TimelineEvent, event_id)
                    if row is None:
                        continue      # aged out by retention meanwhile
                    sync_plate_claim(db, row)
                    project_attributes(db, row)
                    db.commit()
                    repaired += 1
                except Exception:  # noqa: BLE001
                    logger.warning("enrichment backfill: repair failed for %s",
                                   event_id, exc_info=True)
                    db.rollback()
        finally:
            db.close()

    # ── Phase 3: reopen and advance ─────────────────────────────────
    if dropped_at is not None:
        # The gate refused this visit (live traffic arrived between the
        # idle check and the call). Nobody looked, so the cursor holds
        # just above it: the next pass re-offers it and everything below.
        # Everything above it in this batch WAS handled and is not
        # re-offered — plan_batch walks down from the cursor.
        lowest = int(dropped_at) + 1
    else:
        lowest = min(int(i["event_id"]) for i in items)
    db = SessionLocal()
    try:
        state = dict(read_state(db))
        state["cursor"] = lowest
        if dropped_at is not None:
            state["held_at"] = int(dropped_at)
        else:
            state.pop("held_at", None)
        state["examined"] = int(state.get("examined", 0)) + len(res["handled"])
        state["captioned"] = int(state.get("captioned", 0)) + captioned
        state["described"] = int(state.get("described", 0)) + described
        state["embedded"] = int(state.get("embedded", 0)) + embedded
        state["caption_embedded"] = (int(state.get("caption_embedded", 0))
                                     + caption_embedded)
        state["repaired"] = int(state.get("repaired", 0)) + repaired
        _write_state(db, state)
    except Exception:  # noqa: BLE001
        logger.exception("enrichment backfill: could not record progress")
        db.rollback()
    finally:
        db.close()

    logger.info(
        "enrichment backfill: %s examined, %s captioned, %s described, "
        "%s embedded, %s captions embedded, %s repaired, cursor now %s",
        len(items), captioned, described, embedded, caption_embedded, repaired,
        lowest,
    )
    return state


async def reembed_due() -> tuple[bool, str | None]:
    """Does the store hold vectors from a producer other than the one
    that would embed today? ``(True, producer)`` when the embedding
    adapter's model changed under the store — the walk must run again
    and replace them (``enrich_event_embedding`` replaces a vector from
    another producer). ``(False, None)`` with no adapter, nothing stale,
    or embedding off."""
    from core.config import settings
    if not bool(getattr(settings, "events_embed_enrichment", False)):
        return False, None
    from services.embed_enrichment import _resolve_embed_producer
    try:
        found = await _resolve_embed_producer()
    except Exception:  # noqa: BLE001
        return False, None
    if found is None:
        return False, None
    from core.database import SessionLocal
    from services import embedding_store
    db = SessionLocal()
    try:
        stale = embedding_store.stale_rows(db, found[1])
    finally:
        db.close()
    return (stale > 0), found[1]


def reset_for_reembed(db, producer: str) -> dict[str, Any]:
    """Start the walk over so every visit is offered to the new producer.
    The old state is replaced, not amended: ``done`` and the cursor are
    the walk's, and this is a new walk."""
    state = {"reembed_for": producer}
    _write_state(db, state)
    return state


async def run_backfill_loop(batch: int = DEFAULT_BATCH,
                            pause: float = DEFAULT_PAUSE_SECONDS,
                            interval: float = DEFAULT_INTERVAL_SECONDS,
                            *, catch_up: bool = True,
                            catch_up_interval: float = CATCH_UP_INTERVAL_S) -> None:
    """Sweep history until it is done, then stay as the catch-up.

    The walk is finite: once the cursor reaches the oldest visit there
    is no more history. What remains is the safety net for the live
    path — every visit the gate dropped (#583) is newer than where the
    walk began, and ``catch_up_once`` is what describes it later. With
    ``catch_up=False`` the loop returns when history is done, as it did
    before the gate existed.
    """
    from core.config import settings

    if not getattr(settings, "events_enrichment_backfill", False):
        return
    # No early return when both enrichers are off. The sweep also
    # repairs projections and plate claims from data already stored,
    # which spends no inference and does not depend on either setting —
    # plate reads are a third one (events_plate_enrichment), so a box
    # with captions and descriptors off can still have real work here.
    logger.info("enrichment backfill: starting (batch=%s, pause=%ss)",
                batch, pause)
    from services.enrichment_governor import wait_for_backfill_slot

    previous: Any = object()
    while True:
        # A pass begins only once the governor has seen the box NORMAL
        # for the idle period (and inside the operator's window, if
        # any): history is swept when the box finds the resources, never
        # while it is busy with today (#583).
        await wait_for_backfill_slot(starting=True)
        state = await backfill_once(batch=batch, pause=pause)
        if state.get("done"):
            break
        cursor = state.get("cursor")
        held = state.get("held_at") is not None
        if cursor == previous and not held:
            # The cursor is the only thing guaranteeing forward progress.
            # If a pass ends where the last one did, something is wrong
            # with the walk, and a loop that spins on the same batch
            # forever is worse than one that stops and says so: it would
            # re-enrich the same visits for the life of the process.
            # A HELD cursor is the one exception: the gate refused a
            # visit, the pass stopped there on purpose, and retrying is
            # the point.
            logger.error("enrichment backfill: cursor stuck at %s; stopping",
                         cursor)
            return
        previous = cursor
        await asyncio.sleep(max(interval, HELD_RETRY_S) if held else interval)
    if not catch_up:
        return
    logger.info("enrichment backfill: history complete — staying as the "
                "catch-up for visits the live path drops (every %.0fs)",
                catch_up_interval)
    while True:
        await asyncio.sleep(catch_up_interval)
        # Same bar as a pass of the walk: NORMAL for the idle period,
        # nothing live in hand, inside the window.
        await wait_for_backfill_slot(starting=True)
        # The embedding model changed under the store (CLIP → SigLIP 2):
        # the old vectors are a non-match for every new query. Start
        # the walk over; it replaces them, newest first.
        try:
            due, producer = await reembed_due()
        except Exception:  # noqa: BLE001
            due, producer = False, None
        if due and producer:
            from core.database import SessionLocal
            db = SessionLocal()
            try:
                reset_for_reembed(db, producer)
            finally:
                db.close()
            logger.info("enrichment backfill: embeddings from another producer "
                        "found; walking history again for %s", producer)
            return await run_backfill_loop(batch=batch, pause=pause, interval=interval,
                                           catch_up=catch_up, catch_up_interval=catch_up_interval)
        try:
            await catch_up_once(batch=batch, pause=pause)
        except Exception:  # noqa: BLE001
            logger.warning("enrichment backfill: catch-up pass failed",
                           exc_info=True)
