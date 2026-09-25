# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The catalogue must not advertise an adapter nothing can start.

``adapters_index.yml`` is what the AI models page and ``GET
/adapters/index`` show an operator, and for a long time it listed eleven
adapters of which four had a compose service. The other seven were a
catalogue entry with nothing behind it: the endpoint is read-only, no
installer knew about them, and there was no service to bring up.

What that cost is on record. A deployment ran for weeks with
``EVENTS_CAPTION_ENRICHMENT=true``, produced zero captions and zero
claims of any kind but ``plate``, and answered "red van" with nothing —
correctly, because nothing had ever looked at a colour. Every layer was
honest. The catalogue was not, and it was the layer the operator read.

This is the same defect this repository keeps finding: an enumerated
list that falls behind the thing it enumerates. The fix is not to
remember — it is to make adding a listing without a way to run it fail
here, where it is cheap, rather than on somebody's box, where it looks
like a broken feature.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "server" / "config" / "adapters_index.yml"

#: Compose files an adapter may legitimately live in. Not one file,
#: because where a service belongs is a real distinction: the always-on
#: detector is in the base stack, app adapters ride the apps overlay, and
#: the voice pair only makes sense with the camera agent.
COMPOSE_FILES = (
    "docker-compose.yml",
    "docker-compose.apps.yml",
    "docker-compose.camera-agent.yml",
)

#: Listed, and deliberately not startable yet. An entry here is a
#: decision somebody made on purpose, with the reason attached — which is
#: the whole point of the list. Deleting an entry must make the test fail
#: until a service exists.
LISTED_WITHOUT_A_SERVICE = {
    "bytetrack-multi-object-tracker":
        "Tracking runs inside the detect pipeline today; the standalone "
        "adapter is listed for deployments that want to swap it out, and "
        "nothing in this repo composes one yet.",
}


def _repo(image: str) -> str:
    """``ghcr.io/open-nvr/clip-adapter:${ADAPTER_TAG:-latest}`` → the repo.

    Splitting on the first colon is enough for every reference here and
    for the index's own ``:latest``: these are all ghcr paths with no
    registry port, so the first colon can only be the tag's.
    """
    return (image or "").split(":")[0]


def _index() -> list[dict]:
    if not INDEX.exists():                      # pragma: no cover
        pytest.skip("adapters_index.yml not present in this checkout")
    return yaml.safe_load(INDEX.read_text()) or []


def _compose_text() -> str:
    parts = []
    for name in COMPOSE_FILES:
        path = ROOT / name
        if path.exists():
            parts.append(path.read_text())
    if not parts:                               # pragma: no cover
        pytest.skip("compose files not present in this checkout")
    return "\n".join(parts)


def test_every_listed_adapter_can_actually_be_started():
    """A listing with no service is a promise the deployment cannot keep."""
    text = _compose_text()
    missing = []
    for entry in _index():
        image = (entry.get("image") or "").strip()
        if not image:
            continue
        # Match on the image reference without its tag: the compose file
        # pins ${ADAPTER_TAG:-latest} and the index says :latest, and a
        # tag mismatch is not what this test is about.
        if _repo(image) in text:
            continue
        if entry.get("id") in LISTED_WITHOUT_A_SERVICE:
            continue
        missing.append(entry.get("id"))
    assert not missing, (
        "listed in adapters_index.yml with no compose service anywhere: "
        f"{sorted(missing)}. Either add the service (see the core "
        "enrichment block at the bottom of docker-compose.apps.yml) or "
        "add it to LISTED_WITHOUT_A_SERVICE with the reason."
    )


def test_the_exceptions_are_real_listings():
    """A stale exception silently re-opens the hole it was excusing."""
    ids = {e.get("id") for e in _index()}
    stale = sorted(set(LISTED_WITHOUT_A_SERVICE) - ids)
    assert not stale, (
        f"LISTED_WITHOUT_A_SERVICE names adapters that are no longer in "
        f"the index: {stale}"
    )


def test_one_task_is_not_claimed_twice_by_the_all_on_profile():
    """``--profile enrichment`` must not start two adapters answering the
    same task.

    blip and moondream both advertise ``scene_caption``. Which one a
    deployment wants is a choice about cost and quality, and leaving two
    registered means the answer to "describe this visit" depends on
    whichever KAI-C picks — a difference an operator cannot see and
    cannot control. So the everything-on profile carries exactly one
    adapter per task, and the alternative gets a profile of its own.
    """
    apps = ROOT / "docker-compose.apps.yml"
    if not apps.exists():                       # pragma: no cover
        pytest.skip("apps overlay not present in this checkout")
    compose = yaml.safe_load(apps.read_text()) or {}
    by_repo = {_repo(e["image"]): e for e in _index() if e.get("image")}
    claimed: dict[str, list[str]] = {}
    for name, svc in (compose.get("services") or {}).items():
        if "enrichment" not in (svc.get("profiles") or []):
            continue
        entry = by_repo.get(_repo(svc.get("image") or ""))
        if entry is None:
            continue
        for task in entry.get("tasks_advertised") or []:
            claimed.setdefault(task, []).append(name)
    doubled = {t: v for t, v in claimed.items() if len(v) > 1}
    assert not doubled, (
        f"--profile enrichment starts two adapters for the same task: "
        f"{doubled}. Give one of them a profile of its own."
    )


def test_the_enrichment_profile_actually_covers_enrichment():
    """The three things core's enrichment needs, all reachable from one
    profile — otherwise "turn on enrichment" is three lookups and an
    operator gets two of them."""
    apps = ROOT / "docker-compose.apps.yml"
    if not apps.exists():                       # pragma: no cover
        pytest.skip("apps overlay not present in this checkout")
    compose = yaml.safe_load(apps.read_text()) or {}
    by_repo = {_repo(e["image"]): e for e in _index() if e.get("image")}
    tasks: set[str] = set()
    for svc in (compose.get("services") or {}).values():
        if "enrichment" not in (svc.get("profiles") or []):
            continue
        entry = by_repo.get(_repo(svc.get("image") or ""))
        if entry:
            tasks.update(entry.get("tasks_advertised") or [])
    assert {"embed", "scene_caption", "visual_qa"} <= tasks, (
        f"--profile enrichment advertises {sorted(tasks)}; core's "
        "enrichment needs embed, scene_caption and visual_qa."
    )
