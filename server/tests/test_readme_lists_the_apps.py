# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The README's application table must not fall behind the repo.

THE SAME DEFECT CLASS, ON THE PRODUCT SURFACE.

The README said "Eleven of the thirteen shipped examples are listed
above". There were sixteen. Three real applications —
``gate-controller``, ``guard-scan-compliance`` and ``alert-notifier`` —
existed, shipped, were listed in the App Catalog index, and appeared
nowhere in the README at all.

That is worse here than in code. A hand-maintained list that drifts in
a module is a maintenance problem; the README is where somebody decides
whether this project is worth their evening, and work that exists but
is never mentioned may as well not. Nobody stars a feature they were
not told about.

Deliberately loose about the PROSE and strict about the SET. The
narrative sentence around the table can be rewritten freely; what is
pinned is that every example directory is either listed, or explicitly
declared as deliberately unlisted with a reason. "We forgot" is the bug
this exists to catch, so an unexplained entry in the exemption set
defeats it.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
_EXAMPLES = REPO_ROOT / "examples"
_APPS_INDEX = REPO_ROOT / "server" / "config" / "apps_index.yml"

#: Example directories that are NOT applications, so the table would be
#: misleading rather than incomplete without them.
NOT_APPLICATIONS = {
    # Weights bundles: build artefacts an adapter consumes, with no app
    # to install and no rule to adapt.
    "package-detection-weights",
    "yolo-pose-weights",
    "yolov8-weights",
}

#: Applications deliberately outside the table, each with a reason.
DELIBERATELY_UNLISTED = {
    "inference-listener": "a minimal subscriber template, named in the "
                          "paragraph under the table rather than given a row",
    "alerts-subscriber": "same — a template, not a solution",
}


def _example_dirs() -> set[str]:
    return {p.name for p in _EXAMPLES.iterdir()
            if p.is_dir() and (p / "README.md").exists()}


def _linked_in_readme() -> set[str]:
    """Example dirs the README links to, anywhere in the document."""
    return set(re.findall(r"\(examples/([a-z0-9-]+)[)/]", _README))


def _catalog_ids() -> set[str]:
    text = _APPS_INDEX.read_text(encoding="utf-8")
    return set(re.findall(r"(?m)^\s*-?\s*id:\s*([a-z0-9-]+)", text))


def test_the_scan_found_the_examples():
    """Guard the guard — if the directory walk or the link regex broke,
    every assertion below would pass over an empty set."""
    dirs = _example_dirs()

    assert len(dirs) > 10, f"only {len(dirs)} example dirs found — wrong path?"
    assert "intrusion-detection" in dirs
    assert "intrusion-detection" in _linked_in_readme()


def test_every_shipped_application_is_mentioned_in_the_readme():
    """The drift that actually happened."""
    apps = _example_dirs() - NOT_APPLICATIONS
    missing = sorted(apps - _linked_in_readme() - set(DELIBERATELY_UNLISTED))

    assert not missing, (
        "these applications ship in examples/ but the README never "
        "mentions them, so nobody reading the front page knows they "
        "exist: " + ", ".join(missing) + ". Add a row to the "
        "applications table, or an entry to DELIBERATELY_UNLISTED with "
        "a reason.")


def test_every_catalog_app_is_mentioned_in_the_readme():
    """An app a user can install in one click from the UI, and cannot
    read about on the front page, is a strange thing to ship."""
    missing = sorted(_catalog_ids() - _linked_in_readme())

    assert not missing, (
        "installable from the App Catalog but absent from the README: "
        + ", ".join(missing))


def test_the_readme_does_not_link_an_example_that_is_gone():
    """The opposite drift: a row pointing at a directory that was
    removed or renamed. The docs link checker catches the dead path;
    this says which application it was."""
    ghosts = sorted(_linked_in_readme() - _example_dirs())

    assert not ghosts, (
        "the README links to example directories that do not exist: "
        + ", ".join(ghosts))


def test_the_stated_count_matches_the_table():
    """The sentence under the table quotes two numbers. Both were wrong.

    Pinned by parsing them back out rather than by hard-coding, so the
    test does not need editing every time an app is added — only when
    the sentence stops being true.
    """
    m = re.search(r"([A-Za-z]+) of the ([a-z]+) shipped examples are "
                  r"listed above", _README)
    assert m, ("the sentence stating how many examples are listed has "
               "been reworded — update this test, or drop the count "
               "from the README so there is nothing to fall behind")

    words = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
             "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
             "eighteen": 18, "nineteen": 19, "twenty": 20}
    listed_claim = words[m.group(1).lower()]
    total_claim = words[m.group(2).lower()]

    apps = _example_dirs() - NOT_APPLICATIONS
    rows = len(re.findall(r"(?m)^\| \[`[a-z0-9-]+`\]\(examples/", _README))

    assert total_claim == len(apps), (
        f"README says {total_claim} shipped examples; examples/ has "
        f"{len(apps)} (excluding weights bundles)")
    assert listed_claim == rows, (
        f"README says {listed_claim} are listed; the table has {rows} rows")
