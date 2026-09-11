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


# ── Task aliases (review fix) ───────────────────────────────────────


def _canonical(name: str) -> str:
    from routers.ai_models import _load_tasks_registry, canonicalize_task

    return canonicalize_task(name, _load_tasks_registry())


def test_every_advertised_task_canonicalizes_to_a_known_task(entries):
    """The catalog compared raw strings, so an adapter advertising
    `audio_transcription` was invisible to `?task=speech_to_text` even
    though the routing layer folds the two together — the operator saw
    an empty page for a capability they had installed."""
    known = {entry["task"] for entry in yaml.safe_load(TASKS_PATH.read_text())}
    for entry in entries:
        for task in entry.tasks_advertised:
            assert _canonical(task) in known, (
                f"{entry.id}: advertises {task!r}, which is neither a "
                f"canonical task nor an alias in tasks.yml — no app can ever "
                f"route to it")


def test_aliases_and_canonical_names_select_the_same_adapters(entries):
    """`?task=` must accept whichever spelling the caller has."""
    registry = yaml.safe_load(TASKS_PATH.read_text())
    for task_entry in registry:
        for alias in task_entry.get("aliases", []):
            assert _canonical(alias) == task_entry["task"]


def test_known_tiers_match_the_validator():
    """The router and `scripts/validate_adapters_index.py` each carry a
    copy — the script stays free of the server's dependencies, so this
    is what keeps the two from drifting."""
    source = (REPO_ROOT / "scripts" / "validate_adapters_index.py").read_text()
    declared = source.split("KNOWN_TIERS = ", 1)[1].split("\n", 1)[0]
    assert eval(declared) == KNOWN_TIERS  # noqa: S307 — a literal set


def test_an_unknown_tier_is_rejected():
    """A made-up tier used to reach the UI as an unknown badge."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AdapterIndexEntry.model_validate({
            "id": "x", "name": "X", "summary": "s", "version": "1.0.0",
            "image": "ghcr.io/x/x:1", "tasks_advertised": ["object_detection"],
            "tier": "certified",
        })


# ── Egress disclosure (review fix) ──────────────────────────────────


def test_the_validator_warns_about_every_undisclosed_egress_host(tmp_path):
    """It checked only the FIRST declared host, so an adapter that
    mentioned its vendor API and quietly added a telemetry endpoint
    passed clean."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "validate_adapters_index_under_test",
        REPO_ROOT / "scripts" / "validate_adapters_index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    index = tmp_path / "adapters_index.yml"
    index.write_text(yaml.safe_dump([{
        "id": "chatty", "name": "Chatty", "version": "1.0.0",
        "summary": "Calls api.vendor.com to do the work.",
        "image": "ghcr.io/x/chatty:1",
        "tasks_advertised": ["object_detection"],
        "permissions": {"network_egress": ["api.vendor.com",
                                           "telemetry.vendor.com"]},
    }]))
    module.INDEX = index

    import io
    import contextlib

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert module.main() == 0          # a warning, not an error
    printed = out.getvalue()
    assert "telemetry.vendor.com" in printed, printed
    # The host it DID disclose must not be nagged about.
    assert "api.vendor.com" not in printed.split("telemetry.vendor.com")[0]


def test_the_route_filters_by_canonical_task(monkeypatch):
    """`?task=speech_to_text` answered `count=0` while the catalog was
    listing Whisper under `audio_transcription` — the alias tasks.yml
    exists to reconcile."""
    import asyncio

    from routers import adapters_catalog

    monkeypatch.setattr(adapters_catalog, "load_adapters_index", lambda: [
        AdapterIndexEntry(
            id="whisper", name="Whisper", summary="ASR.", version="1.0.0",
            image="ghcr.io/x/whisper:1",
            tasks_advertised=["audio_transcription"]),
    ])

    for spelling in ("speech_to_text", "audio_transcription", "SPEECH_TO_TEXT"):
        body = asyncio.run(adapters_catalog.get_adapters_index(
            task=spelling, current_user=None, db=None))
        assert body["count"] == 1, spelling
        assert body["adapters"][0]["id"] == "whisper"

    # The grouping map is keyed by the canonical name, so the UI shows
    # one row per capability rather than one per spelling.
    body = asyncio.run(adapters_catalog.get_adapters_index(
        task=None, current_user=None, db=None))
    assert "speech_to_text" in body["tasks"]
    assert body["tasks"]["speech_to_text"] == ["whisper"]


def test_an_unknown_task_still_matches_itself(monkeypatch):
    """Free-text tasks register and stay as-is (§15.1); the catalog must
    not lose them by canonicalizing."""
    import asyncio

    from routers import adapters_catalog

    monkeypatch.setattr(adapters_catalog, "load_adapters_index", lambda: [
        AdapterIndexEntry(
            id="odd", name="Odd", summary="s.", version="1.0.0",
            image="ghcr.io/x/odd:1", tasks_advertised=["bespoke_thing"]),
    ])
    body = asyncio.run(adapters_catalog.get_adapters_index(
        task="bespoke_thing", current_user=None, db=None))
    assert body["count"] == 1
