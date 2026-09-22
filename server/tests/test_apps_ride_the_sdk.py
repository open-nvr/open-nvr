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
# Each is one Dockerfile that puts an ONNX graph into an image and ships
# it, so there is no base class for them to ride — and no /manifest or
# /state for one to provide, because nothing in them ever runs as a
# service. yolov8 and yolo-pose export their graph from ultralytics at
# build time; package-detection bakes in a pinned release asset instead,
# because that model is ours and there is no upstream to export from.
#
# Unlike ALLOWLISTED this set is not debt and carries no shrink-only
# rule: a directory belongs here when it is not an app, so it grows
# whenever another weights image lands.
EXCLUDED = frozenset({
    "yolov8-weights",
    "yolo-pose-weights",
    "package-detection-weights",
})

# Gap 8's VISIBILITY debt is retired: the agent now serves /manifest and
# /state and self-registers with the App Catalog (Phase 1 contract
# parity — test_contract_parity.py in the agent's suite). What remains
# allowlisted is the narrower base-class debt: the agent still extends
# no Detector/FrameApp/AlertSubscriber, which is what THIS suite checks.
# That was a deliberate scope choice ("contract parity only, not a
# base-class rewrite"); adopting a base someday removes this entry via
# test_allowlist_only_shrinks. This set may ONLY shrink.
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


# ── the image must contain the modules the app imports ───────────────
#
# A separate failure from the SDK rule above, and a nastier one, because
# the test suites cannot see it. `uv sync` installs each example as an
# EDITABLE package, so every top-level module resolves from the source
# tree whatever the Dockerfile copies — the app imports fine locally and
# the IMAGE crashes on startup with ModuleNotFoundError. It happened
# twice on smart-doorbell (visit_log.py, then chime.py) before the
# app-images-smoke job caught it, which is a slow way to find out.


def _copied_module_names(dockerfile: Path, app: str) -> tuple[set[str], bool]:
    """Module basenames a Dockerfile copies, and whether it uses a glob.

    A glob or a whole-directory COPY cannot fall behind the app, so it
    satisfies this rule by construction and the name set is not used.
    """
    names: set[str] = set()
    globbed = False
    for raw in dockerfile.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.upper().startswith("COPY "):
            continue
        for token in line.split()[1:]:
            if token.startswith("--"):
                continue
            if f"examples/{app}" not in token:
                continue
            if "*" in token or token.rstrip("/").endswith(app):
                globbed = True
            if token.endswith(".py"):
                names.add(token.rsplit("/", 1)[-1])
    return names, globbed


def _top_level_imports(app_dir: Path) -> set[str]:
    """Sibling modules the app's own sources import from each other.

    Only bare ``import x`` / ``from x import`` where ``x.py`` sits beside
    them — those are the ones that must be in the image. Third-party and
    SDK imports come from pip.
    """
    siblings = {p.stem for p in app_dir.glob("*.py")}
    needed: set[str] = set()
    for src in _python_sources(app_dir):
        if src.parent != app_dir:
            continue
        tree = ast.parse(src.read_text(encoding="utf-8"), filename=str(src))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                root = (node.module or "").split(".")[0]
                if root in siblings:
                    needed.add(root)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in siblings:
                        needed.add(root)
    return needed


def test_every_dockerfile_copies_the_modules_its_app_imports():
    offenders: dict[str, list[str]] = {}
    for app_dir in _app_dirs():
        dockerfile = app_dir / "Dockerfile"
        if not dockerfile.is_file():
            continue
        copied, globbed = _copied_module_names(dockerfile, app_dir.name)
        if globbed:
            continue
        missing = sorted(
            f"{m}.py" for m in _top_level_imports(app_dir)
            if f"{m}.py" not in copied
        )
        if missing:
            offenders[app_dir.name] = missing
    assert not offenders, (
        "these Dockerfiles enumerate their app's modules and have fallen "
        f"behind it: {offenders}. The image would crash on startup with "
        "ModuleNotFoundError while every test still passed, because uv "
        "installs the app as an editable package and the import resolves "
        "from the source tree. Copy the modules by glob "
        "(COPY examples/<app>/*.py ./) rather than by name."
    )
