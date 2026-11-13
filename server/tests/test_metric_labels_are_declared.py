# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""A label passed but never declared is silently dropped.

``_Metric._key`` builds its key from ``self.labelnames`` and nothing
else::

    return tuple(str(labels.get(n, "")) for n in self.labelnames)

so a caller passing ``{"binding": "nearest"}`` to a metric declared with
``("kind", "task", "adapter")`` gets no error, no warning, and no
``binding`` dimension. The series looks healthy. The dashboard that was
going to split on it cannot.

THIS ACTUALLY HAPPENED, which is why the test exists rather than the
rule being assumed. ``DESCRIPTORS_WRITTEN.inc`` was given a ``binding``
label with a comment beside it reading "Watchable: a deployment where
'nearest' is climbing is one where apps are guessing more than they are
measuring." It was not watchable. The label had been dropped on every
call since it was written, and nothing failed.

That is the same defect shape this codebase keeps meeting: a promise
made in a comment, kept nowhere, and invisible because the failure mode
is silence. The fix was to declare the label. The guard is this file, so
the next one is loud.

It works by AST rather than by running anything: every ``X.inc({...})``
and ``X.observe(v, {...})`` in the server tree with a literal dict is
checked against the labelnames ``X`` was declared with. Literal dicts
are the overwhelming majority and the ones a human writes by hand; a
computed dict is skipped rather than guessed at, and noted below.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))

METRICS_SRC = SERVER / "services" / "search_metrics.py"

#: Constructors whose third positional argument (or `labelnames`) names
#: the dimensions. Histogram takes buckets first, so the index differs.
_LABEL_ARG_INDEX = {"Counter": 2, "Gauge": 2, "Histogram": 3}


def _declared_labels() -> dict[str, tuple[str, ...]]:
    """metric name -> declared labelnames, read from the source."""
    tree = ast.parse(METRICS_SRC.read_text())
    out: dict[str, tuple[str, ...]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        kind = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if kind not in _LABEL_ARG_INDEX:
            continue
        idx = _LABEL_ARG_INDEX[kind]
        args = node.value.args
        labels: tuple[str, ...] = ()
        if len(args) > idx and isinstance(args[idx], (ast.Tuple, ast.List)):
            labels = tuple(
                e.value for e in args[idx].elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            )
        for target in node.targets:
            if isinstance(target, ast.Name):
                out[target.id] = labels
    return out


def _literal_label_calls() -> list[tuple[str, str, tuple[str, ...], str]]:
    """Every ``METRIC.inc({...})`` / ``.observe(v, {...})`` with a literal
    dict. Returns (metric, method, keys, where)."""
    found: list[tuple[str, str, tuple[str, ...], str]] = []
    for path in sorted(SERVER.rglob("*.py")):
        if "migrations" in path.parts or path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:                        # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"inc", "observe", "set"}):
                continue
            owner = node.func.value
            name = owner.attr if isinstance(owner, ast.Attribute) else getattr(
                owner, "id", None)
            if not name or not name.isupper():
                continue
            for arg in node.args:
                if isinstance(arg, ast.Dict) and all(
                        isinstance(k, ast.Constant) for k in arg.keys):
                    keys = tuple(k.value for k in arg.keys)
                    found.append((name, node.func.attr, keys,
                                  f"{path.relative_to(SERVER)}:{node.lineno}"))
    return found


@pytest.fixture(scope="module")
def declared() -> dict[str, tuple[str, ...]]:
    d = _declared_labels()
    assert d, "no metrics parsed out of search_metrics.py — the parser broke"
    return d


def test_the_parser_sees_the_metric_that_had_the_bug(declared):
    """A guard on the guard. If this fixture silently stopped finding
    metrics, every assertion below would pass vacuously — which is the
    exact failure mode the file is about."""
    assert "DESCRIPTORS_WRITTEN" in declared
    assert "binding" in declared["DESCRIPTORS_WRITTEN"], (
        "the label whose silent drop motivated this test is undeclared again")


def test_every_label_passed_to_a_metric_is_declared_on_it(declared):
    """The rule. An undeclared label is dropped by ``_key`` without a
    word, so the only way it can be loud is here."""
    problems = []
    for name, method, keys, where in _literal_label_calls():
        if name not in declared:
            # A metric from another module, or a name this test cannot
            # resolve. Not a finding — reporting it would train people
            # to ignore this test.
            continue
        undeclared = [k for k in keys if k not in declared[name]]
        if undeclared:
            problems.append(
                f"{where}: {name}.{method}() passes {undeclared}, "
                f"declared labels are {list(declared[name])}")

    assert not problems, (
        "labels passed but never declared — these are dropped silently:\n  "
        + "\n  ".join(problems))


def test_a_deliberately_undeclared_label_would_be_caught(declared):
    """Proof the check bites, rather than passing because it found
    nothing to look at. Builds the same mistake in memory and asserts
    the comparison rejects it."""
    labels = declared["DESCRIPTORS_WRITTEN"]
    undeclared = [k for k in ("kind", "not_a_real_label") if k not in labels]

    assert undeclared == ["not_a_real_label"]


def test_the_scan_actually_found_calls_to_check():
    """The other vacuous-pass guard: if the AST walk stopped matching
    the call shape, the rule above would hold over an empty set."""
    calls = _literal_label_calls()

    assert len(calls) > 5, (
        f"only {len(calls)} labelled metric calls found — the scan has "
        f"probably stopped recognising the call shape")


def test_dropping_an_undeclared_label_is_still_how_key_behaves():
    """Pins the behaviour this whole file is compensating for. If
    ``_key`` ever starts raising on an unknown label, this test fails
    and the file can be deleted — which would be the better outcome."""
    from services.search_metrics import Counter

    c = Counter("opennvr_test_only", "fixture", ("declared",))
    c.inc({"declared": "a", "undeclared": "b"})

    rendered = "\n".join(c.render())
    assert 'declared="a"' in rendered
    assert "undeclared" not in rendered, (
        "_key now keeps unknown labels — good; update or remove this file")
