# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""RFC-0002 "The SDK rule": every first-party example app extends an SDK base.

A contributor who copies any ``examples/`` folder must land on
``opennvr_app_sdk`` base classes (``Detector`` / ``FrameApp`` /
``AlertSubscriber``) — that is what makes the contracts (``/manifest``,
``/state``, ``/health``) and the registry's app-manifest view (RFC-0002
decision 2) come for free. This suite is the CI enforcement the RFC names.

Rules encoded here:

* Every example app directory (minus EXCLUDED non-apps) imports at least
  one SDK base class somewhere in its non-test Python sources.
* ALLOWLISTED is the set of apps *known* not to conform yet — RFC-0002
  gap 8. The allowlist may only shrink: an allowlisted app that starts
  conforming must be removed from the list, and adding a new app to the
  list means shipping new non-conforming code, which this suite fails.

Detection is AST-based (``ast.parse`` + ``ImportFrom``), so single-line
imports and parenthesized multiline blocks (e.g. abandoned-object's
``from opennvr_app_sdk import (...)``) are handled identically, and a
commented-out import never passes.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = REPO_ROOT / "examples"

# The three SDK base classes an app may ride (sdk/opennvr-app-sdk).
BASES = frozenset({"Detector", "FrameApp", "AlertSubscriber"})

# Not apps at all: build-support directories with no Python entrypoint.
EXCLUDED = frozenset({"yolov8-weights"})

# RFC-0002 gap 8 ("The flagship app is invisible to its own platform"):
# the camera agent imports SDK utilities but extends no base class, serves
# no /manifest or /state, and never self-registers. Phase 1's "Agent
# contract parity" task retires this entry. This set may ONLY shrink —
# see test_allowlist_only_shrinks.
ALLOWLISTED = frozenset({"camera-agent"})


def _app_dirs() -> list[Path]:
    return sorted(
        p for p in EXAMPLES.iterdir()
        if p.is_dir() and p.name not in EXCLUDED
    )


def _python_sources(app_dir: Path) -> list[Path]:
    return sorted(
        f for f in app_dir.rglob("*.py")
        if "tests" not in f.relative_to(app_dir).parts
    )


def _sdk_base_imports(app_dir: Path) -> set[str]:
    """Base classes the app imports from opennvr_app_sdk, across all
    non-test sources (a base buried in a helper module still counts)."""
    found: set[str] = set()
    for src in _python_sources(app_dir):
        tree = ast.parse(src.read_text(encoding="utf-8"), filename=str(src))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if module != "opennvr_app_sdk" and not module.startswith(
                    "opennvr_app_sdk."):
                continue
            found.update(a.name for a in node.names if a.name in BASES)
    return found


def test_roster_names_are_real_directories():
    # A renamed or deleted app must not leave a stale roster entry that
    # silently stops guarding anything.
    existing = {p.name for p in EXAMPLES.iterdir() if p.is_dir()}
    for name in sorted(EXCLUDED | ALLOWLISTED):
        assert name in existing, (
            f"{name!r} is on the conformance roster but examples/{name} "
            "does not exist — update EXCLUDED/ALLOWLISTED")


def test_every_example_app_rides_the_sdk():
    offenders = {}
    for app_dir in _app_dirs():
        if app_dir.name in ALLOWLISTED:
            continue
        bases = _sdk_base_imports(app_dir)
        if not bases:
            offenders[app_dir.name] = "imports no SDK base class"
    assert not offenders, (
        "RFC-0002 SDK rule: every first-party example app must extend an "
        f"opennvr_app_sdk base ({', '.join(sorted(BASES))}) so contributors "
        "copy one shape and /manifest + /state come for free. "
        f"Non-conforming: {offenders}. Either port the app to a base class "
        "or (for pre-existing debt only, per the RFC) discuss allowlisting "
        "it — new apps are never allowlisted."
    )


def test_allowlist_only_shrinks():
    # An allowlisted app that now conforms must come OFF the list, so the
    # list can never mask a future regression in that app.
    stale = {
        name: sorted(_sdk_base_imports(EXAMPLES / name))
        for name in ALLOWLISTED
        if _sdk_base_imports(EXAMPLES / name)
    }
    assert not stale, (
        f"allowlisted app(s) now import an SDK base: {stale} — remove them "
        "from ALLOWLISTED (the allowlist may only shrink; RFC-0002 gap 8)")


def test_camera_agent_debt_is_still_open():
    # Documents the current state precisely: the agent DOES use the SDK
    # (utilities), it just doesn't ride a base yet. When Phase 1's
    # contract-parity work lands, test_allowlist_only_shrinks (not this
    # test) is the one that forces the roster update.
    agent = EXAMPLES / "camera-agent"
    uses_sdk_at_all = any(
        "opennvr_app_sdk" in src.read_text(encoding="utf-8")
        for src in _python_sources(agent)
    )
    assert uses_sdk_at_all, (
        "camera-agent no longer references opennvr_app_sdk at all — the "
        "gap-8 story in RFC-0002 (utilities without contracts) is out of "
        "date; re-audit before touching the allowlist")
