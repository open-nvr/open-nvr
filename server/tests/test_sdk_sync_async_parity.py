# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The async SDK offers what the sync one does, with the same arguments.

``client.py`` and ``aio.py`` are two hand-maintained copies of one API.
Every app-facing capability has to be added to both, and nothing until
now checked that it was. That is the defect this tree keeps producing
under different names — an enumerated list falling behind the thing it
lists — and it is quiet in a particular way here: an app written
against the async client simply cannot reach the feature, and the
author has no reason to suspect it exists.

``read_evidence`` is the recent example. It was added to both by hand,
correctly, and only because whoever wrote it remembered. This makes
that not a matter of memory.

Two things are compared, because the second is the one that gets
missed. Method NAMES, so a capability cannot be sync-only. And
SIGNATURES — positional and keyword-only parameter names — so
``read_evidence(path)`` does not quietly become ``read_evidence(rel)``
on the async side, which type checkers will not catch for an app
calling it by keyword and which reads as a bug in the caller.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_SDK = (Path(__file__).resolve().parents[2]
        / "sdk" / "opennvr-app-sdk" / "opennvr_app_sdk")

#: Lifecycle differs by construction and is not a capability: a sync
#: client closes, an async one is awaited closed. Listed rather than
#: pattern-matched so a THIRD name cannot quietly join them.
_LIFECYCLE = {"close", "aclose"}


def _public_methods(path: Path) -> dict[tuple[str, str], tuple[list, list]]:
    tree = ast.parse(path.read_text())
    out: dict[tuple[str, str], tuple[list, list]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if item.name.startswith("_") or item.name in _LIFECYCLE:
                continue
            out[(node.name, item.name)] = (
                [a.arg for a in item.args.args if a.arg != "self"],
                [a.arg for a in item.args.kwonlyargs],
            )
    return out


def _sync():
    return _public_methods(_SDK / "client.py")


def _async():
    return _public_methods(_SDK / "aio.py")


def _pairs():
    """(sync class, async class) for every class that has both."""
    sync_classes = {c for c, _ in _sync()}
    async_classes = {c for c, _ in _async()}
    return [(c, "Async" + c) for c in sorted(sync_classes)
            if "Async" + c in async_classes]


def test_there_are_classes_to_compare():
    """If the naming convention ever changes, every test below starts
    passing by comparing nothing."""
    pairs = _pairs()
    assert len(pairs) >= 4, (
        f"only {pairs} pair up as X / AsyncX; the rest of this file is "
        "comparing empty sets")


def test_every_sync_method_has_an_async_one():
    sync, asyn = _sync(), _async()
    missing = sorted(
        f"{cls}.{meth}" for (cls, meth) in sync
        if ("Async" + cls, meth) not in asyn
        and "Async" + cls in {c for c, _ in asyn}
    )
    assert missing == [], (
        f"these exist only on the sync client: {missing}. An app written "
        "against the async client cannot reach them, and its author has "
        "no reason to think they exist.")


def test_every_async_method_has_a_sync_one():
    """The other direction is just as bad: a capability only the async
    client has is one the docs and examples will describe without
    saying which client you need."""
    sync, asyn = _sync(), _async()
    missing = sorted(
        f"{cls}.{meth}" for (cls, meth) in asyn
        if (cls.removeprefix("Async"), meth) not in sync
        and cls.startswith("Async")
        and cls.removeprefix("Async") in {c for c, _ in sync}
    )
    assert missing == [], f"these exist only on the async client: {missing}"


def test_the_arguments_match():
    """The half that gets missed. A parameter renamed on one side is
    invisible until an app calls it by keyword, and then it reads as a
    bug in the app."""
    sync, asyn = _sync(), _async()
    drift = {}
    for (cls, meth), sig in sync.items():
        other = asyn.get(("Async" + cls, meth))
        if other is not None and other != sig:
            drift[f"{cls}.{meth}"] = {"sync": sig, "async": other}
    assert drift == {}, (
        f"same method, different arguments: {drift}")


def test_shared_types_are_imported_not_copied():
    """``Camera`` and ``Recording`` are plain data, and aio.py imports
    them from client.py rather than declaring its own. A second
    declaration would drift field by field, and an app passing one to
    the other client would fail somewhere far away."""
    aio = (_SDK / "aio.py").read_text()
    tree = ast.parse(aio)
    declared = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    for shared in ("Camera", "Recording"):
        assert shared not in declared, (
            f"aio.py declares its own {shared}; import it from client.py "
            "so the two clients cannot disagree about its fields")
        assert shared in aio, (
            f"aio.py no longer references {shared} at all — if it moved, "
            "this test is checking a name nobody uses")


# ── and the platform the SDK is a client of ──────────────────────────

#: Every app-platform route, and the SDK method that reaches it.
#:
#: A route with no wrapper is not unusable — an app can build the HTTP
#: call itself — but it is a capability the SDK's own users will never
#: discover, and one where every app re-implements the auth header, the
#: error envelope and the retry. ``read_evidence`` sat in exactly that
#: state until an app needed it.
#:
#: A route added without an entry here fails, which is the point: the
#: choice to leave something unwrapped should be made on purpose and
#: written down, not arrived at by forgetting.
_ROUTE_WRAPPERS: dict[str, str] = {
    "GET /cameras/{camera_id}/snapshot": "snapshot",
    "GET /cameras/{camera_id}/stream": "stream",
    "GET /recordings/{camera_id}": "recordings",
    "GET /recordings/{camera_id}/url": "url",
    "GET /search": "find",
    "GET /plates/stats": "plate_stats",
    "GET /plates/summary": "plate_summary",
    "GET /plates/sessions": "plate_sessions",
    "GET /alerts": "inbox",
    "GET /site-mode": "site_mode",
    "POST /evidence": "save_evidence",
    "GET /evidence/{rel_path:path}": "read_evidence",
    "GET /state": "items",
    "GET /state/{key}": "get",
    "PUT /state/{key}": "set",
    "DELETE /state/{key}": "delete",
}


def _platform_routes() -> set[str]:
    router = (Path(__file__).resolve().parents[1]
              / "routers" / "app_platform.py").read_text()
    return {
        f"{m.group(1).upper()} {m.group(2)}"
        for m in re.finditer(r'@router\.(get|post|put|delete)\("([^"]+)"', router)
    }


def test_every_platform_route_is_reachable_from_the_sdk():
    routes = _platform_routes()
    unwrapped = sorted(routes - set(_ROUTE_WRAPPERS))
    assert unwrapped == [], (
        f"these app-platform routes have no SDK wrapper recorded: "
        f"{unwrapped}. Add the method and name it here, or record the "
        "route with the method that covers it — an app author reading "
        "the SDK will not find a route the SDK never mentions.")


def test_the_wrapper_table_describes_routes_that_exist():
    """A wrapper named for a route that has been renamed or removed is
    an entry that covers nothing, and it makes the count above look
    complete."""
    routes = _platform_routes()
    stale = sorted(set(_ROUTE_WRAPPERS) - routes)
    assert stale == [], (
        f"{stale} are listed as wrapped but are no longer routes")


def test_the_named_wrappers_are_real_methods():
    sync, asyn = _sync(), _async()
    known = {meth for _, meth in sync} | {meth for _, meth in asyn}
    missing = sorted({m for m in _ROUTE_WRAPPERS.values()} - known)
    assert missing == [], (
        f"{missing} are named as SDK wrappers but no such method exists; "
        "the table is describing an SDK that is not there")


# ── and the doc an app author actually reads ─────────────────────────

_PLATFORM_DOC = (Path(__file__).resolve().parents[2]
                 / "docs" / "APP_PLATFORM.md")

#: Routes the Surface table is not expected to name individually,
#: because it names them as a group and the group is unambiguous.
_DOCUMENTED_AS_A_GROUP = {
    "GET /plates/stats": "/internal/app/plates/*",
    "GET /plates/summary": "/internal/app/plates/*",
    "GET /plates/sessions": "/internal/app/plates/*",
    "GET /state": "/internal/app/state[/{}]",
    "GET /state/{key}": "/internal/app/state[/{}]",
    "PUT /state/{key}": "/internal/app/state[/{}]",
    "DELETE /state/{key}": "/internal/app/state[/{}]",
    "GET /recordings/{camera_id}/url": "/internal/app/recordings/{}[/url]",
}


def _shape(text: str) -> str:
    """``{camera_id}`` and ``{id}`` and ``{rel_path:path}`` are the same
    hole. Comparing the shapes rather than the names is what lets the
    doc call it ``{id}`` and the router call it ``{camera_id}``."""
    return re.sub(r"\{[^}]*\}", "{}", text)


def test_the_platform_doc_names_every_capability():
    """APP_PLATFORM.md's Surface table is where an app author finds out
    what the platform can do. A capability missing from it is one nobody
    discovers — and three were: the camera stream grant, and both halves
    of the evidence store. save_evidence and read_evidence are how an app
    keeps a photo and gets it back after a restart, which is the whole
    reason the doorbell can enrol a face it saw last week.

    The stream one was worse than absent. `ai.stream()` WAS in the table
    — a KAI-C inference session, nothing to do with a camera's live view
    — so a reader scanning for "stream" found the wrong thing and
    stopped looking.
    """
    doc = _shape(_PLATFORM_DOC.read_text())
    missing = []
    for route in sorted(_platform_routes()):
        group = _DOCUMENTED_AS_A_GROUP.get(route)
        if group:
            if _shape(group) not in doc:
                missing.append(f"{route} (expected under {group})")
            continue
        method, path = route.split(" ", 1)
        # Method AND full shape. Matching the path prefix alone let
        # `GET /evidence/{path}` be satisfied by the POST /evidence row
        # sitting above it — the same loose-substring mistake this file
        # exists to prevent, made inside the file itself.
        if _shape(f"{method} /internal/app{path}") not in doc:
            missing.append(route)
    assert missing == [], (
        f"these routes are not in APP_PLATFORM.md's Surface table: "
        f"{missing}. An app author reading the doc will never find them, "
        "and the SDK method for them might as well not exist.")


def test_the_group_entries_describe_routes_that_exist():
    """A group heading for routes that were renamed away covers nothing
    while still making the table look complete."""
    stale = sorted(set(_DOCUMENTED_AS_A_GROUP) - _platform_routes())
    assert stale == [], (
        f"{stale} are excused as group-documented but are no longer routes")


def test_both_streams_are_told_apart():
    """The table names two unrelated things `stream`. If the note that
    says so is ever dropped, the trap comes back."""
    doc = _PLATFORM_DOC.read_text()
    assert "/internal/app/cameras/{id}/stream" in doc
    assert "infer/{adapter}/stream" in doc
    assert "unrelated" in doc, (
        "the doc names two different things `stream` and no longer says "
        "they are different")
