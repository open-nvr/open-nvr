# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Attendly's wiring in this repository.

Attendly is a first-party closed-source app (docs/APP_LISTING_TERMS.md
§1): its source is in a private repository, and what lives here is the
compose service that pulls its signed image and the catalog entry. These
are the checks on that wiring that the app's own tests used to make when
it was built from this checkout.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _service() -> dict:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.apps.yml").read_text(encoding="utf-8"))
    return compose["services"]["attendly"]


def _entry() -> dict:
    index = yaml.safe_load((REPO_ROOT / "server/config/apps_index.yml").read_text(encoding="utf-8"))
    entries = index["apps"] if isinstance(index, dict) else index
    return next(e for e in entries if e["id"] == "attendly")


def test_overlay_timezone_defaults_to_the_documented_zone():
    """Class windows and meal hours are local times; docs/apps/attendly.md
    promises Asia/Kolkata unless TZ says otherwise."""
    env = _service()["environment"]
    assert "TZ=${TZ:-Asia/Kolkata}" in env
    assert "ATTENDLY_TIMEZONE=${ATTENDLY_TIMEZONE:-${TZ:-Asia/Kolkata}}" in env


def test_the_service_pulls_the_signed_image_and_never_builds():
    """There is no source here: a build: section would fail on every
    install, and so would the build pull policy the other apps default to."""
    svc = _service()
    assert "build" not in svc
    assert svc["image"] == "${ATTENDLY_IMAGE:-ghcr.io/open-nvr/attendly:latest}"
    assert svc["pull_policy"] == "missing"


def test_the_catalog_entry_is_first_party_closed_source():
    entry = _entry()
    assert entry["license"] == "proprietary"
    assert entry["author"] == "OpenNVR"
    assert "source" not in entry and "build_context" not in entry
    assert entry["docs_url"].endswith("/docs/apps/attendly.md")
    assert (REPO_ROOT / "docs/apps/attendly.md").is_file()


def test_no_attendly_source_in_this_repository():
    """The code moved to a private repository; it must not drift back."""
    assert not (REPO_ROOT / "examples/attendly").exists()
