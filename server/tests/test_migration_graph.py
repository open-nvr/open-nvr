# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The Alembic revision graph is well-formed.

These are cheap structural checks, and they exist because the failure they
catch is silent and expensive. Revision ids here are hand-written in a rolling
pattern (``...aa11bb22cc33 -> bb22cc33dd44 -> cc33dd44ee55...``), so two
branches developed in parallel naturally reach for the same next value. The
filenames differ, so git merges both without a conflict and CI stays green:
a fresh database is built from the ORM models by ``create_all``, never from
the migration chain, so nothing fails until someone upgrades an EXISTING
deployment. There, Alembic sees two heads, refuses to pick one, and the app
skips migrations entirely — every query against the new column then 500s.

That is exactly what reached main once (two migrations both claiming
``aa11bb22cc33``, leaving ``cameras.assignments`` uncreated). One assert would
have stopped it at review time.

Run with:

    cd server && pytest tests/test_migration_graph.py -v
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

SERVER_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def script_dir() -> ScriptDirectory:
    cfg = Config(str(SERVER_ROOT / "alembic.ini"))
    # Absolute, so the test does not depend on the working directory pytest
    # happened to be invoked from.
    cfg.set_main_option("script_location", str(SERVER_ROOT / "migrations"))
    return ScriptDirectory.from_config(cfg)


def test_revision_ids_are_unique(script_dir: ScriptDirectory):
    """No two migrations may claim the same revision id.

    Alembic keys its graph by revision id, so a collision silently drops one
    migration from the chain — it can never be applied, because the id that
    would reach it resolves to the other script.
    """
    seen: dict[str, list[str]] = {}
    for script in script_dir.walk_revisions():
        seen.setdefault(script.revision, []).append(Path(script.path).name)

    collisions = {rev: files for rev, files in seen.items() if len(files) > 1}
    assert not collisions, (
        "Duplicate Alembic revision id(s). Renumber the migration that is not "
        f"yet referenced by any other and re-run: {collisions}"
    )


def test_exactly_one_head(script_dir: ScriptDirectory):
    """A branched graph makes `upgrade head` ambiguous.

    The app refuses to auto-migrate when it finds multiple heads, so a
    deployment silently keeps an old schema against new models.
    """
    heads = script_dir.get_heads()
    assert len(heads) == 1, (
        f"Expected a single migration head, found {len(heads)}: {heads}. "
        "Renumber the stray migration onto the tip, or `alembic merge` them."
    )


def test_loading_the_graph_emits_no_duplicate_warning(script_dir: ScriptDirectory):
    """Belt and braces on the id check.

    Alembic warns rather than raising on a duplicate, and a warning in a
    container log is easy to miss — this turns it into a failing test.
    """
    cfg = Config(str(SERVER_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(SERVER_ROOT / "migrations"))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        reloaded = ScriptDirectory.from_config(cfg)
        list(reloaded.walk_revisions())

    offenders = [str(w.message) for w in caught if "more than once" in str(w.message)]
    assert not offenders, offenders


def test_every_migration_is_reachable_from_base(script_dir: ScriptDirectory):
    """The chain has no orphan: walking down from the head must reach them all.

    A migration whose down_revision points at an id that was later renumbered
    would otherwise sit outside the graph and never run.
    """
    heads = script_dir.get_heads()
    # Don't tuple-unpack: on a branched graph that raises a bare ValueError
    # and buries the actual problem, which test_exactly_one_head names.
    if len(heads) != 1:
        pytest.skip(f"graph is branched ({heads}); see test_exactly_one_head")

    reachable = {s.revision for s in script_dir.walk_revisions("base", heads[0])}
    everything = {s.revision for s in script_dir.walk_revisions()}

    assert reachable == everything, (
        "Migrations not reachable from the single head: "
        f"{sorted(everything - reachable)}"
    )
