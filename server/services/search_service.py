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

"""Search over the canonical event store.

The store is already the right shape for this: one row per visit, with a
class label, a time range, a plate, and the best frame Tier-0 had of the
object. What search adds is (a) matching several labels at once, (b)
matching WORDS — the caption and attributes an enricher wrote into
``event_text`` — and (c) ordering by how well a row fits rather than only
by recency.

It deliberately extends :func:`timeline_service._events_query` instead of
writing a second predicate. That function is the single place scoping
lives; a search that built its own WHERE clause would be one refactor
away from returning a camera the caller cannot see.

Text matching is dialect-split, and honestly so:

* **Postgres** (production) uses the GIN-indexed expression from
  migration ``c1d2e3f4a5b6``. The query must build the SAME expression
  or the planner ignores the index — so it lives in one constant, here
  and in the migration, and the test suite asserts they agree.
* **SQLite** (tests, and a developer laptop) has no such index and falls
  back to LIKE. Correct, unranked, and fine at test sizes; anyone
  running a real deployment on SQLite has bigger problems than this.

Ranking has two arms now, and the second one is optional.

* **words** — the dialect-split match above, ranked by ``ts_rank`` on
  Postgres and unranked on SQLite.
* **vectors** — cosine similarity over ``event_embeddings``, when this
  deployment has any (:mod:`services.embedding_store`).

They are fused with RECIPROCAL RANK FUSION: ``1/(k + rank)`` summed
across the arms a row appears in, ``k=60``. RRF rather than a weighted
blend of the raw numbers because the two arms are on incompatible
scales — ``ts_rank`` is unbounded and corpus-relative, cosine is
[-1, 1] — so any weighting between them is a constant somebody has to
tune per deployment and nobody will. Ranks are comparable by
construction; scores are not. k=60 is the published default and is left
alone deliberately: an untuned constant that works is better than a
tuned one that drifts.

The second arm is DETECTED, never required. No embeddings in the store,
or no adapter advertising ``embed``, and this is the word search it has
always been: same call, same response shape, no error and no
degraded-mode banner. A site with the hardware for an embedding adapter
gets better ranking; a site without gets exactly what it got before.
That is the rule the camera-agent's hardware panel already follows —
detect, recommend, never gate — applied to the store.

One asymmetry is deliberate. The vector arm ranks the structural
candidate set WITHOUT the text predicate. Restricting similarity to
rows that already matched the words would make the second arm
structurally incapable of finding anything the first one missed, which
is the only reason to have a second arm.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import func, literal, or_, select, text as sql_text

from models import EventText, TimelineEvent, VisitDescriptor
from services.timeline_service import _events_query

__all__ = ["SearchHit", "SearchPage", "search_events", "search_page",
           "count_search_events", "FTS_EXPR", "RRF_K"]

#: MUST match migration c1d2e3f4a5b6's index expression exactly.
FTS_EXPR = (
    "to_tsvector('simple', coalesce(event_text.caption, '') || ' ' "
    "|| coalesce(event_text.attributes, ''))"
)

#: Reciprocal Rank Fusion's damping constant.
#:
#: The published default, left alone on purpose. Its job is to stop rank
#: 1 from dominating rank 2 so heavily that the second arm can never
#: move anything — with k=60, first place is worth 1/61 and tenth is
#: worth 1/70, so agreement between the arms matters more than a narrow
#: win in either. Tuning it per deployment would mean tuning it per
#: deployment, which nobody does; a constant that works untuned is the
#: feature.
RRF_K = 60

#: Candidates each arm contributes to the fusion pool.
#:
#: Deeper pools do not improve the top of the list — they add rows that
#: neither arm ranked highly, which is noise with a score attached — and
#: they cost a wider scan on the vector side. 50 per arm is the common
#: practical default and matches what the pool is for: reordering the
#: plausible, not widening it.
ARM_DEPTH = 50


@dataclass
class SearchHit:
    """One result: the visit, its words, what the skills claimed about it,
    and why it ranked where it did."""

    event: TimelineEvent
    caption: str | None
    attributes: str | None
    score: float
    claims: list[dict[str, Any]] = field(default_factory=list)
    #: Where this row placed in each arm that produced it, 1-based:
    #: ``{"text": 3, "vector": None}``. ``None`` means that arm did not
    #: return the row at all, which is different from placing last —
    #: fusion gives it nothing, and a person debugging a result can see
    #: whether the words or the vectors put it here.
    #:
    #: Empty when only one arm ran, because a rank nothing was fused
    #: with explains nothing.
    ranks: dict[str, int | None] = field(default_factory=dict)


@dataclass
class SearchPage:
    """A page of results plus what it took to produce it.

    ``total`` keeps its original meaning — how many rows match the
    structural filters and the words — so every existing caller reads
    the same number it always did. Rows the VECTOR arm contributed can
    sit outside that count, which is the point of them; ``semantic``
    says how many.
    """

    hits: list[SearchHit]
    total: int
    #: ``None`` when no vector arm ran (no embeddings, or no query
    #: vector). A dict when it did — see :func:`search_page`.
    semantic: dict[str, Any] | None = None


def _is_postgres(db) -> bool:
    return db.bind.dialect.name == "postgresql" if db.bind is not None else False


def db_exists_claim(kind: str | None, value: str):
    """A correlated EXISTS over one descriptor claim.

    ``kind`` may be None or empty, which matches the value under ANY
    kind. That is not laziness, it is the only spelling-proof option for
    a caller that did not choose the vocabulary: the colour kind is
    ``colour`` and every caller who has not read
    ``descriptor_enrichment.LABEL_KINDS`` will send ``color``. A
    kind-scoped query with the wrong spelling returns nothing and looks
    exactly like "no blue cars", which is the failure this whole area
    keeps producing. Values are distinctive enough that an unscoped
    match is right nearly always, and a caller who knows the kind can
    still say so.
    """
    where = [
        VisitDescriptor.event_id == TimelineEvent.id,
        VisitDescriptor.value == value.strip().lower(),
    ]
    if kind and kind.strip():
        where.append(VisitDescriptor.kind == kind.strip().lower())
    return select(VisitDescriptor.id).where(*where).exists()


def _apply_search_filters(
    q,
    *,
    labels: list[str] | None,
    camera_ids: list[int] | None,
    text: str = "",
    attrs: list[tuple[str, str]] | None = None,
    postgres: bool = False,
):
    """The search-only predicates, on top of the shared event predicate.

    ``labels`` and ``camera_ids`` are ORs within themselves: a visit row
    is one object on one camera, so "car or truck" is the only meaning
    several labels can have. Text is ANDed with them.

    The join to ``event_text`` is unconditional so a result can carry its
    caption whether or not the query mentioned words. Whether it is OUTER
    or INNER depends on whether there are words, and that is not a
    micro-optimisation — it is the difference between using the GIN index
    and not.

    WITHOUT text, the join must be OUTER, for the reason it was written:
    "truck yesterday" has to find the trucks nobody has captioned, and an
    inner join would silently restrict every search to enriched rows.

    WITH text, an outer join is what STOPPED the index being used, on
    every Postgres deployment, since the index was added. The predicate
    is ``to_tsvector(coalesce(caption,'') || ' ' || coalesce(attributes,''))
    @@ q``, and COALESCE makes it non-strict: an unmatched row yields
    ``to_tsvector('') @@ q``, which is false rather than NULL. Postgres
    therefore cannot prove the outer join is equivalent to an inner one,
    cannot push the filter down to ``event_text``, and evaluates
    ``to_tsvector`` as a join filter over every row in the table. Measured
    on 20,000 visits: 132ms seq-scanning, 5.7ms once the Bitmap Heap Scan
    on ``ix_event_text_fts`` is reachable.

    The migration's comment warned that the query must build the same
    EXPRESSION or the planner ignores the index. It does build the same
    expression. The join shape defeated it anyway, and nothing noticed
    because the test suite runs on SQLite, where there is no such index
    to fail to use.

    An inner join is exactly equivalent here: a visit with no row in
    ``event_text`` has no words, so it cannot match a query for words.
    Dropping it changes no result — only the plan.
    """
    if text:
        q = q.join(EventText, EventText.event_id == TimelineEvent.id)
    else:
        q = q.outerjoin(EventText, EventText.event_id == TimelineEvent.id)
    if labels:
        wanted = [s.strip().lower() for s in labels if s and s.strip()]
        if wanted:
            q = q.filter(TimelineEvent.label.in_(wanted))
    if camera_ids:
        q = q.filter(TimelineEvent.camera_id.in_([int(c) for c in camera_ids]))
    if attrs:
        # EXISTS per claim, ANDed: "red" AND "van" must both be true of
        # the SAME visit. A join would multiply rows per claim and make
        # the count lie; a subquery cannot.
        for kind, value in attrs:
            q = q.filter(
                db_exists_claim(kind, value)
            )
    if text:
        if postgres:
            q = q.filter(
                sql_text(f"{FTS_EXPR} @@ plainto_tsquery('simple', :fts_q)")
                .bindparams(fts_q=text)
            )
        else:
            # SQLite: every word must appear in the caption or the
            # attributes — the same AND-across-words, OR-across-columns
            # meaning plainto_tsquery gives us on Postgres.
            for word in text.split():
                like = f"%{word.lower()}%"
                q = q.filter(
                    or_(
                        func.lower(func.coalesce(EventText.caption, "")).like(like),
                        func.lower(func.coalesce(EventText.attributes, "")).like(like),
                    )
                )
    return q


def _base(db, *, filters: dict[str, Any], labels, camera_ids, text: str,
          attrs: list[tuple[str, str]] | None = None):
    """One filtered query — the shared event predicate plus the search
    predicates. Both the page and the count come from this object."""
    return _apply_search_filters(
        _events_query(db, **filters),
        labels=labels,
        camera_ids=camera_ids,
        text=text,
        attrs=attrs,
        postgres=_is_postgres(db),
    )


def search_events(
    db,
    *,
    labels: list[str] | None = None,
    camera_ids: list[int] | None = None,
    text: str = "",
    attrs: list[tuple[str, str]] | None = None,
    limit: int = 50,
    skip: int = 0,
    **filters: Any,
) -> list[SearchHit]:
    """Matching visits — best first when there is text to rank by,
    newest first otherwise.

    ``filters`` are the shared event filters (``scope``, ``from_``,
    ``to``, ``plate``, ``source``, ``camera_id``, ``label``), scoping
    included, because this is the same predicate the timeline uses.
    """
    limit = max(1, min(500, int(limit)))
    skip = max(0, int(skip))
    base = _base(db, filters=filters, labels=labels, camera_ids=camera_ids, text=text,
                 attrs=attrs)

    if text and _is_postgres(db):
        score = func.ts_rank(
            sql_text(FTS_EXPR), func.plainto_tsquery("simple", text)
        ).label("score")
        # Recency still breaks ties: two rows that fit the words equally
        # well are not equally interesting, and the newer one is the one
        # an operator means.
        order = (score.desc(), TimelineEvent.started_at.desc(), TimelineEvent.id.desc())
    else:
        # Nothing to rank by — say 1.0 rather than invent a number the
        # UI would then sort by and believe.
        score = literal(1.0).label("score")
        order = (TimelineEvent.started_at.desc(), TimelineEvent.id.desc())

    rows = (
        base.with_entities(TimelineEvent, EventText.caption, EventText.attributes, score)
        .order_by(*order)
        .offset(skip)
        .limit(limit)
        .all()
    )
    hits = [
        SearchHit(event=r[0], caption=r[1], attributes=r[2], score=float(r[3] or 0.0))
        for r in rows
    ]
    _attach_claims(db, hits)
    return hits


def _attach_claims(db, hits: list[SearchHit]) -> None:
    """What each skill claimed about these visits.

    One extra query for the whole page, not one per row, so a result can
    show "red · van · plate KA01AB1234" and name the skill behind each.
    """
    ids = [h.event.id for h in hits]
    if not ids:
        return
    claims: dict[int, list[dict[str, Any]]] = {}
    for d in db.query(VisitDescriptor).filter(VisitDescriptor.event_id.in_(ids)).all():
        claims.setdefault(d.event_id, []).append({
            "kind": d.kind, "value": d.value, "confidence": d.confidence,
            "task": d.source_task, "adapter": d.source_adapter,
        })
    for h in hits:
        h.claims = sorted(claims.get(h.event.id, []), key=lambda c: c["kind"])


def count_search_events(
    db,
    *,
    labels: list[str] | None = None,
    camera_ids: list[int] | None = None,
    text: str = "",
    attrs: list[tuple[str, str]] | None = None,
    **filters: Any,
) -> int:
    """How many visits these filters match, ignoring paging — built from
    the same query object as the page, so the two cannot disagree."""
    return _base(
        db, filters=filters, labels=labels, camera_ids=camera_ids, text=text, attrs=attrs
    ).count()


# ── two arms, fused ──────────────────────────────────────────────────


def _rrf(arms: dict[str, list[int]]) -> tuple[dict[int, float],
                                              dict[int, dict[str, int | None]]]:
    """Reciprocal Rank Fusion over ``{arm_name: [ids, best first]}``.

    Returns the fused score per id and, per id, its 1-based place in
    each arm (``None`` where an arm did not return it at all).

    The arithmetic is the whole algorithm and it is four lines, which is
    the argument for it: no weights, no normalisation, no per-deployment
    constant. A row both arms liked beats a row one arm loved, and that
    is the behaviour worth having when the arms disagree about what
    "similar" means.
    """
    scores: dict[int, float] = {}
    places: dict[int, dict[str, int | None]] = {}
    names = list(arms)
    for name, ids in arms.items():
        for i, eid in enumerate(ids):
            rank = i + 1
            scores[eid] = scores.get(eid, 0.0) + 1.0 / (RRF_K + rank)
            places.setdefault(eid, {n: None for n in names})[name] = rank
    for eid in scores:
        places.setdefault(eid, {n: None for n in names})
    return scores, places


def search_page(
    db,
    *,
    labels: list[str] | None = None,
    camera_ids: list[int] | None = None,
    text: str = "",
    attrs: list[tuple[str, str]] | None = None,
    query_vector: Sequence[float] | None = None,
    total: int | None = None,
    limit: int = 50,
    skip: int = 0,
    **filters: Any,
) -> SearchPage:
    """A page of results, with the vector arm fused in when there is one.

    This is what routes should call. :func:`search_events` remains the
    single-arm path and is unchanged, because plenty of callers want a
    list of hits and nothing else.

    ``query_vector`` is supplied by the caller — core does not decide
    what embeds a query any more than it decides what captions a frame.
    ``None`` (no adapter, no vectors, or a caller that simply does not
    want the second arm) takes the word-only path, which is the old
    behaviour exactly.

    WHAT THE SECOND ARM IS ALLOWED TO SEE

    The vector arm ranks the structural candidate set — camera, window,
    class, plate, claims, scope — with the TEXT predicate removed. That
    is the asymmetry that makes it useful: an arm restricted to rows the
    words already found can only reorder them, never add the one that
    was captioned "a lorry" when the operator typed "truck".

    It is also why the scoping has to come from the same place. The
    candidate query is built by :func:`_base` with ``text=""``, so the
    scope filter is the identical one the word arm used. A second arm
    that built its own WHERE clause would be one refactor away from
    ranking a camera the caller cannot see.

    ``total`` is the match count, and a caller that has ALREADY counted
    passes it in rather than paying for it twice. The operator route
    does exactly that: it times its count separately, because that timer
    feeds ``opennvr_search_count_seconds``, and the point of that metric
    is that an exact total is the one cost this API added over the old
    app store — an operator has to be able to see it to decide whether
    to keep it. Counting here as well made every operator search run the
    COUNT twice: ~6ms of duplicate work per search at 20k visits, more
    as the store grows, and invisible because both calls returned the
    same right answer.
    """
    limit = max(1, min(500, int(limit)))
    skip = max(0, int(skip))

    if total is None:
        total = count_search_events(
            db, labels=labels, camera_ids=camera_ids, text=text, attrs=attrs,
            **filters)

    if not query_vector:
        hits = search_events(
            db, labels=labels, camera_ids=camera_ids, text=text, attrs=attrs,
            limit=limit, skip=skip, **filters)
        return SearchPage(hits=hits, total=total, semantic=None)

    from services import embedding_store

    cap = embedding_store.capability(db)
    if not cap.available:
        # A caller handed us a vector and this deployment has nothing to
        # compare it against. Not an error — it is the ordinary state of
        # a site with no embedding adapter — but it IS reported, because
        # "semantic search did nothing" and "semantic search found
        # nothing" are different facts and the caller cannot tell them
        # apart from the results.
        hits = search_events(
            db, labels=labels, camera_ids=camera_ids, text=text, attrs=attrs,
            limit=limit, skip=skip, **filters)
        return SearchPage(hits=hits, total=total, semantic={
            "used": False, "reason": "no-embeddings", "note": cap.note})

    # HOW DEEP EACH ARM GOES, and it is not a constant.
    #
    # Fusion can only return rows that appeared in an arm, so the pool IS
    # the addressable result set. With a fixed depth of 50 per arm the
    # pool is at most 100 rows, and `skip=100` returned an EMPTY page in
    # the middle of a 250-row match — while `total` said 250 and the same
    # query with no embeddings returned rows. Turning semantic ranking on
    # broke paging for every caller.
    #
    # So the arms go at least as deep as the page being asked for. The
    # floor is ARM_DEPTH because fusion needs more rows than the page to
    # have anything to reorder; the ceiling is the same 500 search_events
    # clamps to, and reaching it is REPORTED rather than silently
    # truncated (`exhausted` below).
    depth = max(ARM_DEPTH, min(500, skip + limit))

    # Arm 1 — words. Ids only: this arm's rows are thrown away after the
    # ranking, so hydrating them through search_events meant loading 50
    # TimelineEvent + EventText rows and running a whole VisitDescriptor
    # query over them, to keep `.id` and discard the rest — then doing
    # the claims query AGAIN on the real page.
    text_ids: list[int] = []
    if text:
        text_ids = _arm_ids(
            db, filters=filters, labels=labels, camera_ids=camera_ids,
            text=text, attrs=attrs, limit=depth, ranked=_is_postgres(db))

    # Arm 2 — vectors, over the structural set with no text predicate.
    candidate_ids = _arm_ids(
        db, filters=filters, labels=labels, camera_ids=camera_ids,
        text="", attrs=attrs, limit=cap.ceiling + 1, ranked=False)
    sim = embedding_store.rank_by_similarity(
        db, query_vector=query_vector, candidate_ids=candidate_ids,
        limit=depth, cap=cap)

    arms: dict[str, list[int]] = {"vector": sim.ids}
    if text_ids:
        arms["text"] = text_ids
    scores, places = _rrf(arms)
    if not scores:
        return SearchPage(hits=[], total=total, semantic={
            "used": True, "reason": "no-candidates",
            "considered": sim.considered, "truncated": sim.truncated,
            "arms": {k: 0 for k in arms}, "note": cap.note})

    # Ties are a real outcome — two rows placing symmetrically in the two
    # arms fuse to the same number — so something has to break them, and
    # leaving it to dict order would make the same query answer
    # differently on different runs. Newer first, which is the same bias
    # the word arm already uses for equal ts_rank.
    ordered = sorted(scores, key=lambda eid: (-scores[eid], -eid))
    page_ids = ordered[skip:skip + limit]

    hits = _hydrate(db, page_ids)
    for h in hits:
        h.score = round(scores.get(h.event.id, 0.0), 6)
        h.ranks = places.get(h.event.id, {})
    _attach_claims(db, hits)

    vector_only = [eid for eid in ordered if places.get(eid, {}).get("text") is None]
    # WHAT `total` MEANS WHEN TWO ARMS RAN.
    #
    # The vector arm deliberately returns rows the text predicate does
    # not match — that is the entire point of it. Reporting the text
    # count as `total` therefore produced a response that contradicted
    # itself: one result, `total: 0`, and a "why did nothing match"
    # block, all describing the same page.
    #
    # The addressable set is the fused pool, so that is `total`. The word
    # count is still reported, under `text_total`, because the drop from
    # one to the other is how a reader sees the second arm working.
    return SearchPage(hits=hits, total=len(ordered), semantic={
        "used": True,
        "fusion": "rrf",
        "k": RRF_K,
        "text_total": total,
        "arms": {name: len(ids) for name, ids in arms.items()},
        # The pool is the addressable set, so a caller paging through it
        # needs to know when it ran out because the ARMS stopped, not
        # because the store did.
        "depth": depth,
        "exhausted": any(len(ids) >= depth for ids in arms.values()),
        # How many candidates the similarity scan actually looked at, and
        # whether it stopped early. These two travel together on purpose:
        # `considered` without `truncated` reads like a total, and
        # `truncated` without `considered` cannot be acted on.
        "considered": sim.considered,
        "truncated": sim.truncated,
        "ceiling": cap.ceiling,
        # Rows the words would never have returned. The honest measure of
        # what the second arm is contributing — if this is always zero,
        # the embeddings are costing a scan and buying a reordering.
        "added_by_vector": len(vector_only),
        "accel": cap.accel,
        "note": cap.note,
    })


def _arm_ids(db, *, filters, labels, camera_ids, text, attrs,
             limit: int, ranked: bool) -> list[int]:
    """Just the ids an arm contributes, in its own order.

    An arm's rows are discarded once the ranking is built, so loading
    them is pure waste — and it was not cheap waste: routing the word
    arm through :func:`search_events` hydrated 50 ``TimelineEvent`` +
    ``EventText`` rows and ran a full ``VisitDescriptor`` query over
    them, to keep ``.id`` from each and throw the rest away, before the
    real page did the claims query all over again.

    Built from :func:`_base`, so the scoping is the identical predicate
    the rest of search uses. An arm with its own WHERE clause would be
    one refactor away from ranking a camera the caller cannot see.
    """
    q = _base(db, filters=filters, labels=labels, camera_ids=camera_ids,
              text=text, attrs=attrs).with_entities(TimelineEvent.id)
    if ranked and text:
        score = func.ts_rank(sql_text(FTS_EXPR),
                             func.plainto_tsquery("simple", text))
        q = q.order_by(score.desc(), TimelineEvent.started_at.desc(),
                       TimelineEvent.id.desc())
    else:
        q = q.order_by(TimelineEvent.started_at.desc(), TimelineEvent.id.desc())
    return [int(r[0]) for r in q.limit(max(1, int(limit))).all()]


def _hydrate(db, ids: list[int]) -> list[SearchHit]:
    """Load ``ids`` as hits, preserving the order given.

    Fusion decides the order; SQL does not get a say. An ``IN`` clause
    returns rows in whatever order the planner likes, so the mapping
    back to the fused ranking is done here rather than hoped for.
    """
    if not ids:
        return []
    rows = (
        db.query(TimelineEvent, EventText.caption, EventText.attributes)
        .outerjoin(EventText, EventText.event_id == TimelineEvent.id)
        .filter(TimelineEvent.id.in_(ids))
        .all()
    )
    by_id = {r[0].id: r for r in rows}
    out: list[SearchHit] = []
    for eid in ids:
        r = by_id.get(eid)
        if r is None:
            # Deleted between the ranking and the fetch. Skipping is
            # right; a placeholder would be a result with no visit.
            continue
        out.append(SearchHit(event=r[0], caption=r[1], attributes=r[2], score=0.0))
    return out


def anchor_for(event: TimelineEvent) -> dict[str, Any]:
    """Where in the footage this result lives: the camera and the instant
    a player should open at.

    The visit's start, minus nothing: a result that opens exactly when
    the object appeared is what an operator expects, and any lead-in
    belongs to the player (which knows its own buffering), not to the
    search API.
    """
    at: datetime | None = event.started_at
    return {
        "camera_id": event.camera_id,
        "at": at.isoformat() if at else None,
        "ended_at": event.ended_at.isoformat() if event.ended_at else None,
    }


# ── Answering, not just listing ──────────────────────────────────────
#
# A result list makes the operator do the counting. "Was a red van here
# this morning?" is answered by ten rows they have to read and tally,
# and the thing they most need to know — that four of those ten were
# never described by any skill, so the absence of "red" on them means
# nothing — is the one thing a list cannot say.
#
# So search also answers. Two rules make that safe:
#
#   1. Every number below is COUNTED from rows already returned. Nothing
#      is inferred, generalised or predicted. If the store does not say
#      it, it does not appear.
#   2. The answer is DATA, not a sentence. The server has no business
#      emitting English prose that the French UI then cannot translate,
#      and a sentence assembled on the server is a sentence nobody can
#      re-word without a release. The UI composes it.
#
# Everything here describes THIS PAGE of results, not the whole match
# set, because that is what was actually loaded — counting claims across
# a ten-thousand-row match would be a second full query, and quoting
# page numbers as though they were totals is the exact dishonesty this
# is meant to remove. ``shown`` and ``total`` are both reported so the
# UI can say which it is talking about.

#: How many distinct cameras / claims / plates the answer names before
#: it stops. Past a handful the summary stops being a summary.
_ANSWER_TOP_N = 4


def summarise_hits(
    hits: list[SearchHit],
    *,
    total: int,
    camera_names: dict[int, str] | None = None,
) -> dict[str, Any]:
    """Counted facts about ``hits`` — what matched, where, when, what was
    claimed about them, and what is NOT known.

    Returns ``{}`` for an empty page: there is nothing to summarise, and
    an answer block full of zeroes reads as a finding.
    """
    if not hits:
        return {}

    names = camera_names or {}
    events = [h.event for h in hits]

    per_camera: dict[int, int] = {}
    for e in events:
        per_camera[e.camera_id] = per_camera.get(e.camera_id, 0) + 1
    cameras = sorted(
        ({"id": cid, "name": names.get(cid), "count": n}
         for cid, n in per_camera.items()),
        key=lambda c: (-c["count"], c["id"]),
    )

    starts = sorted(e.started_at for e in events if e.started_at)

    per_claim: dict[tuple[str, str], int] = {}
    for h in hits:
        # Distinct within a visit: two skills agreeing that a van is red
        # is one red van, not two. Counting agreements as sightings would
        # make the best-enriched visits look like a crowd.
        for kind, value in {(c["kind"], c["value"]) for c in h.claims
                            if c.get("kind") and c.get("value")}:
            per_claim[(kind, value)] = per_claim.get((kind, value), 0) + 1
    claims = sorted(
        ({"kind": k, "value": v, "count": n} for (k, v), n in per_claim.items()),
        key=lambda c: (-c["count"], c["kind"], c["value"]),
    )

    plates = sorted({e.plate_text for e in events if e.plate_text})

    return {
        # What this block is counted over, stated rather than implied.
        "scope": "page",
        "shown": len(hits),
        "total": total,
        "cameras": cameras[:_ANSWER_TOP_N],
        "camera_count": len(cameras),
        "first_at": starts[0].isoformat() if starts else None,
        "last_at": starts[-1].isoformat() if starts else None,
        "claims": claims[:_ANSWER_TOP_N],
        "claim_count": len(claims),
        "plates": plates[:_ANSWER_TOP_N],
        "plate_count": len(plates),
        "with_evidence": sum(1 for e in events if e.evidence_path),
        # The honest part, and the reason this is worth shipping. A visit
        # with no claims was never ASKED about — no skill ran on it — so
        # its silence on "red" is not evidence that it was not red. An
        # operator reading a list has no way to see that.
        "undescribed": sum(1 for h in hits if not h.claims),
    }
