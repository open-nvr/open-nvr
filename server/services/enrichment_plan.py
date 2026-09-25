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

__all__ = ["SkillPlan", "TASK_DESCRIPTORS", "build_plan", "plan_for_label",
           "compute_enrichment_plan"]


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
        # CORE does not write it — descriptor_enrichment refuses the
        # kind outright, and descriptor_store keeps it out of the
        # projected words so a name cannot be reached by free-text
        # search. It is left to an app the operator installed on
        # purpose, and since RFC-0003 one exists: smart-doorbell writes
        # face_id through /internal/app/visits/claims, roster-scoped,
        # with the binding recorded.
        #
        # This comment used to say the kind was unproduced and that no
        # app wrote it. That went stale when the doorbell shipped, and
        # it is the kind of staleness that costs real time: a reader
        # trusting it concludes "was this person here?" cannot be
        # answered, when on a deployment running that app it already
        # can. tests/test_descriptor_producers.py is the authority —
        # it carries the same reasoning and fails if it drifts.
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
    raw_adapters = caps.get("adapters")
    # KAI-C's /capabilities is a DICT keyed by adapter name, each entry
    # ``{url, capabilities: {tasks_advertised: [...], ...}}`` (or
    # ``{url, error}`` for one it could not reach). The list-of-
    # ``{name, tasks}`` shape is what the first tests were written
    # against and is kept; the dict shape is what a running box sends,
    # and until it was read here the plan on every real deployment was
    # empty — the UI offered no attribute filters and the descriptor
    # enricher, asking this plan what it could run, ran nothing.
    adapters: list[dict[str, Any]] = []
    if isinstance(raw_adapters, dict):
        for name, entry in raw_adapters.items():
            if not isinstance(entry, dict):
                continue
            inner = entry.get("capabilities")
            tasks = entry.get("tasks")
            if tasks is None and isinstance(inner, dict):
                tasks = inner.get("tasks_advertised")
            elif tasks is None and isinstance(inner, (list, str)):
                tasks = inner
            adapters.append({"name": str(name), "tasks": tasks or []})
    elif isinstance(raw_adapters, list):
        adapters = raw_adapters

    healthy_names: set[str] = set()
    unhealthy_names: set[str] = set()
    raw_health = (health or {}).get("adapters")
    if isinstance(raw_health, dict):
        for name, entry in raw_health.items():
            if isinstance(entry, dict):
                # /adapters/health says ``{"status": "ok"}`` per adapter;
                # ``healthy: bool`` is honoured when a producer sends it.
                ok = (bool(entry["healthy"]) if "healthy" in entry
                      else entry.get("status") in (None, "ok", "healthy"))
            else:
                ok = bool(entry)
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


async def compute_enrichment_plan(label: str | None = None) -> dict[str, Any]:
    """The plan as a dict — registered ∩ healthy skills, their descriptor
    kinds, narrowed to ``label`` when given.

    ONE implementation for the two readers. ``GET /search/enrichment-plan``
    serves this to the UI, and ``descriptor_enrichment`` asks it what to
    run on a visit. The enricher used to import a function of this name
    from the router that had never existed there — the ImportError was
    caught and logged at DEBUG, the plan came back empty, and not one
    descriptor was written on any deployment while the flag said the
    feature was on. The tests patched ``_plan_skills`` and never noticed.

    Asks KAI-C at most every ``PlanCache.ttl`` seconds; an unreachable
    registry is an empty plan (and a counted one), never an exception —
    this runs on the enrichment path.
    """
    from services import search_metrics as metrics
    from services.kai_c_service import KaiCService

    plan = CACHE.get()
    if plan is None:
        svc = KaiCService()
        caps: dict = {}
        health: dict = {}
        try:
            caps = await svc.get_capabilities()
        except Exception:  # noqa: BLE001
            caps = {}
            metrics.REGISTRY_UNREACHABLE.inc()
        try:
            health = await svc.check_kai_c_health()
        except Exception:  # noqa: BLE001
            health = {}
        plan = CACHE.put(build_plan(caps, health))
        metrics.SKILLS.set(len(plan), {"state": "registered"})
        metrics.SKILLS.set(sum(1 for s in plan if s.healthy), {"state": "healthy"})
    shown = plan_for_label(plan, label) if label else plan
    kinds = sorted({k for s in shown if s.healthy for k in s.descriptor_kinds})
    return {
        "skills": [s.as_dict() for s in shown],
        "descriptor_kinds": kinds,
        "label": label,
    }
