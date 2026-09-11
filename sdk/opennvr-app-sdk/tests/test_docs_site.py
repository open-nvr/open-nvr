# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""The documentation site is derived, and this test keeps it that way.

`make sdk-site` publishes mkdocs-material + mkdocstrings, so the
reference IS the docstrings — there is no second copy of the API. The
pieces that could still drift are the navigation and the generated
pages, so both are checked against `opennvr_app_sdk.API_TIERS` here.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import opennvr_app_sdk as sdk

ROOT = Path(__file__).resolve().parent.parent
MKDOCS = ROOT / "mkdocs.yml"
DOCS = ROOT / "docs_src"


@pytest.fixture(scope="module")
def config() -> dict:
    class Loader(yaml.SafeLoader):
        """mkdocs.yml carries `!!python/name:` tags in some setups; the
        keys this test reads never do, so unknown tags become None."""

    Loader.add_multi_constructor("", lambda loader, suffix, node: None)
    return yaml.load(MKDOCS.read_text(encoding="utf-8"), Loader=Loader)


def nav_files(nav) -> list[str]:
    out: list[str] = []
    for entry in nav:
        if isinstance(entry, str):
            out.append(entry)
        elif isinstance(entry, dict):
            for value in entry.values():
                out.extend(nav_files(value) if isinstance(value, list) else [value])
    return out


def test_generated_pages_are_current():
    """`scripts/gen_reference.py --check` is the whole assertion: if a
    tier changed and nobody regenerated, this fails here rather than
    silently publishing a stale reference."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_reference.py"), "--check"],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_every_tier_has_a_reference_page_in_the_nav(config):
    pages = set(nav_files(config["nav"]))
    for slug in sdk.API_TIERS:
        page = f"reference/{slug}.md"
        assert (DOCS / page).exists(), f"{page} was never generated"
        assert page in pages, f"{page} is not in mkdocs.yml nav"


def test_the_nav_has_no_dead_links(config):
    missing = [page for page in nav_files(config["nav"])
               if not (DOCS / page).exists()]
    assert missing == [], f"nav points at pages that do not exist: {missing}"


def test_no_page_is_orphaned_from_the_nav(config):
    pages = set(nav_files(config["nav"]))
    orphans = sorted(
        str(p.relative_to(DOCS)) for p in DOCS.rglob("*.md")
        if str(p.relative_to(DOCS)) not in pages
    )
    assert orphans == [], f"written but unreachable: {orphans}"


def test_every_reference_page_documents_its_tier(config):
    for slug, names in sdk.API_TIERS.items():
        body = (DOCS / "reference" / f"{slug}.md").read_text(encoding="utf-8")
        for name in names:
            assert f"::: opennvr_app_sdk.{name}\n" in body, \
                f"{name} is in tier {slug!r} but not on its page"


def test_snippets_resolve():
    """Pages pull code out of the cookbook with `--8<--`. A moved file or
    a shifted line range silently publishes the wrong code, so check
    every snippet path exists."""
    import re

    for page in DOCS.rglob("*.md"):
        for match in re.finditer(r'--8<--\s+"([^":]+)(?::[\d:]*)?"',
                                 page.read_text(encoding="utf-8")):
            target = ROOT / match.group(1)
            assert target.exists(), f"{page.name} includes missing {match.group(1)}"


def test_the_site_points_at_the_right_places(config):
    assert config["site_url"].startswith("https://opennvr.org")
    assert config["docs_dir"] == "docs_src"
    # mkdocstrings must resolve the package from the repo, not a stale
    # install, or the reference documents whatever pip has.
    options = config["plugins"]
    handler = next(p for p in options if isinstance(p, dict) and "mkdocstrings" in p)
    assert handler["mkdocstrings"]["handlers"]["python"]["paths"] == ["."]
