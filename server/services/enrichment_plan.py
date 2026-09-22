# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
#
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""What can be asked about a visit on THIS box, right now.

A visit is worth more the more that is known about it: a truck with a
plate read, a colour and a body type is findable and linkable in ways a
row labelled "truck" is not. But what can be known depends entirely on
what is registered in KAI-C and healthy at that moment, and that differs
per deployment and changes while running.

So enrichment asks rather than assumes. This module turns KAI-C's
``/capabilities`` and ``/adapters/health`` into a plan: the tasks that
are both registered AND healthy, the descriptor kinds each is expected to
produce, and which object classes each is worth running on.

Two rules the plan encodes, both learned from how attribute matching goes
wrong:

* **A missing skill is not a failed enrichment.** A site with nothing but
  a detector still gets class, camera and time, and its visits stay
  comparable with everything else. What must never happen is a visit
  being skipped, or treated as "no colour" when the truth is "nobody
  looked".
* **A skill is worth running on some classes and not others.** Plate OCR
  on a pedestrian is wasted GPU and a source of nonsense reads; face
  recognition on a lorry likewise.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

__all__ = ["SkillPlan", "TASK_DESCRIPTORS", "build_plan", "plan_for_label"]


@dataclass
class SkillPlan:
    """One task an enricher may run, and what it is expected to produce."""

    task: str
    adapters: list[str] = field(default_factory=list)
    #: Descriptor kinds this task writes — the contract a report reads.
    descriptor_kinds: list[str] = field(default_factory=list)
    #: Object classes it is worth running on; empty means "any".
    labels: list[str] = field(default_factory=list)
    healthy: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "adapters": self.adapters,
            "descriptor_kinds": self.descriptor_kinds,
            "labels": self.labels,
            "healthy": self.healthy,
        }


#: The canonical tasks (sdk validate.KNOWN_TASKS) an enricher can use on a
#: still frame, what each is expected to claim, and where it is worth the
#: compute. Anything not listed can still be run by an enricher that knows
#: what it is doing — this is the shipped default, not a whitelist.
TASK_DESCRIPTORS: dict[str, dict[str, Any]] = {
    "license_plate_recognition": {
        "kinds": ["plate"],
        "labels": ["car", "truck", "bus", "motorcycle"],
    },
    "face_recognition": {
        # The strongest claim in the set, and the most sensitive: a name
        # attached to a person on a camera. Enabled per deployment, and
        # every report that uses it says so.
        #
        # Declared, and currently unproduced. No enricher in core writes
        # face_id and no app does either, so this row describes what the
        # kind would mean rather than something the store holds. See
        # tests/test_descriptor_producers.py — it fails if the set of
        # unproduced kinds changes without the reason changing with it.
        "kinds": ["face_id"],
        "labels": ["person"],
    },
    "image_captioning": {
        # Free text rather than a claim — it lands in event_text, not in
        # descriptors, because "a man in a red jacket" is not a fact with
        # a confidence, it is a sentence.
        "kinds": [],
        "labels": [],
    },
    "vqa": {
        # Asked one question per descriptor kind ("what colour is this
        # vehicle?"), which is how a general model becomes a specific
        # claim with a confidence.
        "kinds": ["colour", "vehicle_type", "clothing_top", "carrying"],
        "labels": [],
    },
}


#: alias (lowercased) -> canonical task name, from server/config/tasks.yml.
#: Cached: build_plan runs behind a TTL cache but the file never changes
#: while the process lives.
_CANON_CACHE: dict[str, str] | None = None


def _canonical_tasks() -> dict[str, str]:
    """Alias map from the shipped taxonomy. Best-effort and cached.

    An unreadable or malformed tasks.yml degrades to "no aliases known",
    which is exactly the behaviour that shipped before this map existed —
    never an exception, because this runs on the enrichment path.
    """
    global _CANON_CACHE
    if _CANON_CACHE is not None:
        return _CANON_CACHE
    mapping: dict[str, str] = {}
    try:
        from pathlib import Path

        import yaml

        raw = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "config/tasks.yml").read_text())
        entries = raw.get("tasks") if isinstance(raw, dict) else raw
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            canonical = str(entry.get("task") or "").strip()
            if not canonical:
                continue
            mapping[canonical.lower()] = canonical
            for alias in entry.get("aliases") or []:
                # A canonical name always wins over an alias that collides
                # with it, matching canonicalize_task's documented rule.
                mapping.setdefault(str(alias).strip().lower(), canonical)
    except Exception:  # noqa: BLE001
        mapping = {}
    _CANON_CACHE = mapping
    return mapping


def build_plan(
    capabilities: dict[str, Any] | None,
    health: dict[str, Any] | None,
) -> list[SkillPlan]:
    """Registered ∩ healthy, as a list of runnable skills.

    ``capabilities`` is KAI-C's ``/capabilities`` and ``health`` its
    ``/adapters/health``; both are taken defensively, because an enricher
    that crashes on an unexpected registry shape is worse than one that
    enriches less.
    """
    caps = capabilities or {}
    adapters = caps.get("adapters")
    if not isinstance(adapters, list):
        adapters = []

    healthy_names: set[str] = set()
    unhealthy_names: set[str] = set()
    raw_health = (health or {}).get("adapters")
    if isinstance(raw_health, dict):
        for name, entry in raw_health.items():
            ok = entry.get("healthy") if isinstance(entry, dict) else bool(entry)
            (healthy_names if ok else unhealthy_names).add(str(name))
    elif isinstance(raw_health, list):
        for entry in raw_health:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("adapter") or "")
            if not name:
                continue
            ok = entry.get("healthy", entry.get("status") in (None, "healthy", "ok"))
            (healthy_names if ok else unhealthy_names).add(name)

    # Adapters advertise whichever spelling they like; tasks.yml is the
    # taxonomy that says which spellings are the same skill. Without
    # canonicalising here, the TASK_DESCRIPTORS lookup below silently
    # missed on two of its four entries — Moondream advertises
    # "visual_qa" against a table keyed "vqa", BLIP advertises
    # "scene_caption" against "image_captioning". Neither crashed: the
    # plan reported a healthy skill promising NO descriptor kinds, so the
    # colour filter was never worth offering and an enricher reading the
    # plan had nothing to run.
    #
    # Read straight from the YAML rather than through
    # routers.ai_models.canonicalize_task: importing a ROUTER from a
    # service pulls core.config.settings into a background task, which
    # raises wherever the environment is not fully configured. The first
    # version of this fix did exactly that, and its except-clause turned
    # the whole thing into a silent no-op.
    canon = _canonical_tasks()

    by_task: dict[str, list[str]] = {}
    for entry in adapters:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        tasks = entry.get("tasks") or entry.get("capabilities") or []
        if isinstance(tasks, str):
            tasks = [tasks]
        for task in tasks:
            # Grouped by the CANONICAL name, so one skill advertised under
            # two spellings is one plan entry with both adapters rather
            # than two half-described ones.
            key = canon.get(str(task).strip().lower(), str(task))
            by_task.setdefault(key, []).append(name)

    plan: list[SkillPlan] = []
    for task, names in sorted(by_task.items()):
        # An adapter nobody reported on is treated as available: health is
        # a signal that something is WRONG, and a registry that has not
        # answered yet must not silently disable every skill on the box.
        live = [n for n in names if n not in unhealthy_names]
        spec = TASK_DESCRIPTORS.get(task, {})
        plan.append(SkillPlan(
            task=task,
            adapters=sorted(set(names)),
            descriptor_kinds=list(spec.get("kinds", [])),
            labels=list(spec.get("labels", [])),
            healthy=bool(live),
        ))
    return plan


def plan_for_label(plan: list[SkillPlan], label: str | None) -> list[SkillPlan]:
    """The subset worth running on a visit of this class — plate OCR on a
    pedestrian is wasted GPU and a source of nonsense reads."""
    lab = (label or "").strip().lower()
    out = []
    for skill in plan:
        if not skill.healthy:
            continue
        if skill.labels and lab and lab not in skill.labels:
            continue
        out.append(skill)
    return out


class PlanCache:
    """The registry is asked at most every ``ttl`` seconds.

    Enrichment runs per visit — thousands a day — and the answer changes
    on the timescale of an adapter restarting, not of a frame.
    """

    def __init__(self, ttl: float = 30.0) -> None:
        self._ttl = ttl
        self._at = 0.0
        self._plan: list[SkillPlan] = []

    def get(self) -> list[SkillPlan] | None:
        if self._plan and (time.time() - self._at) < self._ttl:
            return self._plan
        return None

    def put(self, plan: list[SkillPlan]) -> list[SkillPlan]:
        self._plan, self._at = plan, time.time()
        return plan


CACHE = PlanCache()
