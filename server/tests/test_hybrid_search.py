# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Two arms, fused — and the site with one arm loses nothing.

The point of this file is the second half of that sentence. Semantic
ranking is an improvement available to a deployment that has an
embedding adapter and the hardware to run one; it must never be the
thing that decides whether OpenNVR works. So the cases that matter most
here are the NEGATIVE ones: no embeddings, no query vector, a store
half-embedded, vectors of the wrong size. Every one of them has to come
back as ordinary word search with no error and the same response shape.

The positive cases pin the two properties that make fusion worth having
at all:

* the vector arm searches the structural candidate set WITHOUT the text
  predicate, so it can surface a visit the words could never have found;
* a truncated scan is distinguishable from an empty one, because "the
  scan stopped early" and "nothing was similar" are different facts and
  a caller that cannot tell them apart will misreport both.
"""
from __future__ import annotations

import os
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("INTERNAL_API_KEY", "site_" + secrets.token_hex(16))
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_hybrid_test.db")

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.database import Base  # noqa: E402
from models import (Camera, EventEmbedding, EventText, Role,  # noqa: E402
                    TimelineEvent, User)
from services import embedding_store  # noqa: E402
from services.search_service import (ARM_DEPTH, RRF_K, _rrf,  # noqa: E402
                                     search_page)

T0 = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)

#: Small on purpose. Dimensionality is not what any of this is testing,
#: and a 512-wide fixture makes every failure unreadable.
DIM = 4


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", future=True, poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Role(id=1, name="admin", description="t"))
    s.commit()
    s.add(User(id=1, username="o", email="o@x.t", hashed_password="x",
               is_active=True, role_id=1))
    s.commit()
    for cid, name in ((1, "Yard"), (2, "Gate")):
        s.add(Camera(id=cid, name=name, ip_address=f"10.0.0.{cid}",
                     rtsp_url=f"rtsp://x/{cid}", owner_id=1))
    s.commit()
    yield s
    s.close()


def _visit(db, *, cam=1, start_s=0, label="truck", caption=None, vec=None,
           model="test-clip"):
    row = TimelineEvent(
        camera_id=cam, label=label, event_type="visit", source="tier0",
        started_at=T0 + timedelta(seconds=start_s),
        ended_at=T0 + timedelta(seconds=start_s + 20))
    db.add(row)
    db.commit()
    if caption is not None:
        db.add(EventText(event_id=row.id, caption=caption,
                         attributes=caption, source="test"))
        db.commit()
    if vec is not None:
        embedding_store.put_embedding(db, event_id=row.id, vector=vec, model=model)
    return row


# ── the arithmetic ───────────────────────────────────────────────────


def test_rrf_rewards_agreement_over_a_single_strong_placing():
    """The property that makes RRF the right default.

    B is second in both arms. A is first in one and absent from the
    other. Two second places beat one first place, which is what you
    want when the arms measure different things and neither is
    authoritative.
    """
    scores, places = _rrf({"text": [1, 2], "vector": [3, 2]})

    assert scores[2] > scores[1], "agreement across arms did not win"
    assert scores[2] > scores[3]
    assert places[2] == {"text": 2, "vector": 2}


def test_rrf_records_absence_as_none_not_as_last_place():
    """`None` and "ranked worst" are different, and fusion must not
    quietly turn one into the other — a row an arm never returned got
    nothing from it, and a person reading the ranks needs to see that."""
    _, places = _rrf({"text": [1], "vector": [2]})

    assert places[1] == {"text": 1, "vector": None}
    assert places[2] == {"text": None, "vector": 1}


def test_rrf_k_is_the_published_default():
    """Pinned so a well-meaning tune has to argue with a test first."""
    assert RRF_K == 60


# ── the deployment with no vectors, which must lose nothing ──────────


def test_no_embeddings_means_ordinary_word_search(db):
    """THE case. A site with no embedding adapter gets what it always
    got: results, no error, and no second arm."""
    _visit(db, caption="a red truck at the dock")
    _visit(db, caption="a white van by the gate", start_s=60)

    page = search_page(db, text="truck", scope=None)

    assert [h.event.id for h in page.hits] == [1]
    assert page.total == 1
    assert page.semantic is None, "a vector arm was reported where none ran"


def test_a_query_vector_against_an_empty_store_is_reported_not_failed(db):
    """The caller had a vector; the deployment had nothing to compare it
    to. That is an ordinary state, not an error — but it is SAID, because
    "semantic search did nothing" and "semantic search found nothing"
    are different facts that produce identical result lists."""
    _visit(db, caption="a red truck at the dock")

    page = search_page(db, text="truck", query_vector=[1.0, 0, 0, 0], scope=None)

    assert [h.event.id for h in page.hits] == [1], "word search stopped working"
    assert page.semantic is not None
    assert page.semantic["used"] is False
    assert page.semantic["reason"] == "no-embeddings"
    assert page.semantic["note"], "an operator was told nothing actionable"


def test_capability_reports_off_without_pretending_it_is_broken(db):
    cap = embedding_store.capability(db)

    assert cap.available is False
    assert cap.mode == "none"
    assert "embed" in cap.note, "the note does not say what would turn it on"


# ── the second arm earning its place ─────────────────────────────────


def test_the_vector_arm_finds_a_visit_the_words_could_not(db):
    """The only reason to have a second arm.

    One visit is captioned "a lorry at the loading bay" and never uses
    the word the operator typed. Its vector is the closest to the query.
    Word search alone returns nothing; fusion returns it.
    """
    lorry = _visit(db, caption="a lorry at the loading bay", vec=[1.0, 0, 0, 0])
    _visit(db, caption="a cyclist on the path", start_s=60, vec=[0, 0, 0, 1.0])

    words_only = search_page(db, text="truck", scope=None)
    assert words_only.hits == [], "fixture is wrong — the word matched something"

    fused = search_page(db, text="truck", query_vector=[0.99, 0.01, 0, 0],
                        scope=None)

    assert lorry.id in [h.event.id for h in fused.hits], (
        "the vector arm was restricted to rows the words already matched, "
        "which makes it incapable of adding anything")
    assert fused.semantic["added_by_vector"] >= 1


def test_the_vector_arm_still_obeys_the_camera_scope(db):
    """The second arm is built from the same predicate as the first.

    A scoped caller must not be able to reach another camera's footage
    by handing over a vector instead of a word.
    """
    _visit(db, cam=1, caption="mine", vec=[1.0, 0, 0, 0])
    theirs = _visit(db, cam=2, caption="theirs", start_s=60, vec=[1.0, 0, 0, 0])

    page = search_page(db, text="", query_vector=[1.0, 0, 0, 0], scope={1})

    ids = [h.event.id for h in page.hits]
    assert theirs.id not in ids, "a vector query crossed the camera scope"


def test_the_vector_arm_obeys_the_time_window(db):
    """Structural filters apply before similarity, not after. This is the
    half that an ANN index gets wrong by default, and the reason the
    scan path is not merely a poor relation of one."""
    old = _visit(db, start_s=0, caption="old", vec=[1.0, 0, 0, 0])
    recent = _visit(db, start_s=6000, caption="recent", vec=[1.0, 0, 0, 0])

    page = search_page(db, text="", query_vector=[1.0, 0, 0, 0],
                       from_=T0 + timedelta(seconds=3000), scope=None)

    ids = [h.event.id for h in page.hits]
    assert recent.id in ids
    assert old.id not in ids, "similarity outranked the window filter"


def test_a_row_both_arms_found_outranks_one_only_the_words_found(db):
    """Agreement wins, even against a better placing in one arm.

    The un-embedded row is NEWER, so on SQLite — where the word arm is
    unranked and orders newest-first — it takes first place in the only
    arm it appears in. The embedded row places second there and first in
    the vector arm, and two placings beat one.

    That asymmetry is the fixture's whole point: both rows in both arms
    would be a symmetric tie, which measures nothing.
    """
    both = _visit(db, caption="a red truck", vec=[1.0, 0, 0, 0])
    words_only = _visit(db, caption="a truck", start_s=60)   # no vector

    page = search_page(db, text="truck", query_vector=[1.0, 0, 0, 0], scope=None)

    assert page.hits[0].event.id == both.id, (
        "a row only one arm found outranked a row both arms found")
    assert page.hits[0].ranks == {"vector": 1, "text": 2}
    assert page.hits[1].event.id == words_only.id
    assert page.hits[1].ranks == {"vector": None, "text": 1}


def test_an_exact_tie_breaks_towards_the_newer_visit(db):
    """Two rows fused to the same score is a real outcome, not an error,
    and something has to decide. Recency is the honest tie-break for
    surveillance: asked twice, the store gives the same answer, and the
    answer prefers the thing that happened most recently.
    """
    older = _visit(db, caption="a truck", vec=[1.0, 0, 0, 0])
    newer = _visit(db, caption="a truck", start_s=60, vec=[0, 1.0, 0, 0])

    page = search_page(db, text="truck", query_vector=[1.0, 0, 0, 0], scope=None)

    assert page.hits[0].score == page.hits[1].score, "fixture is not a tie"
    assert page.hits[0].event.id == newer.id
    assert page.hits[1].event.id == older.id


# ── truncation must not look like emptiness ──────────────────────────


def test_a_truncated_scan_says_so(db, monkeypatch):
    """The failure this field is here to prevent: ask for 25, get 3, and
    read it as "nothing matched" when it was really "the scan stopped".

    The ceiling is forced down rather than seeding 20,000 rows — the
    number is not what is being tested, the REPORTING is.
    """
    monkeypatch.setattr(embedding_store, "SCAN_CEILING_NUMPY", 2)
    monkeypatch.setattr(embedding_store, "SCAN_CEILING_PYTHON", 2)
    for i in range(6):
        _visit(db, start_s=i * 60, caption=f"visit {i}", vec=[1.0, 0, 0, 0])

    page = search_page(db, text="", query_vector=[1.0, 0, 0, 0], scope=None)

    assert page.semantic["truncated"] is True
    assert page.semantic["considered"] == 2
    assert page.semantic["ceiling"] == 2


def test_an_untruncated_scan_says_that_too(db):
    """The other half — `truncated` must be a real signal, which means it
    has to be False when nothing was cut."""
    for i in range(3):
        _visit(db, start_s=i * 60, caption=f"visit {i}", vec=[1.0, 0, 0, 0])

    page = search_page(db, text="", query_vector=[1.0, 0, 0, 0], scope=None)

    assert page.semantic["truncated"] is False
    assert page.semantic["considered"] == 3


# ── a half-embedded or mis-embedded store ────────────────────────────


def test_visits_without_a_vector_are_not_scored_as_zero(db):
    """Zero is a real similarity — orthogonal. A visit nobody embedded
    has no similarity at all, and lumping the two together would rank an
    un-enriched visit as actively dissimilar rather than unknown."""
    embedded = _visit(db, caption="embedded", vec=[1.0, 0, 0, 0])
    _visit(db, caption="never embedded", start_s=60)

    page = search_page(db, text="", query_vector=[1.0, 0, 0, 0], scope=None)

    assert page.semantic["considered"] == 1
    assert [h.event.id for h in page.hits] == [embedded.id]


def test_a_dimension_mismatch_is_skipped_not_scored(db):
    """Vectors from two different models are not comparable. Scoring
    across them would produce a confident ordering of meaningless
    numbers, which is worse than returning less."""
    right = _visit(db, caption="right size", vec=[1.0, 0, 0, 0])
    _visit(db, caption="wrong size", start_s=60, vec=[1.0, 0, 0, 0, 0, 0])

    page = search_page(db, text="", query_vector=[1.0, 0, 0, 0], scope=None)

    assert [h.event.id for h in page.hits] == [right.id]
    assert page.semantic["considered"] == 1


def test_mixed_vector_sizes_are_called_out_in_the_capability_note(db):
    """A half-migrated store ranks incoherently, and the symptom is
    subtle. The table can say so directly, so it does."""
    _visit(db, caption="a", vec=[1.0, 0, 0, 0], model="old")
    _visit(db, caption="b", start_s=60, vec=[1.0, 0, 0, 0, 0, 0], model="new")

    cap = embedding_store.capability(db)

    assert cap.dim is None, "two vector sizes were reported as one"
    assert "not comparable" in cap.note


def test_mixed_producers_of_the_same_size_are_a_warning_not_an_error(db):
    _visit(db, caption="a", vec=[1.0, 0, 0, 0], model="old")
    _visit(db, caption="b", start_s=60, vec=[0, 1.0, 0, 0], model="new")

    cap = embedding_store.capability(db)

    assert cap.available is True, "a mixed store was switched off entirely"
    assert cap.mixed_models is True
    assert "Mixed producers" in cap.note


# ── the storage layer's own promises ─────────────────────────────────


def test_vectors_are_normalised_on_write_so_similarity_is_a_dot_product(db):
    row = _visit(db, caption="x", vec=[3.0, 4.0, 0, 0])

    stored = embedding_store.unpack(db.get(EventEmbedding, row.id).vector)

    assert abs(sum(v * v for v in stored) - 1.0) < 1e-5
    assert abs(stored[0] - 0.6) < 1e-5


def test_re_embedding_replaces_rather_than_accumulates(db):
    row = _visit(db, caption="x", vec=[1.0, 0, 0, 0], model="old")
    embedding_store.put_embedding(db, event_id=row.id, vector=[0, 1.0, 0, 0],
                                  model="new")

    rows = db.query(EventEmbedding).filter_by(event_id=row.id).all()
    assert len(rows) == 1
    assert rows[0].model == "new"
    assert embedding_store.unpack(rows[0].vector)[1] == pytest.approx(1.0)


def test_an_empty_vector_writes_no_row(db):
    """A row that can never match is not a record of anything, and its
    presence would make "which visits still need embedding" wrong."""
    row = _visit(db, caption="x")

    assert embedding_store.put_embedding(db, event_id=row.id, vector=[]) is None
    assert db.get(EventEmbedding, row.id) is None


def test_a_zero_vector_is_stored_without_dividing_by_zero(db):
    """An adapter returning zeros is broken. Taking the ingest path down
    with it is not the correct response; being similar to nothing is."""
    row = _visit(db, caption="x")
    embedding_store.put_embedding(db, event_id=row.id, vector=[0.0] * DIM)

    page = search_page(db, text="", query_vector=[1.0, 0, 0, 0], scope=None)
    assert page.semantic["used"] is True
    assert [h.event.id for h in page.hits] == [row.id]
    assert page.hits[0].score >= 0.0


# ── paging and ordering ──────────────────────────────────────────────


def test_the_fused_order_survives_the_hydrating_query(db):
    """`IN (...)` returns rows in whatever order the planner likes. The
    ranking is decided by fusion, so the mapping back is done in Python
    rather than hoped for."""
    for i in range(5):
        _visit(db, start_s=i * 60, caption=f"v{i}",
               vec=[1.0 - i * 0.2, i * 0.2, 0, 0])

    page = search_page(db, text="", query_vector=[1.0, 0, 0, 0], scope=None,
                       limit=5)

    scores = [h.score for h in page.hits]
    assert scores == sorted(scores, reverse=True), "fused order was lost"


def test_total_is_what_the_caller_can_actually_page_through(db):
    """`total` has to describe the same set as `hits`.

    The vector arm returns rows the text predicate does not match — the
    whole point of it — so reporting the WORD count as `total` produced
    a response that contradicted itself: one result, `total: 0`, and a
    "why did nothing match" block, all about the same page.

    The addressable set is the fused pool, so that is `total`. The word
    count is still reported, as `text_total`, because the gap between
    them is how a reader sees the second arm doing something.
    """
    _visit(db, caption="a red truck", vec=[1.0, 0, 0, 0])
    _visit(db, caption="a lorry", start_s=60, vec=[0.99, 0.01, 0, 0])

    page = search_page(db, text="truck", query_vector=[1.0, 0, 0, 0], scope=None)

    assert page.total == len(page.hits) == 2, (
        "total describes a different set than the hits it came with")
    assert page.semantic["text_total"] == 1
    assert page.semantic["added_by_vector"] == 1


def test_total_is_untouched_when_only_the_word_arm_runs(db):
    """The deployment with no embeddings reads exactly the number it
    always read."""
    _visit(db, caption="a red truck")
    _visit(db, caption="a lorry", start_s=60)

    page = search_page(db, text="truck", scope=None)

    assert page.total == 1
    assert page.semantic is None


def test_a_deep_page_is_reachable_once_a_second_arm_exists(db):
    """Fusion can only return rows that appeared in an arm, so the pool
    IS the addressable set. With a fixed 50-per-arm depth the pool
    capped at 100 rows, and `skip=100` returned an EMPTY page in the
    middle of a match set — while `total` said otherwise and the same
    query with no embeddings returned rows.

    Turning semantic ranking on must not break paging.
    """
    # The page asked for has to sit beyond what a FIXED pool could
    # reach, and that takes more care than it looks.
    #
    # A first version used skip=30 with ARM_DEPTH=50, so the old
    # constant covered the page anyway. A second version reached past
    # 50, but gave the arms OPPOSITE orderings — words newest-first,
    # vectors oldest-first — so their union covered 75 of 75 rows and
    # the page was reachable regardless. Both passed against the bug.
    #
    # Here the two arms AGREE: similarity rises with recency, and on
    # SQLite the word arm is newest-first too. The union is therefore
    # one arm deep, which is the worst case and the one that has to
    # work.
    skip, limit = ARM_DEPTH + 5, 10
    assert skip + limit > ARM_DEPTH, "fixture does not reach past a fixed pool"
    n = skip + limit + 10
    for i in range(n):
        _visit(db, start_s=i * 60, caption=f"truck {i}",
               vec=[0.1 + i * 0.9 / n, 1.0 - i * 0.9 / n, 0, 0])

    deep = search_page(db, text="truck", query_vector=[1.0, 0, 0, 0],
                       scope=None, skip=skip, limit=limit)

    assert len(deep.hits) == limit, (
        f"a page at skip={skip} returned {len(deep.hits)} rows — the arms "
        f"are not going deep enough to cover the page being asked for")


def test_pages_do_not_overlap_or_skip_rows(db):
    """The other half of paging working: walking the pool page by page
    visits every row exactly once."""
    for i in range(12):
        _visit(db, start_s=i * 60, caption=f"truck {i}",
               vec=[1.0 - i * 0.05, i * 0.05, 0, 0])

    seen = []
    for skip in (0, 5, 10):
        page = search_page(db, text="truck", query_vector=[1.0, 0, 0, 0],
                           scope=None, skip=skip, limit=5)
        seen.extend(h.event.id for h in page.hits)

    assert len(seen) == len(set(seen)) == 12, (
        f"paging returned {len(seen)} rows, {len(set(seen))} distinct, "
        f"for 12 visits")


def test_an_exhausted_pool_says_so(db):
    """The pool ending because the ARMS stopped is different from it
    ending because the store did, and a caller paging through needs to
    be able to tell."""
    for i in range(6):
        _visit(db, start_s=i * 60, caption=f"truck {i}",
               vec=[1.0 - i * 0.1, i * 0.1, 0, 0])

    small = search_page(db, text="truck", query_vector=[1.0, 0, 0, 0],
                        scope=None, skip=0, limit=1)
    assert small.semantic["depth"] >= ARM_DEPTH

    roomy = search_page(db, text="truck", query_vector=[1.0, 0, 0, 0],
                        scope=None, limit=50)
    assert roomy.semantic["exhausted"] is False, (
        "a pool that comfortably held every match reported as exhausted")


def test_arm_depth_bounds_the_pool_not_the_page(db):
    """Fusion reorders a pool. A pool the size of the page would have
    nothing to reorder, so the arms go deeper than the caller's limit."""
    assert ARM_DEPTH > 1
    for i in range(8):
        _visit(db, start_s=i * 60, caption=f"truck {i}",
               vec=[1.0 - i * 0.1, i * 0.1, 0, 0])

    page = search_page(db, text="truck", query_vector=[1.0, 0, 0, 0],
                       scope=None, limit=3)

    assert len(page.hits) == 3
    assert page.semantic["arms"]["text"] > 3, (
        "the word arm was cut to the page size and fusion had nothing to do")


# ── the two inner loops must agree ───────────────────────────────────


def test_the_numpy_and_pure_python_scans_produce_the_same_ranking(db):
    """NumPy is an optimisation, not a second implementation.

    A deployment with NumPy and one without must rank identically, or
    the "adaptive" story is really two behaviours wearing one name. This
    forces both branches over the same data and compares them.

    Skipped rather than failed where NumPy is absent: this asserts the
    two AGREE, and with only one of them available there is nothing to
    compare. The pure-Python branch is covered by every other test in
    this file, which is the branch that always exists.
    """
    numpy = pytest.importorskip("numpy")
    assert numpy is not None

    import itertools
    for i, (a, b) in enumerate(itertools.islice(
            itertools.product([0.1, 0.5, 0.9], repeat=2), 9)):
        _visit(db, start_s=i * 60, caption=f"v{i}", vec=[a, b, b - a, a * b])

    ids = [row.id for row in db.query(TimelineEvent).all()]
    q = [0.7, 0.2, 0.1, 0.4]

    with_numpy = embedding_store.rank_by_similarity(
        db, query_vector=q, candidate_ids=ids, limit=20)

    # Force the fallback by making the import fail, exactly as it would
    # on a box without NumPy installed.
    import services.embedding_store as es
    original = es._numpy
    es._numpy = lambda: None
    try:
        pure = es.rank_by_similarity(
            db, query_vector=q, candidate_ids=ids, limit=20)
    finally:
        es._numpy = original

    assert with_numpy.ids == pure.ids, "the two scans ranked differently"
    for a, b in zip(with_numpy.scores, pure.scores):
        assert a == pytest.approx(b, abs=1e-5), "the two scans scored differently"


# ── the join shape decides whether the index is reachable ────────────
#
# The GIN index on event_text had never been used by any Postgres
# deployment. The expression matched the migration exactly — which is
# what the migration's comment warned about and what the suite checked —
# but the join was OUTER, and with COALESCE the text predicate is not
# strict: an unmatched row yields `to_tsvector('') @@ q`, which is FALSE,
# not NULL. Postgres therefore cannot prove the outer join is equivalent
# to an inner one, cannot push the filter down to event_text, and
# re-evaluates to_tsvector as a join filter over the whole table.
#
# Measured on 20,000 visits, same statistics either way:
#     LEFT JOIN  141ms   Seq Scan on event_text
#     JOIN         8.6ms Bitmap Index Scan on ix_event_text_fts
#
# Nothing caught it because the suite runs on SQLite, where there is no
# such index to fail to use. So what is pinned here is the JOIN SHAPE
# itself, which is dialect-independent and therefore testable anywhere.


def _compiled(db, **kwargs) -> str:
    from services.search_service import _base
    q = _base(db, filters={"scope": None}, labels=None, camera_ids=None,
              attrs=None, **kwargs)
    return str(q.with_entities(TimelineEvent.id).statement).replace("\n", " ")


def test_a_text_query_joins_event_text_inline_so_the_index_is_reachable(db):
    sql = _compiled(db, text="red truck")

    assert "LEFT OUTER JOIN event_text" not in sql, (
        "the text search is back on an outer join — on Postgres that "
        "makes ix_event_text_fts unreachable and the query a seq scan")
    assert "JOIN event_text" in sql


def test_a_query_with_no_words_still_outer_joins(db):
    """The other half, and the reason the join was outer to begin with:
    "trucks yesterday" has to find the trucks nobody captioned. An inner
    join here would silently restrict every search to enriched rows."""
    sql = _compiled(db, text="")

    assert "LEFT OUTER JOIN event_text" in sql


def test_switching_to_an_inner_join_changes_no_result(db):
    """Equivalence, asserted rather than argued.

    A visit with no event_text row has no words, so it cannot match a
    query for words — the join change is a plan change, not a semantic
    one. This pins that an un-captioned visit is absent from a text
    search and present in a wordless one.
    """
    captioned = _visit(db, caption="a red truck at the dock")
    bare = _visit(db, start_s=60, label="truck")          # no event_text

    words = search_page(db, text="truck", scope=None)
    assert [h.event.id for h in words.hits] == [captioned.id]

    no_words = search_page(db, labels=["truck"], scope=None)
    assert {h.event.id for h in no_words.hits} == {captioned.id, bare.id}, (
        "an un-captioned visit fell out of a search that never mentioned "
        "words — the inner join leaked into the wordless path")


# ── the count is paid for once ───────────────────────────────────────


def test_a_supplied_total_is_not_counted_again(db, monkeypatch):
    """The operator route times its COUNT separately, because that timer
    is what opennvr_search_count_seconds is for: an exact total is the
    one cost this API added over the old app store, and an operator has
    to be able to see it to decide whether to keep it.

    So the route counts first and hands the answer over. When it does,
    search_page must not count again — that made every operator search
    run the COUNT twice, which was invisible precisely because both
    calls returned the same right answer.
    """
    import services.search_service as ss

    calls = []
    real = ss.count_search_events
    monkeypatch.setattr(ss, "count_search_events",
                        lambda *a, **kw: (calls.append(1), real(*a, **kw))[1])

    _visit(db, caption="a red truck")

    page = ss.search_page(db, text="truck", total=7, scope=None)

    assert calls == [], "the total was supplied and counted anyway"
    assert page.total == 7


def test_no_total_supplied_still_counts_once(db, monkeypatch):
    """The convenience path stays convenient — and counts exactly once,
    not zero times and not twice."""
    import services.search_service as ss

    calls = []
    real = ss.count_search_events
    monkeypatch.setattr(ss, "count_search_events",
                        lambda *a, **kw: (calls.append(1), real(*a, **kw))[1])

    _visit(db, caption="a red truck")

    page = ss.search_page(db, text="truck", scope=None)

    assert len(calls) == 1, f"counted {len(calls)} times"
    assert page.total == 1
