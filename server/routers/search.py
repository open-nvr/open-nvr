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

"""Footage search — "find me…" over the canonical event store.

``GET /search?q=red truck at the dock yesterday`` parses the sentence,
searches the visits, and answers with three things: what it understood,
the rows, and where in the footage each row lives.

**The interpretation is part of the answer, not debug output.** Natural
language search fails by parsing a query wrongly and then answering
confidently with nothing; showing what was understood — and letting the
caller override any part of it with an explicit parameter — is what
makes that recoverable. Editing a chip in the UI is exactly passing the
parameter.

This replaces the footage-search app's private SQLite index, which
answered the same questions from a second copy of the same data, with no
camera scoping, no evidence, and no way to open the clip.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.database import get_db
from core.permissions import user_has_permission
from models import AppAlert, Camera, CameraZone, EventText, TimelineEvent, User, VisitDescriptor

# The internal door is defined once, next to the pipeline's write routes;
# the metrics scrape is the same door and should not grow a second lock.
from routers.internal_camera_agent import _platform_only, _require_internal_key
from services import search_metrics as metrics
from services.camera_scope import scope_query, visible_camera_ids
from services.search_query import ParsedQuery, parse_query
from services.search_service import (
    anchor_for,
    count_search_events,
    search_page,
    summarise_hits,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["search"])

#: A page of results is a screen of thumbnails, not a report.
DEFAULT_LIMIT = 25
MAX_LIMIT = 200


def _visible_cameras(db: Session, scope: set[int] | None) -> dict[int, str]:
    """{id: name} for the cameras this caller may see — the vocabulary
    "at the dock" is resolved against, and the scope results obey.

    Takes the scope rather than recomputing it: it is a query of its own,
    and a search that asked twice could, in principle, answer from two
    different answers."""
    q = db.query(Camera.id, Camera.name)
    if scope is not None:
        if not scope:
            return {}
        q = q.filter(Camera.id.in_(scope))
    return {row[0]: row[1] or f"cam{row[0]}" for row in q.all()}


def _record_search(shape: str, parsed: ParsedQuery, page, count, total: int, *,
                   parse: bool, overridden: bool) -> None:
    """One search, as numbers.

    Kept out of the handler because none of it may change the answer: a
    metrics failure must never turn a working search into a 500, so
    everything here is best-effort and swallows.
    """
    try:
        metrics.SEARCH_SECONDS.observe(page.seconds, {"shape": shape})
        metrics.COUNT_SECONDS.observe(count.seconds, {"shape": shape})
        metrics.RESULT_COUNT.observe(total, {"shape": shape})
        metrics.QUERIES.inc({"shape": shape, "outcome": "hit" if total else "empty"})

        # Parse coverage. ``matched`` is what the sentence gave us,
        # ``ignored`` what was thrown away — the ratio is the only
        # accuracy signal available with nobody labelling anything.
        if parsed.matched or parsed.ignored:
            used = sum(len(str(v).split()) for v in parsed.matched.values())
            metrics.QUERY_WORDS.inc({"state": "matched"}, used)
            metrics.QUERY_WORDS.inc({"state": "ignored"}, len(parsed.ignored))
            metrics.IGNORED_WORDS.observe(len(parsed.ignored))

        if not parse:
            metrics.REFINEMENTS.inc({"kind": "explicit"})
        elif overridden:
            metrics.REFINEMENTS.inc({"kind": "corrected"})
    except Exception:  # pragma: no cover - instrumentation is never load-bearing
        from core.logging_config import main_logger

        main_logger.debug("search metrics not recorded", exc_info=True)


@router.get("/search/metrics")
async def search_metrics(
    db: Session = Depends(get_db),
    principal=Depends(_require_internal_key),
):
    """Prometheus exposition for search (site key, like every other
    internal door).

    Not under the user API: these are operational numbers about the
    deployment, not about the caller's cameras, and a scraper is a
    platform component rather than a person. The coverage gauges are
    sampled on a timer inside, so scraping this often is cheap.
    """
    _platform_only(principal)
    metrics.COVERAGE.maybe_refresh(db)
    return Response(content=metrics.render(), media_type="text/plain; version=0.0.4")


@router.post("/search/opened")
async def search_opened(
    rank: int = Query(..., ge=1, le=10_000, description="1-based position in the result list."),
    current_user: User = Depends(get_current_active_user),
):
    """The operator opened this result.

    The only relevance judgement a system with no labelled footage can
    collect: not whether the answer was right, but where in the list the
    thing they wanted actually was. Consistently rank 1 to 3 means ranking
    works; rank 11 means it does not; nothing opened at all means neither
    does the search.

    Deliberately anonymous — a position and nothing else. Who searched
    for what is not an operational metric, and a search log that names
    people and plates is a liability rather than an asset.
    """
    metrics.OPENED.inc()
    metrics.OPEN_RANK.observe(rank)
    return {"ok": True}


@router.get("/search/journey")
async def journey(
    event_id: int = Query(..., description="The visit to follow."),
    window_minutes: float = Query(30.0, ge=1, le=240),
    max_hops: int = Query(6, ge=1, le=12),
    min_score: float = Query(0.35, ge=0.0, le=1.0),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Where this object went — the route across cameras.

    Answers the question the event store cannot answer on its own, since
    ``track_id`` belongs to one camera. Two ways, and the response always
    says which was used:

    * ``identity`` — the same plate or the same recognised face on the
      next camera. Both come from KAI-C adapters, both are exact, and no
      inference is involved.
    * ``evidence`` — for everything with no exact identity (a person in
      a crowd, an unplated van, a trolley): the learned camera graph says
      where it could have gone and in what time, and the descriptors
      those same KAI-C skills wrote say which candidate fits. The more
      skills a deployment runs, the sharper this gets.

    Every hop carries its reasons, including the ones against it, because
    a route is used to say where somebody was and one that cannot be
    audited is not evidence.
    """
    from services.journey import find_journey

    with metrics.Timer() as timer:
        result = find_journey(
            db,
            event_id=event_id,
            scope=visible_camera_ids(db, current_user),
            window_minutes=window_minutes,
            max_hops=max_hops,
            min_score=min_score,
        )
    if result is None:
        raise HTTPException(status_code=404, detail="unknown or unreachable event")
    # The method mix is the point: a deployment answering mostly
    # "time-only" is guessing, and that should be visible on a dashboard
    # rather than only in the caveat on each answer.
    metrics.JOURNEYS.inc({"method": result.method})
    metrics.JOURNEY_HOPS.observe(len(result.hops))
    metrics.JOURNEY_SECONDS.observe(timer.seconds, {"method": result.method})
    body = result.as_dict()
    cameras = _visible_cameras(db, visible_camera_ids(db, current_user))
    body["anchor"]["camera_name"] = cameras.get(result.anchor.camera_id)
    for hop in body["hops"]:
        hop["camera_name"] = cameras.get(hop["camera_id"])
    return body


@router.get("/search/enrichment-plan")
async def enrichment_plan(
    label: str | None = Query(None, description="Narrow to what is worth running on this class."),
    current_user: User = Depends(get_current_active_user),
):
    """The skills that could describe a visit on this box, right now.

    Registered in KAI-C AND healthy — asked rather than assumed, because
    it differs per deployment and changes while running. An enricher uses
    it to decide what to run; the UI uses it to say what searching by
    colour or by face would even mean here, instead of offering filters
    that can never match.
    """
    from services.enrichment_plan import compute_enrichment_plan

    return await compute_enrichment_plan(label)


#: The claim kind whose value is a PERSON. Named here, next to the
#: endpoint that lists them, because the reason the list has to exist is
#: the reason the name is not a search word: see
#: ``descriptor_store._UNPROJECTED_KINDS``.
PERSON_KIND = "face_id"


@router.get("/search/people")
def search_people(
    limit: int = Query(200, ge=1, le=1000),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """The people visits on this box have actually been attributed to.

    "Was Varun here yesterday" is ``attr=face_id:varun``, and that filter
    has worked since the claim store landed. What never existed is any way
    for an operator to ASK it. A name is deliberately kept out of the
    projected free text — tokenising it would make every query containing
    that word match the person, and would widen who can discover the
    identity from the app that produced it to anyone with search access —
    so typing a name into the box matches nothing, silently, forever. A
    filter that can only be reached by knowing the exact value is a filter
    that does not exist; this is the list that makes it a picker.

    The names come from the CLAIMS, not from the recogniser's enrolment
    roster, for two independent reasons. The roster lives inside the app
    that owns the model and core has no route to it. And a name that is
    enrolled but was never seen would offer a filter that can only ever
    return nothing — the exact failure this endpoint exists to remove.
    What is offered here is precisely what is findable.

    Scoped to the caller's cameras, like every other read on this router.
    It discloses nothing a caller could not already learn by passing the
    attr filter with a guessed name; it only removes the guessing.
    """
    scope = visible_camera_ids(db, current_user)
    q = (
        db.query(
            VisitDescriptor.value.label("value"),
            # DISTINCT on the event, because one visit can carry the same
            # name from two tasks and that is one sighting, not two.
            func.count(func.distinct(VisitDescriptor.event_id)).label("visits"),
            func.max(TimelineEvent.started_at).label("last_seen"),
        )
        .join(TimelineEvent, TimelineEvent.id == VisitDescriptor.event_id)
        .filter(VisitDescriptor.kind == PERSON_KIND)
    )
    rows = (
        scope_query(q, TimelineEvent.camera_id, scope)
        .group_by(VisitDescriptor.value)
        # Most recently seen first: a picker is read top-down and the
        # person somebody is asking about is usually the recent one.
        .order_by(func.max(TimelineEvent.started_at).desc())
        .limit(limit)
        .all()
    )
    return {
        "kind": PERSON_KIND,
        "people": [
            {
                "value": row.value,
                "attr": f"{PERSON_KIND}:{row.value}",
                "visits": int(row.visits or 0),
                "last_seen": row.last_seen.isoformat() if row.last_seen else None,
            }
            for row in rows
        ],
    }


def _search_vocabulary(db: Session | None = None) -> set[str]:
    """The description words this box can act on — colours, vehicle
    types, carried things, headwear, clothing cues — plus the words its
    own captions use, so a typo of one of them is read as the word
    (search_query.correct_word) instead of falling through to the
    caption text, where it matches nothing. "camoflage" and "pikup" got
    nothing on the Ventuno Q (2026-11-10) because neither word is a
    filter; both are all over its captions."""
    from services import search_intent as si

    words = set(si.COLOUR_WORDS) | set(si.VEHICLE_TYPE_WORDS) | set(si.CARRYING_WORDS) \
        | set(si.GENDER_WORDS) | set(si.HEADWEAR_WORDS) | set(si.CLOTHING_CUES)
    if db is not None:
        words |= _caption_vocabulary(db)
    return words


#: The captions' own words, read at most every ten minutes from the
#: newest few thousand captions: a few milliseconds of Python over text
#: the database hands back in one round trip. Words of four letters and
#: more that occur at least twice — one-offs are as likely to be the
#: captioner's own typos as vocabulary.
CAPTION_VOCAB_TTL_S = 600.0
CAPTION_VOCAB_CAPTIONS = 4000
CAPTION_VOCAB_MIN_COUNT = 2
_caption_vocab_cache: tuple[float, frozenset[str]] | None = None


def _caption_vocabulary(db: Session, now: float | None = None) -> frozenset[str]:
    global _caption_vocab_cache
    import re as _re
    import time as _time
    from collections import Counter

    now = _time.monotonic() if now is None else now
    if _caption_vocab_cache is not None and now - _caption_vocab_cache[0] < CAPTION_VOCAB_TTL_S:
        return _caption_vocab_cache[1]
    try:
        rows = (db.query(EventText.caption).filter(EventText.caption.isnot(None))
                .order_by(EventText.event_id.desc()).limit(CAPTION_VOCAB_CAPTIONS).all())
        counts: Counter[str] = Counter()
        for (cap,) in rows:
            counts.update(w for w in _re.split(r"[^a-z]+", str(cap).lower()) if len(w) >= 4)
        vocab = frozenset(w for w, n in counts.items() if n >= CAPTION_VOCAB_MIN_COUNT)
    except Exception:  # noqa: BLE001 — a vocabulary must never break a search
        logger.debug("search: caption vocabulary unavailable", exc_info=True)
        vocab = _caption_vocab_cache[1] if _caption_vocab_cache is not None else frozenset()
    _caption_vocab_cache = (now, vocab)
    return vocab


def _known_people(db: Session, scope: set[int] | None) -> list[str]:
    """The ``face_id`` values visits in scope carry — the vocabulary a
    typed name is matched against (the same list ``/search/people``
    offers as a picker). Claims, not the recogniser's roster: a name
    enrolled but never seen would resolve to a filter that can only
    return nothing."""
    try:
        q = (
            db.query(VisitDescriptor.value)
            .join(TimelineEvent, TimelineEvent.id == VisitDescriptor.event_id)
            .filter(VisitDescriptor.kind == PERSON_KIND)
        )
        rows = scope_query(q, TimelineEvent.camera_id, scope).distinct().limit(1000).all()
        return [str(r[0]) for r in rows if r[0]]
    except Exception:  # noqa: BLE001 — a name lookup must never break a search
        logger.debug("search: people lookup failed", exc_info=True)
        return []


async def _needs_for(db: Session, scope: set[int] | None, *, parsed: ParsedQuery,
                     labels, words: str, attrs, wants_plate: bool, plate: str):
    """Resolve the question's needs against this box, and return the
    box's abilities alongside (the requested lane reads them) — see
    ``services.search_intent``. Best-effort: an unreachable registry
    means every need reads as not-on-this-box, which is the honest
    answer at that moment, and never an error."""
    from core.config import settings
    from services.enrichment_plan import compute_enrichment_plan
    from services.search_intent import BoxAbilities, resolve_needs
    box = BoxAbilities(people_descriptors=bool(getattr(settings, "events_descriptor_people", False)))
    try:
        plan = await compute_enrichment_plan(None)
        for s in plan.get("skills") or []:
            if not s.get("healthy"):
                continue
            box.offered_kinds.update(s.get("descriptor_kinds") or [])
            if s.get("task") == "image_captioning":
                box.captions = True
    except Exception:  # noqa: BLE001
        logger.debug("search: enrichment plan unavailable", exc_info=True)
    try:
        q = (
            db.query(VisitDescriptor.kind)
            .join(TimelineEvent, TimelineEvent.id == VisitDescriptor.event_id)
        )
        rows = scope_query(q, TimelineEvent.camera_id, scope).distinct().all()
        box.claimed_kinds = {str(r[0]) for r in rows if r[0]}
    except Exception:  # noqa: BLE001
        logger.debug("search: claimed kinds lookup failed", exc_info=True)
    try:
        # Which apps bring which skill — so "never produced" can name the
        # app to pick the camera in, rather than a skill to assign.
        from models import InstalledApp
        from services.skill_assignments import app_tasks
        for row in db.query(InstalledApp).all():
            for task in app_tasks(row.manifest_json):
                box.apps_by_skill.setdefault(task, []).append(str(row.name or row.id))
    except Exception:  # noqa: BLE001
        logger.debug("search: app skills lookup failed", exc_info=True)
    return resolve_needs(
        words=(words or "").split(), labels=labels, attrs=attrs,
        wants_plate=wants_plate, plate=plate, box=box,
    ), box


@router.get("/search")
async def search(
    q: str = Query("", description="Natural-language query, e.g. 'red truck at the dock yesterday'."),
    # Explicit overrides. Any of these WINS over what the sentence was
    # taken to mean — which is how a corrected chip reaches the query.
    label: list[str] | None = Query(None, description="Object class; repeatable (OR)."),
    camera_id: list[int] | None = Query(None, description="Camera; repeatable (OR)."),
    text: str | None = Query(None, description="Words to match in captions/attributes."),
    min_people: int | None = Query(None, ge=1, le=99,
                                   description="At least this many people in frame with the visit."),
    plate: str | None = Query(None),
    zone: str | None = Query(None, max_length=60,
                             description="Zone id or name (among the caller's cameras)."),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    source: str | None = Query(None, description="tier0 | camera | app | adapter"),
    attr: list[str] | None = Query(
        None,
        description="A claim a skill made, as kind:value (colour:red, "
                    "vehicle_type:van, face_id:ravi). Repeatable, ANDed — "
                    "the same visit must carry all of them.",
    ),
    parse: bool = Query(
        True,
        description="Parse q into filters. false = the explicit parameters "
                    "are the whole query (how a UI driving from edited chips "
                    "clears a filter the sentence implied).",
    ),
    correct: bool = Query(
        True,
        description="Read a word one letter off the box's vocabulary as that "
                    "word (said in interpretation.corrections); false searches "
                    "the letters as typed.",
    ),
    describe: bool = Query(
        True,
        description="When the question needs a caption or a claim the matching "
                    "visits do not have yet, queue them for description (up to "
                    "EVENTS_ENRICHMENT_REQUEST_CAP, newest first) and report it "
                    "under `pending`. false = answer from what exists and queue "
                    "nothing.",
    ),
    skip: int = Query(0, ge=0, le=100_000),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    rerank: int | None = Query(
        None, ge=0, le=20,
        description="Re-rank the first N results with the box's own language model "
                    "(matching captions first); unset = SEARCH_RERANK_TOP; 0 = off."),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Search visits. Returns the interpretation, the page, and the total.

    And, when the question leans on a description the matching visits
    do not have yet, ``pending``: how many are missing it, how many this
    call queued to be described now, and roughly how long — so an empty
    page can say "not yet" instead of "no", and the caller can ask again.

    Scoping is the store's own: a caller sees results only from cameras
    they can view, and the camera vocabulary the parser matches names
    against is the same set — so "the dock" cannot even name a camera
    somebody else owns.
    """
    try:
        from services.enrichment_governor import note_interactive
        note_interactive("search")                 # the backfill stands aside
    except Exception:  # noqa: BLE001 - never fail a search over bookkeeping
        pass
    scope = visible_camera_ids(db, current_user)
    cameras = _visible_cameras(db, scope)
    parsed = (parse_query(q, cameras=cameras, people=_known_people(db, scope),
                          vocab=_search_vocabulary(db), correct=correct)
              if parse else ParsedQuery())

    # Explicit parameters beat the parse, field by field, so a caller can
    # correct one part without having to restate the rest. A UI driving
    # entirely from edited chips passes parse=false and owns every filter
    # — which is also the only way to CLEAR something the sentence
    # implied, since an absent parameter means "no opinion", not "none".
    labels = [s for s in (label or []) if s] or parsed.labels
    cams = [c for c in (camera_id or []) if c] or parsed.camera_ids
    words = text if text is not None else parsed.text
    plate_q = plate if plate is not None else parsed.plate
    start = from_ if from_ is not None else parsed.from_
    end = to if to is not None else parsed.to

    # kind:value pairs, skipping anything malformed rather than 422ing a
    # whole search over one bad chip.
    attrs: list[tuple[str, str]] = []
    for raw in attr or []:
        kind, sep, value = str(raw).partition(":")
        if sep and kind.strip() and value.strip():
            attrs.append((kind.strip().lower(), value.strip().lower()))

    zone_id = _resolve_zone(db, zone, cams[0] if len(cams) == 1 else None, scope)
    if not attr:
        attrs = list(parsed.attrs)
    # "What is the plate of…" — only visits that carry a read can answer.
    wants_plate = bool(parsed.wants_plate) and plate is None
    # "Three people together": from the pipeline's count of people in
    # frame with each visit. An explicit ``min_people`` beats the parse.
    people_min = min_people if min_people is not None else parsed.min_people
    filters = dict(
        from_=start, to=end, plate=plate_q or None, source=source, scope=scope,
        zone_id=zone_id, has_plate=wants_plate, min_people=people_min or None,
    )
    needs, box = await _needs_for(db, scope, parsed=parsed, labels=labels, words=words,
                                  attrs=attrs, wants_plate=wants_plate, plate=plate_q or "")
    # A typed "red van" or "blue shirt" filters on the claim where this box
    # holds claims of that kind (exact), and stays a caption word where it
    # does not. Only for a parsed question: explicit text= is never touched.
    if parse and text is None and not attr:
        from services.search_intent import promote_words_to_claims
        if promote_words_to_claims(parsed, box):
            attrs = list(parsed.attrs)
            words = parsed.text
    # What the question needs that the matching visits may not have, and
    # the request to produce it (services/enrichment_requests). Words
    # need a caption; a chip needs a claim of its kind; a need the box
    # cannot meet at all (state missing) is not requested — the `needs`
    # list already says why.
    # First page only: paging through the same question, and the UI's
    # per-keystroke refetch, must not re-scan the window and re-queue on
    # every call; page 1 is where the question is asked.
    pending: dict | None = None
    if describe and (words or attrs) and skip == 0:
        from services.enrichment_requests import kinds_for_attrs, pending_for

        # Every kind the question leans on; pending_for keeps only those
        # a healthy skill on this box offers, so a need the box cannot
        # meet queues nothing (the `needs` list already says why).
        want_kinds = {k for k in (kinds_for_attrs(attrs) | {n.kind for n in needs})
                      if k != "face_id"}
        try:
            pending = await pending_for(
                db, filters=filters, labels=labels, camera_ids=cams,
                want_caption=bool(words), want_kinds=want_kinds,
                # The plan was already read for `needs`; do not read it twice.
                offers=(bool(box.captions), set(box.offered_kinds) - {"face_id"}))
        except Exception:  # noqa: BLE001 — a hint must never 500 a search
            logger.debug("search: pending-enrichment lookup failed", exc_info=True)
            pending = None
    shape = metrics.query_shape(
        labels=labels, camera_ids=cams, text=words or "", attrs=attrs,
        plate=plate_q or "", from_=start, to=end,
    )
    # The query's own vector, when this deployment has an adapter that
    # can make one and anything to compare it against. Every way that
    # can fail is an ABSENCE, not an error — no adapter registered, no
    # visit embedded yet, an adapter that only does images, KAI-C
    # unreachable — and every one of them lands here as None, which is
    # the word search this route has always been.
    # The vector arms hear the whole corrected question (class words,
    # colours and counts included), not the residue the word arm gets:
    # "pink car" leaves no residue and used to run no semantic arm at
    # all. Explicit text= is embedded as given, like everything else.
    embed_words = (parsed.question or words or "") if text is None else (words or "")
    query_vector, caption_query_vector, no_vector_reason = await _query_vectors(db, embed_words)
    from core.config import settings
    floor = float(getattr(settings, "search_vector_min_similarity", 0) or 0) or None
    caption_floor = float(
        getattr(settings, "search_caption_min_similarity", 0) or 0) or None
    arm_weights = _arm_weights(settings)
    # Class words the PARSER produced are a hint to the word arm and no
    # wall for the vector arms (search_page); a label= chip stays a wall.
    labels_soft = not label and bool(parsed.labels)

    # Counted FIRST, and timed on its own, because that timer is the
    # whole point of opennvr_search_count_seconds — the exact total is
    # the one cost this API added over the old app store. The result is
    # handed to search_page so the count is not paid for twice.
    with metrics.Timer() as count_timer:
        total = count_search_events(
            db, labels=labels, camera_ids=cams, text=words or "", attrs=attrs, **filters
        )
    with metrics.Timer() as page_timer:
        page = search_page(
            db, labels=labels, camera_ids=cams, text=words or "", attrs=attrs,
            query_vector=query_vector, caption_query_vector=caption_query_vector,
            total=total, limit=limit, skip=skip,
            min_similarity=floor, caption_min_similarity=caption_floor,
            arm_weights=arm_weights, labels_soft=labels_soft, **filters,
        )
    hits = page.hits
    # `text_total` is what the metric has always recorded — how many rows
    # the words matched — so the history behind that series keeps its
    # meaning. `total` below is what the CALLER can page through, which
    # is the fused pool when two arms ran. They differ only with
    # embeddings on, and `semantic.text_total` carries the other one.
    text_total = total
    total = page.total

    # ── A LEFTOVER WORD MUST NOT EMPTY THE PAGE ────────────────────
    #
    # The parser hands whatever it could not interpret to the text
    # filter, and the text filter is an AND: every word has to appear in
    # a caption. That is right for a word the operator meant and wrong
    # for the sentence they actually type. "can you tell me if you seen
    # any car in last 15 mins what is number of it" parses `car` and the
    # window correctly and leaves `number` — a question word no caption
    # will ever contain — which turned 421 matching cars into nothing.
    # `did a red truck come by earlier today`, the example question this
    # project ships, leaves `red come earlier` and fails the same way.
    #
    # The answer is not a longer stopword list. That list is already
    # long and careful and was one word short, and the next sentence
    # brings a word it has not met either.
    #
    # So when the words empty the page, drop them, return what the rest
    # of the parse found, and SAY SO. The count is the one _why_empty
    # was already computing to offer "Without the words (number): 421
    # results" one click away — the system knew the answer and showed a
    # blank page next to it.
    #
    # Three conditions, each load-bearing:
    #   * `not hits` — never changes a search that found anything.
    #   * `parse` and not an explicit `text=` — a caller who PASSED words
    #     meant them, and gets the empty result they asked for. Only the
    #     parser's own residue is droppable.
    #   * a non-zero count without them — relaxing into another empty
    #     page would be noise, and the structural chips stay untouched
    #     either way: dropping a label or a time window would answer a
    #     different question, which is the failure this is fixing.
    #   * something else to stand on. "zebra 42" parses to no label, no
    #     window and no camera, so dropping `zebra` does not relax the
    #     search — it removes it, and answers "did you see a zebra" with
    #     the entire database. An existing test caught this; the empty
    #     result is the honest one when the words were the whole query.
    relaxed: dict | None = None
    _structural = any((labels, cams, attrs, filters.get("from_"),
                       filters.get("to"), filters.get("plate")))
    if not hits and words and parse and not text and _structural:
        try:
            without = count_search_events(
                db, labels=labels, camera_ids=cams, text="", attrs=attrs,
                **filters)
        except Exception:  # noqa: BLE001 — a fallback must never 500 a search
            logger.debug("relax-on-empty count failed", exc_info=True)
            without = 0
        if without > 0:
            relaxed = {"dropped": words, "matched": 0, "without": without}
            page = search_page(
                db, labels=labels, camera_ids=cams, text="", attrs=attrs,
                query_vector=query_vector, caption_query_vector=caption_query_vector,
                total=without, limit=limit,
                skip=skip, min_similarity=floor,
                caption_min_similarity=caption_floor,
                arm_weights=arm_weights, labels_soft=labels_soft, **filters)
            hits = page.hits
            total = page.total
            # The interpretation reports what was APPLIED, so the words
            # move to `ignored` — the field whose whole purpose is
            # saying what the parser set aside "instead of pretending".
            parsed.ignored = list(parsed.ignored) + words.split()
            words = ""

    _record_search(shape, parsed, page_timer, count_timer, text_total, parse=parse,
                   overridden=bool(label or camera_id or text or plate or from_
                                   or to or source or attr))

    # ── A bounded yes/no re-rank of the first page by the box's own LLM ──
    # Fusion cannot read; the captioner's model can read ten captions and
    # say which show what was asked. Those move to the front in their
    # fused order; the rest keep theirs. Off unless asked for (per request
    # or SEARCH_RERANK_TOP), and only for the first page of a worded
    # question whose vector arms ran — nothing to judge otherwise.
    top = rerank if rerank is not None else int(getattr(settings, "search_rerank_top", 0) or 0)
    if top > 0 and skip == 0 and hits and (words or parsed.question) and page.semantic and page.semantic.get("used"):
        from services.search_rerank import apply as _apply_rerank, rerank as _rerank
        head = hits[:top]
        yes, info = await _rerank(
            parsed.question or words or "", [h.caption or "" for h in head],
            timeout_s=float(getattr(settings, "search_rerank_timeout_s", 8.0) or 8.0))
        if yes is not None:
            hits = _apply_rerank(head, yes) + hits[top:]
        page.semantic["rerank"] = info

    interpretation = parsed.as_dict()
    interpretation.update({
        # Whether these filters came from the sentence or from the caller.
        "source": "parsed" if parse else "explicit",
        "labels": labels,
        "camera_ids": cams,
        "text": words or "",
        "plate": plate_q or "",
        "attrs": [f"{k}:{v}" for k, v in attrs],
        "wants_plate": wants_plate,
        "min_people": people_min or None,
        # What the question asked of the box's skills, and whether each
        # is here — the difference between "nothing was red" and "nothing
        # here has ever looked at a colour".
        "needs": [n.as_dict() for n in needs],
        "zone_id": zone_id,
        "from": start.isoformat() if start else None,
        "to": end.isoformat() if end else None,
        # Which parts the CALLER pinned, so the UI can render those chips
        # as edited rather than as the parser's guess.
        "overridden": sorted(
            k for k, v in (
                ("labels", label), ("camera_ids", camera_id), ("text", text),
                ("plate", plate), ("from", from_), ("to", to), ("source", source),
                ("attrs", attr), ("zone_id", zone),
            ) if v
        ),
    })

    # App alerts are not visits — occupancy over a limit, loitering, a
    # package left — but a question like "loitering at the gate last night"
    # has its answer in the alerts table. Matched on the words against the
    # alert's type, title and description, in the same window and cameras,
    # newest first, and shown beside the visits rather than mixed in.
    alerts_out = _matching_alerts(db, scope, current_user, words=words or "",
                                  labels=labels, camera_ids=cams, start=start, end=end,
                                  cameras=cameras)

    # No time in the question: the whole index was searched. Say how far
    # back that reaches and how much it covered, so the operator can
    # narrow with one click instead of scrolling a year.
    scope_note = None
    if start is None and end is None:
        scope_note = _all_time_scope(db, cams or sorted(scope) if scope else cams, total)

    return {
        "query": q,
        "interpretation": interpretation,
        "alerts": alerts_out,

        "scope": scope_note,
        # What is not described yet and what this call queued (None when
        # the question needed nothing a description would add).
        "pending": pending,
        "results": [
            {
                "id": h.event.id,
                "camera_id": h.event.camera_id,
                "camera_name": cameras.get(h.event.camera_id),
                "label": h.event.label,
                "score": round(h.score, 4),
                "started_at": h.event.started_at.isoformat() if h.event.started_at else None,
                "ended_at": h.event.ended_at.isoformat() if h.event.ended_at else None,
                "plate_text": h.event.plate_text,
                "caption": h.caption,
                "attributes": h.attributes,
                # What the skills claimed, each with the skill that said
                # it — a result that can be justified, not just returned.
                "claims": h.claims,
                "source": h.event.source,
                "event_type": h.event.event_type,
                # The evidence photo is served by the timeline router,
                # which owns the on-disk evidence store and its auth.
                "evidence_url": (
                    f"/api/v1/events/{h.event.id}/evidence" if h.event.evidence_path else None
                ),
                # Where a player should open. The route is the UI's to
                # build; the API says which camera and which instant.
                "anchor": anchor_for(h.event),
            }
            for h in hits
        ],
        "count": len(hits),
        "total": total,
        # Counted facts about the results above — what matched, where,
        # when, what the skills claimed, and how many were never
        # described at all. Data, not a sentence: the UI composes the
        # wording so it can be translated and re-worded without a
        # release. Absent when nothing matched.
        "answer": summarise_hits(hits, total=total, camera_names=cameras),
        # Empty result: which ONE chip is responsible, and what dropping
        # it would find. Absent when there were results, and absent when
        # no single chip explains it.
        # Gated on HITS, not on the count. The vector arm returns rows
        # the text predicate does not match, so `total == 0` alongside a
        # non-empty page is a real state — and it used to run a batch of
        # "why did nothing match" queries on a search that had just
        # returned results, then tell the operator nothing matched while
        # showing them a row.
        # Present only when a leftover word was dropped to avoid an
        # empty page: what was dropped, and how many the rest matched.
        # The UI owes the operator a visible undo — these results are
        # NOT the search they typed.
        **({"relaxed": relaxed} if relaxed else {}),
        "relax": (
            _why_empty(db, labels=labels, cams=cams, words=words,
                       attrs=attrs, filters=filters)
            if not hits else []
        ),
        # How the ranking was produced, when a second arm took part —
        # or why it did not, when this site HAS embeddings and the
        # embedder could not be reached. Still absent on a deployment
        # that has simply never embedded anything, which is most of
        # them: a block saying "semantic: off" on every response would
        # be noise about a feature nobody switched on.
        **_semantic_block(page, no_vector_reason),
    }


# ── Home Assistant (HA-116/HA-502): zones by name, and period summaries ──


class TranscribeBody(BaseModel):
    """A spoken search: a short WAV (or anything the STT adapter decodes),
    base64, at most ``MAX_VOICE_BYTES`` decoded."""

    audio_b64: str = Field(..., min_length=16)
    language: str = Field("en", max_length=8)


MAX_VOICE_BYTES = 2 * 1024 * 1024


@router.post("/search/transcribe")
async def transcribe_search(
    body: TranscribeBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """A spoken question → the words to search for.

    The clip goes to the box's own speech-to-text adapter (the agent's
    whisper on the NPU, or Parakeet on the CPU) through KAI-C — nothing
    leaves the box — and the transcript is read back as a typed query:
    number words to digits, spelled-out plates to one token, the "show
    me" of a spoken request dropped. ``changes`` says what was altered
    and ``transcript`` is the untouched text, so the operator can see
    both; the caller then searches ``query`` exactly as if typed.

    503 with ``no-stt`` when no adapter advertises speech to text: voice
    search is only as available as the adapter behind it.
    """
    import base64
    import binascii

    from services.search_voice import spoken_to_text, transcribe

    try:
        audio = base64.b64decode(body.audio_b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="audio_b64 is not base64") from None
    if not audio:
        raise HTTPException(status_code=400, detail="empty audio")
    if len(audio) > MAX_VOICE_BYTES:
        raise HTTPException(status_code=413, detail=f"audio over {MAX_VOICE_BYTES} bytes")
    with contextlib.suppress(Exception):           # a person is waiting; never fail over bookkeeping
        from services.enrichment_governor import note_interactive

        note_interactive("search")
    transcript, adapter = await transcribe(audio, language=body.language)
    if adapter is None:
        raise HTTPException(status_code=503, detail={
            "reason": "no-stt",
            "message": "No speech-to-text adapter is registered on this box; "
                       "the camera agent's whisper or parakeet adapter provides one."})
    spoken = spoken_to_text(transcript or "")
    return {"transcript": transcript or "", "adapter": adapter, **spoken.as_dict()}


def _arm_weights(settings) -> dict[str, float]:
    """The fusion's per-arm votes from the settings (text / vector /
    caption), each defaulting to 1 when unset or unreadable."""
    out: dict[str, float] = {}
    for arm, key in (("text", "search_text_arm_weight"), ("vector", "search_vector_arm_weight"),
                     ("caption", "search_caption_arm_weight")):
        try:
            out[arm] = float(getattr(settings, key, 1.0))
        except (TypeError, ValueError):
            out[arm] = 1.0
    return out


def _semantic_block(page, no_vector_reason: str | None) -> dict:
    """The ``semantic`` key, or nothing.

    Present when an arm ran, and present when one SHOULD have run and
    could not. Absent on a site with no embeddings at all.
    """
    if page.semantic:
        return {"semantic": page.semantic}
    if no_vector_reason == "embedder-unreachable":
        return {"semantic": {
            "used": False,
            "reason": "embedder-unreachable",
            "note": ("This site has embedded visits, but no adapter could "
                     "turn the query into a vector — results are ranked by "
                     "words only until the embedding adapter is reachable."),
        }}
    return {}


async def _query_vectors(
    db, words: str,
) -> tuple[list[float] | None, list[float] | None, str | None]:
    """The query through BOTH arms' models: ``(frame vector, caption
    vector, reason)``. The frame vector is :func:`_query_vector`'s; the
    caption vector comes from the sentence encoder where one is
    registered (else it is the same vector, answered once). Either may
    be None on its own — a site with caption vectors and no frame
    embedder still gets its caption arm — and the reason is the frame
    arm's, as before, or ``embedder-unreachable`` when neither answered
    over a store that has vectors."""
    words = (words or "").strip()
    if not words:
        return None, None, None
    frame, reason = await _query_vector(db, words)
    if reason == "no-embeddings":
        return None, None, reason
    try:
        from services.embed_enrichment import embed_caption_query

        adapter = None
        if frame:
            # embed_text's adapter name is not kept by _query_vector; the
            # caption resolver answers the same vector when it is the
            # same adapter, so ask it with the frame in hand.
            from services.embed_enrichment import _resolve_embed_adapter

            adapter = await _resolve_embed_adapter()
        caption, _ = await embed_caption_query(words, frame=(frame, adapter))
    except Exception as exc:                       # noqa: BLE001
        logger.debug("search: caption query embedding unavailable (%s)", exc)
        caption = None
    if frame is None and caption is None:
        return None, None, reason or "embedder-unreachable"
    return frame, caption, (None if frame else reason)


async def _query_vector(db, words: str) -> tuple[list[float] | None, str | None]:
    """A vector for the operator's words, and WHY there isn't one.

    Returns ``(vector, reason)``. A reason is returned even though the
    search proceeds either way, because the reasons are not equivalent
    and the response has to be able to tell them apart:

    * ``None`` — there is a vector, or there were no words to embed.
    * ``"no-embeddings"`` — this deployment has never embedded anything.
      The ordinary state of most sites, and not worth alarming anyone
      about.
    * ``"embedder-unreachable"`` — this site HAS embeddings and the
      adapter could not produce a query vector. Semantic ranking is off
      right now and it should not be, which is an operational fact.

    Folding the last two together is the mistake this codebase keeps
    making in other clothes: a site with 200,000 embedded visits and a
    dead KAI-C returned a response byte-identical to a site that has
    never embedded anything, so the one deployment that would want to
    know had no way to find out.

    Asked cheapest-refusal-first. Checking the STORE before calling the
    adapter matters: a config flag saying embeddings are on, over an
    empty table, would buy an inference round trip per search and
    compare the answer against nothing.

    Nothing here raises. A search must not fail because an optional
    ranking could not be improved.
    """
    words = (words or "").strip()
    if not words:
        return None, None
    try:
        from services.embedding_store import capability

        if not capability(db).available:
            return None, "no-embeddings"
        from services.embed_enrichment import embed_text

        vector, _adapter = await embed_text(words)
        if vector:
            return vector, None
        return None, "embedder-unreachable"
    except Exception as exc:                       # noqa: BLE001
        logger.debug("search: query embedding unavailable (%s)", exc)
        return None, "embedder-unreachable"


def _why_empty(db, *, labels, cams, words, attrs, filters, limit: int = 5) -> list[dict]:
    """Which single chip is responsible for a search matching nothing.

    A parse the operator can SEE is the design here; this closes the
    loop by making it a parse they can act on. "Nothing matched —
    remove a chip" asks them to guess which one, and the honest answer
    is cheap: drop each filter in turn and count. If one of those
    counts is non-zero, that chip is the whole reason, and the UI can
    offer the search they meant in one click.

    Only runs on an empty result, so it costs nothing on the normal
    path, and it counts rather than fetching. Everything is
    best-effort: a failure here must leave a working (if empty) search
    alone rather than turning it into a 500.
    """
    if not any((labels, cams, words, attrs, filters.get("from_"),
                filters.get("to"), filters.get("plate"))):
        return []

    def count(**over) -> int:
        base = dict(labels=labels, camera_ids=cams, text=words or "",
                    attrs=attrs, **filters)
        base.update(over)
        return count_search_events(db, **base)

    # Ordered by how often each one is the culprit, so the first
    # suggestion is usually the right one.
    candidates: list[tuple[str, str, dict]] = []
    if words:
        candidates.append(("text", words, {"text": ""}))
    if filters.get("from_") or filters.get("to"):
        candidates.append(("when", "", {"from_": None, "to": None}))
    if cams:
        candidates.append(("camera_ids", "", {"camera_ids": []}))
    if labels:
        candidates.append(("labels", "", {"labels": []}))
    if filters.get("plate"):
        candidates.append(("plate", str(filters["plate"]), {"plate": None}))

    out: list[dict] = []
    for field, value, override in candidates[:limit]:
        try:
            found = count(**override)
        except Exception:  # noqa: BLE001 — a hint must never break a search
            logger.debug("empty-search hint failed for %s", field, exc_info=True)
            continue
        if found > 0:
            out.append({"drop": field, "value": value, "would_match": found})
    return out


def _resolve_zone(db: Session, zone: str | None, camera_id: int | None,
                  scope: set[int] | None) -> int | None:
    """A zone id from an id or a name. Names match exactly (case-insensitive,
    no wildcards) and only among cameras the caller can see: another user's
    zone names are not an oracle."""
    if zone is None or zone == "":
        return None
    if zone.isdigit():
        return int(zone)
    q = db.query(CameraZone).filter(func.lower(CameraZone.name) == zone.strip().lower())
    if scope is not None:
        q = q.filter(CameraZone.camera_id.in_(scope or {-1}))
    if camera_id is not None:
        q = q.filter(CameraZone.camera_id == camera_id)
    rows = q.all()
    if not rows:
        raise HTTPException(status_code=404, detail=f"No zone named {zone!r}")
    if len(rows) > 1:
        raise HTTPException(status_code=409,
                            detail=f"Zone name {zone!r} is on several cameras; add camera_id")
    return rows[0].id



#: Words that name an app alert's subject rather than a visit's: a question
#: carrying one of these looks in the alerts table too. Alert types are
#: matched by prefix so "loiter" finds "loitering_detected".
ALERT_WORDS: frozenset[str] = frozenset({
    "alert", "alerts", "alarm", "alarms", "loiter", "loitering", "occupancy",
    "crowd", "crowded", "overcrowding", "capacity", "package", "parcel",
    "delivery", "intrusion", "intruder", "tamper", "tampering", "guard",
    "patrol", "violence", "fight", "fall", "fallen", "abandoned", "garbage",
    "fire", "smoke", "weapon", "gun", "knife", "queue", "dwell",
})


def _matching_alerts(db, scope, current_user, *, words: str, labels, camera_ids,
                     start, end, cameras: dict[int, str] | None = None,
                     limit: int = 20) -> list[dict] | None:
    """App alerts the question's words name, in the same window and cameras.
    None when the question names nothing alert-shaped or the caller may not
    see alerts; [] when it does and nothing matched."""
    tokens = [w for w in (words or "").lower().split() if w]
    hits = [w for w in tokens if w in ALERT_WORDS or any(w.startswith(a) for a in ALERT_WORDS if len(a) > 4)]
    if not hits:
        return None
    if not user_has_permission(current_user, "alerts.view"):
        return None
    from routers.alerts_inbox import _scope_alerts
    q = _scope_alerts(db.query(AppAlert), scope)
    if start:
        q = q.filter(AppAlert.fired_at >= start)
    if end:
        q = q.filter(AppAlert.fired_at < end)
    if camera_ids:
        handles = [f"cam{int(c)}" for c in camera_ids] + [str(int(c)) for c in camera_ids]
        q = q.filter(AppAlert.camera_id.in_(handles))
    from sqlalchemy import or_
    conds = []
    for w in hits:
        if w in ("alert", "alerts", "alarm", "alarms"):
            continue
        like = f"%{w[:6]}%"
        conds.append(or_(func.lower(func.coalesce(AppAlert.alert_type, "")).like(like),
                         func.lower(AppAlert.title).like(like),
                         func.lower(func.coalesce(AppAlert.description, "")).like(like),
                         func.lower(func.coalesce(AppAlert.source_name, "")).like(like)))
    if conds:
        q = q.filter(or_(*conds))
    rows = q.order_by(AppAlert.fired_at.desc()).limit(limit).all()
    out = []
    for a in rows:
        out.append({
            "alert_id": a.alert_id, "fired_at": a.fired_at.isoformat() if a.fired_at else None,
            "severity": a.severity, "title": a.title, "description": a.description,
            "alert_type": a.alert_type, "source_name": a.source_name,
            "camera_id": _alert_camera_num(a.camera_id),
            "camera_name": (cameras or {}).get(_alert_camera_num(a.camera_id) or -1),
        })
    return out

def _all_time_scope(db, camera_ids, total: int) -> dict | None:
    """How far back an all-time search reached: the oldest visit's day in
    scope and the span in days. The query itself is cheap (an index); what
    a long span costs is the operator's time scrolling, and the describing
    of undescribed visits, which is capped per question."""
    try:
        q = db.query(func.min(TimelineEvent.started_at), func.max(TimelineEvent.started_at))
        if camera_ids:
            q = q.filter(TimelineEvent.camera_id.in_([int(c) for c in camera_ids]))
        oldest, newest = q.one()
    except Exception:  # noqa: BLE001 — a note must never break a search
        return None
    if oldest is None:
        return None
    days = max(1, (newest - oldest).days + 1) if newest else 1
    return {"all_time": True, "oldest": oldest.isoformat(), "days": days, "total": int(total)}

def _alert_camera_num(handle: str | None) -> int | None:
    from services.alerts_inbox import _camera_num

    return _camera_num(handle)


#: Longest period one summary covers.
MAX_SUMMARY_DAYS = 31


@router.get("/search/summary")
async def search_summary(
    from_: datetime = Query(..., alias="from"),
    to: datetime | None = None,
    camera_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """What happened between ``from`` and ``to`` (default now), per camera:
    events counted by label (and the first and last), alerts by severity.
    Scoped like ``/search``: events need ``recordings.view``, alerts
    ``alerts.view``, on visible cameras only. Cameras with nothing are
    left out."""
    from datetime import UTC, timedelta

    from routers.alerts_inbox import _scope_alerts

    def aware(value: datetime) -> datetime:  # a naive time is UTC
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    start, end = aware(from_), aware(to or datetime.now(UTC))
    if end <= start:
        raise HTTPException(status_code=422, detail="'to' must be after 'from'")
    if end - start > timedelta(days=MAX_SUMMARY_DAYS):
        raise HTTPException(status_code=422,
                            detail=f"A summary covers at most {MAX_SUMMARY_DAYS} days")
    scope = visible_camera_ids(db, current_user)
    if camera_id is not None and scope is not None and camera_id not in scope:
        raise HTTPException(status_code=404, detail="Camera not found")

    cams: dict[int, dict] = {}

    def cam(cid: int) -> dict:
        return cams.setdefault(cid, {"camera_id": cid, "events": {}, "event_count": 0,
                                     "first_event": None, "last_event": None,
                                     "alerts": {}, "alert_count": 0})

    if user_has_permission(current_user, "recordings.view"):
        eq = scope_query(db.query(TimelineEvent.camera_id, TimelineEvent.label,
                                  func.count(TimelineEvent.id),
                                  func.min(TimelineEvent.started_at),
                                  func.max(TimelineEvent.started_at)),
                         TimelineEvent.camera_id, scope)
        eq = eq.filter(TimelineEvent.started_at >= start, TimelineEvent.started_at < end)
        if camera_id is not None:
            eq = eq.filter(TimelineEvent.camera_id == camera_id)
        for cid, label, count, first, last in eq.group_by(
                TimelineEvent.camera_id, TimelineEvent.label).all():
            if cid is None:
                continue
            c = cam(cid)
            c["events"][label or "unknown"] = count
            c["event_count"] += count
            for key, value, pick in (("first_event", first, min), ("last_event", last, max)):
                if value is not None:
                    iso = value.isoformat()
                    c[key] = iso if c[key] is None else pick(c[key], iso)

    site_alerts: dict[str, int] = {}   # alerts about no camera in particular
    if user_has_permission(current_user, "alerts.view"):
        aq = _scope_alerts(db.query(AppAlert.camera_id, AppAlert.severity,
                                    func.count(AppAlert.id)), scope)
        aq = aq.filter(AppAlert.fired_at >= start, AppAlert.fired_at < end)
        for handle, severity, count in aq.group_by(AppAlert.camera_id, AppAlert.severity).all():
            sev = severity or "unknown"
            cid = _alert_camera_num(handle)
            if cid is None:
                if camera_id is None and not handle:
                    site_alerts[sev] = site_alerts.get(sev, 0) + count
                continue
            if camera_id is not None and cid != camera_id:
                continue
            c = cam(cid)
            c["alerts"][sev] = c["alerts"].get(sev, 0) + count
            c["alert_count"] += count

    names = dict(db.query(Camera.id, Camera.name).filter(Camera.id.in_(list(cams) or [-1])).all())
    rows = sorted(cams.values(), key=lambda c: (-(c["event_count"] + c["alert_count"]),
                                                 c["camera_id"]))
    for c in rows:
        c["name"] = names.get(c["camera_id"])
    return {"from": start.isoformat(), "to": end.isoformat(), "cameras": rows,
            "site_alerts": site_alerts,
            "totals": {"events": sum(c["event_count"] for c in rows),
                       "alerts": sum(c["alert_count"] for c in rows)
                       + sum(site_alerts.values())}}
