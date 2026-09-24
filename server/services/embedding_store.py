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

"""Vectors for visits — stored beside the events, never in a sidecar service.

Frigate settled this question the same way and for the same reason: its
embeddings live in the MAIN database (``sqlite-vec`` as an extension, not
a separate store), because every surveillance query is already filtered —
this camera, this window, this class — and the moment the vectors live
somewhere else you are doing two queries and joining by id, with the
filter applied on the wrong side of the network.

So ``event_embeddings`` is keyed by ``event_id`` and nothing else, and the
candidate set for a similarity search is produced by the SAME predicate
the rest of search uses. One store, one scoping rule.

WHAT THIS IS NOT
----------------

It is not a requirement. OpenNVR runs on a mini-PC and it runs on a
rack, and semantic search must not be the thing that decides which. The
rule throughout is the one the camera-agent's hardware panel already
follows: **detect, recommend, never gate**. No embeddings in the table,
or no adapter advertising ``embed``, means :func:`capability` reports
``none`` and search quietly returns to matching words. Same route, same
response shape, no error, no degraded-mode banner. The site that has a
GPU and an embedding adapter gets a better ranking; the site that does
not gets exactly what it got before.

The same principle applies one level down, to the arithmetic. NumPy is
not a server dependency. If it is importable the scan uses it and the
ceiling is high; if it is not, the scan is pure Python and the ceiling
is lower. Both are correct, one is faster, neither is required.

HOW SIMILARITY IS COMPUTED
--------------------------

Vectors are NORMALISED ON WRITE, so cosine similarity is a plain dot
product at read time. That is not a micro-optimisation — it removes the
per-query square roots from the inner loop, which is where a pure-Python
fallback lives or dies, and it means a vector stored by one adapter and
one stored by another are comparable without knowing either's scale.

Storage is float32 little-endian, packed with :mod:`array`. Portable
across SQLite and Postgres with no extension, no dialect branch, and no
type that a ``pg_dump`` on one machine cannot restore on another.

THE EXTENSION POINT, AND THE TRAP IN IT
---------------------------------------

Today the vector arm SCANS — it loads the filtered candidates and sorts
them. That is honest at the sizes OpenNVR sites actually run, because
the filter comes first and a camera-plus-window candidate set is small.
It stops being honest at fleet scale with no filter, which is exactly
when an ANN index (pgvector HNSW, ``sqlite-vec``) earns its place.

Whoever adds that: the trap is documented, so it does not have to be
rediscovered. A filtered HNSW query returns FEWER ROWS THAN ASKED FOR,
because the traversal collects ``ef_search`` candidates first and applies
the WHERE clause afterwards. Every query in this system is filtered, so
this is not an edge case here, it is the normal case. pgvector 0.8.0's
``hnsw.iterative_scan`` is the documented answer.

The deeper point is the one this module already encodes: a truncated
vector arm and an empty one must never look the same. Ask for 25, get 3,
and "the index gave up early" reads identically to "nothing matched"
unless something says which. :func:`rank_by_similarity` returns
``considered`` and ``truncated`` for that reason, and search carries them
out to the caller.
"""

from __future__ import annotations

import logging
import math
from array import array
from dataclasses import dataclass
from typing import Iterable, Sequence

from sqlalchemy import func as _func

from models import EventEmbedding

logger = logging.getLogger(__name__)

__all__ = [
    "EMBED_TASK",
    "VectorCapability",
    "capability",
    "normalise",
    "pack",
    "put_embedding",
    "rank_by_similarity",
    "unpack",
]

#: The canonical task name an adapter advertises to produce these.
#: Registered in ``server/config/tasks.yml`` like every other task, so it
#: shows up in the skills registry, the adapter catalog and the agent's
#: hardware panel without any of them learning a special case.
EMBED_TASK = "embed"

#: Most candidates a single scan will score, with and without NumPy.
#:
#: These are CEILINGS ON WORK, not correctness limits — crossing one sets
#: ``truncated`` and the caller is told. The pure-Python number is lower
#: because the inner loop is ~40x slower, and a search that takes two
#: seconds to be slightly better ranked is worse than one that took
#: twelve milliseconds to be ranked by words.
#:
#: The ordering that survives truncation is the shared event predicate's
#: — newest first — so a truncated scan scores the most recent
#: candidates rather than an arbitrary page. That is the right bias for
#: surveillance: "have you seen this lately" is the question being asked.
SCAN_CEILING_NUMPY = 20_000
SCAN_CEILING_PYTHON = 2_000


def _numpy():
    """NumPy if this deployment has it, else ``None``. Never raises."""
    try:  # pragma: no cover - trivial, and the absence path is the tested one
        import numpy  # type: ignore
        return numpy
    except Exception:
        return None


# ── packing ──────────────────────────────────────────────────────────


def normalise(vector: Sequence[float]) -> list[float]:
    """Unit-length copy of ``vector``.

    A zero vector normalises to itself rather than raising: an adapter
    that returns zeros is broken, but the enrichment path must not take
    the ingest handler down with it. It will simply never be similar to
    anything, which is the correct behaviour for a vector carrying no
    information.
    """
    values = [float(v) for v in vector]
    norm = math.sqrt(sum(v * v for v in values))
    if norm <= 0.0:
        return values
    return [v / norm for v in values]


def pack(vector: Sequence[float]) -> bytes:
    """float32 little-endian bytes. ``vector`` is normalised first."""
    a = array("f", normalise(vector))
    # Little-endian regardless of the host, so a database file or a dump
    # moves between architectures intact. `byteswap` is a no-op on the
    # machines anyone runs this on; correctness should not depend on that.
    import sys
    if sys.byteorder != "little":  # pragma: no cover - no big-endian CI
        a.byteswap()
    return a.tobytes()


def unpack(blob: bytes) -> list[float]:
    """The inverse of :func:`pack`."""
    a = array("f")
    a.frombytes(blob)
    import sys
    if sys.byteorder != "little":  # pragma: no cover
        a.byteswap()
    return list(a)


# ── writing ──────────────────────────────────────────────────────────


def put_embedding(db, *, event_id: int, vector: Sequence[float],
                  model: str | None = None) -> EventEmbedding | None:
    """Store (or replace) one visit's vector.

    Replaces rather than accumulates: a visit has one embedding at a
    time, and re-running a better adapter over old footage should
    improve the store rather than double it. Which adapter produced the
    current one is recorded in ``model``, so a re-run can be targeted
    and a mixed-model store can be spotted — see :func:`capability`.

    Returns ``None`` for an empty vector instead of writing a row that
    can never match anything.
    """
    values = [float(v) for v in (vector or [])]
    if not values:
        return None

    blob = pack(values)
    row = db.get(EventEmbedding, int(event_id))
    if row is None:
        row = EventEmbedding(event_id=int(event_id))
        db.add(row)
    row.vector = blob
    row.dim = len(values)
    row.model = (model or "")[:120] or None
    db.commit()
    return row


# ── capability ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class VectorCapability:
    """What this deployment can do about semantic search, right now.

    Shaped like the camera-agent's ``hardware_recommendation()`` on
    purpose: a fact, a reason, and a sentence a UI can show — never a
    switch that turns a feature off.
    """

    #: Can a vector arm run at all? False means search matches words,
    #: which is what it did before any of this existed.
    available: bool
    #: ``"scan"`` (filter first, score in process) or ``"none"``.
    mode: str
    #: Dimensionality of the stored vectors, when they agree.
    dim: int | None
    #: How many visits carry one.
    rows: int
    #: Distinct producers in the store. More than one is worth surfacing:
    #: vectors from different models are NOT comparable, and a store that
    #: has been re-enriched halfway will rank incoherently until the
    #: backfill finishes.
    models: tuple[str, ...]
    #: ``"numpy"`` or ``"python"`` — which inner loop, hence which ceiling.
    accel: str
    #: Most candidates one query will score before truncating.
    ceiling: int
    #: One sentence, for an operator, about what is and is not switched on.
    note: str

    @property
    def mixed_models(self) -> bool:
        return len(self.models) > 1


def capability(db) -> VectorCapability:
    """Detect, do not require.

    Cheap enough to call per request: one COUNT and one small DISTINCT
    over a table keyed by event_id. It deliberately asks the STORE what
    is possible rather than asking config what is enabled — a setting
    that says embeddings are on, over a table with no rows in it, would
    produce a vector arm that silently matches nothing.
    """
    np = _numpy()
    accel = "numpy" if np is not None else "python"
    ceiling = SCAN_CEILING_NUMPY if np is not None else SCAN_CEILING_PYTHON

    try:
        rows = int(db.query(_func.count(EventEmbedding.event_id)).scalar() or 0)
    except Exception:  # table absent (a database older than the migration)
        return VectorCapability(
            available=False, mode="none", dim=None, rows=0, models=(),
            accel=accel, ceiling=ceiling,
            note="No embedding store — search matches words only.")

    if rows == 0:
        return VectorCapability(
            available=False, mode="none", dim=None, rows=0, models=(),
            accel=accel, ceiling=ceiling,
            note=("No visit has been embedded yet — search matches words "
                  "only. Register an adapter advertising the "
                  f"'{EMBED_TASK}' task to add semantic ranking."))

    dims = [int(d) for (d,) in db.query(EventEmbedding.dim).distinct().all() if d]
    models = tuple(sorted(m for (m,) in db.query(EventEmbedding.model)
                          .distinct().all() if m))

    note = (f"Semantic ranking on, over {rows:,} embedded visits "
            f"({accel} scan).")
    if len(dims) > 1:
        # Vectors of different lengths cannot be compared at all. Say so
        # loudly; this is a misconfiguration, not a tuning question.
        note = (f"Embedding store holds {len(dims)} different vector sizes "
                f"({', '.join(str(d) for d in sorted(dims))}) — these are "
                f"not comparable. Re-run the backfill with one adapter.")
    elif len(models) > 1:
        note += (f" Mixed producers ({', '.join(models)}) — ranking is only "
                 f"coherent once one adapter has covered the store.")

    return VectorCapability(
        available=True, mode="scan", dim=dims[0] if len(dims) == 1 else None,
        rows=rows, models=models, accel=accel, ceiling=ceiling, note=note)


# ── reading ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SimilarityResult:
    """Ranked ids, and enough context to know what the ranking is worth."""

    #: Event ids, most similar first. At most ``limit`` of them.
    ids: list[int]
    #: Similarity per id, same order. Exposed rather than hidden because
    #: the fusion downstream uses RANKS, and an operator debugging a bad
    #: result needs to see whether the winner scored 0.9 or 0.11.
    scores: list[float]
    #: How many candidates were actually scored.
    considered: int
    #: True when the candidate set was larger than the ceiling and the
    #: tail was never looked at. THE field that stops "the scan gave up"
    #: reading exactly like "nothing is similar".
    truncated: bool


def rank_by_similarity(db, *, query_vector: Sequence[float],
                       candidate_ids: Iterable[int],
                       limit: int = 50,
                       cap: "VectorCapability | None" = None) -> SimilarityResult:
    """Rank ``candidate_ids`` by similarity to ``query_vector``.

    ``candidate_ids`` has already been narrowed by the shared event
    predicate — camera, window, class, plate, claims, and the caller's
    scope. Filtering BEFORE scoring is the whole reason this is viable
    in process, and it is also the thing an ANN index makes hard: an
    HNSW traversal filters after it has chosen its candidates, which is
    how a filtered query quietly returns three rows when asked for
    twenty-five. Here the filter is exact and the truncation is
    reported.

    A candidate with no stored vector, or one of a different length than
    the query, is skipped rather than scored as zero. Zero is a real
    similarity — orthogonal — and a dimension mismatch is a
    misconfiguration; conflating the two would bury the second inside
    the first.
    """
    ids = list(dict.fromkeys(int(i) for i in candidate_ids))
    if not ids or not query_vector:
        return SimilarityResult(ids=[], scores=[], considered=0, truncated=False)

    # The caller usually has one already. capability() is a COUNT plus
    # two DISTINCTs, and the search path was asking for it three times
    # per query — once to decide whether to embed the text at all, once
    # in search_page, and once here.
    cap = cap or capability(db)
    ceiling = cap.ceiling
    truncated = len(ids) > ceiling
    ids = ids[:ceiling]

    q = normalise(query_vector)
    qdim = len(q)

    rows = (
        db.query(EventEmbedding.event_id, EventEmbedding.vector, EventEmbedding.dim)
        .filter(EventEmbedding.event_id.in_(ids))
        .all()
    )
    usable = [(int(eid), blob) for eid, blob, dim in rows
              if blob and int(dim or 0) == qdim]
    if not usable:
        return SimilarityResult(ids=[], scores=[], considered=0,
                                truncated=truncated)

    scored = _score(usable, q, qdim)
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    top = scored[:max(1, int(limit))]
    return SimilarityResult(
        ids=[eid for eid, _ in top],
        scores=[float(s) for _, s in top],
        considered=len(usable),
        truncated=truncated,
    )


def _score(usable: list[tuple[int, bytes]], q: Sequence[float],
           qdim: int) -> list[tuple[int, float]]:
    """Dot product per candidate. Both vectors are unit-length, so this
    IS cosine similarity — no norms in the inner loop."""
    np = _numpy()
    if np is not None:
        qv = np.asarray(q, dtype=np.float32)
        mat = np.frombuffer(b"".join(blob for _, blob in usable),
                            dtype="<f4").reshape(len(usable), qdim)
        sims = mat @ qv
        return [(eid, float(s)) for (eid, _), s in zip(usable, sims)]

    # Pure Python. `zip` + a generator beats indexing by enough to matter
    # at these sizes, which is why the ceiling is 2,000 and not 200.
    out: list[tuple[int, float]] = []
    for eid, blob in usable:
        v = unpack(blob)
        out.append((eid, sum(a * b for a, b in zip(q, v))))
    return out
