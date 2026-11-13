# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The SDK's task list and the server's task registry must agree.

THE DEFECT CLASS, AGAIN: an enumerated list that falls behind the thing
it enumerates. ``KNOWN_TASKS`` in the SDK is a hand-maintained copy of
``server/config/tasks.yml``, and by the time anyone looked it was two
tasks behind — ``pose_estimation`` and ``package_detection`` were
canonical, registered, and warned about as unknown.

That failure is quiet, which is why it lasted. A manifest declaring a
perfectly real task gets a warning, the app still loads, and nobody
chases a warning that looks like a typo in somebody else's file.

The copy itself is not the bug and cannot be removed: the SDK is
published as its own package and installed where no server config
exists, so it has to carry its own list. What it does not have to be is
unchecked. This test is the check, and it runs in the repo where both
files are present.

It fails in BOTH directions on purpose. A task in the registry and not
in the SDK is the drift above. A task in the SDK and not in the
registry is the opposite mistake — a name that was renamed or dropped
and left behind — and it produces the same silent wrongness with the
roles reversed: an app declaring a task nothing provides, validated
without complaint.

There are also two copies of tasks.yml itself (the server's and the
camera-agent's mirror), so that pair is checked here too.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SERVER_TASKS = ROOT / "server" / "config" / "tasks.yml"
AGENT_TASKS = ROOT / "examples" / "camera-agent" / "tasks.yml"
SDK_SRC = ROOT / "sdk" / "opennvr-app-sdk"


def _canonical(path: Path) -> set[str]:
    entries = yaml.safe_load(path.read_text()) or []
    return {str(e["task"]) for e in entries if isinstance(e, dict) and e.get("task")}


@pytest.fixture(scope="module")
def known_tasks() -> frozenset[str]:
    sys.path.insert(0, str(SDK_SRC))
    try:
        from opennvr_app_sdk.validate import KNOWN_TASKS
    finally:
        sys.path.remove(str(SDK_SRC))
    return KNOWN_TASKS


def test_every_registered_task_is_known_to_the_sdk(known_tasks):
    """The drift that actually happened. A canonical task the SDK does
    not know is a correct manifest being warned about."""
    missing = sorted(_canonical(SERVER_TASKS) - set(known_tasks))

    assert not missing, (
        f"tasks.yml registers {missing} but opennvr_app_sdk.validate."
        f"KNOWN_TASKS does not list them — an app declaring one of these "
        f"is warned about for being correct")


def test_the_sdk_knows_no_task_the_registry_dropped(known_tasks):
    """The same drift with the roles reversed: a name that was renamed
    or removed and left behind here, so an app can declare a task
    nothing on the box provides and be validated without complaint."""
    extra = sorted(set(known_tasks) - _canonical(SERVER_TASKS))

    assert not extra, (
        f"KNOWN_TASKS lists {extra}, which tasks.yml does not register — "
        f"either the registry dropped them or this list was never updated")


def test_the_agent_mirror_lists_the_same_tasks():
    """Two copies of tasks.yml, same reason and same risk. The agent's
    hardware panel is built from ITS copy, so a task present only in the
    server's is a task an operator is never told the hardware for."""
    server, agent = _canonical(SERVER_TASKS), _canonical(AGENT_TASKS)

    assert server == agent, (
        f"only in server: {sorted(server - agent)}; "
        f"only in agent: {sorted(agent - server)}")


def test_aliases_do_not_collide_with_canonical_names():
    """An alias that is also somebody else's canonical task makes
    ``canonicalize_task`` ambiguous, and which one wins depends on file
    order — a bug that only shows up after an unrelated edit."""
    entries = yaml.safe_load(SERVER_TASKS.read_text()) or []
    canonical = {str(e["task"]) for e in entries if e.get("task")}

    collisions = []
    seen_aliases: dict[str, str] = {}
    for e in entries:
        for alias in (e.get("aliases") or []):
            alias = str(alias)
            if alias in canonical:
                collisions.append(f"{alias} (alias of {e['task']}, also a task)")
            if alias in seen_aliases and seen_aliases[alias] != e["task"]:
                collisions.append(
                    f"{alias} (claimed by {seen_aliases[alias]} and {e['task']})")
            seen_aliases[alias] = str(e["task"])

    assert not collisions, "; ".join(collisions)
