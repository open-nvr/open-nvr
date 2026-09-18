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

Ranking is the layer that will grow. Today: rows matching text are
ranked by textual relevance, everything else newest-first. The place for
vector similarity is the same query, fused with these ranks (RRF), once
something in the stack produces embeddings — which is why the row scores
are exposed rather than hidden.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, literal, or_, select, text as sql_text

from models import EventText, TimelineEvent, VisitDescriptor
from services.timeline_service import _events_query

__all__ = ["SearchHit", "search_events", "count_search_events", "FTS_EXPR"]

#: MUST match migration c1d2e3f4a5b6's index expression exactly.
FTS_EXPR = (
    "to_tsvector('simple', coalesce(event_text.caption, '') || ' ' "
    "|| coalesce(event_text.attributes, ''))"
)


@dataclass
class SearchHit:
    """One result: the visit, its words, what the skills claimed about it,
    and why it ranked where it did."""

    event: TimelineEvent
    caption: str | None
    attributes: str | None
    score: float
    claims: list[dict[str, Any]] = field(default_factory=list)


def _is_postgres(db) -> bool:
    return db.bind.dialect.name == "postgresql" if db.bind is not None else False


def db_exists_claim(kind: str, value: str):
    """A correlated EXISTS over one descriptor claim."""
    return (
        select(VisitDescriptor.id)
        .where(
            VisitDescriptor.event_id == TimelineEvent.id,
            VisitDescriptor.kind == kind.strip().lower(),
            VisitDescriptor.value == value.strip().lower(),
        )
        .exists()
    )


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

    The join to ``event_text`` is unconditional and OUTER: unconditional
    so a result can carry its caption whether or not the query mentioned
    words, outer so a query for "truck yesterday" still finds the trucks
    nobody has captioned. An inner join here would silently restrict
    every search to enriched rows.
    """
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
    # One extra query for the whole page, not one per row: what each skill
    # claimed about these visits, so a result can show "red · van · plate
    # KA01AB1234" and name the skill behind each.
    ids = [h.event.id for h in hits]
    if ids:
        claims: dict[int, list[dict[str, Any]]] = {}
        for d in db.query(VisitDescriptor).filter(VisitDescriptor.event_id.in_(ids)).all():
            claims.setdefault(d.event_id, []).append({
                "kind": d.kind, "value": d.value, "confidence": d.confidence,
                "task": d.source_task, "adapter": d.source_adapter,
            })
        for h in hits:
            h.claims = sorted(claims.get(h.event.id, []), key=lambda c: c["kind"])
    return hits


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
