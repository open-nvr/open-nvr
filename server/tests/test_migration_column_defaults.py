# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""A server_default that is valid on SQLite and invalid on Postgres.

THE BUG THIS EXISTS FOR was live and unnoticed. Migration
``e8a1b2c3d4f5`` added::

    sa.Column("overlay_enabled", sa.Boolean(), nullable=False,
              server_default=sa.text("0"))

SQLite has no boolean type, so ``0`` is an integer it stores in a column
it treats as numeric anyway, and the migration ran. Postgres — the
dialect every real deployment uses — refuses it::

    column "overlay_enabled" is of type boolean
    but default expression is of type integer

which aborts that migration and therefore every migration after it. The
chain was unrunnable end to end on Postgres, and the whole test suite
runs on SQLite, so nothing saw it. It was found by pointing the latency
benchmark at a real Postgres, which is not a place bugs should have to
be found.

The failure has the shape this codebase keeps meeting: correct on the
convenient dialect, broken on the real one, and silent in between.

WHAT IS CHECKED, AND WHAT IS NOT

Statically, by AST: any ``sa.Column(..., sa.Boolean(), server_default=X)``
in a migration where X is an integer-ish literal. That is the exact
mistake that happened and the one a person writes by hand.

It does NOT attempt to typecheck every default against every dialect —
that is what running the migrations against Postgres does, and
``test_migrations_run_on_postgres`` below does exactly that when a
Postgres is reachable, skipping when it is not. A static check that
always runs plus an integration check that sometimes runs is better
than one thorough check nobody can execute in CI.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"

#: Literals that are integers pretending to be booleans. ``"true"`` and
#: ``sa.false()`` are both fine and neither appears here.
_INTEGERISH = {"0", "1", 0, 1}


def _is_boolean_column(call: ast.Call) -> bool:
    for arg in call.args:
        name = None
        if isinstance(arg, ast.Call):
            f = arg.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        elif isinstance(arg, ast.Attribute):
            name = arg.attr
        if name == "Boolean":
            return True
    return False


def _default_literal(call: ast.Call):
    """The server_default value, when it is a literal we can judge."""
    for kw in call.keywords:
        if kw.arg != "server_default":
            continue
        node = kw.value
        # sa.text("0") — unwrap to the string inside.
        if isinstance(node, ast.Call):
            f = node.func
            fname = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if fname == "text" and node.args and isinstance(node.args[0], ast.Constant):
                return node.args[0].value
            return None          # sa.false(), sa.func.now(), … all fine
        if isinstance(node, ast.Constant):
            return node.value
    return None


def _offenders() -> list[str]:
    out: list[str] = []
    for path in sorted(VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "Column"):
                continue
            if not _is_boolean_column(node):
                continue
            value = _default_literal(node)
            if value in _INTEGERISH:
                out.append(f"{path.name}:{node.lineno}: Boolean column with "
                           f"server_default={value!r} — use sa.true()/sa.false()")
    return out


def test_no_boolean_column_takes_an_integer_default():
    """The exact bug, and every future instance of it."""
    assert not _offenders(), (
        "these migrations run on SQLite and abort on Postgres:\n  "
        + "\n  ".join(_offenders()))


def test_the_scan_recognises_the_shape_it_is_looking_for():
    """A vacuous-pass guard. If the AST matching drifted, the test above
    would pass over an empty set and prove nothing — which is precisely
    how the original bug survived."""
    tree = ast.parse(
        'sa.Column("x", sa.Boolean(), nullable=False, server_default=sa.text("0"))')
    call = tree.body[0].value

    assert _is_boolean_column(call)
    assert _default_literal(call) == "0"


def test_the_scan_accepts_the_correct_spellings():
    for src, why in (
        ('sa.Column("x", sa.Boolean(), server_default=sa.false())', "sa.false()"),
        ('sa.Column("x", sa.Boolean(), server_default="true")', "a string literal"),
        ('sa.Column("n", sa.Integer(), server_default="0")', "an integer column"),
    ):
        call = ast.parse(src).body[0].value
        value = _default_literal(call)
        flagged = _is_boolean_column(call) and value in _INTEGERISH
        assert not flagged, f"{why} was wrongly flagged"


def test_it_found_some_migrations_to_look_at():
    files = list(VERSIONS.glob("*.py"))
    assert len(files) > 5, f"only {len(files)} migrations found — wrong path?"


@pytest.mark.skipif(
    not os.environ.get("POSTGRES_TEST_URL"),
    reason="no Postgres reachable; set POSTGRES_TEST_URL to run the real chain")
def test_migrations_run_on_postgres():
    """The check the static one is a cheap stand-in for.

    Skipped without a Postgres rather than failing, because a developer
    laptop should not need one — but when CI has one, this is the test
    that would have caught the original bug on the day it landed, rather
    than months later from a benchmark.
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", os.environ["POSTGRES_TEST_URL"])
    command.upgrade(cfg, "head")
