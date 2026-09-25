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

import re
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


#: Services whose image is chosen by an environment variable — ONE
#: container that can be any of several adapters. Declared rather than
#: inferred: a regex over ``ghcr.io/open-nvr/${VAR}-adapter`` matches
#: every adapter in the catalogue and would call whisper "deployable as
#: the captioner". The candidates are the ones .env.example documents.
SELECTABLE_SERVICES = {
    "docker-compose.camera-agent.yml:caption-adapter": (
        "CAPTION_ADAPTER", ("ollamavlm", "moondream", "blip")),
}


def _selectable_repos() -> set[str]:
    return {
        f"ghcr.io/open-nvr/{candidate}-adapter"
        for _var, candidates in SELECTABLE_SERVICES.values()
        for candidate in candidates
    }


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
        if _repo(image) in text or _repo(image) in _selectable_repos():
            # Reachable through a selectable service: one container whose
            # image an env var chooses. See SELECTABLE_SERVICES.
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
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _resolve(image: str) -> str:
    """Substitute ``${VAR:-default}`` with its default.

    The captioner's image is
    ``ghcr.io/open-nvr/${CAPTION_ADAPTER:-moondream}-adapter:...`` —
    ONE service that can be three different adapters depending on an env
    var. Reading it as a literal string finds no catalogue entry and the
    service silently drops out of every check below, which is how a
    duplicate captioner survived review in the first place. The default
    is what a deployment that sets nothing actually runs, so that is what
    is checked; the alternatives are the same task by construction.
    """
    return _VAR.sub(lambda m: m.group(2) or "", image or "")


def _adapter_services() -> list[tuple[str, str, dict]]:
    """(compose file, service name, service body) for every service whose
    image is an adapter in the index — across ALL the compose files, not
    just the apps overlay."""
    by_repo = {_repo(e["image"]): e for e in _index() if e.get("image")}
    out = []
    for name in COMPOSE_FILES:
        path = ROOT / name
        if not path.exists():
            continue
        compose = yaml.safe_load(path.read_text()) or {}
        for svc_name, svc in (compose.get("services") or {}).items():
            if not isinstance(svc, dict):
                continue
            key = f"{name}:{svc_name}"
            if key in SELECTABLE_SERVICES:
                # Every task ANY of its candidates can answer. The union
                # is the conservative reading for the duplicate check: an
                # operator who switches CAPTION_ADAPTER must not have to
                # re-derive which profiles now collide.
                tasks: set[str] = set()
                for candidate in SELECTABLE_SERVICES[key][1]:
                    e = by_repo.get(f"ghcr.io/open-nvr/{candidate}-adapter")
                    tasks.update((e or {}).get("tasks_advertised") or [])
                out.append((name, svc_name,
                            {**svc, "_entry": {"tasks_advertised": sorted(tasks)}}))
                continue
            entry = by_repo.get(_repo(_resolve(svc.get("image") or "")))
            if entry is not None:
                out.append((name, svc_name, {**svc, "_entry": entry}))
    return out


def test_no_task_is_answered_by_two_adapters_in_one_profile():
    """Two adapters answering one task means the answer to "describe this
    visit" depends on which one KAI-C picks — a difference the operator
    cannot see and did not choose, at double the CPU.

    This looks ACROSS compose files, which the first version of this test
    did not, and that blind spot was not hypothetical: the apps overlay
    grew a moondream service while docker-compose.camera-agent.yml had
    been running a captioner for months. Both carried a profile an
    operator would plausibly enable together, and the test that was
    supposed to forbid exactly this could not see one of them.
    """
    claimed: dict[tuple[str, str], list[str]] = {}
    for fname, svc_name, svc in _adapter_services():
        for profile in (svc.get("profiles") or []):
            for task in svc["_entry"].get("tasks_advertised") or []:
                claimed.setdefault((profile, task), []).append(
                    f"{fname}:{svc_name}")
    doubled = {k: v for k, v in claimed.items() if len(set(v)) > 1}
    assert not doubled, (
        "these profiles start two adapters for the same task: "
        f"{ {f'{p}/{t}': v for (p, t), v in doubled.items()} }"
    )


def test_the_enrichment_profile_actually_covers_enrichment():
    """The three things core's enrichment needs, all reachable from one
    profile — otherwise "turn on enrichment" is several lookups and an
    operator gets some of them. Spans files deliberately: the embedder
    and the captioner live in different overlays and that is invisible
    from .env, which is exactly why this has to be checked."""
    tasks: set[str] = set()
    for _fname, _svc_name, svc in _adapter_services():
        if "enrichment" in (svc.get("profiles") or []):
            tasks.update(svc["_entry"].get("tasks_advertised") or [])
    assert {"embed", "scene_caption", "visual_qa"} <= tasks, (
        f"--profile enrichment advertises {sorted(tasks)}; core's "
        "enrichment needs embed, scene_caption and visual_qa."
    )


def test_every_enrichment_adapter_has_a_registration_job():
    """An adapter nobody registers is invisible to KAI-C, and invisible
    is indistinguishable from not installed — the failure that cost a day
    on a live box. Each of these services must have a sibling in the same
    profile whose job is to register it."""
    for fname, svc_name, svc in _adapter_services():
        profiles = set(svc.get("profiles") or [])
        if not profiles & {"enrichment", "embeddings", "descriptions"}:
            continue
        compose = yaml.safe_load((ROOT / fname).read_text()) or {}
        siblings = {
            n for n, other in (compose.get("services") or {}).items()
            if isinstance(other, dict)
            and profiles & set(other.get("profiles") or [])
            and "register" in n
        }
        assert siblings, (
            f"{fname}:{svc_name} is startable by {sorted(profiles)} with no "
            "registration job in any of those profiles")


