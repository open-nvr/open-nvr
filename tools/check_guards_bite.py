#!/usr/bin/env python3
"""Check that the repository's static guards actually fail.

This tree has a growing family of tests that guard against DRIFT rather
than against wrong answers: the Dockerfile that falls behind its app's
imports, the CI matrix that falls behind the apps, the token form that
falls behind the server's scopes, the translation catalogue that falls
behind its call sites, the descriptor kind that is priced as evidence
and never written.

They share a weakness. A guard is a claim of coverage, and a claim is
believed — so one that quietly stops failing is worse than no guard at
all, because the next person reads the file and stops looking. Two
assertions written in this family turned out to be satisfiable without
the thing they checked: one compared a substring where `numberX:`
matched `number:`, and one asserted a 404 on paths that did not exist,
so five of its seven cases passed against no check whatsoever.

So each guard gets a mutation that removes exactly what it protects,
and has to fail. Run it after touching a guard, or when a guard starts
looking decorative:

    python3 tools/check_guards_bite.py

An anchor that no longer matches is a FAILURE, not a skip. Skipping is
how this script would rot into the same decoration it exists to
prevent — and the first run after it was written already found one
anchor that had drifted, because a Dockerfile had been reformatted.
Exits non-zero on any survivor or any drift.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Interpreter per project. The camera-agent has its own environment.
SERVER_PY = sys.executable
AGENT_PY = sys.executable

#: (label, file, find, replace_with, run_dir, test)
#:
#: `find` must appear EXACTLY once. `replace_with` must remove or
#: reverse the protected behaviour — a mutation that leaves the
#: behaviour intact proves nothing when the guard passes.
MUTATIONS: list[tuple[str, str, str, str, str, str]] = [
    # ── camera scoping ───────────────────────────────────────────────
    ("an empty camera scope stops meaning nothing",
     "server/services/camera_scope.py",
     "    if not scope:\n        return q.filter(camera_column.in_([-1]))",
     "    if not scope:\n        return q",
     "server", "tests/test_scope_query_empty.py"),

    ("an unreachable core reads as an empty car park",
     "examples/license-plate-recognition/license_plate_recognition.py",
     "            return self._inside_cache or None",
     "            return []",
     "examples/license-plate-recognition", "tests"),
    ("overstay stops telling one visit from the next",
     "examples/license-plate-recognition/license_plate_recognition.py",
     '            key = (plate, entered_at)', '            key = (plate, "")',
     "examples/license-plate-recognition", "tests"),
    ("gate occupancy stops saying WHEN each vehicle came in",
     "server/services/timeline_service.py",
     'return {"inside": len(inside), "plates": inside[:200], "entries": entries}',
     'return {"inside": len(inside), "plates": inside[:200]}',
     "server", "tests/test_plate_stats.py"),

    # ── the canonical event store ────────────────────────────────────
    ("a plate retraction stops dropping the claim",
     "server/services/plate_enrichment.py",
     "    if db is not None:\n        from services.descriptor_store import sync_plate_claim\n\n        sync_plate_claim(db, row)",
     "    pass",
     "server", "tests/test_attribute_projection.py"),

    ("a descriptor kind is priced as evidence with no producer",
     "server/services/journey.py",
     '    "colour": 0.7,', '    "colour": 0.7,\n    "gait": 1.1,',
     "server", "tests/test_descriptor_producers.py"),
    ("face_id becomes reachable through free-text search",
     "server/services/descriptor_store.py",
     '_UNPROJECTED_KINDS = frozenset({"face_id"})',
     "_UNPROJECTED_KINDS = frozenset()",
     "server", "tests/test_descriptor_producers.py"),

    # ── search answers ───────────────────────────────────────────────
    ("the search answer stops saying what it did not look at",
     "server/services/search_service.py",
     '"undescribed": sum(1 for h in hits if not h.claims),',
     '"undescribed": 0,',
     "server", "tests/test_search_answer.py"),
    ("a page of results is quoted as the whole match set",
     "server/services/search_service.py",
     '"scope": "page",', '"scope": "all",',
     "server", "tests/test_search_answer.py"),
    ("two skills agreeing become two sightings",
     "server/services/search_service.py",
     '        for kind, value in {(c["kind"], c["value"]) for c in h.claims\n'
     '                            if c.get("kind") and c.get("value")}:',
     '        for kind, value in [(c["kind"], c["value"]) for c in h.claims\n'
     '                            if c.get("kind") and c.get("value")]:',
     "server", "tests/test_search_answer.py"),
    ("the route stops carrying the answer",
     "server/routers/search.py",
     '"answer": summarise_hits(hits, total=total, camera_names=cameras),',
     '"answer": {},',
     "server", "tests/test_search_answer.py"),

    # ── images and CI ────────────────────────────────────────────────
    ("a Dockerfile enumerates modules again",
     "examples/smart-doorbell/Dockerfile",
     "COPY examples/smart-doorbell/*.py", "COPY examples/smart-doorbell/smart_doorbell.py",
     "server", "tests/test_apps_ride_the_sdk.py"),
    ("an app leaves the image-smoke matrix",
     ".github/workflows/app-images-smoke.yml",
     "guard-scan-compliance", "guard-scan-compliance-RENAMED",
     "server", "tests/test_apps_ride_the_sdk.py"),

    # ── the operator's language ──────────────────────────────────────
    ("the UI asks for a translation key nobody defines",
     "app/src/views/AIEngine.tsx",
     "t('ai.save')", "t('ai.saveNowPlease')",
     "server", "tests/test_translation_catalogs.py"),
    ("French loses a key English still has",
     "app/src/locales/fr.ts",
     "'ai.save': 'Enregistrer', ", "",
     "server", "tests/test_translation_catalogs.py"),
    ("a view formats a date without the operator's locale",
     "app/src/views/Support.tsx",
     "{fmt.dateTime(new Date())}", "{new Date().toLocaleString()}",
     "server", "tests/test_ui_date_localisation.py"),

    # ── API tokens ───────────────────────────────────────────────────
    ("the server gains a scope the token form cannot grant",
     "server/services/api_tokens.py",
     '    "apps.view",', '    "apps.view", "reports.export",',
     "server", "tests/test_token_scope_rosters.py"),
    ("a grantable scope disappears from the form",
     "app/src/views/settings/ApiTokens.tsx",
     "'apps.view',", "",
     "server", "tests/test_token_scope_rosters.py"),
]


#: Mutations that are EXPECTED to survive, with the reason.
#:
#: A limit nobody wrote down is a limit everybody forgets, and the whole
#: argument of this file is that unexamined coverage is worse than none.
#: So the known gaps are listed rather than omitted — and if one starts
#: being caught, that is reported too, because the guard got better and
#: the note is now wrong.
KNOWN_UNCOVERED: list[tuple[str, str, str, str, str, str, str]] = [
    ("one branch of a big function loses its sync",
     "server/services/plate_enrichment.py",
     "                    clear_plate(row, db)",
     "                    row.plate_text = None",
     "server", "tests/test_attribute_projection.py",
     "enrich_event_plate legitimately writes plate_text and syncs "
     "elsewhere in its 340 lines, so a per-function check sees the other "
     "call and passes. Telling the branches apart needs flow analysis, "
     "which a static guard should not pretend to do. What protects this "
     "instead is that clear_plate now drops the claim itself — the "
     "mutation has to delete a call to it to get here."),

    ("the app-search short-circuit for an empty roster",
     "server/routers/app_platform.py",
     '    if roster is not None and not roster:\n        return {"results": [], "count": 0, "total": 0, "answer": {}}\n',
     "",
     "server", "tests/test_app_search_route.py",
     "It is a shortcut, not a guard, and the tests are right not to "
     "notice it going. Removing it leaves the empty roster to "
     "scope_query, which already matches nothing, so the route still "
     "answers correctly — two database round trips slower. A test that "
     "failed here would be pinning an optimisation as if it were the "
     "scoping. The comment in the route used to claim it WAS the "
     "scoping; that claim is what this entry replaces."),

    ("scope_query's explicit empty-set branch, with the route's "
     "short-circuit already gone",
     "server/services/camera_scope.py",
     "    if not scope:\n        return q.filter(camera_column.in_([-1]))\n",
     "",
     "server", "tests/test_scope_query_empty.py",
     "Falls through to in_(sorted(set())), and SQLAlchemy 2.x compiles "
     "an empty IN to a false predicate: no rows, no warning. So the "
     "mutated code is still CORRECT, and a test that failed here would "
     "be asserting on how a library renders a degenerate expression. "
     "The branch is kept because a site-wide scoping invariant should "
     "not rest silently on that rendering across a major version — but "
     "what is pinned is the behaviour, which the sentinels-disagree "
     "test covers. Deleting the branch in a way that actually widens "
     "the query (returning q unfiltered) IS caught; see MUTATIONS."),
]


def _run(run_dir: str, test: str) -> bool:
    py = AGENT_PY if "camera-agent" in run_dir else SERVER_PY
    proc = subprocess.run(
        [py, "-m", "pytest", test, "-q", "--no-header", "-x"],
        cwd=ROOT / run_dir, capture_output=True, text=True)
    return proc.returncode == 0


def main() -> int:
    survived: list[str] = []
    drifted: list[str] = []

    print(f"{'result':<10} mutation")
    print("-" * 72)
    for label, rel, find, replace, run_dir, test in MUTATIONS:
        path = ROOT / rel
        original = path.read_text()
        if original.count(find) != 1:
            print(f"{'DRIFTED':<10} {label}")
            print(f"{'':<10}   anchor appears {original.count(find)}x in {rel}")
            drifted.append(label)
            continue
        path.write_text(original.replace(find, replace, 1))
        try:
            passed = _run(run_dir, test)
        finally:
            path.write_text(original)
            assert path.read_text() == original, f"failed to restore {rel}"
        if passed:
            print(f"{'SURVIVED':<10} {label}")
            survived.append(label)
        else:
            print(f"{'caught':<10} {label}")

    print()
    print(f"{'result':<10} known gap (expected to survive)")
    print("-" * 72)
    unexpectedly_caught = []
    for label, rel, find, replace, run_dir, test, why in KNOWN_UNCOVERED:
        path = ROOT / rel
        original = path.read_text()
        if original.count(find) != 1:
            print(f"{'DRIFTED':<10} {label}")
            drifted.append(label)
            continue
        path.write_text(original.replace(find, replace, 1))
        try:
            passed = _run(run_dir, test)
        finally:
            path.write_text(original)
            assert path.read_text() == original, f"failed to restore {rel}"
        if passed:
            print(f"{'as noted':<10} {label}")
            print(f"{'':<10}   {why}")
        else:
            print(f"{'CAUGHT':<10} {label}")
            unexpectedly_caught.append(label)

    print("-" * 72)
    ok = len(MUTATIONS) - len(survived) - len(drifted)
    print(f"{ok} caught, {len(survived)} survived, {len(drifted)} drifted")

    for label in survived:
        print(f"  SURVIVED: {label} — its guard passes without the thing "
              "it guards, so the guard is decoration")
    for label in drifted:
        print(f"  DRIFTED: {label} — the mutation no longer applies; fix "
              "the anchor here, and check the guard still means what it said")

    for label in unexpectedly_caught:
        print(f"  NOW CAUGHT: {label} — the guard improved; drop it from "
              "KNOWN_UNCOVERED so the note stops claiming a gap that closed")

    return 1 if (survived or drifted or unexpectedly_caught) else 0


if __name__ == "__main__":
    raise SystemExit(main())
