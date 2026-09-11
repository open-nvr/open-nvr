# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The adapter catalog — which models a deployment can install.

Apps have had a catalog since the beginning; adapters had none, so a
third party with a conformant model had nowhere to be listed and an
operator had nowhere to look for a better one. These tests pin the
catalog and the one idea it exists to serve: an app asks for a TASK,
never for an adapter by name, so the catalog's job is to show the
choices for a capability.
"""
from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

# Settings must validate for the router under test to import — the same
# preamble every other router suite here carries.
from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

from routers.adapters_catalog import (  # noqa: E402
    ADAPTERS_INDEX_PATH, AdapterIndexEntry, KNOWN_TIERS, load_adapters_index,
)
TASKS_PATH = REPO_ROOT / "server" / "config" / "tasks.yml"
APPS_INDEX_PATH = REPO_ROOT / "server" / "config" / "apps_index.yml"


@pytest.fixture(scope="module")
def entries() -> list[AdapterIndexEntry]:
    return load_adapters_index()


def known_task_names() -> set[str]:
    rows = yaml.safe_load(TASKS_PATH.read_text()) or []
    rows = rows if isinstance(rows, list) else rows.get("tasks", [])
    names: set[str] = set()
    for row in rows:
        if isinstance(row, dict):
            if row.get("task"):
                names.add(str(row["task"]))
            names.update(str(a) for a in (row.get("aliases") or []))
    return names


# ── The catalog itself ──────────────────────────────────────────────


def test_the_index_exists_and_parses(entries):
    assert ADAPTERS_INDEX_PATH.is_file()
    assert entries, "the adapter catalog is empty"


def test_every_entry_is_complete(entries):
    for entry in entries:
        assert entry.id and entry.name and entry.summary
        assert entry.image, f"{entry.id}: no image, so nothing to install"
        assert entry.version
        assert entry.tier in KNOWN_TIERS


def test_ids_are_unique(entries):
    ids = [entry.id for entry in entries]
    assert len(ids) == len(set(ids))


def test_every_adapter_advertises_a_task(entries):
    """An adapter advertising nothing gets no work from KAI-C, so a
    listing without a task is a card nobody can act on."""
    for entry in entries:
        assert entry.tasks_advertised, f"{entry.id} advertises no task"


def test_every_advertised_task_is_a_known_convention(entries):
    """A typo in a task name is invisible at runtime — nothing asks for
    `object_detecton`, so the adapter simply never receives work. This
    is the check that makes it visible."""
    known = known_task_names()
    for entry in entries:
        for task in entry.tasks_advertised:
            assert task in known, (
                f"{entry.id} advertises {task!r}, which is not in "
                f"server/config/tasks.yml (as a task or an alias)")


def test_every_task_an_app_requires_has_an_adapter(entries):
    """The catalog's reason to exist: an app that requires a task with
    no listed adapter greys out on a fresh install, and the operator is
    told to find a model with no idea where to look."""
    advertised = {task for entry in entries for task in entry.tasks_advertised}
    apps = yaml.safe_load(APPS_INDEX_PATH.read_text()) or []
    required = {str(task) for app in apps if isinstance(app, dict)
                for task in (app.get("requires_tasks") or [])}
    missing = sorted(required - advertised)
    assert not missing, (
        f"listed apps require {missing} but no listed adapter advertises them")


def test_permissions_are_declared_honestly(entries):
    """KAI-C refuses to register an adapter asking for more than the
    operator granted, so the listing has to match what the adapter
    requests — over-declaring blocks the install."""
    for entry in entries:
        assert isinstance(entry.permissions.gpu, bool)
        for host in entry.permissions.network_egress:
            assert "/" not in host, f"{entry.id}: egress is a host, not a URL"


def test_first_party_adapters_point_at_their_source(entries):
    for entry in entries:
        if entry.tier == "first_party":
            assert entry.source and entry.source.startswith("https://")
            assert entry.model_card_url, (
                f"{entry.id}: an operator auditing what runs near their "
                f"cameras needs the model card")


# ── Degrading, not exploding ────────────────────────────────────────


def test_a_malformed_entry_is_skipped_not_fatal(tmp_path):
    """One bad entry must cost one card, never the whole catalog."""
    index = tmp_path / "adapters_index.yml"
    index.write_text(yaml.safe_dump([
        {"id": "good", "name": "Good", "summary": "s", "version": "1.0.0",
         "image": "ghcr.io/x/good:1", "tasks_advertised": ["object_detection"]},
        {"id": "bad", "name": "Bad"},          # missing required fields
        "not even a mapping",
    ]))
    loaded = load_adapters_index(index)
    assert [entry.id for entry in loaded] == ["good"]


def test_an_unreadable_index_is_an_empty_catalog(tmp_path):
    assert load_adapters_index(tmp_path / "missing.yml") == []
    broken = tmp_path / "broken.yml"
    broken.write_text("{[not yaml")
    assert load_adapters_index(broken) == []


# ── The validator that keeps it honest ──────────────────────────────


def test_the_shipped_index_passes_its_own_validator():
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "validate_adapters_index.py")],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
