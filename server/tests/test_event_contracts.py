# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""RFC-0002 Phase 0: the event-contracts doc is CI-enforced, not advisory.

Two directions, one ratchet:

* Any ``opennvr.events.*`` subject a first-party source file mentions
  MUST be contracted in ``docs/EVENT_CONTRACTS.md``. Publishing (or
  subscribing to) a domain event nobody wrote down is exactly the
  drift RFC-0002 decision 3 exists to stop — the failure message is
  the review prompt.
* The doc itself must keep contracting the v1 set that Phase 0 named,
  so a careless edit can't silently un-contract an event that
  consumers already rely on (contracts are retired by version, with
  notice — never by deletion).

The scan is string-literal level on purpose: subjects are built with
f-strings whose static prefix still contains ``opennvr.events.<domain>.
<event>.v<N>`` (the camera_id is the only runtime token, and it is the
last token by contract), so a source regex catches every real use
without importing anything.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = REPO_ROOT / "docs" / "EVENT_CONTRACTS.md"

# Where first-party bus traffic can originate.
SCANNED_TREES = ("server", "kai-c", "detect-pipeline", "examples", "sdk")

# Test directories are exempt from the scans: expected-value literals in
# tests legitimately hard-code cameras and invent throwaway subjects.
# Subjects that matter live in producers and consumers — source files.
_TEST_DIR_PARTS = frozenset({"test", "tests"})


def _is_test_file(path: Path) -> bool:
    return bool(_TEST_DIR_PARTS.intersection(path.parts))


# A contracted name as it appears in code AND in the doc:
# opennvr.events.<domain>.<event>.v<N>
SUBJECT_RE = re.compile(r"opennvr\.events\.([a-z_]+)\.([a-z_]+)\.v(\d+)")

# The Phase 0 baseline — the doc may grow, never silently shrink.
BASELINE_V1 = frozenset({
    "detection.observed.v1",
    "visit.recorded.v1",
    "plate.recognized.v1",
    "alert.fired.v1",
})


def _contracted_schemas() -> set[str]:
    text = CONTRACTS.read_text(encoding="utf-8")
    return {
        f"{m.group(1)}.{m.group(2)}.v{m.group(3)}"
        for m in SUBJECT_RE.finditer(text)
    }


def _schemas_used_in_code() -> dict[str, set[str]]:
    """schema -> set of files (repo-relative) that mention it."""
    used: dict[str, set[str]] = {}
    for tree in SCANNED_TREES:
        root = REPO_ROOT / tree
        if not root.is_dir():
            continue
        for src in root.rglob("*.py"):
            if _is_test_file(src.relative_to(REPO_ROOT)):
                continue
            try:
                text = src.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for m in SUBJECT_RE.finditer(text):
                schema = f"{m.group(1)}.{m.group(2)}.v{m.group(3)}"
                used.setdefault(schema, set()).add(
                    str(src.relative_to(REPO_ROOT)))
    return used


def test_contracts_doc_exists_and_holds_the_baseline():
    assert CONTRACTS.is_file(), (
        "docs/EVENT_CONTRACTS.md is gone — the domain-event surface has "
        "no normative definition. Contracts are retired by version with "
        "notice, never by deleting the doc (RFC-0002 Phase 0).")
    contracted = _contracted_schemas()
    missing = BASELINE_V1 - contracted
    assert not missing, (
        f"EVENT_CONTRACTS.md no longer defines {sorted(missing)} — v1 "
        "contracts consumers rely on cannot be silently un-contracted; "
        "supersede with a v2 and a migration note instead")


def test_every_domain_subject_in_code_is_contracted():
    contracted = _contracted_schemas()
    offenders = {
        schema: sorted(files)
        for schema, files in _schemas_used_in_code().items()
        if schema not in contracted
    }
    assert not offenders, (
        "uncontracted opennvr.events.* subject(s) in first-party code: "
        f"{offenders}. Add the schema to docs/EVENT_CONTRACTS.md (subject, "
        "producer, payload table, additive-only) before shipping the "
        "producer or consumer — that doc is what lets a subscriber build "
        "on the event without reading your source.")


def test_camera_id_is_the_last_subject_token_in_code():
    # The contract puts <camera_id> last so wildcard subscriptions work
    # (`opennvr.events.<domain>.<event>.v1.>`). A literal that continues
    # past the version with a fixed token (anything but a format field,
    # wildcard, or quote-end) is breaking that shape.
    bad: list[str] = []
    tail_re = re.compile(
        r"opennvr\.events\.[a-z_]+\.[a-z_]+\.v\d+\.(?![>{*\"'])")
    for tree in SCANNED_TREES:
        root = REPO_ROOT / tree
        if not root.is_dir():
            continue
        for src in root.rglob("*.py"):
            if _is_test_file(src.relative_to(REPO_ROOT)):
                continue
            try:
                text = src.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for m in tail_re.finditer(text):
                line = text[:m.start()].count("\n") + 1
                bad.append(f"{src.relative_to(REPO_ROOT)}:{line}")
    assert not bad, (
        f"hard-coded token after the version segment at {bad} — the "
        "camera_id must be the final, runtime-substituted subject token "
        "(f-string field or wildcard), or per-camera wildcards break")
