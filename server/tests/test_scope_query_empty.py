# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scope=set()`` matches nothing; ``scope=None`` matches everything.

Two sentinels one character apart in a type annotation
(``set[int] | None``) with opposite meanings, and the wrong one is a
site-wide footage leak from the narrowest principal in the system. Every
scoped query in the server — timeline, search, stats, evidence — reaches
this one function, so this is where the property belongs.

It is written here because of how it was found, and the finding is
worth writing down: the empty case is defended THREE times over, and
the two visible defences are both spares.

The app-search route carried a short-circuit for the empty roster whose
comment claimed it was what stopped the leak. Deleting it failed
nothing. Deleting ``scope_query``'s ``in_([-1])`` branch as well —
leaving ``camera_column.in_(sorted(set()))`` — failed nothing either,
because SQLAlchemy 2.x compiles an empty ``IN`` to a false predicate:
no rows, no warning. There was never a version of this code that
leaked.

Which leaves an honest reason to keep the explicit branch rather than a
dramatic one. It is not holding the door; it is saying out loud which
way the door swings, so that the site-wide footage-scoping invariant
does not rest silently on how one library chooses to render a
degenerate expression. Library behaviour in degenerate cases is exactly
what changes quietly across a major version, and "every scoped query in
the server" is not a blast radius to discover that in.

So what is pinned below is the BEHAVIOUR — empty means nothing, absent
means everything — not any one line that produces it. A mutation that
deletes a single layer survives these tests, correctly: the mutated
code is still right. Only making the two sentinels agree fails them,
which is the change that would actually be wrong.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.camera_scope import scope_query  # noqa: E402

Base = declarative_base()


class _Row(Base):
    """A stand-in for any camera-keyed table. The point under test is
    the predicate, not the schema it lands on."""

    __tablename__ = "scoped_rows"
    id = Column(Integer, primary_key=True)
    camera_id = Column(Integer, nullable=False)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", future=True, poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    for cid in (1, 2, 3):
        session.add(_Row(camera_id=cid))
    session.commit()
    yield session
    session.close()


def _ids(q):
    return sorted(r.camera_id for r in q.all())


def test_an_empty_scope_matches_nothing(db):
    """The failure that matters, stated once, here.

    An app with no cameras assigned, a user whose last share was
    revoked, a token scoped to a camera that has since been deleted —
    all of them arrive as an empty set, and all of them must see an
    empty page.
    """
    assert _ids(scope_query(db.query(_Row), _Row.camera_id, set())) == []


def test_no_scope_means_unrestricted(db):
    """``None`` is the platform-component case: not an absence of
    scoping to be filled in later, but the decision that there is none."""
    assert _ids(scope_query(db.query(_Row), _Row.camera_id, None)) == [1, 2, 3]


def test_a_scope_restricts_to_itself(db):
    assert _ids(scope_query(db.query(_Row), _Row.camera_id, {1, 3})) == [1, 3]


def test_an_id_outside_the_scope_is_not_reachable(db):
    """Sorting the set for the IN clause must not let anything in that
    was not in it."""
    assert _ids(scope_query(db.query(_Row), _Row.camera_id, {2})) == [2]


def test_an_empty_scope_is_filtered_explicitly_not_incidentally(db):
    """A WHERE clause must appear for the empty case.

    Not a style point. If ``scope_query`` ever returns the query
    untouched for an empty scope, every row is reachable and the only
    thing standing between an app with no cameras and the whole site is
    whatever the caller happens to do next. This is the assertion that
    would have failed if the emptiness had been left to fall out of the
    caller rather than decided here.
    """
    compiled = str(scope_query(db.query(_Row), _Row.camera_id, set()))
    assert "WHERE" in compiled.upper(), (
        "an empty scope produced an unfiltered query; every scoped route "
        "in the server now depends on its caller to be careful")


def test_the_two_sentinels_do_not_agree(db):
    """The whole invariant in one line, so a refactor that makes
    ``set()`` behave like ``None`` fails loudly rather than widening
    every scoped query in the server at once."""
    empty = _ids(scope_query(db.query(_Row), _Row.camera_id, set()))
    unrestricted = _ids(scope_query(db.query(_Row), _Row.camera_id, None))
    assert empty != unrestricted
    assert empty == []
