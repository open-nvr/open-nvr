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

A third arm, ``caption``, is the same query vector against the visits'
CAPTION vectors (``embedding_store.KIND_CAPTION``: the caption and
attribute words through the embedding model's text tower). Words catch
the exact term, the image arm catches what the frame shows, and the
caption arm catches what the description MEANS — "man in a blue kurta"
against "person wearing a light blue tunic", which neither of the other
two can. It is fused like the others, over the same candidate set, with
its own floor (``SEARCH_CAPTION_MIN_SIMILARITY``), and a visit with no
caption vector simply does not score in it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import func, literal, or_, select, text as sql_text
from sqlalchemy.orm import aliased

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


#: Colour words, and the things a colour is said OF. "blue shirt" means
#: blue next to shirt; "person in blue" means blue clothing. Plain word
#: matching read "blue" anywhere ("blue jeans", "light blue wall") and
#: "blue shirt" as blue AND shirt in any roles ("green t-shirt and blue
#: jeans" matched). Postgres can say "within two words": the query below
#: is built as `blue <2> (shirt | top | ...)`, which is what the words
#: meant. CLIP-family embeddings lose exactly this binding (bag-of-words
#: behaviour, arXiv 2502.03566), so the lexical lane has to carry it.
COLOUR_WORDS = frozenset({
    "red", "blue", "green", "yellow", "orange", "purple", "pink", "black",
    "white", "grey", "gray", "brown", "beige", "navy", "maroon", "silver",
    "gold", "golden", "cream", "teal", "violet", "khaki", "tan", "olive",
})
CLOTHING_WORDS = (
    "shirt", "t-shirt", "tshirt", "tee", "top", "jacket", "coat", "hoodie",
    "sweater", "sweatshirt", "jumper", "kurta", "dress", "saree", "sari",
    "suit", "uniform", "vest", "blouse", "trousers", "pants", "jeans",
    "shorts", "skirt", "cap", "hat", "helmet", "scarf", "turban", "hijab",
    "clothes", "clothing", "outfit", "bag", "backpack", "van", "car", "truck",
    "bike", "scooter", "umbrella",
)


def fts_query(text: str) -> tuple[str, str]:
    """The Postgres text-search query for the words: ``(function, argument)``.

    A colour followed by a thing within two words becomes a proximity
    query on that pair; a colour on its own becomes a proximity query on
    the colour and any clothing word; everything else is the plain AND
    of the words (``plainto_tsquery``). Returns ``("to_tsquery", expr)``
    or ``("plainto_tsquery", text)``."""
    words = [w for w in text.lower().replace("-", " ").split() if w]
    if not words or not any(w in COLOUR_WORDS for w in words):
        return "plainto_tsquery", text
    # Postgres's ``<N>`` means EXACTLY N positions apart (``<->`` is ``<1>``),
    # not "within N": ``blue <2> shirt`` missed "light blue shirt" on the
    # Ventuno Q (2026-11-07) and matched only "blue cotton shirt". Within
    # two words is the two distances OR'd.
    def near(a: str, b: str) -> str:
        return f"(({a} <-> {b}) | ({a} <2> {b}))"

    parts: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        if w in COLOUR_WORDS:
            nxt = [x for x in words[i + 1:i + 3] if x not in COLOUR_WORDS]
            thing = next((x for x in nxt if x.replace("-", "") in {c.replace("-", "") for c in CLOTHING_WORDS}), None)
            if thing is not None:
                parts.append(near(w, thing))
                i = words.index(thing, i + 1) + 1
                continue
            alts = " | ".join(dict.fromkeys(c.replace("-", " ").split()[0] for c in CLOTHING_WORDS))
            parts.append(near(w, f"({alts})"))
        else:
            parts.append(w)
        i += 1
    return "to_tsquery", " & ".join(parts)


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
    word = value.strip().lower()
    where = [
        VisitDescriptor.event_id == TimelineEvent.id,
        VisitDescriptor.value == word,
    ]
    if kind and kind.strip():
        where.append(VisitDescriptor.kind == kind.strip().lower())
        return select(VisitDescriptor.id).where(*where).exists()
    # A BARE word — the agent's "did you see a blue car" — means "described
    # as blue", and a caption is a description too: the captioner may have
    # written "a blue car" on a visit the VQA enricher has not reached (or
    # never will, on a box without it). A kind-scoped chip (colour:blue)
    # stays a claim match, which is what the search UI's chips mean.
    # An alias: the outer query already joins event_text, and a correlated
    # subquery on the same table would auto-correlate it away (no FROM).
    words = aliased(EventText)
    caption_says = (
        select(words.event_id)
        .where(words.event_id == TimelineEvent.id)
        .where(or_(
            func.lower(func.coalesce(words.caption, "")).like(f"%{word}%"),
            func.lower(func.coalesce(words.attributes, "")).like(f"%{word}%"),
        ))
        .exists()
    )
    return or_(select(VisitDescriptor.id).where(*where).exists(), caption_says)


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
            fn, arg = fts_query(text)
            q = q.filter(
                sql_text(f"{FTS_EXPR} @@ {fn}('simple', :fts_q)")
                .bindparams(fts_q=arg)
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
        _fn, _arg = fts_query(text)
        # Cover density: words near each other rank above the same words
        # scattered through a long caption.
        score = func.ts_rank_cd(
            sql_text(FTS_EXPR), getattr(func, _fn)("simple", _arg)
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


def _rrf(arms: dict[str, list[int]],
         weights: dict[str, float] | None = None) -> tuple[dict[int, float],
                                                           dict[int, dict[str, int | None]]]:
    """Reciprocal Rank Fusion over ``{arm_name: [ids, best first]}``.

    Returns the fused score per id and, per id, its 1-based place in
    each arm (``None`` where an arm did not return it at all).

    The arithmetic is the whole algorithm and it is four lines, which is
    the argument for it: no normalisation, no per-deployment constant. A
    row both arms liked beats a row one arm loved, and that is the
    behaviour worth having when the arms disagree about what "similar"
    means. ``weights`` scales an arm's vote (``{"caption": 0.5}``; an
    arm not named weighs 1): the labelled eval on the Ventuno Q
    (bench/ventuno-q-text-embed-2026-11-10.md) found the caption arm at
    half weight and the frame floor at 0.12 lift every metric over equal
    votes, because two vector arms that agree on a near-miss outvoted
    the words that named the thing.
    """
    scores: dict[int, float] = {}
    places: dict[int, dict[str, int | None]] = {}
    names = list(arms)
    weights = weights or {}
    for name, ids in arms.items():
        w = float(weights.get(name, 1.0))
        for i, eid in enumerate(ids):
            rank = i + 1
            scores[eid] = scores.get(eid, 0.0) + w / (RRF_K + rank)
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
    caption_query_vector: Sequence[float] | None = None,
    total: int | None = None,
    limit: int = 50,
    skip: int = 0,
    min_similarity: float | None = None,
    caption_min_similarity: float | None = None,
    arm_weights: dict[str, float] | None = None,
    **filters: Any,
) -> SearchPage:
    """A page of results, with the vector arms fused in when there are any.

    ``min_similarity`` floors the image arm, ``caption_min_similarity``
    the caption arm; each is a cosine on its own model's scale, and
    ``None`` or 0 means no floor.

    ``caption_query_vector`` is the query through the CAPTION arm's model
    (the sentence encoder, where one is registered); left ``None`` the
    caption arm scores ``query_vector``, which is right when one model
    serves both arms and a silent non-match (a dimension mismatch is
    skipped) when it does not — so callers that have both pass both.

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

    if not query_vector and not caption_query_vector:
        hits = search_events(
            db, labels=labels, camera_ids=camera_ids, text=text, attrs=attrs,
            limit=limit, skip=skip, **filters)
        return SearchPage(hits=hits, total=total, semantic=None)
    caption_vector = caption_query_vector if caption_query_vector else query_vector

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

    # Arms 2 and 3 — vectors, over the structural set with no text
    # predicate. One candidate query serves both: the image arm scores
    # each candidate's frame vector, the caption arm its caption vector,
    # and a candidate missing either is skipped by that arm, not scored
    # as zero (see rank_by_similarity).
    candidate_ids = _arm_ids(
        db, filters=filters, labels=labels, camera_ids=camera_ids,
        text="", attrs=attrs, limit=cap.ceiling + 1, ranked=False)
    empty = embedding_store.SimilarityResult(ids=[], scores=[], considered=0, truncated=False)
    sim = embedding_store.rank_by_similarity(
        db, query_vector=query_vector, candidate_ids=candidate_ids,
        limit=depth, cap=cap, kind=embedding_store.KIND_IMAGE) if query_vector else empty
    csim = embedding_store.rank_by_similarity(
        db, query_vector=caption_vector, candidate_ids=candidate_ids,
        limit=depth, cap=cap, kind=embedding_store.KIND_CAPTION) if caption_vector else empty

    # THE FLOOR. rank_by_similarity returns its top-N whatever the
    # numbers are, so a query nothing resembles still produced a full
    # arm of rows — and fusion, counting rows not similarity, made them
    # the answer. "A person in a red shirt" on a box that never saw one
    # returned fifty people at cosine 0.19. Below the floor a row is not
    # a result; how many were cut is reported so a reader can tell "the
    # floor removed everything" from "nothing had a vector". Each vector
    # arm has its own floor: text-vs-text and text-vs-image similarities
    # come off the same model but do not sit on the same scale.
    best = max(sim.scores) if sim.scores else None
    vector_ids, below_floor = _apply_floor(sim, min_similarity)
    caption_best = max(csim.scores) if csim.scores else None
    caption_ids, caption_below_floor = _apply_floor(csim, caption_min_similarity)

    arms: dict[str, list[int]] = {"vector": vector_ids}
    if text_ids:
        arms["text"] = text_ids
    if caption_ids:
        arms["caption"] = caption_ids
    caption_block = {
        "considered": csim.considered, "truncated": csim.truncated,
        "floor": caption_min_similarity, "below_floor": caption_below_floor,
        "best": None if caption_best is None else round(float(caption_best), 4),
    }
    scores, places = _rrf(arms, arm_weights)
    if not scores:
        return SearchPage(hits=[], total=total, semantic={
            "used": True,
            "reason": ("below-floor" if (below_floor or caption_below_floor)
                       else "no-candidates"),
            "considered": sim.considered, "truncated": sim.truncated,
            "floor": min_similarity, "below_floor": below_floor, "best": best,
            "caption": caption_block,
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
    caption_only = [eid for eid in vector_only
                    if places.get(eid, {}).get("vector") is None]
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
        # what the vector arms are contributing — if this is always zero,
        # the embeddings are costing a scan and buying a reordering.
        "added_by_vector": len(vector_only),
        # Of those, the rows ONLY the caption arm found: neither the
        # words nor the frame matched, the description's meaning did.
        "added_by_caption": len(caption_only),
        # The caption arm's own scan and floor, kept apart from the
        # image arm's: an operator tuning SEARCH_CAPTION_MIN_SIMILARITY
        # needs this arm's `best` and `below_floor`, not a blend.
        "caption": caption_block,
        # The floor and what it did, next to the best score the arm saw,
        # so an operator tuning SEARCH_VECTOR_MIN_SIMILARITY has the two
        # numbers that matter on the same line.
        "floor": min_similarity,
        "below_floor": below_floor,
        "best": None if best is None else round(float(best), 4),
        "accel": cap.accel,
        "note": cap.note,
    })


def _apply_floor(sim, floor: float | None) -> tuple[list[int], int]:
    """The ids at or above ``floor``, and how many were cut. No floor
    (``None`` or 0) keeps every id the scan returned."""
    ids = list(sim.ids)
    if floor is None or floor <= 0 or not sim.scores:
        return ids, 0
    kept = [eid for eid, s in zip(sim.ids, sim.scores) if s >= floor]
    return kept, len(ids) - len(kept)


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

    # Which of these the WORDS found, and which are here only because a
    # vector resembled the query. A row with no ``ranks`` came from the
    # single-arm path and matched whatever was asked. This is the number
    # a yes/no question turns on: "did you see a red shirt" answered by
    # five similar-looking visits and zero that were described as red
    # is a no, and a list of five cannot say so.
    similar_only = sum(
        1 for h in hits
        if getattr(h, "ranks", None) and h.ranks.get("text") is None
    )

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
        "matched_words": len(hits) - similar_only,
        "similar_only": similar_only,
    }
