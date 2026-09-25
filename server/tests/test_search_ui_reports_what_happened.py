# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The search response says what happened. The UI has to pass it on.

A field the server sends and the page ignores is not a cosmetic gap.
``relaxed`` is the worked example: the route drops a leftover word
rather than showing a blank page for a sentence it mostly understood —
"what is NUMBER of it" should not cost you 421 cars — and reports the
substitution so the operator knows these are not the results they
asked for. The page rendered them silently. Same numbers on screen,
opposite meaning, and the operator finds out weeks later when they
wonder why a filter did nothing.

``semantic`` is the same shape. "Ranked by words only because the
embedder is unreachable" and "ranked by words and meaning" produce
identical-looking lists.

These live on the server side because the frontend has no test runner —
the same reasoning, and the same trade, as
``test_translation_catalogs.py``. They only read files.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_ROUTE = (REPO_ROOT / "server" / "routers" / "search.py").read_text(encoding="utf-8")
_VIEW = (REPO_ROOT / "app" / "src" / "views" / "Search.tsx").read_text(encoding="utf-8")

#: Response keys the page deliberately does not read, and why. An
#: unexplained addition here defeats the test — "we forgot" is the bug
#: it exists to catch.
NOT_CONSUMED: dict[str, str] = {
    "query": "the echo of what was typed; the page already holds it",
    "count": "results.length is the same number, from the same array",
}


def _response_keys() -> set[str]:
    """Top-level keys of the dict the /search route returns.

    Read from the literal rather than by calling the route, so this
    needs no database, no fixtures and no app configuration.

    THREE spellings, and the first version of this test only handled
    one — which made it pass over exactly the two fields it was written
    for. ``relaxed`` and ``semantic`` are conditional, so they do not
    appear as plain keys:

        "total": total,                      a plain key
        **({"relaxed": relaxed} if ...),     a conditional spread
        **_semantic_block(page, reason),     a helper that returns one

    A guard that only sees the easy spelling is the enumerated-list
    defect again, one level up.
    """
    start = _ROUTE.index("    return {\n", _ROUTE.index("async def search("))
    end = _ROUTE.index("\n    }\n", start)
    block = _ROUTE[start:end]

    keys = set(re.findall(r'(?m)^        "([a-z_]+)":', block))
    keys |= set(re.findall(r'\*\*\(?\{"([a-z_]+)":', block))
    # A spread helper contributes whatever its own return literal names.
    for helper in re.findall(r"\*\*(_\w+)\(", block):
        body = _ROUTE[_ROUTE.index(f"def {helper}("):]
        keys |= set(re.findall(r'return \{\s*"([a-z_]+)":', body[:2000]))
    return keys


def test_the_response_literal_was_actually_found():
    """Guard the guard: a refactor that moves the return would leave
    every assertion below comparing two empty sets."""
    keys = _response_keys()

    assert len(keys) >= 6, f"parsed only {sorted(keys)} — the response parser has drifted"
    assert {"results", "total", "interpretation"} <= keys


def test_every_field_that_changes_what_the_results_MEAN_is_shown():
    """The rule."""
    unread = sorted(
        k for k in _response_keys()
        if k not in NOT_CONSUMED and k not in _VIEW
    )

    assert not unread, (
        "the search response carries these and Search.tsx never reads "
        "them, so the page shows results whose meaning it is not "
        "passing on: " + ", ".join(unread) + ". Render them, or add "
        "them to NOT_CONSUMED with a reason.")


def test_a_relaxed_search_is_admitted_and_reversible():
    """Dropping a word is a substitution, not a refinement.

    The operator gets results they did not ask for. That is the right
    call — a blank page for a sentence the parser mostly understood is
    worse — but only while it is said out loud and reversible. An
    explicit `text` is never dropped by the route, so pinning the word
    IS the way back.
    """
    assert "relaxed" in _VIEW, "the page never mentions a relaxed search"
    assert re.search(r"base\.text\s*=\s*relaxed\.dropped", _VIEW), (
        "no way back to the strict search — setting the dropped word as "
        "an explicit text filter is what the route will not relax")


def test_the_empty_state_asks_what_this_box_can_describe():
    """"Nothing matched" has two meanings and the page used to guess.

    /search/enrichment-plan reports the skills KAI-C has registered AND
    healthy; its docstring has always said the UI should use it "to say
    what searching by colour or by face would even mean here, instead
    of offering filters that can never match".
    """
    assert "/api/v1/search/enrichment-plan" in _VIEW, (
        "the empty state still describes deployments in general rather "
        "than this one")
    assert "descriptor_kinds" in _VIEW


def test_an_unreachable_registry_is_not_reported_as_an_empty_one():
    """The third state.

    Asked and told nothing, versus could not ask. Rendering the second
    as the first tells an operator their box describes nothing because
    KAI-C blinked — a worse lie than the vague sentence this replaced.
    """
    assert re.search(r"kinds\s*===\s*undefined", _VIEW), (
        "no branch distinguishes an unreachable registry from one that "
        "reports no skills")
