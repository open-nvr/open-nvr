# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every relative link in the docs points at something that exists.

For an open-source platform the docs are the product surface: a
contributor who follows a link into nothing concludes the project is
half-finished, and they are not entirely wrong. This found five dead
links, all from ordinary decay rather than carelessness —
``docs/DOCKER_SETUP.md`` was removed in a "remove dead files" commit and
two live links were left pointing at it, one of them the last line of
the local-dev checklist, which is exactly where a new contributor is
standing when they read it.

Anchors are checked too. ``FILE.md#some-heading`` where the heading was
renamed lands the reader at the top of a long document with no idea
what they were meant to see, which is its own kind of broken and much
harder to notice.

Two things are deliberately NOT checked:

* External URLs. They fail for reasons that have nothing to do with
  this repository, and a test that needs the network to pass is a test
  that fails on a train.
* ``CHANGELOG.md``. It describes what was true at each release, and a
  link to a file that has since been deleted is accurate history.
  Rewriting it to keep a link checker happy would be falsifying the
  record.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$", re.M)

_SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".venv"}

#: Files whose links are not expected to resolve, with the reason.
_EXEMPT = {
    "CHANGELOG.md": "history — links describe the tree as it was then",
}

#: Scaffold templates. Their links contain placeholders that
#: ``scaffold.py`` substitutes when an app is generated, so the file as
#: committed is not meant to resolve. Detected by the placeholder rather
#: than by path, so a template moving does not silently un-exempt it.
_PLACEHOLDER = re.compile(r"__[A-Z][A-Z0-9_]*__")


def _slug(heading: str) -> str:
    """GitHub's anchor rules: lowercase, drop punctuation, then turn
    EACH remaining space into a hyphen.

    The last word is the one that matters and it cost a rewrite. A
    first version collapsed runs of whitespace, which is the obvious
    reading and the wrong one: "Compute-gated inference (Tier-0 →
    agents/apps)" loses the arrow from between two spaces and GitHub
    emits `...tier-0--agentsapps` with a DOUBLE hyphen. Collapsing gave
    a single one and declared nine perfectly good anchors broken — a
    test that would have had somebody "fix" links that worked.
    """
    text = heading.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return text.replace(" ", "-")


def _markdown_files():
    for path in sorted(_ROOT.rglob("*.md")):
        if _SKIP_DIRS & set(path.parts):
            continue
        rel = path.relative_to(_ROOT).as_posix()
        if rel in _EXEMPT:
            continue
        text = path.read_text(errors="ignore")
        if _PLACEHOLDER.search(text):
            continue
        yield path, rel, text


def test_every_relative_link_points_at_a_file_that_exists():
    broken = []
    checked = 0
    for path, rel, text in _markdown_files():
        for match in _LINK.finditer(text):
            target = match.group(2).split("#")[0].strip()
            if not target or target.startswith(
                    ("http://", "https://", "mailto:", "#")):
                continue
            checked += 1
            if not (path.parent / target).resolve().exists():
                broken.append(f"{rel}: [{match.group(1)[:40]}] -> {target}")
    assert checked > 200, (
        f"only {checked} relative links found; the scan is not reaching "
        "the docs and would pass whatever happened to them")
    assert broken == [], f"{len(broken)} dead link(s):\n  " + "\n  ".join(broken)


def test_every_anchor_points_at_a_heading_that_exists():
    """A renamed heading drops the reader at the top of a long document
    with no idea what they were sent to read."""
    broken = []
    for path, rel, text in _markdown_files():
        for match in _LINK.finditer(text):
            raw = match.group(2).strip()
            if raw.startswith(("http://", "https://", "mailto:")):
                continue
            file_part, _, anchor = raw.partition("#")
            if not anchor:
                continue
            target = (path if not file_part
                      else (path.parent / file_part).resolve())
            if not target.exists() or target.is_dir():
                continue          # the link test above owns that failure
            slugs = {_slug(h) for h in _HEADING.findall(
                target.read_text(errors="ignore"))}
            if anchor.lower() not in slugs:
                broken.append(
                    f"{rel}: [{match.group(1)[:34]}] -> {raw} "
                    f"(no heading slugs to '{anchor}')")
    assert broken == [], (
        f"{len(broken)} link(s) to a heading that does not exist:\n  "
        + "\n  ".join(broken))


def test_the_exemptions_are_real_and_reasoned():
    """An exemption for a file that has been renamed stops covering
    anything while still making the scan look complete."""
    missing = sorted(f for f in _EXEMPT if not (_ROOT / f).exists())
    assert missing == [], f"{missing} are exempted but no longer exist"
    for name, why in _EXEMPT.items():
        assert len(why.split()) >= 5, f"{name} needs a real reason, not a note"


def test_templates_are_exempt_by_placeholder_not_by_path():
    """The SDK scaffold template links to __DOCS__FIRST_DETECTOR.md and
    friends, which scaffold.py rewrites per generated app. Exempting it
    by its placeholder means moving the template cannot accidentally
    make it exempt-forever or subject-to-a-rule-it-cannot-pass."""
    templates = [
        p for p in _ROOT.rglob("*.md")
        if not (_SKIP_DIRS & set(p.parts))
        and _PLACEHOLDER.search(p.read_text(errors="ignore"))
    ]
    assert templates, (
        "no templates carry a __PLACEHOLDER__ any more; if the scaffold "
        "stopped using them, this exemption is now hiding real links")
    scaffold = (_ROOT / "sdk" / "opennvr-app-sdk" / "opennvr_app_sdk"
                / "scaffold.py")
    assert "__DOCS__" in scaffold.read_text(), (
        "scaffold.py no longer substitutes __DOCS__, so the template's "
        "doc links now ship broken to every generated app")
