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

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from core.auth import get_current_active_user
from core.database import get_db
from models import (AppAlert, Camera, CameraZone, TimelineEvent, User,
                    VisitDescriptor)

# The internal door is defined once, next to the pipeline's write routes;
# the metrics scrape is the same door and should not grow a second lock.
from routers.internal_camera_agent import _platform_only, _require_internal_key
from services import search_metrics as metrics
from core.permissions import user_has_permission
from services.camera_scope import scope_query, visible_camera_ids
from services.search_query import ParsedQuery, parse_query
from services.search_service import (anchor_for, count_search_events,
                                     search_page, summarise_hits)

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
    from services.enrichment_plan import CACHE, build_plan, plan_for_label
    from services.kai_c_service import KaiCService

    plan = CACHE.get()
    if plan is None:
        svc = KaiCService()
        caps: dict = {}
        health: dict = {}
        try:
            caps = await svc.get_capabilities()
        except Exception:
            # A registry that cannot be reached means "nothing extra can be
            # run right now", not an error page: the visit still has its
            # class, camera and time, and enrichment is additive by design.
            caps = {}
            metrics.REGISTRY_UNREACHABLE.inc()
        try:
            health = await svc.check_kai_c_health()
        except Exception:
            health = {}
        plan = CACHE.put(build_plan(caps, health))
        # What KAI-C makes available right now. Coverage is produced from
        # this, so a fall here is tomorrow's recall complaint.
        metrics.SKILLS.set(len(plan), {"state": "registered"})
        metrics.SKILLS.set(sum(1 for s in plan if s.healthy), {"state": "healthy"})

    shown = plan_for_label(plan, label) if label else plan
    kinds = sorted({k for s in shown if s.healthy for k in s.descriptor_kinds})
    return {
        "skills": [s.as_dict() for s in shown],
        # The descriptor kinds this deployment can actually produce — what
        # a "colour" or "face" filter is worth offering at all.
        "descriptor_kinds": kinds,
        "label": label,
    }


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


@router.get("/search")
async def search(
    q: str = Query("", description="Natural-language query, e.g. 'red truck at the dock yesterday'."),
    # Explicit overrides. Any of these WINS over what the sentence was
    # taken to mean — which is how a corrected chip reaches the query.
    label: list[str] | None = Query(None, description="Object class; repeatable (OR)."),
    camera_id: list[int] | None = Query(None, description="Camera; repeatable (OR)."),
    text: str | None = Query(None, description="Words to match in captions/attributes."),
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
    skip: int = Query(0, ge=0, le=100_000),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Search visits. Returns the interpretation, the page, and the total.

    Scoping is the store's own: a caller sees results only from cameras
    they can view, and the camera vocabulary the parser matches names
    against is the same set — so "the dock" cannot even name a camera
    somebody else owns.
    """
    scope = visible_camera_ids(db, current_user)
    cameras = _visible_cameras(db, scope)
    parsed = parse_query(q, cameras=cameras) if parse else ParsedQuery()

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
    filters = dict(
        from_=start, to=end, plate=plate_q or None, source=source, scope=scope,
        zone_id=zone_id,
    )
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
    query_vector, no_vector_reason = await _query_vector(db, words or "")

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
            query_vector=query_vector, total=total, limit=limit, skip=skip,
            **filters,
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
                query_vector=query_vector, total=without, limit=limit,
                skip=skip, **filters)
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

    interpretation = parsed.as_dict()
    interpretation.update({
        # Whether these filters came from the sentence or from the caller.
        "source": "parsed" if parse else "explicit",
        "labels": labels,
        "camera_ids": cams,
        "text": words or "",
        "plate": plate_q or "",
        "attrs": [f"{k}:{v}" for k, v in attrs],
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

    return {
        "query": q,
        "interpretation": interpretation,
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
